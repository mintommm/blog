# Hugo Blog Content Sync with Google Drive

## 概要 (Overview)

このリポジトリは、[Hugo](https://gohugo.io/) で構築された静的サイトのコンテンツを、指定された Google Drive フォルダ内の Google ドキュメントから自動的に取得、変換、同期するためのシステムです。

GitHub Actions を利用して定期的に Google Drive をチェックし、変更があったドキュメントを Markdown に変換、画像を最適化（AVIF形式へ変換）、フロントマターを自動設定した後、Hugo でサイトをビルドし、Cloudflare Pages にデプロイします。

**主な機能:**

*   Google Docs から Markdown への自動変換
*   フロントマター（日付、タイトル等）の自動設定・更新
*   埋め込み PNG 画像の AVIF 形式への自動変換・リサイズ
*   Google Drive の更新に基づいた差分更新（キャッシュ利用）
*   Google Drive 側でのファイル削除・移動の自動反映（ローカルファイルの削除）
*   GitHub Actions による定期実行・手動実行
*   **Direct Workload Identity Federation** による Google Cloud 認証
*   コンテンツに変更があった場合のみ Cloudflare Pages へデプロイ（**条件付きデプロイ**）

## このリポジトリの転用について (Forking and Usage Notes)

このリポジトリをフォークしてご自身の Hugo ブログに適用する場合、以下の点にご注意ください。

1.  **Google Cloud Platform (GCP) 設定:**
    *   ご自身の GCP プロジェクトで Google Drive API を有効にする必要があります。
    *   Workload Identity Federation を設定し、ご自身の GitHub リポジトリが Google Cloud にアクセスできるように設定する必要があります。**Direct Workload Identity Federation** を使用するため、サービスアカウントの権限借用ではなく、直接アクセストークンを取得します。詳細は Appendix の「セットアップと設定」を参照してください。
2.  **GitHub Secrets の設定:**
    *   リポジトリ設定で、GCP Workload Identity プロバイダー名、Google Drive の対象フォルダ ID、Cloudflare の認証情報などを Secrets として設定する必要があります。必要な Secrets のリストは Appendix を参照してください (`GCP_SERVICE_ACCOUNT` は通常不要です)。
3.  **`main.py` のカスタマイズ:**
    *   **出力ディレクトリ:** デフォルトでは `content/google-drive` に Markdown ファイルが出力されます。ご自身の Hugo プロジェクトの構成に合わせて `OUTPUT_SUBDIR` 定数を変更してください。
    *   **フロントマター:** `MarkdownProcessor.process_content` メソッド内でフロントマターを設定しています。必要に応じて、追加のフィールド設定や既存フィールドのロジックを変更してください。
    *   **画像処理:** 画像の幅 (`IMAGE_WIDTH`)、品質 (`IMAGE_QUALITY`)、形式 (`IMAGE_FORMAT`) はスクリプト冒頭の定数で変更可能です。
4.  **GitHub Actions ワークフローのカスタマイズ:**
    *   `.github/workflows/google-drive-access.yml` ファイル内のトリガー（スケジュール実行の間隔など）や、Hugo のビルドオプション、デプロイ先（Cloudflare Pages 以外を使用する場合）などを適宜変更してください。
    *   Python のバージョン (`.python-version`) や依存関係 (`pyproject.toml`) を確認してください。
5.  **コンテンツの移行:** 既存の Markdown コンテンツを Google ドキュメントに移行するか、あるいはこのシステムと併用するかを検討してください。このスクリプトは指定された Google Drive フォルダのみを対象とします。

---

## Appendix: 技術詳細とメンテナンス情報

### A.1. 主要コンポーネント

#### A.1.1. `main.py`

Google Drive からコンテンツを取得し、Hugo 用に処理するコアスクリプトです。

*   **役割:** Google Drive API と連携し、指定されたフォルダ内の Google ドキュメントを処理します。
*   **主な機能:**
    *   **認証:** Google Cloud への認証。GitHub Actions ワークフローから渡される **Direct Workload Identity Federation** 経由で取得したアクセストークン (`GOOGLE_OAUTH_ACCESS_TOKEN` 環境変数) を使用します。
    *   **ファイルリスト取得:** 指定された Google Drive フォルダ内の Google ドキュメントを再帰的に検索します。
    *   **ダウンロード:** Google ドキュメントを Markdown 形式 (`text/markdown`) でエクスポート（ダウンロード）します。
    *   **フロントマター処理:** ダウンロードした Markdown にフロントマターを追加・更新します。
        *   `date`: 既存の値があれば優先。なければ Drive の `createdTime` を使用。
        *   `lastmod`: 既存の値があれば優先。なければ Drive の `modifiedTime` を使用。
        *   `title`: 既存の値があれば優先。なければ Drive のファイル名を使用。
        *   `draft`: デフォルトで `false` を設定（既存の値があれば優先）。
        *   `google_drive_id`: 対応する Google Drive ファイル ID を記録。
        *   `modifiedTime`: Drive の `modifiedTime` の生文字列をキャッシュ比較用に記録。
    *   **画像変換:** Markdown 内の Base64 PNG 画像 (`data:image/png;base64,...`) を検出し、AVIF 形式 (`data:image/avif;base64,...`) に変換します（Pillow と pillow-avif-plugin を使用）。画像幅が設定値 (`IMAGE_WIDTH`) を超える場合はリサイズします。
    *   **キャッシュ管理:** ローカルに保存されている Markdown ファイルの `modifiedTime` フロントマターと、Drive から取得した最新の `modifiedTime` を比較し、変更がない場合はダウンロード・処理をスキップします。
    *   **ローカルファイル同期:** Drive 上に存在しないファイルに対応するローカル Markdown ファイル (`content/google-drive/{file_id}.md`) を削除します。
    *   **変更検知マーカー:** ローカルファイルの削除、または Drive からのダウンロード・更新により `content` ディレクトリに変更があった場合にのみ、リポジトリルートに `.content-updated` ファイルを作成します。
    *   **並列処理:** 各ファイルのダウンロードと処理を `concurrent.futures.ProcessPoolExecutor` を使用して並列化し、処理時間を短縮します。
    *   **エラーハンドリングとリトライ:** Google Drive API 呼び出し時に一時的なエラーが発生した場合、指数バックオフを用いたリトライ処理を行います。処理中に回復不能なエラーが発生した場合はログに出力し、最終的にエラーがあった場合は非ゼロの終了コードで終了します。
    *   **ロギング:** `logging` モジュールを使用し、処理の進行状況やエラー情報を標準出力に出力します。
*   **クラス構成:**
    *   `GoogleDriveClient`: Google Drive API との通信（認証、リスト、ダウンロード、リトライ）を担当。
    *   `MarkdownProcessor`: Markdown ファイルの処理（キャッシュチェック、フロントマター、画像変換、保存）を担当。
*   **依存関係:** `pyproject.toml` を参照してください。主なライブラリは `google-api-python-client`, `google-auth-httplib2`, `python-frontmatter`, `Pillow`, `pillow-avif-plugin` です。
*   **設定:** スクリプト冒頭の定数 (`MAX_RETRIES`, `OUTPUT_SUBDIR` など) や、環境変数 `GOOGLE_DRIVE_PARENT_ID` で動作を制御します。

#### A.1.2. `.github/workflows/google-drive-access.yml`

コンテンツ取得から Hugo ビルド、デプロイまでの一連のプロセスを自動化する GitHub Actions ワークフローです。

*   **目的:** 定期的（スケジュール実行）または手動でトリガーされ、`main.py` を実行してコンテンツを更新し、Hugo サイトをビルドして Cloudflare Pages にデプロイします。
*   **トリガー:**
    *   `schedule`: cron 形式で定期実行（デフォルトでは6時間ごと）。
    *   `workflow_dispatch`: GitHub UI から手動で実行可能。キャッシュを無視するオプション (`ignore_cache`) 付き。
*   **主要ステップ:**
    1.  **Checkout code:** リポジトリのコードをチェックアウトします (`actions/checkout@v4`)。サブモジュールも取得します。
    2.  **Authenticate to Google Cloud (Direct WIF):** GitHub OIDC トークンを取得し、Google STS API を呼び出して Google Cloud アクセストークンを取得します。`google-github-actions/auth` は使用しません。
    3.  **Set up Python:** `.python-version` ファイルに基づいて Python 環境をセットアップします (`actions/setup-python@v5`)。
    4.  **Install uv and dependencies:** `uv` をインストールし、`uv sync` コマンドで `uv.lock` に基づいて Python の依存関係をインストールします。
    5.  **Restore content cache:** `content` ディレクトリのキャッシュを復元します (`actions/cache@v4`)。手動実行時に `ignore_cache` が `true` の場合はスキップされます。キャッシュキーは OS と `main.py` のハッシュに基づきます。
    6.  **Remove old marker file:** 前回のマーカーファイル `.content-updated` を削除します。
    7.  **Run Python script:** `main.py` を実行します。必要な環境変数 (`GOOGLE_DRIVE_PARENT_ID`, `GOOGLE_OAUTH_ACCESS_TOKEN`) を設定します。コンテンツに変更があれば `main.py` が `.content-updated` を作成します。
    8.  **Check for content update marker:** `.content-updated` ファイルが存在するかチェックします。
    9.  **Save content cache:** `main.py` によって更新された `content` ディレクトリをキャッシュに保存します (`actions/cache/save@v4`)。
    10. **Setup Hugo:** Hugo 環境をセットアップします (`peaceiris/actions-hugo@v3`)。
    11. **Build Hugo site:** `hugo` コマンドでサイトをビルドします。`--minify` オプション付きです。
    12. **Deploy to Cloudflare Pages:** **`.content-updated` が存在するか、`ignore_cache` が `true` の場合のみ**、ビルドされたサイト (`public` ディレクトリ) を Cloudflare Pages にデプロイします (`cloudflare/pages-action@v1`)。
*   **必要な Secrets:** ワークフローが Google Cloud や Cloudflare と連携するために、以下の GitHub Secrets の設定が必要です。
    *   `GCP_WORKLOAD_IDENTITY_PROVIDER`: Google Cloud Workload Identity プールの **プロバイダーリソース名** (例: `projects/123.../providers/my-provider`)。
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
    *   **Direct Workload Identity Federation** を使用するため、GitHub Actions が発行した OIDC トークンを直接 Google Cloud アクセストークンに交換できるように設定します。この際、サービスアカウントへの権限借用 (impersonation) ではなく、STS トークン交換 API を利用します。アクセス権限は、Workload Identity プール/プロバイダーに紐付けられたプリンシパル（例: `principal://iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/<pool-id>/subject/<repo:owner/repo:ref:ref_name>`）に対して直接、またはグループ経由で付与します。Google Drive API への読み取り権限が必要です。

#### A.2.2. GitHub Secrets の設定

リポジトリの `Settings` > `Secrets and variables` > `Actions` で、以下の Secrets を設定します。

*   `GCP_WORKLOAD_IDENTITY_PROVIDER`: Google Cloud Workload Identity プールの **プロバイダーリソース名** (例: `projects/123456789/locations/global/workloadIdentityPools/my-pool/providers/my-provider`)。
*   `GOOGLE_DRIVE_PARENT_ID`: (例: `1qFJo01Q8gjXnMH1DcLWFQ_ELc9wrtFUP`)
*   `CLOUDFLARE_API_TOKEN`: Cloudflare API トークン。
*   `CLOUDFLARE_ACCOUNT_ID`: Cloudflare アカウント ID。
*   `CLOUDFLARE_PROJECT_NAME`: Cloudflare Pages プロジェクト名。
*   (注: `GCP_SERVICE_ACCOUNT` は Direct WIF では通常不要です。)

#### A.2.3. Python 環境

*   リポジトリには `.python-version` ファイルが含まれており、使用する Python バージョンを指定しています。
*   依存関係は `pyproject.toml` で定義され、`uv.lock` でバージョンが固定されています。GitHub Actions では `uv sync` でインストールされます。ローカルで開発する場合も同様に `uv sync` を使用してください。

### A.3. 実行方法

#### A.3.1. 自動実行

*   GitHub Actions ワークフロー (`.github/workflows/google-drive-access.yml`) は、`schedule` トリガーによって定期的に自動実行されます（デフォルトは6時間ごと）。

#### A.3.2. 手動実行

1.  リポジトリの GitHub ページにアクセスします。
2.  `Actions` タブを選択します。
3.  左側のワークフローリストから `Download Content, Build and Deploy` を選択します。
4.  `Run workflow` ドロップダウンをクリックします。
5.  オプション: `ignore_cache` を `true` に設定すると、キャッシュを無視して強制的に全ファイルを再処理します。
6.  `Run workflow` ボタンをクリックします。

### A.4. 依存関係

主要な Python 依存関係は `pyproject.toml` ファイルにリストされています。

```toml
[project]
# ...
dependencies = [
    "google-api-python-client>=2.167.0",
    "pillow-avif-plugin>=1.5.1",
    "python-frontmatter>=1.1.0",
    "google-auth-httplib2>=0.1.0", # Added for AuthorizedHttp
    "httplib2>=0.20.0", # Added as dependency for google-auth-httplib2 and direct use
    "tzdata", # Added for zoneinfo
]
```

### A.5. 今後のメンテナンス (LLM エージェント向け)

*   **コード構造:** 主要なロジックは `main.py` 内の `GoogleDriveClient` クラス（API通信）と `MarkdownProcessor` クラス（ファイル処理）に分割されています。`main()` 関数が全体の処理フローを制御します。
*   **設定変更:**
    *   基本的な動作設定（リトライ回数、画像サイズ、出力サブディレクトリ名など）は `main.py` 冒頭の定数を変更します。
    *   Google Drive の対象フォルダは GitHub Secret `GOOGLE_DRIVE_PARENT_ID` で設定します。
    *   デプロイ先や認証情報は GitHub Secrets で管理されます。
*   **エラー発生時の確認ポイント:**
    *   GitHub Actions の実行ログを確認してください。`main.py` は `logging` モジュールを使用しており、詳細な情報やエラーメッセージが出力されます。
    *   `main.py` 内の `_execute_with_retry` メソッドは API エラー時のリトライ状況をログ出力します。
    *   ファイル処理に失敗した場合、スクリプトは非ゼロの終了コードで終了し、Actions のステップが失敗します。ログで `Processing failed for ...` や `Exiting with error code 1 ...` といったメッセージを探してください。
    *   キャッシュ関連の問題が疑われる場合は、手動実行時に `ignore_cache` を `true` にして試してください。
*   **機能追加・変更:**
    *   Google Drive API 関連の変更は `GoogleDriveClient` クラスを修正します。
    *   Markdown の処理（フロントマター、画像変換など）の変更は `MarkdownProcessor` クラスを修正します。
    *   並列処理や全体のフローの変更は `main()` 関数および `process_single_file_task` 関数を修正します。
    *   ワークフロー自体の変更（トリガー、使用する Action、デプロイ方法など）は `.github/workflows/google-drive-access.yml` を編集します。
