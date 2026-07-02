"""Synaps ダッシュボード (Streamlit)。

- 「収益・DL」タブ: AdMob レポート CSV / App Store Connect Sales レポート
  (TSV/CSV, gzip可)のアップロード(または API 自動取得)から、収益・DL・eCPM の
  KPI と推移グラフを表示する。
- 「維持率」タブ: App Store Connect の維持率エクスポート CSV から、D1/D7/D30 の
  推移・コホートヒートマップを表示し、2.6 リリース前後の効果測定を支援する。

収益は機微情報のため非公開デプロイ(招待制 + 簡易パスワード)前提。
"""

from __future__ import annotations

import hmac
import time
from datetime import date, timedelta
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

import admob_api
import asc_api
from parsers import parse_admob, parse_asc_retention, parse_asc_sales

st.set_page_config(page_title="Synaps ダッシュボード", layout="wide")


# ---------------------------------------------------------------------------
# 認証 (公開URL向けパスワードゲート / fail-closed)
# ---------------------------------------------------------------------------
#
# このダッシュボードは収益(機微)データを扱い、公開URL(Streamlit Community
# Cloud・Public)で配布する。パスワードゲートが唯一の防御層のため、以下を厳守:
#   - fail-closed: Secrets に password が未設定/空なら「アクセス拒否」。
#     絶対に "未設定なら素通り(fail-open)" にしない(設定漏れ=全露出を防ぐ)。
#   - 認証前は一切データを描画・取得しない(main 冒頭で require_auth を呼び、
#     通過するまで st.stop() で以降を完全停止)。
#   - 比較は hmac.compare_digest(タイミング攻撃対策)。
#   - 総当たり緩和(失敗ごと sleep + 一定回数でロック)。
#   - 認証は session_state で保持し、ログアウトで破棄できる。

# 総当たり緩和パラメータ。
_LOCK_THRESHOLD = 5          # この回数連続失敗するとロック。
_LOCK_SECONDS = 30           # ロック時間(秒)。
_FAIL_SLEEP_SECONDS = 0.5    # 失敗ごとの待機(即時連打を抑止)。


def _get_secret_password() -> str | None:
    """Streamlit Secrets からパスワードを取得する。

    Returns:
        設定された空でないパスワード文字列。
        未設定・空・空白のみ・secrets.toml 不在などはすべて ``None``(=未設定)。

    fail-closed の起点。値の中身はログにも画面にも出さない。
    """
    try:
        raw = st.secrets.get("password", "")
    except Exception:  # noqa: BLE001 — secrets.toml 自体が無い場合がある
        return None
    text = str(raw)
    # 空文字・空白のみは "未設定" とみなす(設定漏れを素通りさせない)。
    if not text.strip():
        return None
    return text


def require_auth() -> None:
    """認証を要求する。通過するまで st.stop() で以降の実行を止める。

    - Secrets に password が無ければ fail-closed(管理者設定エラーで停止)。
    - 未認証ならパスワード入力欄のみを描画し停止(データ取得・描画は一切走らない)。
    - 認証済み(session_state["authed"]==True)なら即 return し本体へ進む。
    """
    # 既に認証済みなら何も描画せず通過。
    if st.session_state.get("authed") is True:
        return

    expected = _get_secret_password()

    # ★fail-closed: パスワード未設定なら公開URLで全露出しないよう必ず拒否。
    if expected is None:
        st.title("Synaps ダッシュボード")
        st.error(
            "管理者設定エラー: パスワード未設定のため表示できません。"
            "デプロイの Secrets に `password` を設定してください。"
        )
        st.stop()

    st.title("Synaps ダッシュボード")
    st.caption("収益データを含むため、閲覧にはパスワードが必要です。")

    now = time.monotonic()
    locked_until = float(st.session_state.get("_auth_locked_until", 0.0))
    remaining = locked_until - now
    if remaining > 0:
        # ロック中は入力自体を受け付けない。
        st.error(
            f"試行回数が上限に達しました。約 {int(remaining) + 1} 秒後に再試行してください。"
        )
        st.stop()

    entered = st.text_input("パスワード", type="password", key="_auth_input")

    if entered:
        # タイミング攻撃を避けるため定数時間比較を使う(入力・期待値とも文字列)。
        if hmac.compare_digest(str(entered), str(expected)):
            st.session_state["authed"] = True
            # 認証成功時は失敗カウンタ・ロックを掃除。
            st.session_state["_auth_fail_count"] = 0
            st.session_state["_auth_locked_until"] = 0.0
            st.rerun()
        else:
            # 失敗カウント + 即時連打の抑止。
            fails = int(st.session_state.get("_auth_fail_count", 0)) + 1
            st.session_state["_auth_fail_count"] = fails
            time.sleep(_FAIL_SLEEP_SECONDS)
            if fails >= _LOCK_THRESHOLD:
                st.session_state["_auth_locked_until"] = (
                    time.monotonic() + _LOCK_SECONDS
                )
                st.error(
                    f"試行回数が上限に達しました。約 {_LOCK_SECONDS} 秒後に"
                    "再試行してください。"
                )
            else:
                # ヒントを出さない汎用エラー。
                st.error("パスワードが違います。")

    # 未認証(未入力/誤り)の間は本体を一切実行しない。
    st.stop()


def _logout_button() -> None:
    """サイドバー下部のログアウト。押下で認証状態を破棄し再度パスワードを要求。"""
    st.sidebar.divider()
    if st.sidebar.button("ログアウト", key="_logout_btn"):
        for k in ("authed", "_auth_fail_count", "_auth_locked_until", "_auth_input"):
            st.session_state.pop(k, None)
        st.rerun()


# ---------------------------------------------------------------------------
# 集計ヘルパー
# ---------------------------------------------------------------------------


def _filter_by_period(
    df: pd.DataFrame, start: date, end: date, col: str = "date"
) -> pd.DataFrame:
    """期間 [start, end] で日付列をフィルタする。"""
    if df.empty or col not in df.columns:
        return df
    d = df.copy()
    d[col] = pd.to_datetime(d[col])
    mask = (d[col].dt.date >= start) & (d[col].dt.date <= end)
    return d[mask]


def _month_to_date_sum(df: pd.DataFrame, latest: date, value_col: str) -> float:
    """latest の当月1日〜latest までの value_col 合計を返す。"""
    if df.empty or value_col not in df.columns:
        return 0.0
    first = latest.replace(day=1)
    sub = _filter_by_period(df, first, latest)
    return float(sub[value_col].sum()) if not sub.empty else 0.0


def _prev_day_value(df: pd.DataFrame, latest: date, value_col: str) -> float | None:
    """latest の1つ前の日付の value_col 値(前日比表示用)。無ければ None。"""
    if df.empty or "date" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.date
    prev = d[d["date"] < latest].sort_values("date")
    if prev.empty:
        return None
    return float(prev.iloc[-1][value_col])


# 通貨記号テーブル。currencyCode(JPY/USD 等) から表示用記号を導出する。
_CURRENCY_SYMBOLS = {
    "JPY": "¥",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "KRW": "₩",
    "CNY": "¥",
    "INR": "₹",
    "AUD": "A$",
    "CAD": "C$",
    "TWD": "NT$",
    "HKD": "HK$",
    "SGD": "S$",
    "BRL": "R$",
}

# 小数を持たない(最小単位が1)通貨。これらは 0 桁で表示する。
_ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "TWD", "HUF", "CLP", "VND", "IDR"}


def _currency_symbol(currency: str) -> str:
    """currencyCode を表示用の記号に変換する。未知のコードはそのまま返す。"""
    code = str(currency or "").upper()
    return _CURRENCY_SYMBOLS.get(code, code)


def _currency_decimals(currency: str) -> int:
    """通貨ごとの小数桁数を返す(JPY 等=0、その他=2)。"""
    return 0 if str(currency or "").upper() in _ZERO_DECIMAL_CURRENCIES else 2


def _money_axis_format(currency: str) -> str:
    """Altair(D3)用の通貨フォーマット文字列。桁区切り + 通貨記号 + 小数桁。

    例: JPY -> "¥,.0f" / USD -> "$,.2f" / 未知コード -> ",.2f"
    """
    symbol = _currency_symbol(currency)
    decimals = _currency_decimals(currency)
    prefix = symbol if len(symbol) == 1 else ""  # 複数文字記号は軸に付けず桁区切りのみ
    return f"{prefix},.{decimals}f"


def _fmt_money(value: float, currency: str) -> str:
    """KPI カード等の文字列表示用: 通貨記号 + 桁区切り + 通貨ごとの小数桁。"""
    symbol = _currency_symbol(currency)
    decimals = _currency_decimals(currency)
    return f"{symbol}{value:,.{decimals}f}"


# ---------------------------------------------------------------------------
# 表示: KPI カード
# ---------------------------------------------------------------------------


def _render_admob_kpis(admob: dict[str, Any]) -> None:
    daily = admob["daily"]
    currency = admob["currency"]
    if daily.empty:
        st.info("AdMob データがありません。")
        return

    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"]).dt.date
    latest = daily["date"].max()
    latest_row = daily[daily["date"] == latest].iloc[0]

    earn_latest = float(latest_row["earnings"])
    ecpm_latest = float(latest_row["ecpm"])
    mtd_earn = _month_to_date_sum(daily, latest, "earnings")

    prev_earn = _prev_day_value(daily, latest, "earnings")
    prev_ecpm = _prev_day_value(daily, latest, "ecpm")

    sym = _currency_symbol(currency)
    dec = _currency_decimals(currency)
    c1, c2, c3 = st.columns(3)
    c1.metric(
        f"直近確定日の収益 ({latest})",
        _fmt_money(earn_latest, currency),
        delta=(
            None
            if prev_earn is None
            else f"{sym}{earn_latest - prev_earn:+,.{dec}f}"
        ),
    )
    c2.metric("当月累計収益", _fmt_money(mtd_earn, currency))
    c3.metric(
        "直近eCPM",
        _fmt_money(ecpm_latest, currency),
        delta=(
            None
            if prev_ecpm is None
            else f"{sym}{ecpm_latest - prev_ecpm:+,.{max(dec, 2)}f}"
        ),
    )


def _render_asc_kpis(asc: dict[str, Any]) -> None:
    daily = asc["daily"]
    if daily.empty:
        st.info("App Store DL データがありません。")
        return

    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"]).dt.date
    latest = daily["date"].max()

    total_dl = int(asc["total_downloads"])
    mtd_dl = int(_month_to_date_sum(daily, latest, "downloads"))
    latest_dl = int(daily[daily["date"] == latest].iloc[0]["downloads"])
    prev_dl = _prev_day_value(daily, latest, "downloads")

    c1, c2, c3 = st.columns(3)
    c1.metric("累計DL(期間内)", f"{total_dl:,}")
    c2.metric("当月DL", f"{mtd_dl:,}")
    c3.metric(
        f"直近日DL ({latest})",
        f"{latest_dl:,}",
        delta=(None if prev_dl is None else f"{latest_dl - int(prev_dl):+,}"),
    )

    # Product Type 別内訳(初回DL確定コードごと)。透明性のため任意表示。
    bpt = asc.get("by_product_type")
    if isinstance(bpt, pd.DataFrame) and not bpt.empty:
        with st.expander("Product Type 別内訳（初回DLのみ集計）"):
            st.caption(
                "更新(7*)・アプリ内課金/サブスク(IA*)・bare 1 は集計から除外しています。"
            )
            st.dataframe(bpt, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# 表示: グラフ・国別
# ---------------------------------------------------------------------------


def _line(
    df: pd.DataFrame,
    value_col: str,
    label: str,
    *,
    y_title: str,
    y_format: str,
    color: str = "#38BDF8",
) -> None:
    """日付を x 軸にした折れ線を Altair で描く。

    Args:
        y_title: Y 軸タイトル(単位込み。例「収益 (¥)」)。
        y_format: 値の D3 数値フォーマット(例「¥,.0f」「,.0f」)。tooltip/軸に適用。
        color: 線色(ダーク背景で視認性のある明色)。
    """
    if df.empty or value_col not in df.columns:
        st.info(f"{label}: データなし")
        return
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    chart = (
        alt.Chart(d)
        .mark_line(point=True, color=color)
        .encode(
            x=alt.X("date:T", title="日付", axis=alt.Axis(format="%m/%d")),
            y=alt.Y(
                f"{value_col}:Q",
                title=y_title,
                axis=alt.Axis(format=y_format),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="日付", format="%Y-%m-%d"),
                alt.Tooltip(f"{value_col}:Q", title=label, format=y_format),
            ],
        )
        .properties(height=280)
    )
    st.altair_chart(chart, use_container_width=True)


def _country_top10(
    df: pd.DataFrame,
    value_col: str,
    label: str,
    *,
    x_title: str,
    x_format: str,
    color: str = "#38BDF8",
) -> None:
    """国別 Top10 を表と横棒グラフ(Altair)で表示する。

    Args:
        x_title: 値軸タイトル(単位込み。例「収益 (¥)」)。
        x_format: 値の D3 数値フォーマット。軸・数値ラベル・tooltip に適用。
    """
    if df.empty or value_col not in df.columns:
        st.info(f"{label}: データなし")
        return
    top = df.sort_values(value_col, ascending=False).head(10)
    col_table, col_chart = st.columns([1, 1])
    with col_table:
        st.dataframe(top, use_container_width=True, hide_index=True)
    with col_chart:
        base = alt.Chart(top).encode(
            x=alt.X(f"{value_col}:Q", title=x_title, axis=alt.Axis(format=x_format)),
            y=alt.Y("country:N", title="国", sort="-x"),
            tooltip=[
                alt.Tooltip("country:N", title="国"),
                alt.Tooltip(f"{value_col}:Q", title=label, format=x_format),
            ],
        )
        bars = base.mark_bar(color=color)
        labels = base.mark_text(
            align="left", baseline="middle", dx=3, color="#E2E8F0"
        ).encode(text=alt.Text(f"{value_col}:Q", format=x_format))
        st.altair_chart((bars + labels).properties(height=300), use_container_width=True)


# ---------------------------------------------------------------------------
# 期間フィルタ UI
# ---------------------------------------------------------------------------


def _collect_date_range(
    admob: dict[str, Any] | None, asc: dict[str, Any] | None
) -> tuple[date | None, date | None]:
    """両データの日付範囲(最小・最大)を求める。"""
    dates: list[date] = []
    for src in (admob, asc):
        if src and not src["daily"].empty:
            col = pd.to_datetime(src["daily"]["date"]).dt.date
            dates.extend([col.min(), col.max()])
    if not dates:
        return None, None
    return min(dates), max(dates)


def _period_selector(dmin: date, dmax: date) -> tuple[date, date]:
    """サイドバーの期間フィルタ。7/30/90日・当月・カスタムを選べる。"""
    st.sidebar.subheader("期間フィルタ")
    choice = st.sidebar.radio(
        "対象期間",
        ["直近7日", "直近30日", "直近90日", "当月", "カスタム"],
        index=1,
    )
    if choice == "直近7日":
        start = max(dmin, dmax - timedelta(days=6))
        return start, dmax
    if choice == "直近30日":
        start = max(dmin, dmax - timedelta(days=29))
        return start, dmax
    if choice == "直近90日":
        start = max(dmin, dmax - timedelta(days=89))
        return start, dmax
    if choice == "当月":
        return dmax.replace(day=1), dmax
    # カスタム
    rng = st.sidebar.date_input(
        "期間を選択",
        value=(dmin, dmax),
        min_value=dmin,
        max_value=dmax,
    )
    if isinstance(rng, tuple) and len(rng) == 2:
        return rng[0], rng[1]
    return dmin, dmax


def _apply_period(src: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """daily を期間で絞り、country を絞り込み後 daily から再集計する。"""
    out = dict(src)
    out["daily"] = _filter_by_period(src["daily"], start, end)
    return out


# ---------------------------------------------------------------------------
# 自動取得 (App Store Connect API)
# ---------------------------------------------------------------------------


def _asc_autofetch_sidebar() -> dict[str, Any] | None:
    """Secrets が揃っていれば「App StoreからDL取得」ボタンを出す。

    - 認証情報(ASC_ISSUER_ID/ASC_KEY_ID/ASC_P8/ASC_VENDOR_NUMBER)が無ければ
      案内だけ出して None を返す(＝従来の CSV アップロードにフォールバック)。
    - ボタン押下で asc_api.fetch_downloads() を呼び、結果を session_state に保持。
    - 取得済みなら「最終取得: …」を表示する。
    戻り値: parse_asc_sales 互換の dict(未取得なら None)。
    """
    st.sidebar.header("自動取得 (App Store)")

    # 再起動後でも前回値が見えるように、session_state が空ならキャッシュ復元。
    if st.session_state.get("_asc_auto") is None:
        restored = asc_api.load_cache()
        if restored is not None and not restored["daily"].empty:
            st.session_state["_asc_auto"] = restored

    if not asc_api.has_credentials():
        st.sidebar.info(
            "ASC の Secrets 未設定です。CSV アップロードで表示できます。\n\n"
            "自動取得には Secrets に "
            "`ASC_ISSUER_ID` `ASC_KEY_ID` `ASC_P8` `ASC_VENDOR_NUMBER` "
            "を設定してください（README 参照）。"
        )
        # Secrets 未設定でも、前回キャッシュがあれば表示に使う。
        return st.session_state.get("_asc_auto")

    lookback = st.sidebar.slider("取得日数（直近）", 7, 90, 30, step=1, key="asc_lookback")
    if st.sidebar.button("App StoreからDL取得", key="asc_fetch_btn"):
        with st.spinner("App Store Connect から取得中…"):
            try:
                result = asc_api.fetch_downloads(lookback_days=int(lookback))
                asc_api.save_cache(result)
                st.session_state["_asc_auto"] = result
                meta = result.get("meta", {})
                st.sidebar.success(
                    f"取得完了: {meta.get('fetched_days', 0)}日 / "
                    f"スキップ {meta.get('skipped_days', 0)}日"
                )
            except asc_api.ASCAuthError as exc:  # 認証失敗は明示メッセージのみ
                st.sidebar.error(str(exc))
            except Exception as exc:  # noqa: BLE001 — UI 側フォールバック
                st.sidebar.error(f"自動取得に失敗しました: {exc}")

    st.sidebar.caption(
        "※ App Store Connect は太平洋時間で日次確定します。"
        "当日・前日分は未確定で取得できないことがあります。"
    )

    result = st.session_state.get("_asc_auto")
    if result is not None:
        meta = result.get("meta", {})
        fetched_at = meta.get("fetched_at", "不明")
        source = "（前回キャッシュ）" if meta.get("source") == "cache" else ""
        st.sidebar.caption(f"最終取得: {fetched_at}{source}")
    return result


# ---------------------------------------------------------------------------
# 自動取得 (AdMob Reporting API)
# ---------------------------------------------------------------------------


def _admob_autofetch_sidebar() -> dict[str, Any] | None:
    """Secrets が揃っていれば「AdMobから収益取得」ボタンを出す。

    - 認証情報(ADMOB_CLIENT_ID/ADMOB_CLIENT_SECRET/ADMOB_REFRESH_TOKEN)が無ければ
      案内だけ出して None を返す(＝従来の AdMob CSV アップロードにフォールバック)。
    - ボタン押下で admob_api.fetch_revenue() を呼び、結果を session_state に保持。
    - 取得済みなら「最終取得: …」を表示する。
    戻り値: parse_admob 互換の dict(未取得なら None)。
    """
    st.sidebar.header("自動取得 (AdMob)")

    # 再起動後でも前回値が見えるように、session_state が空ならキャッシュ復元。
    if st.session_state.get("_admob_auto") is None:
        restored = admob_api.load_cache()
        if restored is not None and not restored["daily"].empty:
            st.session_state["_admob_auto"] = restored

    if not admob_api.has_credentials():
        st.sidebar.info(
            "AdMob の Secrets 未設定です。CSV アップロードで表示できます。\n\n"
            "自動取得には Secrets に "
            "`ADMOB_CLIENT_ID` `ADMOB_CLIENT_SECRET` `ADMOB_REFRESH_TOKEN` "
            "を設定してください（`admob_auth.py` で取得。README 参照）。"
        )
        # Secrets 未設定でも、前回キャッシュがあれば表示に使う。
        return st.session_state.get("_admob_auto")

    lookback = st.sidebar.slider(
        "取得日数（直近）", 7, 90, 30, step=1, key="admob_lookback"
    )
    if st.sidebar.button("AdMobから収益取得", key="admob_fetch_btn"):
        with st.spinner("AdMob から取得中…"):
            try:
                result = admob_api.fetch_revenue(lookback_days=int(lookback))
                admob_api.save_cache(result)
                st.session_state["_admob_auto"] = result
                meta = result.get("meta", {})
                st.sidebar.success(
                    f"取得完了: {meta.get('rows', 0)}行 / "
                    f"{meta.get('range_start', '?')}〜{meta.get('range_end', '?')}"
                )
            except admob_api.AdMobAuthError as exc:  # 認証失敗は明示メッセージのみ
                st.sidebar.error(str(exc))
            except Exception as exc:  # noqa: BLE001 — UI 側フォールバック
                st.sidebar.error(f"自動取得に失敗しました: {exc}")

    st.sidebar.caption(
        "※ AdMob は太平洋時間で日次確定します。"
        "当日・前日分は未確定で取得できないことがあります。"
    )

    result = st.session_state.get("_admob_auto")
    if result is not None:
        meta = result.get("meta", {})
        fetched_at = meta.get("fetched_at", "不明")
        source = "（前回キャッシュ）" if meta.get("source") == "cache" else ""
        st.sidebar.caption(f"最終取得: {fetched_at}{source}")
    return result


# ---------------------------------------------------------------------------
# 表示: 維持率タブ (ASC 維持率 CSV アップロード方式)
# ---------------------------------------------------------------------------

# 折れ線に出すメトリクス(ラベル, 目標経過日)。CSV に Day 30 が無い場合は
# _pick_day_column が近い列(例: Day 28)で代替する。
_RETENTION_METRIC_TARGETS: tuple[tuple[str, int], ...] = (
    ("D1", 1),
    ("D7", 7),
    ("D30", 30),
)
_RETENTION_COLORS = {"D1": "#38BDF8", "D7": "#A78BFA", "D30": "#F472B6"}
_RELEASE_RULE_COLOR = "#FBBF24"
# KPI カード「直近平均」の対象コホート窓(日)。
_RETENTION_RECENT_WINDOW_DAYS = 28
# ヒートマップに出す最大コホート数(行数の爆発防止。直近を優先)。
_HEATMAP_MAX_COHORTS = 90


def _pick_day_column(days: list[int], target: int) -> int | None:
    """target 経過日にちょうど一致する列、無ければ十分近い列を返す。

    ASC のエクスポートは Day 28 までのことが多く、D30 は Day 28 で代替する。
    乖離が大きい(許容幅超)場合は None(そのメトリクスは非表示)。
    Day 0(=常に100%)は代替候補から除外する。
    """
    candidates = [d for d in days if d > 0]
    if not candidates:
        return None
    if target in candidates:
        return target
    best = min(candidates, key=lambda d: (abs(d - target), d))
    tolerance = max(2, int(target * 0.15))
    return best if abs(best - target) <= tolerance else None


def _recent_cohort_mean(
    matrix: pd.DataFrame,
    day: int | None,
    window_days: int = _RETENTION_RECENT_WINDOW_DAYS,
) -> float | None:
    """直近 window_days 分のコホートの維持率平均。

    NaN セル(未到来・プライバシー閾値未達)は除外して平均する。
    有効値が1つも無ければ None。
    """
    if day is None or matrix.empty or day not in matrix.columns:
        return None
    latest = matrix.index.max()
    cutoff = latest - pd.Timedelta(days=window_days - 1)
    values = matrix.loc[matrix.index >= cutoff, day].dropna()
    if values.empty:
        return None
    return float(values.mean())


def _release_date_input() -> date | None:
    """「2.6 リリース日」の入力欄。グラフの縦線マーカーに使う。

    初期値の優先順: セッション保持値 → Secrets の RELEASE_26_DATE(YYYY-MM-DD)。
    入力値はウィジェット外のセッションキーに保持し、再描画後も復元する。
    未指定(空)ならマーカー非表示。
    """
    stored: date | None = st.session_state.get("release_26_date")
    if stored is None:
        raw = ""
        try:
            raw = str(st.secrets.get("RELEASE_26_DATE", "")).strip()
        except Exception:  # noqa: BLE001 — secrets.toml が無い場合
            raw = ""
        if raw:
            try:
                stored = date.fromisoformat(raw)
            except ValueError:
                stored = None
    picked = st.date_input(
        "2.6 リリース日（グラフに点線マーカー表示）",
        value=stored,
        format="YYYY-MM-DD",
        key="_release_26_widget",
        help=(
            "未指定ならマーカー非表示。Secrets の RELEASE_26_DATE"
            "（YYYY-MM-DD）で初期値を設定できます。"
        ),
    )
    if picked is not None:
        st.session_state["release_26_date"] = picked
    return picked


def _retention_trend_chart(
    matrix: pd.DataFrame,
    picks: dict[str, int | None],
    release_date: date | None,
) -> None:
    """D1/D7/D30 のコホート日別推移折れ線(+リリース日の縦点線)を描く。"""
    rows: list[dict[str, Any]] = []
    for label, _target in _RETENTION_METRIC_TARGETS:
        day = picks.get(label)
        if day is None or day not in matrix.columns:
            continue
        series = matrix[day].dropna()
        for cohort_dt, value in series.items():
            rows.append(
                {"cohort_date": cohort_dt, "metric": label, "retention": float(value)}
            )
    if not rows:
        st.info("D1/D7/D30 に対応する経過日列が見つかりませんでした。")
        return
    line_df = pd.DataFrame(rows)
    domain = [
        label
        for label, _t in _RETENTION_METRIC_TARGETS
        if picks.get(label) is not None
    ]
    color_range = [_RETENTION_COLORS[label] for label in domain]
    chart = (
        alt.Chart(line_df)
        .mark_line(point=True)
        .encode(
            x=alt.X(
                "cohort_date:T",
                title="コホート日（インストール日）",
                axis=alt.Axis(format="%m/%d"),
            ),
            y=alt.Y(
                "retention:Q",
                title="維持率 (%)",
                axis=alt.Axis(format=",.1f"),
            ),
            color=alt.Color(
                "metric:N",
                title="指標",
                scale=alt.Scale(domain=domain, range=color_range),
            ),
            tooltip=[
                alt.Tooltip("cohort_date:T", title="コホート日", format="%Y-%m-%d"),
                alt.Tooltip("metric:N", title="指標"),
                alt.Tooltip("retention:Q", title="維持率(%)", format=",.1f"),
            ],
        )
    )
    layers: alt.LayerChart | alt.Chart = chart
    if release_date is not None:
        rule_df = pd.DataFrame({"release": [pd.Timestamp(release_date)]})
        rule = (
            alt.Chart(rule_df)
            .mark_rule(strokeDash=[6, 4], color=_RELEASE_RULE_COLOR, size=2)
            .encode(
                x="release:T",
                tooltip=[
                    alt.Tooltip("release:T", title="2.6 リリース日", format="%Y-%m-%d")
                ],
            )
        )
        layers = chart + rule
    st.altair_chart(layers.properties(height=320), use_container_width=True)


def _retention_heatmap(matrix: pd.DataFrame, release_date: date | None) -> None:
    """コホートヒートマップ(行=コホート日 / 列=経過日数 / 色=維持率)を描く。"""
    heat = matrix.tail(_HEATMAP_MAX_COHORTS)  # index 昇順なので tail=直近コホート
    exclude_d0 = st.checkbox(
        "Day 0 を除外（色スケールを見やすく）",
        value=True,
        key="_ret_heat_exclude_d0",
        help="Day 0 はほぼ 100% のため、含めると他セルの濃淡差が見えにくくなります。",
    )
    long_df = (
        heat.reset_index()
        .melt(id_vars="cohort_date", var_name="day", value_name="retention")
        .dropna(subset=["retention"])
    )
    if long_df.empty:
        st.info("ヒートマップに表示できるセルがありません。")
        return
    long_df = long_df.copy()
    long_df["day"] = long_df["day"].astype(int)
    if exclude_d0:
        long_df = long_df[long_df["day"] != 0]
        if long_df.empty:
            st.info("Day 0 以外に表示できるセルがありません。")
            return
    long_df["cohort"] = pd.to_datetime(long_df["cohort_date"]).dt.strftime("%Y-%m-%d")
    n_cohorts = int(long_df["cohort"].nunique())
    height = min(700, max(240, 18 * n_cohorts + 80))
    heat_chart = (
        alt.Chart(long_df)
        .mark_rect()
        .encode(
            x=alt.X("day:O", title="経過日数（インストール後）"),
            y=alt.Y("cohort:O", title="コホート日", sort="descending"),
            color=alt.Color(
                "retention:Q",
                title="維持率 (%)",
                scale=alt.Scale(scheme="viridis", domainMin=0),
            ),
            tooltip=[
                alt.Tooltip("cohort:N", title="コホート日"),
                alt.Tooltip("day:O", title="経過日数"),
                alt.Tooltip("retention:Q", title="維持率(%)", format=",.1f"),
            ],
        )
        .properties(height=height)
    )
    layers: alt.LayerChart | alt.Chart = heat_chart
    if release_date is not None:
        # O 軸(コホート文字列)にはドメイン外の値を描けないため、
        # リリース日以降で最初に存在するコホート行に点線を引く。
        marks = sorted(
            c for c in long_df["cohort"].unique() if c >= release_date.isoformat()
        )
        if marks:
            rule = (
                alt.Chart(pd.DataFrame({"cohort": [marks[0]]}))
                .mark_rule(strokeDash=[6, 4], color=_RELEASE_RULE_COLOR, size=2)
                .encode(y="cohort:O")
            )
            layers = heat_chart + rule
    if matrix.shape[0] > _HEATMAP_MAX_COHORTS:
        st.caption(
            f"表示は直近 {_HEATMAP_MAX_COHORTS} コホートまで"
            f"（CSV 全体は {matrix.shape[0]} コホート）。"
        )
    st.altair_chart(layers, use_container_width=True)


def _render_retention_tab() -> None:
    """維持率タブ本体。ASC 維持率 CSV のアップロード → パース → 可視化。"""
    st.caption(
        "App Store Connect の維持率（コホート別リテンション）を可視化します。"
        "2.6 リリース前後で D1/D7/D30 がどう動いたかの効果測定用。"
    )
    uploaded = st.file_uploader(
        "ASC 維持率 CSV（App分析 → エンゲージメント → 維持率 からエクスポート）",
        type=["csv", "tsv", "txt", "gz"],
        key="retention_csv",
    )
    if uploaded is None:
        st.info(
            "維持率 CSV をアップロードすると、D1/D7/D30 の推移と"
            "コホートヒートマップを表示します。\n\n"
            "**エクスポート手順**\n"
            "1. [App Store Connect](https://appstoreconnect.apple.com/) → 対象 App"
            " → **App分析**\n"
            "2. **エンゲージメント → 維持率**（Retention）を開く\n"
            "3. 期間を選び、右上の **エクスポート（CSV）** でダウンロード\n"
            "4. このタブにアップロード\n\n"
            "対応形式: コホート日 × 経過日数（Day 0, Day 1, …）の維持率マトリクス。"
            "列名ゆれ（day7 / D30 / 1日後 等）・% 表記・"
            "［日付, 経過日数, 維持率］の3列形式にも対応。"
        )
        return

    try:
        ret = parse_asc_retention(uploaded.getvalue())
    except Exception as exc:  # noqa: BLE001 — UI 側フォールバック
        st.error(f"維持率 CSV の解析に失敗しました: {exc}")
        return

    matrix: pd.DataFrame = ret["matrix"]
    if matrix.empty:
        st.warning(
            "維持率データを検出できませんでした。コホート日の列（Date / 日付）と"
            "経過日数の列（Day 1, Day 7, … / 1日後, …）を含む CSV か確認してください。"
        )
        return

    release_date = _release_date_input()

    days = [int(d) for d in ret["days"]]
    picks = {
        label: _pick_day_column(days, target)
        for label, target in _RETENTION_METRIC_TARGETS
    }

    # --- ① 直近28日コホート平均の KPI カード ---
    st.markdown(
        f"**直近 {_RETENTION_RECENT_WINDOW_DAYS} 日コホートの平均維持率**"
    )
    cols = st.columns(len(_RETENTION_METRIC_TARGETS))
    for col, (label, target) in zip(cols, _RETENTION_METRIC_TARGETS):
        day = picks[label]
        value = _recent_cohort_mean(matrix, day)
        suffix = "" if (day is None or day == target) else f"（Day {day} で代替）"
        col.metric(f"{label}{suffix}", "—" if value is None else f"{value:.1f}%")
    st.caption(
        "未到来・プライバシー閾値未達の空セルは平均から除外しています。"
        "D7/D30 は経過日数が足りないコホートを含まないため、母数が少なくなります。"
    )

    st.divider()

    # --- ② D1/D7/D30 推移折れ線(コホート日別) ---
    st.subheader("D1 / D7 / D30 維持率の推移（コホート日別）")
    if release_date is not None:
        st.caption(f"黄色の点線 = 2.6 リリース日（{release_date}）")
    _retention_trend_chart(matrix, picks, release_date)

    st.divider()

    # --- ③ コホートヒートマップ(コホート日 × 経過日数) ---
    st.subheader("コホートヒートマップ（コホート日 × 経過日数）")
    _retention_heatmap(matrix, release_date)

    # --- 補足: コホート台数(App Units 列があれば) ---
    units = ret["cohort_units"]
    if isinstance(units, pd.DataFrame) and not units.empty:
        with st.expander("コホート台数（App Units）"):
            st.caption("維持率(%)の母数確認用。CSV に台数列があった場合のみ表示。")
            shown = units.copy()
            shown["cohort_date"] = pd.to_datetime(shown["cohort_date"]).dt.date
            st.dataframe(shown, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def main() -> None:
    # 認証が最優先。通過するまで st.stop() で以降(データ取得・描画・API)を止める。
    require_auth()

    st.title("Synaps ダッシュボード")
    tab_revenue, tab_retention = st.tabs(["収益・DL", "維持率"])
    with tab_revenue:
        _render_revenue_dl_tab()
    with tab_retention:
        _render_retention_tab()


def _render_revenue_dl_tab() -> None:
    """従来の収益・DL 画面(タブ化に伴い関数へ移設。表示内容は無改変)。"""
    st.caption(
        "AdMob 収益と App Store ダウンロードを1画面で。"
        "CSV/TSV をアップロードして表示します(API キー不要)。"
    )

    # --- サイドバー: アップロード ---
    st.sidebar.header("データアップロード")
    admob_file = st.sidebar.file_uploader(
        "AdMob レポート CSV", type=["csv"], key="admob"
    )
    asc_file = st.sidebar.file_uploader(
        "App Store Sales レポート (TSV/CSV/gz)",
        type=["tsv", "csv", "txt", "gz"],
        key="asc",
    )

    # --- サイドバー: 自動取得 (AdMob Reporting API / App Store Connect API) ---
    admob_auto = _admob_autofetch_sidebar()
    asc_auto = _asc_autofetch_sidebar()

    # --- サイドバー下部: ログアウト ---
    _logout_button()

    admob: dict[str, Any] | None = None
    asc: dict[str, Any] | None = None

    if admob_file is not None:
        try:
            admob = parse_admob(admob_file.getvalue())
        except Exception as exc:  # noqa: BLE001 — UI 側フォールバック
            st.error(f"AdMob CSV の解析に失敗しました: {exc}")
    if asc_file is not None:
        try:
            asc = parse_asc_sales(asc_file.getvalue())
        except Exception as exc:  # noqa: BLE001
            st.error(f"App Store Sales の解析に失敗しました: {exc}")

    # 自動取得できていれば AdMob / ASC はそれを優先（CSV は無ければのフォールバック）。
    if admob is None and admob_auto is not None and not admob_auto["daily"].empty:
        admob = admob_auto
    if asc is None and asc_auto is not None and not asc_auto["daily"].empty:
        asc = asc_auto

    if admob is None and asc is None:
        st.info(
            "データがありません。\n\n"
            "・**自動取得**: Secrets 設定済みならサイドバー「App StoreからDL取得」。\n"
            "・**手動**: サイドバーから AdMob CSV / App Store Sales レポートを"
            "アップロード（片方だけでも表示可）。サンプルは `sample_data/` にあります。"
        )
        return

    # --- 期間フィルタ ---
    dmin, dmax = _collect_date_range(admob, asc)
    if dmin and dmax:
        start, end = _period_selector(dmin, dmax)
    else:
        start, end = date.today(), date.today()

    if admob is not None:
        admob = _apply_period(admob, start, end)
    if asc is not None:
        asc = _apply_period(asc, start, end)

    # --- 対象期間・遅延の注記 ---
    st.info(
        f"対象期間: {start} 〜 {end}　｜　"
        "注記: AdMob / App Store Connect のデータは日次で遅延・確定します"
        "（当日分は未確定の場合があります）。"
    )

    # --- KPI ---
    st.subheader("KPI")
    if admob is not None:
        st.markdown(f"**広告収益 (AdMob) — 通貨: {admob['currency']}**")
        _render_admob_kpis(admob)
    if asc is not None:
        st.markdown("**ダウンロード (App Store Connect)**")
        _render_asc_kpis(asc)

    st.divider()

    # --- 推移グラフ ---
    st.subheader("推移")
    if admob is not None and not admob["daily"].empty:
        cur = admob["currency"]
        sym = _currency_symbol(cur)
        money_fmt = _money_axis_format(cur)
        g1, g2 = st.columns(2)
        with g1:
            st.markdown("収益推移")
            _line(
                admob["daily"],
                "earnings",
                "収益",
                y_title=f"収益 ({sym})",
                y_format=money_fmt,
            )
        with g2:
            st.markdown("eCPM 推移")
            _line(
                admob["daily"],
                "ecpm",
                "eCPM",
                y_title=f"eCPM ({sym})",
                y_format=money_fmt,
            )
        st.markdown("表示回数推移")
        _line(
            admob["daily"],
            "impressions",
            "表示回数",
            y_title="表示回数 (回)",
            y_format=",.0f",
        )
    if asc is not None and not asc["daily"].empty:
        st.markdown("DL 推移")
        _line(
            asc["daily"],
            "downloads",
            "DL数",
            y_title="DL数 (件)",
            y_format=",.0f",
        )

    st.divider()

    # --- 国別 Top10 ---
    st.subheader("国別 Top10")
    if asc is not None and not asc["country"].empty:
        st.markdown("**DL 国別 Top10**")
        _country_top10(
            asc["country"],
            "downloads",
            "DL",
            x_title="DL数 (件)",
            x_format=",.0f",
        )
    if admob is not None and not admob["country"].empty:
        st.markdown("**収益 国別 Top10**")
        cur = admob["currency"]
        _country_top10(
            admob["country"],
            "earnings",
            "収益",
            x_title=f"収益 ({_currency_symbol(cur)})",
            x_format=_money_axis_format(cur),
        )


if __name__ == "__main__":
    main()
