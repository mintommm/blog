# Hugo Blog Content Sync with Google Drive

## 概要 (Overview)

このリポジトリは、[Hugo](https://gohugo.io/) で構築された静的サイトのコンテンツを、指定された Google Drive フォルダ内の Google ドキュメントから自動的に取得、変換、同期するためのシステムです。

GitHub Actions を利用して定期的に Google Drive をチェックし、変更があったドキュメントを Markdown に変換、画像を最適化（AVIF形式へ変換）、フロントマターを自動設定した後、Hugo でサイトをビルドし、Cloudflare Pages にデプロイします。

**主な機能:**

*   Google Docs から Markdown への自動変換
*   フロントマター（日付、タイトル等）の自動設定・更新
*   埋め込み PNG 画像の AVIF 形式への自動変換・リサイズ
*   Google Docs 内のHugoショートコードのエスケープ自動修正
*   Google Drive の更新に基づいた差分更新（キャッシュ利用）
*   Google Drive 側でのファイル削除・移動の自動反映（ローカルファイルの削除）
*   GitHub Actions による定期実行・手動実行
*   **Workload Identity Federation (`google-github-actions/auth`)** による Google Cloud 認証
*   公開コンテンツ（`draft: false` の記事）の追加・更新・削除があった場合のみ Cloudflare Pages へデプロイ（**条件付きデプロイ**）

## このリポジトリの転用について (Forking and Usage Notes)

このリポジトリをフォークしてご自身の Hugo ブログに適用する場合、以下の点にご注意ください。

1.  **Google Cloud Platform (GCP) 設定:**
    *   ご自身の GCP プロジェクトで Google Drive API を有効にする必要があります。
    *   Workload Identity Federation を設定し、ご自身の GitHub リポジトリが Google Cloud にアクセスできるように設定する必要があります。GitHub Actions ワークフローでは `google-github-actions/auth` アクションを使用し、サービスアカウント経由で認証を行います。詳細は Appendix の「セットアップと設定」を参照してください。
2.  **GitHub Secrets の設定:**
    *   リポジトリ設定で、GCP Workload Identity プロバイダー名、サービスアカウントメールアドレス、Google Drive の対象フォルダ ID、Cloudflare の認証情報などを Secrets として設定する必要があります。必要な Secrets のリストは Appendix を参照してください。
3.  **`gdrive_sync/main.py` のカスタマイズ:**
    *   **出力ディレクトリ:** デフォルトでは `content/posts/google-drive` に Markdown ファイルが出力されます。ご自身の Hugo プロジェクトの構成に合わせて `OUTPUT_SUBDIR` 定数を変更してください。
    *   **フロントマター:** `MarkdownProcessor.process_content` メソッド内でフロントマターを設定しています。必要に応じて、追加のフィールド設定や既存フィールドのロジックを変更してください。
    *   **画像処理:** 画像の幅 (`IMAGE_WIDTH`)、品質 (`IMAGE_QUALITY`)、形式 (`IMAGE_FORMAT`) はスクリプト冒頭の定数で変更可能です。
4.  **GitHub Actions ワークフローのカスタマイズ:**
    *   `.github/workflows/main.yml` ファイル内のトリガー（スケジュール実行の間隔など）や、Hugo のビルドオプション、デプロイ先（Cloudflare Pages 以外を使用する場合）などを適宜変更してください。
    *   Python のバージョン (`gdrive_sync/.python-version`) や依存関係 (`gdrive_sync/pyproject.toml`) を確認してください。
5.  **コンテンツの移行:** 既存の Markdown コンテンツを Google ドキュメントに移行するか、あるいはこのシステムと併用するかを検討してください。このスクリプトは指定された Google Drive フォルダのみを対象とします。

---

## Appendix: 技術詳細とメンテナンス情報

### A.1. 主要コンポーネント

#### A.1.1. `gdrive_sync/main.py`

Google Drive からコンテンツを取得し、Hugo 用に処理するコアスクリプトです。

*   **役割:** Google Drive API と連携し、指定されたフォルダ内の Google ドキュメントを処理します。
*   **主な機能:**
    *   **認証:** Google Cloud への認証。`google.auth.default()` を使用し、GitHub Actions ワークフローで `google-github-actions/auth` によって設定された認証情報（通常は `GOOGLE_APPLICATION_CREDENTIALS` 環境変数経由）を利用します。
    *   **ファイルリスト取得:** 指定された Google Drive フォルダ内の Google ドキュメントを再帰的に検索します。
    *   **ダウンロード:** Google ドキュメントを Markdown 形式 (`text/markdown`) でエクスポート（ダウンロード）します。
    *   **フロントマター処理:** ダウンロードした Markdown にフロントマターを追加・更新します。
        *   `date`: 既存の値があれば優先。なければ Drive の `createdTime` を使用。
        *   `lastmod`: 既存の値があれば優先。なければ Drive の `modifiedTime` を使用。
        *   `title`: 既存の値があれば優先。なければ Drive のファイル名を使用。
        *   `draft`: デフォルトで `false` を設定（既存の値があれば優先）。
        *   `google_drive_id`: 対応する Google Drive ファイル ID を記録。
        *   `modifiedTime`: Drive の `modifiedTime` の生文字列をキャッシュ比較用に記録。
    *   **画像変換:** Markdown 内の Base64 PNG 画像 (`data:image/png;base64,...`) を検出し、AVIF 形式 (`data:image/avif;base64,...`) に変換します。この処理は Pillow ライブラリのネイティブ機能を使用しており、以前使用していた `pillow-avif-plugin` は不要になりました。画像幅が設定値 (`IMAGE_WIDTH`) を超える場合はリサイズします。
    *   **ショートコード処理:** Google ドキュメント内で等幅フォントを利用して書かれ、マークダウンとしてダウンロードされた際に `` `{{< shortcode >}}` `` のように変換されたHugoショートコードを検出し、正しい `{{< shortcode >}}` 形式に自動的に修正します。
    *   **キャッシュ管理:** ローカルに保存されている Markdown ファイルの `modifiedTime` フロントマターと、Drive から取得した最新の `modifiedTime` を比較し、変更がない場合はダウンロード・処理をスキップします。ファイルの `draft` 状態もキャッシュ比較時に考慮されます。
    *   **ローカルファイル同期:** Drive 上に存在しないファイルに対応するローカル Markdown ファイル (`content/google-drive/{file_id}.md`) を削除します。この処理は `main()` 内のヘルパー関数 `_synchronize_local_files()` で実行され、削除されるファイルが以前公開状態 (`draft: false`) であったかどうかも記録されます。
    *   **変更検知マーカー:** 以下のいずれかの条件を満たす場合にのみ、リポジトリルートに `.content-updated` ファイルを作成します。この処理は `main()` 内のヘルパー関数 `_handle_marker_file()` で実行されます。
        1.  公開状態 (`draft: false`) の記事が新規作成または更新された場合。
        2.  以前公開状態 (`draft: false`) であった記事が Google Drive から削除された場合。
    *   **並列処理:** 各ファイルのダウンロードと処理を `concurrent.futures.ProcessPoolExecutor` を使用して並列化し、処理時間を短縮します。
    *   **エラーハンドリングとリトライ:** Google Drive API 呼び出し時に一時的なエラーが発生した場合、指数バックオフを用いたリトライ処理を行います。処理中に回復不能なエラーが発生した場合はログに出力し、最終的にエラーがあった場合は `sys.exit(1)` で終了します。
    *   **ロギング:** `logging` モジュールを使用し、処理の進行状況やエラー情報を標準出力に出力します。
*   **構造:**
    *   `GoogleDriveClient`: Google Drive API との通信（認証、リスト、ダウンロード、リトライ）を担当。
    *   `MarkdownProcessor`: Markdown ファイルの処理を担当。フロントマター処理（日付決定の `_determine_date`, `_determine_lastmod` やその他メタデータ設定の `_set_other_metadata` ヘルパーメソッドを含む）、画像変換、保存などを行います。
    *   `main()`: 全体的な処理フローを制御。ローカルファイル同期 (`_synchronize_local_files`) やマーカーファイル処理 (`_handle_marker_file`) のためのヘルパー関数も呼び出します。
*   **依存関係:** `gdrive_sync/pyproject.toml` を参照してください。主なライブラリは `google-api-python-client`, `google-auth-httplib2`, `python-frontmatter`, `Pillow`, `python-dateutil` です。
    *   **設定:** スクリプト冒頭の定数 (`MAX_RETRIES`, `OUTPUT_SUBDIR` など) や、環境変数 `GOOGLE_DRIVE_PARENT_ID` で動作を制御します。

#### A.1.2. `.github/workflows/main.yml`

コンテンツ取得から Hugo ビルド、デプロイまでの一連のプロセスを自動化する GitHub Actions ワークフローです。

#### A.1.3. `.github/workflows/cloudflare-cleanup.yml`

Cloudflare Pages の古いデプロイメントを定期的にクリーンアップする GitHub Actions ワークフローです。

*   **目的:** Cloudflare Pages のプロジェクトで時間の経過とともに蓄積される古い（エイリアス化されていない）デプロイメントを削除し、デプロイメント数の上限に関する問題を回避します。
*   **トリガー:**
    *   `schedule`: cron 形式で定期実行（デフォルトでは毎週日曜日の0時 UTC）。
    *   `workflow_dispatch`: GitHub UI から手動で実行可能。
*   **主要ステップ:**
    1.  **Set up Node.js:** Node.js 環境をセットアップします。
    2.  **Run Cloudflare deployment cleanup:**
        *   Cloudflare が提供するデプロイメント削除ツールをダウンロードし展開します。
        *   ツールの依存関係をインストールします (`npm install`)。
        *   デプロイメント削除スクリプトを実行します (`npm start`)。
*   **必要な Secrets:**
    *   `CLOUDFLARE_API_TOKEN`: Cloudflare API トークン。
    *   `CLOUDFLARE_ACCOUNT_ID`: Cloudflare アカウント ID。
    *   `CLOUDFLARE_PROJECT_NAME`: Cloudflare Pages のプロジェクト名。
    *   `CF_DELETE_ALIASED_DEPLOYMENTS`: (オプション) `true` に設定すると、エイリアス化されたデプロイメントも削除対象に含めます。デフォルトでは `false` (エイリアス化されていないもののみ削除)。

*   **目的:** 定期的（スケジュール実行）または手動でトリガーされ、`gdrive_sync/main.py` を実行してコンテンツを更新し、Hugo サイトをビルドして Cloudflare Pages にデプロイします。
*   **トリガー:**
    *   `schedule`: cron 形式で定期実行（デフォルトでは毎時）。
    *   `workflow_dispatch`: GitHub UI から手動で実行可能。キャッシュを無視するオプション (`ignore_cache`) 付き。
*   **主要ステップ:**
    1.  **Checkout code:** リポジトリのコードをチェックアウトします (`actions/checkout@v4`)。
    2.  **Disable quotePath for non-ASCII filenames:** Git で非ASCIIファイル名が正しく扱われるように設定します。
    3.  **Authenticate to Google Cloud:** `google-github-actions/auth` アクションを使用し、Workload Identity Federation を介して Google Cloud に認証します。
    4.  **Set up Python:** `gdrive_sync/.python-version` ファイルに基づいて Python 環境をセットアップします (`actions/setup-python@v5`)。
    5.  **Install uv:** Python パッケージインストーラー `uv` をインストールします。
    6.  **Install Python dependencies with uv using lock file:** `uv pip sync --system gdrive_sync/uv.lock` コマンドで、`gdrive_sync` ディレクトリ内の `uv.lock` ファイルに基づいて Python の依存関係をシステム環境にインストールします。**注意:** `gdrive_sync/uv.lock` はリポジトリにコミットされている必要があります。
    7.  **Restore content cache:** `content/posts/google-drive` ディレクトリのキャッシュを復元します (`actions/cache@v4`)。手動実行時に `ignore_cache` が `true` の場合はスキップされます。キャッシュキーは OS と `gdrive_sync/main.py`, `gdrive_sync/pyproject.toml` のハッシュに基づきます。
    8.  **Remove old marker file:** 前回のマーカーファイル `.content-updated` を削除します。
    9.  **Run Python script to sync content from Google Drive:** `uv run python gdrive_sync/main.py` を実行します。必要な環境変数 (`GOOGLE_DRIVE_PARENT_ID`, `GOOGLE_APPLICATION_CREDENTIALS`) を設定します。コンテンツに変更があれば `gdrive_sync/main.py` が `.content-updated` を作成します。
    10. **Check for content update marker:** `.content-updated` ファイルが存在するかチェックし、結果を GitHub Actions の output に設定します。
    11. **Setup Hugo:** Hugo 環境をセットアップします (`peaceiris/actions-hugo@v3`)。
    12. **Build Hugo site:** `hugo` コマンドでサイトをビルドします。`--minify` オプション付きです。
    13. **Deploy to Cloudflare Pages:** `steps.check_update_marker.outputs.content_was_updated == 'true'`（コンテンツが更新された場合）、または手動実行時に `ignore_cache` が `true` の場合のみ、ビルドされたサイト (`public` ディレクトリ) を Cloudflare Pages にデプロイします (`cloudflare/wrangler-action@v3`)。
*   **必要な Secrets:** ワークフローが Google Cloud や Cloudflare と連携するために、以下の GitHub Secrets の設定が必要です。
    *   `GCP_WORKLOAD_IDENTITY_PROVIDER`: Google Cloud Workload Identity プールの **プロバイダーリソース名** (例: `projects/123456789/locations/global/workloadIdentityPools/my-pool/providers/my-provider`)。
    *   `GCP_SERVICE_ACCOUNT`: Google Cloud サービスアカウントのメールアドレス (例: `my-service-account@my-project.iam.gserviceaccount.com`)。`google-github-actions/auth` が使用します。
    *   `GOOGLE_DRIVE_PARENT_ID`: コンテンツを取得する Google Drive の親フォルダ ID。
    *   `CLOUDFLARE_API_TOKEN`: Cloudflare API トークン。
    *   `CLOUDFLARE_ACCOUNT_ID`: Cloudflare アカウント ID。
    *   `CLOUDFLARE_PROJECT_NAME`: Cloudflare Pages のプロジェクト名。

### A.2. セットアップと設定

#### A.2.1. Google Cloud Platform (GCP) 設定

1.  **Google Drive API の有効化:** GCP プロジェクトで Google Drive API を有効にします。
2.  **サービスアカウントの作成:** GitHub Actions が Google Cloud リソースにアクセスするためのサービスアカウントを作成します。
3.  **必要な権限の付与:** 作成したサービスアカウントに、少なくとも Google Drive に対する読み取り権限 (`roles/drive.readonly` など) を付与します。
4.  **Workload Identity Federation の設定:**
    *   Workload Identity プールを作成します。
    *   GitHub Actions と連携するためのプロバイダーを設定します（リポジトリを指定）。
    *   Workload Identity プールを作成します。
    *   GitHub Actions と連携するためのプロバイダーを設定します（リポジトリを指定）。
    *   作成したサービスアカウントが、Workload Identity プールを通じて GitHub Actions からの認証を受け入れられるように、サービスアカウントに Workload Identity ユーザーロール (`roles/iam.workloadIdentityUser`) を付与します。
    *   GitHub Actions ワークフロー (`google-github-actions/auth`) は、GitHub OIDC トークンを使用して Google Cloud に認証し、指定されたサービスアカウントの権限を借用 (impersonate) して、短期的な認証情報を取得します。

#### A.2.2. GitHub Secrets の設定

リポジトリの `Settings` > `Secrets and variables` > `Actions` で、以下の Secrets を設定します。

*   `GCP_WORKLOAD_IDENTITY_PROVIDER`: Google Cloud Workload Identity プールの **プロバイダーリソース名** (例: `projects/123456789/locations/global/workloadIdentityPools/my-pool/providers/my-provider`)。
*   `GCP_SERVICE_ACCOUNT`: Google Cloud サービスアカウントのメールアドレス (例: `my-service-account@my-project.iam.gserviceaccount.com`)。
*   `GOOGLE_DRIVE_PARENT_ID`: (例: `1qFJo01Q8gjXnMH1DcLWFQ_ELc9wrtFUP`)
*   `CLOUDFLARE_API_TOKEN`: Cloudflare API トークン。
*   `CLOUDFLARE_ACCOUNT_ID`: Cloudflare アカウント ID。
*   `CLOUDFLARE_PROJECT_NAME`: Cloudflare Pages プロジェクト名。

#### A.2.3. Python 環境

*   リポジトリの `gdrive_sync/` ディレクトリには `.python-version` ファイルが含まれており、使用する Python バージョンを指定しています。
*   依存関係は `gdrive_sync/pyproject.toml` で定義されています。これらの依存関係を固定したロックファイル `gdrive_sync/uv.lock` がリポジトリにコミットされている必要があります。
*   **GitHub Actions でのインストール:** ワークフローでは `uv pip sync --system gdrive_sync/uv.lock` コマンドを使用して、ロックファイルに基づいた依存関係をインストールします。
*   **ローカル開発でのインストール:** ローカルで開発環境をセットアップする場合、まず `uv` をインストールし、`gdrive_sync` ディレクトリに移動してから以下のコマンドを実行します。
    *   依存関係のインストール: `uv pip sync --system uv.lock` (もし仮想環境を使用している場合は `--system` を外すことも検討)。
    *   ロックファイルの更新/生成: `pyproject.toml` に変更を加えた場合、`uv pip compile pyproject.toml -o uv.lock` を実行して `uv.lock` を更新し、変更をリポジトリにコミットしてください。
*   **`uv` のインストール:** `uv` 自体は `pip install uv` でインストールできます。

### A.3. 実行方法

#### A.3.1. 自動実行

*   GitHub Actions ワークフロー (`.github/workflows/main.yml`) は、`schedule` トリガーによって定期的に自動実行されます（デフォルトは毎時）。
*   GitHub Actions ワークフロー (`.github/workflows/cloudflare-cleanup.yml`) は、`schedule` トリガーによって定期的に自動実行されます（デフォルトは毎週日曜日の0時 UTC）。

#### A.3.2. 手動実行

1.  リポジトリの GitHub ページにアクセスします。
2.  `Actions` タブを選択します。
3.  左側のワークフローリストから `Download Content, Build and Deploy` を選択します。
4.  `Run workflow` ドロップダウンをクリックします。
5.  オプション: `ignore_cache` を `true` に設定すると、キャッシュを無視して強制的に全ファイルを再処理します。
6.  `Run workflow` ボタンをクリックします。

### A.4. 依存関係

主要な Python 依存関係は `gdrive_sync/pyproject.toml` ファイルにリストされています。

```toml
[project]
# ...
dependencies = [
    "google-api-python-client>=2.167.0",
    "Pillow>=10.0.0", # Pillow 10+ has built-in AVIF support
    "python-frontmatter>=1.1.0",
    "google-auth-httplib2>=0.1.0", # For AuthorizedHttp
    "httplib2>=0.20.0", # Dependency for google-auth-httplib2 and direct use
    "tzdata", # For zoneinfo
    "python-dateutil>=2.9.0", # For flexible date parsing
]
```

### A.5. 今後のメンテナンス (LLM エージェント向け)

*   **コード構造 (`gdrive_sync/main.py`):**
    *   `GoogleDriveClient`: Google Drive API との通信（認証、ファイルリスト取得、ダウンロード、APIリトライ処理）を担当します。
    *   `MarkdownProcessor`: ダウンロードされた Markdown コンテンツの処理を担当します。これには、キャッシュチェック、フロントマターの解析と更新（日付処理は `_determine_date`, `_determine_lastmod` ヘルパーメソッドを使用）、画像変換（Pillow のネイティブ機能を使用）、ローカルファイルへの保存などが含まれます。
    *   `main()`: スクリプトのメインエントリーポイントで、全体の処理フローを制御します。`GoogleDriveClient` と `MarkdownProcessor` のインスタンスを作成し、処理を調整します。ローカルファイルの同期 (`_synchronize_local_files`) や変更検知マーカーファイルの処理 (`_handle_marker_file`) といったヘルパー関数も `main` 関数から呼び出されます。
    *   並列処理: `concurrent.futures.ProcessPoolExecutor` を使用して、複数のファイルを並列にダウンロード・処理します。
*   **GitHub Actions ワークフロー (`.github/workflows/main.yml`):**
    *   Python の依存関係は `gdrive_sync/uv.lock` ファイルに基づいて `uv pip sync --system gdrive_sync/uv.lock` を使ってインストールされます。`uv.lock` ファイルは `gdrive_sync/pyproject.toml` から `uv pip compile pyproject.toml -o gdrive_sync/uv.lock` ( `gdrive_sync` ディレクトリ内で実行) によって生成・更新され、リポジトリにコミットされている必要があります。
    *   コンテンツの更新検知は `gdrive_sync/main.py` が出力する `.content-updated` マーカーファイルによって行われます。このマーカーが存在する場合、または手動実行でキャッシュが無視された場合にのみ、Hugo ビルドと Cloudflare Pages へのデプロイが実行されます。
*   **設定変更:**
    *   基本的な動作設定（リトライ回数 `MAX_RETRIES`、画像幅 `IMAGE_WIDTH`、出力サブディレクトリ名 `OUTPUT_SUBDIR` など）は `gdrive_sync/main.py` 冒頭のグローバル定数を変更します。
    *   Google Drive の対象フォルダは GitHub Secret `GOOGLE_DRIVE_PARENT_ID` で設定します。
    *   デプロイ先や認証情報は GitHub Secrets で管理されます。
*   **エラー発生時の確認ポイント:**
    *   GitHub Actions の実行ログを確認してください。`gdrive_sync/main.py` は `logging` モジュールを使用しており、詳細な情報やエラーメッセージが出力されます。
    *   `gdrive_sync/main.py` 内の `_execute_with_retry` メソッドは API エラー時のリトライ状況をログ出力します。
    *   ファイル処理に失敗した場合、スクリプトは `sys.exit(1)` で終了し、Actions のステップが失敗します。ログで `Processing failed for ...` や `Exiting with error code 1 ...` といったメッセージを探してください。
    *   キャッシュ関連の問題が疑われる場合は、GitHub Actions の手動実行時に `ignore_cache` を `true` に設定して試してください。
*   **機能追加・変更:**
    *   Google Drive API 関連の変更（例: 取得するフィールドの変更、クエリの修正など）は `GoogleDriveClient` クラスを修正します。
    *   Markdown の処理ロジック（例: 新しいフロントマターフィールドの追加、画像処理方法の変更など）は `MarkdownProcessor` クラス内の関連メソッド（例: `process_content`, `_determine_date`, `_convert_image` など）を修正します。
    *   並列処理の挙動やメインの制御フローに変更が必要な場合は `main()` 関数および `process_single_file_task` 関数を修正します。
    *   ワークフロー自体の変更（例: トリガー条件の変更、使用する Action のバージョンアップ、デプロイ手順の変更など）は `.github/workflows/main.yml` を編集します。
    *   Python の依存関係を変更する場合は、`gdrive_sync/pyproject.toml` を更新後、`gdrive_sync` ディレクトリ内で `uv pip compile pyproject.toml -o uv.lock` を実行して `uv.lock` ファイルを再生成し、両方のファイルをコミットしてください。
