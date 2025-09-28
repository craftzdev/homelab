Homelab VM デプロイ手順

概要
- Proxmox 上に Kubernetes ノード用の VM を Cloud-Init で一括作成・起動するためのスクリプトです。
- Ubuntu Server 24.04 (noble) の公式 Cloud Image をテンプレート化し、VM をクローンして設定します。
- 実行ファイル: deploy-vm.sh

前提条件（Proxmox ノードで実行）
- 必要コマンドがインストール済み: qm, wget, ssh, curl, tee, pvesm, sha256sum, flock
- （REQUIRE_CLUSTER=true の場合のみ）pvecm
- ストレージ名が pvesm status に存在すること（既定値）
  - CLOUDINIT_IMAGE_TARGET_VOLUME=local-lvm
  - TEMPLATE_BOOT_IMAGE_TARGET_VOLUME=local-lvm
  - BOOT_IMAGE_TARGET_VOLUME=local-lvm
  - SNIPPET_TARGET_VOLUME=local（snippets コンテンツを有効化必須）
- SNIPPET_TARGET_PATH=/var/lib/vz/snippets が存在（なければ作成されます）
- ネットワークブリッジと VLAN が正しく設定されていること（既定: bridge=vmbr1, VLAN_ID=40）
- SSH 接続（Proxmox ノード → 対象ノード）が可能であること（ホスト名または IP）。

主要ファイル
- deploy-vm.sh: VM をテンプレート化し、VM_LIST の内容に従ってクローン・設定・起動します。
- scripts/k8s-node-setup.sh: 各 VM の初回起動時に取得・実行されるセットアップスクリプト。

設定（環境変数で上書き可能）
- TARGET_BRANCH: リポジトリのブランチ。既定: main（引数でも指定可能）
- TEMPLATE_VMID: テンプレート VMID（既定: 9050）。既存テンプレートがある場合はスキップします。
- CLOUDINIT_IMAGE_TARGET_VOLUME, TEMPLATE_BOOT_IMAGE_TARGET_VOLUME, BOOT_IMAGE_TARGET_VOLUME: ストレージ名（既定: local-lvm）
- SNIPPET_TARGET_VOLUME: snippets コンテンツを有効化したストレージ（既定: cephfs01。CephFS/NFS 等の共有ファイルストレージを推奨）
- SNIPPET_TARGET_PATH: スニペット配置パス（既定: /mnt/pve/${SNIPPET_TARGET_VOLUME}/snippets）
- UBUNTU_IMG: noble-server-cloudimg-amd64.img（Ubuntu 24.04）
- DISK_SIZE: VM ディスクサイズのリサイズ量（既定: 30G）
- VLAN_ID: ネットワークタグ（既定: 40）
- VLAN_BRIDGE: ネットワークブリッジ（既定: vmbr1）
- NODE_GATEWAY: ゲートウェイ（既定: 172.16.40.1）
- NODE_CIDR_SUFFIX: サブネットマスクの CIDR（既定: 24）
- NAMESERVERS: DNS サーバ（スペース区切り。既定: "172.16.40.1 1.1.1.1"）
- SEARCHDOMAIN: 検索ドメイン（既定: home.arpa）
- CI_USER: Cloud-Init のユーザ名（既定: cloudinit）
- AUTH_KEYS_URLS: 取得する公開鍵 URL（改行またはスペース区切り。既定: https://github.com/craftzdev.keys）
- REPOSITORY_RAW_SOURCE_URL: k8s-node-setup.sh の取得元（既定: このリポジトリの raw URL）
- SSH_CONNECT_FIELD: SSH 接続先の識別子（host または ip。既定: host）
- RETRY_DELAY: SSH 等のリトライ間隔秒（既定: 2）
- REQUIRE_CLUSTER: Proxmox クラスタの厳格チェックを行う場合は true（単一ノードでは false 推奨）

VM_LIST の書式（deploy-vm.sh 内の配列）
- 形式: "vmid name vCPU mem(MiB) ip targetip targethost"
- 例（既定）:
  - "1001 k8s-cp-1 4 8192 172.16.40.11 - sv-proxmox-02"
- 説明:
  - vmid: VM の ID
  - name: VM 名
  - vCPU: vCPU 数
  - mem(MiB): メモリ（MiB）
  - ip: VM に設定する固定 IP（Cloud-Init の ipconfig0 に使用）
  - targetip: SSH 接続に IP を使う場合の値（SSH_CONNECT_FIELD=ip の時に使用）
  - targethost: SSH 接続にホスト名を使う場合の値（SSH_CONNECT_FIELD=host の時に使用）
  - どちらか片方のみを使う場合、使わない方は "-" としておいて問題ありません。

実行手順
1) Proxmox ノードで本リポジトリのルート（deploy-vm.sh があるディレクトリ）へ移動します。
2) ストレージと snippets コンテンツが有効化されていることを確認します（SNIPPET_TARGET_VOLUME に snippets を有効化）。
3) 必要に応じて VM_LIST や環境変数（上記）を編集します。
4) スクリプトを実行します。
   - 例（既定ブランチ）: ./deploy-vm.sh
   - 例（ブランチを指定）: ./deploy-vm.sh feature-branch
   - 例（環境変数を上書き）: VLAN_ID=20 DISK_SIZE=50G ./deploy-vm.sh
5) スクリプトは以下を自動で行います。
   - Ubuntu 24.04 Cloud Image のダウンロードと SHA256SUM 検証（テンプレート未作成時）
   - テンプレート VM の作成（ネットワーク・QGA 有効化・disk import など）
   - VM のクローン、CPU/メモリ設定、ディスク移動/リサイズ
   - ネットワーク設定（bridge と VLAN tag）
   - Cloud-Init 設定（ipconfig0, DNS, ciuser）
   - ユーザーデータスニペットの作成（runcmd で k8s-node-setup.sh を取得・実行）
   - 公開鍵の投入（AUTH_KEYS_URLS から取得した鍵）
   - Cloud-Init ISO の再生成、VM の起動
6) 実行完了後、ログはカレントディレクトリの deploy-vm.log に保存され、作成・起動した VM の一覧が表示されます。

再実行・テンプレート更新（Ubuntu 24.04 への切替）
- 既にテンプレート（TEMPLATE_VMID）が存在する場合、テンプレート作成はスキップされます。
- Ubuntu 22.04 のテンプレートを 24.04 に差し替えるには、以下のいずれかを実施してください。
  - TEMPLATE_VMID を新しい番号にして再実行（例: TEMPLATE_VMID=9051 ./deploy-vm.sh）
  - 既存テンプレート（9050 など）を削除後に再実行（慎重に運用してください）

トラブルシュート
- [ERROR] storage '...' not found: pvesm status に該当ストレージが存在するか確認してください。
- [ERROR] storage '...' does not have 'snippets' content enabled: SNIPPET_TARGET_VOLUME に snippets コンテンツを有効化してください。
- [ERROR] Cannot reach https://cloud-images.ubuntu.com/...: Proxmox ノードから外部ネットワークへの到達性を確認してください。
- [ERROR] SSH to ... failed: Proxmox ノードからターゲットノードへの SSH（ホスト名または IP）が解決・接続可能か確認してください。
- [ERROR] duplicate VMID / name / IP: VM_LIST の重複を解消してください。
- [ERROR] invalid IPv4: VM_LIST の IP 記載を確認してください。

カスタマイズ例
- VLAN_ID と VLAN_BRIDGE を変更:
  - VLAN_ID=100 VLAN_BRIDGE=vmbr0 ./deploy-vm.sh
- ディスクサイズを 60G に変更:
  - DISK_SIZE=60G ./deploy-vm.sh
- Cloud-Init ユーザを変更:
  - CI_USER=ubuntu ./deploy-vm.sh
- 追加の SSH 公開鍵を投入:
  - AUTH_KEYS_URLS="https://github.com/you.keys https://example.com/key.pub" ./deploy-vm.sh

注意事項
- スクリプトは冪等性を考慮していますが、既存 VMID がある場合はクローン/設定をスキップします。構成変更が必要な場合は VM を事前に停止・削除するか、VMID を変更してください。
- 本番環境でのテンプレート削除・変更は影響範囲が大きい可能性があるため、十分に検証の上で実施してください。