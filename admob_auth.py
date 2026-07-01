"""AdMob 用の refresh_token を1回だけ取得する認可 CLI(代表が実行)。

AdMob Reporting API は OAuth2 のユーザー認可(リフレッシュトークン方式)が必要で、
サービスアカウントは使えない。このスクリプトを **1回だけ** 実行し、表示される
refresh_token を Streamlit Secrets(または環境変数)に入れると、以後は
`admob_api.py` が refresh_token からアクセストークンを自動更新して収益を取得する。

やること:
  1. client_id / client_secret を Secrets or 対話入力で受け取る。
  2. インストールアプリ型 OAuth フロー(InstalledAppFlow.run_local_server)で
     ローカルの一時サーバを立て、ブラウザを自動で開く。
  3. 代表がブラウザで許可 → 自動でローカルサーバへ戻る(コード貼付は不要)。
  4. 取得した **refresh_token を画面に表示**する(このスクリプトは保存しない)。
  5. 代表が refresh_token を Secrets(ADMOB_REFRESH_TOKEN)に入れる。

補足: 2022年に Google が手動コード貼付(アウトオブバンド)方式を廃止したため、
run_local_server(http://localhost リダイレクトを自動受信)を使う。

セキュリティ:
  - client_secret / refresh_token は **ファイルに保存しない**(画面表示のみ)。
  - 代表が Secrets に手で入れる運用(誤コミット防止)。

前提: `pip install google-auth-oauthlib`(requirements.txt に記載)。

実行:
    python admob_auth.py
"""

from __future__ import annotations

import os
from pathlib import Path

# AdMob Reporting API の読み取り専用スコープ。
SCOPES = ["https://www.googleapis.com/auth/admob.readonly"]


def _secret_from_toml(key: str) -> str | None:
    """.streamlit/secrets.toml を直接 TOML パースして値を探す。無ければ None。

    `streamlit run` ではなく素の `python admob_auth.py` で実行すると Streamlit
    ランタイムが無く st.secrets が読めないため、TOML を直接読むフォールバック。
    秘密値はここでログ/例外に出さない(値そのものは print しない)。
    """
    try:
        import tomllib  # Python 3.11+ 標準ライブラリ
    except ImportError:  # noqa: BLE001 — 3.10 以下
        return None
    for path in (
        Path(".streamlit") / "secrets.toml",
        Path(__file__).resolve().parent / ".streamlit" / "secrets.toml",
    ):
        try:
            if not path.is_file():
                continue
            with path.open("rb") as f:
                data = tomllib.load(f)
            v = data.get(key)
            if v:
                return str(v)
        except Exception:  # noqa: BLE001 — 壊れた TOML 等。秘密は出さない。
            continue
    return None


def _secret_or_none(key: str) -> str | None:
    """環境変数 → st.secrets → secrets.toml 直読 の順で値を探す。無ければ None。

    このスクリプトは通常 Streamlit 外(素の python)で実行するため、
    まず環境変数、次に(あれば)Secrets、最後に TOML 直読を見る。
    """
    val = os.environ.get(key)
    if val:
        return val
    try:
        import streamlit as st  # 遅延 import

        try:
            v = st.secrets.get(key)  # type: ignore[attr-defined]
            if v:
                return str(v)
        except Exception:  # noqa: BLE001 — secrets.toml が無い / ランタイム外等
            pass
    except Exception:  # noqa: BLE001 — streamlit 未導入
        pass
    # Streamlit ランタイム外でも secrets.toml から取れるようにする。
    return _secret_from_toml(key)


def _prompt(label: str) -> str:
    """対話入力を1行受け取る(前後空白を除去)。"""
    try:
        return input(f"{label}: ").strip()
    except EOFError:
        return ""


def _get_client_config() -> tuple[str, str]:
    """client_id / client_secret を Secrets or 対話入力で用意する。

    どちらも空なら対話入力を促す。戻り値: (client_id, client_secret)。
    """
    client_id = _secret_or_none("ADMOB_CLIENT_ID")
    client_secret = _secret_or_none("ADMOB_CLIENT_SECRET")

    if not client_id:
        print(
            "\nGCP で発行した OAuth クライアント(デスクトップ アプリ)の "
            "クライアント ID / シークレットを入力してください。"
        )
        client_id = _prompt("Client ID")
    else:
        print(f"Client ID: Secrets/環境変数から取得しました({client_id[:12]}…)")

    if not client_secret:
        client_secret = _prompt("Client Secret")
    else:
        print("Client Secret: Secrets/環境変数から取得しました(表示は伏せます)")

    if not client_id or not client_secret:
        raise SystemExit(
            "Client ID / Client Secret が未入力です。中止します。"
        )
    return client_id, client_secret


def _build_flow(client_id: str, client_secret: str):
    """InstalledAppFlow を client 設定辞書から組み立てる。

    google-auth-oauthlib が必要。未導入なら分かりやすいメッセージで中止する。
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # noqa: BLE001
        raise SystemExit(
            "google-auth-oauthlib が見つかりません。\n"
            "  pip install -r requirements.txt\n"
            "を実行してから再度お試しください。"
        ) from exc

    # 「デスクトップ アプリ」型 OAuth クライアントの client 設定。
    # run_local_server が使う http://localhost リダイレクトのみを許可する。
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    return InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)


def _run_local_server_flow(flow) -> str:
    """ローカルの一時サーバでリダイレクトを自動受信し refresh_token を返す。

    Google は 2022 年に手動コード貼付(アウトオブバンド)を廃止したため、run_local_server
    を使う。空きポートに一時サーバを立て、ブラウザを自動で開いて代表が許可すると
    自動で戻る(コード貼付は不要)。access_type=offline / prompt=consent は
    **kwargs 経由で authorization_url に渡され、refresh_token を確実に取得する。
    """
    print("\n" + "=" * 70)
    print("ブラウザを自動で開きます。AdMob を見られる Google アカウントで許可してください。")
    print("(許可後は自動でこのツールに戻ります。コードの貼り付けは不要です。)")
    print("ブラウザが開かない場合は、表示される URL を手動で開いてください。")
    print("=" * 70)

    # port=0 で空きポートを自動選択(Desktop 型は http://localhost を許可)。
    # access_type / prompt は run_local_server の **kwargs 経由で
    # authorization_url に渡り、refresh_token を確実に得る。
    creds = flow.run_local_server(
        port=0,
        access_type="offline",   # refresh_token を得るため必須
        prompt="consent",        # 既存同意でも refresh_token を確実に再発行
        open_browser=True,
    )

    refresh_token = getattr(creds, "refresh_token", None)
    if not refresh_token:
        raise SystemExit(
            "refresh_token が取得できませんでした。\n"
            "OAuth 同意画面で access_type=offline / prompt=consent になっているか、"
            "既に発行済みで再同意が無効化されていないかを確認してください。"
        )
    return str(refresh_token)


def main() -> int:
    print("=" * 70)
    print("AdMob refresh_token 取得ツール(1回だけ実行)")
    print("=" * 70)
    print(
        "GCP で AdMob API 有効化・OAuth 同意画面(scope admob.readonly / テスト"
        "ユーザーに自分を追加)・OAuth クライアント ID(デスクトップ アプリ)を"
        "作成済みであることが前提です(手順は README を参照)。"
    )

    client_id, client_secret = _get_client_config()
    flow = _build_flow(client_id, client_secret)
    refresh_token = _run_local_server_flow(flow)

    print("\n" + "=" * 70)
    print("✅ refresh_token を取得しました。下の値を Secrets に入れてください。")
    print("=" * 70)
    print("\n--- .streamlit/secrets.toml に追記(またはデプロイ先の Secrets) ---\n")
    print(f'ADMOB_CLIENT_ID     = "{client_id}"')
    print('ADMOB_CLIENT_SECRET = "（あなたの client secret）"')
    print(f'ADMOB_REFRESH_TOKEN = "{refresh_token}"')
    print('# ADMOB_PUBLISHER_ID = "pub-3967754936311621"  # 既定と同じなら省略可')
    print("\n" + "=" * 70)
    print(
        "注意: この refresh_token は機密です。チャットに貼らず、Secrets にのみ"
        "保存してください。このスクリプトはファイルに保存しません。"
    )
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
