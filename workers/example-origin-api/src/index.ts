/**
 * ===========================================================================
 * Cloudflare Workers から自宅 Kubernetes のサービスを呼ぶリファレンス実装
 *
 * 前提:
 *   - 自宅側は cloudflared 経由で公開され、Cloudflare Access で保護されている
 *   - Access のポリシーは decision = "non_identity"、
 *     include に Service Token が指定されている
 *
 * 設計の要点:
 *   1. 認証情報は必ず Secret（wrangler secret put）で渡す。
 *      wrangler.jsonc の vars に書くと平文でダッシュボードに露出する。
 *   2. 401 が返ったらリトライしない。Service Token の失効を示すため、
 *      リトライは無意味であり、むしろ検知を遅らせる。
 *   3. 自宅が落ちている前提で degrade する。自宅の障害が
 *      SaaS 全体の停止に直結しないようにする。
 * ===========================================================================
 */

export interface Env {
  /** 公開ホスト名（例: api.internal.example.com）。vars で渡してよい。 */
  ORIGIN_HOST: string;

  /**
   * Access Service Token。
   * ⚠️ 必ず `wrangler secret put` で登録すること。
   *    値は tofu/20-cloudflare の output から取得する:
   *      tofu output -raw service_token_client_id
   *      tofu output -raw service_token_client_secret
   */
  CF_ACCESS_CLIENT_ID: string;
  CF_ACCESS_CLIENT_SECRET: string;

  /**
   * この Worker 自身の呼び出し元を認証するための共有シークレット。
   *
   * ⚠️⚠️ これが無いと、この Worker は「誰でも使える Access の代理」に
   *      なってしまう。Cloudflare Access が認証しているのは
   *      **この Worker** であって、**Worker の利用者**ではない。
   *      workers.dev の URL は誰でも叩けるため、認証を入れなければ
   *      インターネット上の任意の第三者が Access を通過できる。
   *
   *   wrangler secret put CLIENT_API_KEY
   *   # 生成例: openssl rand -base64 32
   */
  CLIENT_API_KEY: string;
}

/** オリジンへの問い合わせのタイムアウト（ミリ秒） */
const ORIGIN_TIMEOUT_MS = 8_000;

/**
 * オリジンへ転送を許可する HTTP メソッド。
 *
 * allowlist にする理由: 「危険なメソッドを denylist する」方式は
 * 新しいメソッドが増えたときに漏れる。通す物だけを列挙する方が安全。
 */
const ALLOWED_METHODS = new Set(["GET", "HEAD", "POST"]);

/**
 * タイミング攻撃に耐える文字列比較。
 *
 * 素朴な `a === b` は先頭から比較して不一致で即座に返るため、
 * 応答時間の差から 1 文字ずつシークレットを推測されうる。
 * 長さの違いも情報になるため、まず長さを比較してから
 * 全文字を走査する（早期 return しない）。
 */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

/**
 * この Worker の呼び出し元を認証する。
 *
 * ⚠️ 本番では共有シークレットではなく、利用者ごとの JWT 検証や
 *    mTLS を使うべきである。ここでは「認証を入れる場所」を示すための
 *    最小実装としている。
 */
function isAuthorizedCaller(request: Request, env: Env): boolean {
  if (!env.CLIENT_API_KEY) return false;

  const header = request.headers.get("Authorization") ?? "";
  const prefix = "Bearer ";
  if (!header.startsWith(prefix)) return false;

  return timingSafeEqual(header.slice(prefix.length), env.CLIENT_API_KEY);
}

/**
 * Cloudflare Access で保護されたオリジンへリクエストを送る。
 *
 * Access はこの 2 つのヘッダを見て認可を判定し、通過したリクエストには
 * `Cf-Access-Jwt-Assertion` を付与してオリジンへ転送する。
 * オリジン側（cloudflared の originRequest.access）でもこの JWT を
 * 検証しているため、認可は二重に効いている。
 */
async function fetchFromHomelab(
  env: Env,
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const url = `https://${env.ORIGIN_HOST}${path}`;

  // タイムアウトを設ける。自宅の回線断や Ceph の遅延で
  // Worker が待ち続けると、SaaS 全体のレスポンスが劣化する。
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), ORIGIN_TIMEOUT_MS);

  try {
    return await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        ...init.headers,
        "CF-Access-Client-Id": env.CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": env.CF_ACCESS_CLIENT_SECRET,
      },
    });
  } finally {
    clearTimeout(timeout);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // --- ヘルスチェック ---------------------------------------------------
    if (url.pathname === "/health") {
      return Response.json({ status: "ok" });
    }

    // --- 自宅のサービスへプロキシする例 -----------------------------------
    if (url.pathname.startsWith("/api/")) {
      // 設定漏れを早期に検出する。Secret が未設定のまま動かすと
      // 全リクエストが 401 になり、原因の切り分けに時間を取られる。
      if (
        !env.CF_ACCESS_CLIENT_ID ||
        !env.CF_ACCESS_CLIENT_SECRET ||
        !env.CLIENT_API_KEY
      ) {
        console.error(
          "必要な Secret が設定されていません " +
            "(CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET / CLIENT_API_KEY)",
        );
        return Response.json(
          { error: "service_misconfigured" },
          { status: 500 },
        );
      }

      // ---------------------------------------------------------------
      // ⚠️ 呼び出し元の認証（これが無いと Access の代理になる）
      //
      // Cloudflare Access が認証しているのは「この Worker」であって
      // 「Worker の利用者」ではない。ここで呼び出し元を認証しないと、
      // workers.dev の URL を知る誰もが Access を通過できてしまう。
      // ---------------------------------------------------------------
      if (!isAuthorizedCaller(request, env)) {
        return Response.json({ error: "unauthorized" }, { status: 401 });
      }

      // ---------------------------------------------------------------
      // メソッドの allowlist
      //
      // 任意のメソッドをそのまま転送すると、オリジン側が想定しない
      // DELETE / PUT などを受け取ることになる。通す物だけを列挙する。
      // ---------------------------------------------------------------
      if (!ALLOWED_METHODS.has(request.method)) {
        return Response.json(
          { error: "method_not_allowed" },
          { status: 405, headers: { Allow: [...ALLOWED_METHODS].join(", ") } },
        );
      }

      const originPath = url.pathname.replace(/^\/api/, "");

      try {
        const res = await fetchFromHomelab(env, originPath, {
          method: request.method,
          body:
            request.method === "GET" || request.method === "HEAD"
              ? undefined
              : await request.arrayBuffer(),
        });

        // -------------------------------------------------------------
        // 401 = Access の認可に失敗した
        //
        // ⚠️ リトライしないこと。Service Token の失効・無効化を示すため、
        //    再試行しても結果は変わらず、障害の検知を遅らせるだけ。
        //    運用上はここでアラートを上げるべき状態である。
        // -------------------------------------------------------------
        if (res.status === 401 || res.status === 403) {
          console.error(
            `Access denied (${res.status}). Service Token の有効期限が切れているか、` +
              `Access ポリシーが変更された可能性があります。`,
          );
          return Response.json(
            { error: "upstream_unauthorized" },
            { status: 502 },
          );
        }

        return res;
      } catch (err) {
        // -------------------------------------------------------------
        // 自宅側が落ちている / 回線が切れている / タイムアウトした
        //
        // ここで degrade する設計が重要である。自宅は SLA を持たない。
        // SaaS 全体が自宅の可用性に引きずられないよう、
        // キャッシュ済みの結果を返す・機能を縮退させる等の
        // フォールバックを実装すること。
        // -------------------------------------------------------------
        const isTimeout = err instanceof Error && err.name === "AbortError";
        console.error(
          `オリジンへの接続に失敗しました: ${isTimeout ? "timeout" : String(err)}`,
        );

        return Response.json(
          {
            error: "upstream_unavailable",
            degraded: true,
          },
          { status: 503 },
        );
      }
    }

    return new Response("Not Found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;
