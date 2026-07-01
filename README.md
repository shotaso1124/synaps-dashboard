# Synaps ダッシュボード

Synaps アプリの **AdMob 広告収益** と **App Store ダウンロード** を、1画面のKPI・
推移グラフとして確認できる Streamlit 製の **非公開ダッシュボード**です。

データ投入は2通り:

- **手動**: CSV/TSV をアップロード（API キー不要で即動く）。
- **自動取得**: App Store Connect API の Secrets を設定すると、サイドボタンで
  **CSV アップロード無しに** ダウンロード数・国別が自動で入る（`asc_api.py`）。

> ⚠️ **収益は機微情報です。** 公開デプロイは禁止。Streamlit Community Cloud の
> **Private(招待制)** で保護し、加えて簡易パスワードゲートをかける前提です。

---

## 機能

- **アップロード**: サイドバーから「AdMob レポート CSV」「App Store Sales レポート
  (TSV/CSV, gzip 解凍後も可)」を投入。両方任意（片方だけでも表示）。
- **集計** (`parsers.py`):
  - AdMob: 日付・国・収益(ESTIMATED_EARNINGS)・表示回数(IMPRESSIONS)・クリックを
    列名の表記ゆれに寛容に吸収。**eCPM = 収益 / 表示回数 × 1000** を再算出。
  - ASC Sales: `Units`(DL数)・`Country Code`・`Product Type Identifier`
    (`1F` 等の初回DLのみ集計、アプリ内課金 `IA*` は除外)・`Begin Date`。
- **表示**:
  - KPI カード(`st.metric`): 直近確定日の収益／当月累計収益／累計DL／当月DL／
    直近eCPM（前日比の増減つき）。
  - 折れ線(`st.line_chart`): 収益・DL・eCPM・表示回数の推移。
  - 国別 Top10（表＋棒グラフ）: DL国別・収益国別。
  - サイドバーの期間フィルタ(7/30/90日・当月・カスタム)。
  - 通貨コード表示＋「対象期間」注記＋「AdMob/ASC は日次遅延」の注記。
- **自動取得** (`asc_api.py`): App Store Connect API の Secrets があれば、サイドバー
  「App StoreからDL取得」で直近 N 日（7〜90日）を取得。JWT(ES256) を毎リクエスト
  前に生成し、`GET /v1/salesReports`(DAILY/SALES/SUMMARY, gzip-TSV) を日付ループで
  取得 → `1F` 等の初回DLのみ集計 → 既存の可視化に流し込む。**当日など未確定日
  (404/空) はスキップして継続**。取得結果は `data/asc_downloads.parquet` にローカル
  キャッシュ（`data/` は `.gitignore` 済み）。Secrets が無ければ CSV アップロードに
  フォールバックするため、既存動作は壊れません。
- **認証**: `st.secrets["password"]` があれば入力を要求、無ければスキップ
  （ローカル開発用）。

---

## App Store Connect API 自動取得の Secrets 設定

> 実キーはチャットに貼らず、下記の Secrets に **代表が直接** 投入してください。
> 実取得はキー投入後に、サイドバーのボタンで実行されます。

### 必要なもの（App Store Connect で発行）

App Store Connect →「ユーザとアクセス」→「統合」→「App Store Connect API」で、
**Sales and Reports（またはそれ以上）** ロールのキーを発行し、次の4値を用意します。

| Secrets キー | 取得元 |
|---|---|
| `ASC_ISSUER_ID` | 「App Store Connect API」ページ上部の **Issuer ID** |
| `ASC_KEY_ID` | 発行したキーの **Key ID**（10文字程度） |
| `ASC_P8` | 発行時に一度だけDLできる **`AuthKey_XXXXXXXXXX.p8`** の本文 |
| `ASC_VENDOR_NUMBER` | 「販売とトレンド」→「レポート」左上の **Vendor 番号**（数字） |

### ローカル設定（`~/Developer/synaps-dashboard/.streamlit/secrets.toml`）

```toml
# パスワードゲート（任意）
password = "十分に長いランダムなパスワード"

# App Store Connect API
ASC_ISSUER_ID    = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
ASC_KEY_ID       = "ABCDE12345"
ASC_VENDOR_NUMBER = "12345678"

# .p8 は「本文まるごと」を三重引用符で。改行はそのまま貼ればOK。
ASC_P8 = """-----BEGIN PRIVATE KEY-----
MIGTAgEA...（AuthKey_XXXXXXXXXX.p8 の中身をそのまま）...
-----END PRIVATE KEY-----"""
```

- `secrets.toml` の三重引用符なら **改行はそのまま** で構いません。
- 環境変数で渡す場合（`ASC_P8` を1行にする必要があるとき）は、改行を
  `\n` エスケープした文字列にすれば `asc_api.py` 側で実改行に復元します。

### デプロイ時（Streamlit Community Cloud / GitHub Actions）

同じキー名 `ASC_ISSUER_ID` / `ASC_KEY_ID` / `ASC_VENDOR_NUMBER` / `ASC_P8` を、
アプリの **Settings → Secrets**（または Actions の Secrets）に登録します。値は
ローカルの `secrets.toml` と同一です。

### ⚠️ 絶対にコミットしないもの

- `.streamlit/secrets.toml` と `*.p8`（`.gitignore` で除外済み）。
- 取得データ `data/`（同じく除外済み）。
- 万一 `git status` にこれらが出たら **コミット前に必ず除外**してください。

---

## AdMob 広告収益 自動取得の Secrets 設定

> AdMob Reporting API は **OAuth2 のユーザー認可（リフレッシュトークン方式）** が
> 必要で、**サービスアカウントは使えません**。初回だけ `admob_auth.py` を実行して
> refresh_token を取得し、Secrets に入れると、以後はサイドバーのボタンで
> **CSV アップロード無しに** 広告収益・表示回数・国別・eCPM が自動で入ります。
> 実キーはチャットに貼らず、Secrets に **代表が直接** 投入してください。

### 1. GCP プロジェクトと AdMob API の準備（一度だけ）

1. **GCP プロジェクトを作成**: [Google Cloud Console](https://console.cloud.google.com/)
   →「プロジェクトを作成」（既存プロジェクトでも可）。
2. **AdMob API を有効化**: 「API とサービス」→「ライブラリ」→ **「AdMob API」**
   を検索して **有効化**。
3. **OAuth 同意画面を設定**: 「API とサービス」→「OAuth 同意画面」
   - User Type: **外部**（個人 Google アカウントで AdMob を見ている場合）。
   - スコープに **`https://www.googleapis.com/auth/admob.readonly`** を追加。
   - **テストユーザー** に **AdMob を閲覧できる自分の Google アカウント** を追加
     （公開申請は不要。テストユーザーのままで使えます）。
4. **OAuth クライアント ID を発行**: 「API とサービス」→「認証情報」
   →「認証情報を作成」→「OAuth クライアント ID」→ アプリの種類 **「デスクトップ
   アプリ」** を選び作成。表示される **クライアント ID** と **クライアント
   シークレット** を控えます。

### 2. `admob_auth.py` で refresh_token を取得（一度だけ）

```bash
cd ~/Developer/synaps-dashboard
source .venv/bin/activate
python admob_auth.py
```

- Client ID / Client Secret を聞かれたら、手順1で控えた値を入力
  （事前に環境変数 `ADMOB_CLIENT_ID` / `ADMOB_CLIENT_SECRET` を入れておけば自動取得）。
- 表示された **認可 URL をブラウザで開き**、テストユーザーに追加した Google で
  **許可** → 画面に出る **認可コードをコピー** → ターミナルに貼り付け。
- 成功すると **`ADMOB_REFRESH_TOKEN`（refresh_token）** が表示されます。
  このスクリプトは値を **保存しません**（誤コミット防止）。表示値を控えてください。

### 3. Secrets に記入（`~/Developer/synaps-dashboard/.streamlit/secrets.toml`）

```toml
# AdMob Reporting API（OAuth2 リフレッシュトークン方式）
ADMOB_CLIENT_ID     = "xxxxxxxxxxxx-xxxx.apps.googleusercontent.com"
ADMOB_CLIENT_SECRET = "GOCSPX-xxxxxxxxxxxxxxxx"
ADMOB_REFRESH_TOKEN = "1//0xxxxxxxxxxxxxxxxxxxxxxxxx"
# Publisher ID は既定 pub-3967754936311621。別 ID のときだけ設定（"ca-app-" は付けない）。
# ADMOB_PUBLISHER_ID = "pub-3967754936311621"
```

- **Publisher ID の注意**: AdMob アプリ ID の `ca-app-pub-3967754936311621` から
  先頭の `ca-app-` を外した **`pub-3967754936311621`** が API の account 名です
  （既定値なので通常は設定不要。コード側でも `ca-app-` は自動除去します）。
- デプロイ時（Streamlit Community Cloud）は同じキー名をアプリの
  **Settings → Secrets** に登録します。

### 4. 取得

サイドバーの **「自動取得 (AdMob)」→「AdMobから収益取得」** ボタンで直近 N 日
（7〜90日）を取得します。`POST /v1/accounts/{account}/networkReport:generate` を
1回叩き、**収益は micros（実額×1,000,000）で返るので 1,000,000 で割って通貨額** に
変換、`DATE`/`COUNTRY` で集計、**eCPM = 収益 / 表示回数 × 1000** を自算出します。
取得結果は `data/admob_revenue.parquet`（+ JSON メタ）にローカルキャッシュします
（`data/` は `.gitignore` 済み）。Secrets が無ければ AdMob CSV アップロードに
フォールバックするため、既存動作は壊れません。

### ⚠️ 絶対にコミットしないもの（AdMob）

- `ADMOB_CLIENT_SECRET` / `ADMOB_REFRESH_TOKEN`（`secrets.toml` に入れる。除外済み）。
- GCP からダウンロードした OAuth クライアント JSON（`client_secret*.json` 等、
  `.gitignore` 済み）。

---

## ローカル実行

```bash
cd ~/Developer/synaps-dashboard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

ブラウザで http://localhost:8501 を開き、サイドバーから
`sample_data/admob_sample.csv` と `sample_data/asc_sales_sample.tsv` を
アップロードすると動作を確認できます。

### パーサ層だけを CLI で検証

`streamlit run` は headless では確認しづらいため、パーサ層は CLI で検証できます。

```bash
python parsers.py --selftest
```

サンプルの収益・DL・eCPM の集計値が数値として出力されれば OK です。

App Store Connect API 層（`asc_api.py`）も、**実キー・実 API なし**でモック検証
できます（その場で生成した EC P-256 鍵で JWT に署名して構造を確認し、合成
gzip-TSV で `1F` フィルタ・国別集計を数値で検証）。

```bash
python asc_api.py --selftest
```

AdMob API 層（`admob_api.py`）も、**実 OAuth・実 API なし**でモック検証できます
（合成レスポンスで **micros 変換**・日次/国別集計・**eCPM 算出**・アクセストークン
更新リクエストの整形・401/403 の明示エラー化とトークン非漏洩・キャッシュ往復を検証）。

```bash
python admob_api.py --selftest
```

---

## レポートの取得方法（データ元）

- **AdMob CSV**: [AdMob](https://apps.admob.com/) → レポート → 任意の
  ディメンション(日付・国など)と指標(推定収益・表示回数・クリック)を選び、
  右上「エクスポート → CSV」。
- **App Store Sales TSV**: [App Store Connect](https://appstoreconnect.apple.com/)
  → 分析 → 「販売とトレンド」→ レポート → 日次レポートをダウンロード
  (`.txt.gz`)。gzip のままでも解凍後(タブ区切り)でもアップロード可能です。

---

## 非公開デプロイ手順（代表が実施）

> このリポジトリでは **GitHub 作成・push・Streamlit Cloud デプロイは行っていません。**
> 下記の手順は代表が後で実施するためのメモです。

1. **GitHub に Private リポジトリを作成して push**
   ```bash
   cd ~/Developer/synaps-dashboard
   git init
   git add .
   git commit -m "init: synaps dashboard"
   gh repo create synaps-dashboard --private --source=. --remote=origin --push
   ```
   `.gitignore` により `.streamlit/secrets.toml` / `*.p8` / `data/` /
   実データの `*.csv` `*.tsv` `*.txt.gz` / `.env` はコミットされません
   （`sample_data/` の合成データのみ追跡）。

2. **Streamlit Community Cloud でアプリを作成**
   - https://share.streamlit.io/ → 「New app」→ 作成した `synaps-dashboard`
     リポジトリと `app.py` を選択。

3. **Secrets を登録**（アプリの Settings → Secrets）
   ```toml
   password = "十分に長いランダムなパスワード"
   ```
   これで閲覧時にパスワードが要求されます。

4. **Private(招待制)に設定**
   - アプリの Settings → Sharing → 「Only specific people can view this app」
     を選び、閲覧を許可するメールアドレス（代表のみ等）を招待。
   - 収益データを含むため、**公開(Public)にはしない**こと。

---

## セキュリティ / 機密の扱い

- **アップロードデータはサーバに保存しません**（Streamlit のセッション内メモリで
  処理し、リロードで消えます）。
- `.gitignore` で機密ファイルと実データを除外済み。**収益・DL の実 CSV/TSV を
  リポジトリにコミットしないでください。**
- パスワードは Secrets 管理。コードにハードコードしないこと。
