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
}

/** オリジンへの問い合わせのタイムアウト（ミリ秒） */
const ORIGIN_TIMEOUT_MS = 8_000;

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
      if (!env.CF_ACCESS_CLIENT_ID || !env.CF_ACCESS_CLIENT_SECRET) {
        console.error("Access Service Token が設定されていません");
        return Response.json(
          { error: "service_misconfigured" },
          { status: 500 },
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
