"""パーサ層: AdMob レポート CSV と App Store Connect Sales レポート(TSV/CSV)を
表記ゆれに寛容に読み取り、集計しやすい正規化 DataFrame に変換する。

このモジュールは Streamlit に依存しない純粋なデータ処理層なので、
CLI からも単体でテストできる（`python parsers.py --selftest` 参照）。
"""

from __future__ import annotations

import gzip
import io
import re
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------


def _normalize_key(name: str) -> str:
    """列名を比較用に正規化する(小文字化・記号除去)。

    "Estimated earnings (USD)" -> "estimatedearningsusd"
    "ESTIMATED_EARNINGS"        -> "estimatedearnings"
    のように、表記ゆれを吸収して同一視できるようにする。
    """
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """候補キー(正規化済み)のいずれかに部分一致する列名を返す。無ければ None。

    candidates は正規化済みの部分文字列リスト。列名を正規化した文字列に
    候補が含まれるか、または候補が列名を含むかで判定する(寛容マッチ)。
    """
    norm_map = {_normalize_key(c): c for c in df.columns}
    # 1) 完全一致を最優先
    for cand in candidates:
        if cand in norm_map:
            return norm_map[cand]
    # 2) 部分一致(列名側に候補が含まれる)
    for cand in candidates:
        for norm, original in norm_map.items():
            if cand and cand in norm:
                return original
    return None


def _to_numeric(series: pd.Series) -> pd.Series:
    """通貨記号・カンマ・空白を除去して数値化する。変換不能は 0。"""
    cleaned = (
        series.astype(str)
        .str.replace(r"[,$¥€£\s]", "", regex=True)
        .str.replace(r"^\((.*)\)$", r"-\1", regex=True)  # (123) -> -123
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0.0)


def _read_bytes(raw: bytes) -> bytes:
    """gzip 圧縮されていれば解凍して返す。そうでなければそのまま返す。

    App Store Connect の Sales レポートは .txt.gz で配布されるため、
    解凍前後どちらのバイト列でも受け付けられるようにする。
    """
    if raw[:2] == b"\x1f\x8b":  # gzip magic number
        return gzip.decompress(raw)
    return raw


def _decode(raw: bytes) -> str:
    """UTF-8 → UTF-8-SIG → latin-1 の順でデコードを試みる。"""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_and_read(text: str) -> pd.DataFrame:
    """区切り文字(タブ / カンマ)を自動判定して DataFrame を返す。

    App Store Connect はタブ区切り、AdMob はカンマ区切りが基本だが、
    どちらで来ても読めるように先頭行のタブ有無で判定する。
    """
    first_line = text.split("\n", 1)[0]
    sep = "\t" if first_line.count("\t") >= first_line.count(",") else ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False)


def load_table(raw: bytes) -> pd.DataFrame:
    """アップロードされた生バイト列を DataFrame にする(gzip/区切り自動判定)。"""
    return _sniff_and_read(_decode(_read_bytes(raw)))


def _parse_dates(series: pd.Series) -> pd.Series:
    """複数の日付表記を吸収して datetime に変換する。

    AdMob: "2026-06-01" / "20260601" など
    ASC:   "06/01/2026" (MM/DD/YYYY) など
    """
    s = series.astype(str).str.strip()
    # まず一般的なパース(ISO等)
    parsed = pd.to_datetime(s, errors="coerce")
    # MM/DD/YYYY 形式(ASC)が残っていれば再挑戦
    mask = parsed.isna()
    if mask.any():
        alt = pd.to_datetime(s[mask], format="%m/%d/%Y", errors="coerce")
        parsed.loc[mask] = alt
    # YYYYMMDD 形式が残っていれば再挑戦
    mask = parsed.isna()
    if mask.any():
        alt = pd.to_datetime(s[mask], format="%Y%m%d", errors="coerce")
        parsed.loc[mask] = alt
    return parsed


# ---------------------------------------------------------------------------
# AdMob パーサ
# ---------------------------------------------------------------------------

# 正規化済みの列候補(表記ゆれ吸収)
_ADMOB_DATE = ["date", "day"]
_ADMOB_COUNTRY = ["countrycode", "country"]
_ADMOB_EARNINGS = [
    "estimatedearnings",
    "estimatedearningsusd",
    "earnings",
    "revenue",
]
_ADMOB_IMPRESSIONS = ["impressions", "adimpressions", "matchedrequests"]
_ADMOB_CLICKS = ["clicks"]
_ADMOB_ECPM = ["observedecpm", "ecpm", "estimatedecpm"]
_ADMOB_CURRENCY = ["currency", "currencycode"]


def parse_admob(raw: bytes) -> dict[str, Any]:
    """AdMob レポート CSV をパースして正規化する。

    戻り値:
        {
          "daily":   DataFrame[date, earnings, impressions, clicks, ecpm],
          "country": DataFrame[country, earnings, impressions],
          "currency": str,
          "columns": {検出した元列名のマップ},
        }

    eCPM は 収益 / 表示回数 * 1000 で再計算する(元データに列があっても
    集計後の整合性を優先して算出する)。
    """
    df = load_table(raw)
    if df.empty:
        return _empty_admob()

    col_date = _find_column(df, _ADMOB_DATE)
    col_country = _find_column(df, _ADMOB_COUNTRY)
    col_earn = _find_column(df, _ADMOB_EARNINGS)
    col_impr = _find_column(df, _ADMOB_IMPRESSIONS)
    col_click = _find_column(df, _ADMOB_CLICKS)
    col_curr = _find_column(df, _ADMOB_CURRENCY)

    work = pd.DataFrame()
    work["date"] = _parse_dates(df[col_date]) if col_date else pd.NaT
    work["country"] = df[col_country].astype(str) if col_country else "N/A"
    work["earnings"] = _to_numeric(df[col_earn]) if col_earn else 0.0
    work["impressions"] = _to_numeric(df[col_impr]) if col_impr else 0.0
    work["clicks"] = _to_numeric(df[col_click]) if col_click else 0.0

    # 通貨コード: 列があれば最頻値、無ければ列名から USD を推測
    currency = "USD"
    if col_curr and not df[col_curr].empty:
        vals = df[col_curr].astype(str).str.strip()
        vals = vals[vals != ""]
        if not vals.empty:
            currency = vals.mode().iat[0]
    elif col_earn and "usd" in _normalize_key(col_earn):
        currency = "USD"

    # 日次集計
    daily = (
        work.dropna(subset=["date"])
        .groupby("date", as_index=False)[["earnings", "impressions", "clicks"]]
        .sum()
        .sort_values("date")
    )
    daily["ecpm"] = _safe_ecpm(daily["earnings"], daily["impressions"])

    # 国別集計
    country = (
        work.groupby("country", as_index=False)[["earnings", "impressions", "clicks"]]
        .sum()
        .sort_values("earnings", ascending=False)
    )

    return {
        "daily": daily.reset_index(drop=True),
        "country": country.reset_index(drop=True),
        "currency": currency,
        "columns": {
            "date": col_date,
            "country": col_country,
            "earnings": col_earn,
            "impressions": col_impr,
            "clicks": col_click,
        },
    }


def _safe_ecpm(earnings: pd.Series, impressions: pd.Series) -> pd.Series:
    """eCPM = 収益 / 表示回数 * 1000。表示回数 0 の行は 0 とする。"""
    impr = impressions.replace(0, pd.NA)
    ecpm = (earnings / impr * 1000).fillna(0.0)
    return ecpm.round(4)


def _empty_admob() -> dict[str, Any]:
    return {
        "daily": pd.DataFrame(
            columns=["date", "earnings", "impressions", "clicks", "ecpm"]
        ),
        "country": pd.DataFrame(columns=["country", "earnings", "impressions", "clicks"]),
        "currency": "USD",
        "columns": {},
    }


# ---------------------------------------------------------------------------
# App Store Connect Sales パーサ
# ---------------------------------------------------------------------------

_ASC_DATE = ["begindate", "date"]
_ASC_UNITS = ["units"]
_ASC_COUNTRY = ["countrycode", "country"]
_ASC_PRODUCT_TYPE = ["producttypeidentifier", "producttype"]
_ASC_CURRENCY = ["customercurrency", "currency"]
_ASC_PROCEEDS = ["developerproceeds", "proceeds"]

# ダウンロードとしてカウントする Product Type Identifier(無料アプリの初回DL確定コードのみ)。
#   1F = iPhone/iPod の無料アプリ新規DL
#   1T = ユニバーサル(iPad+iPhone)の無料アプリ新規DL
#   F1 = iPad の無料アプリ新規DL(旧表記)
#   FF = iPad の無料アプリ新規DL(現行表記)
# 初回DLのみ。更新(7*)/IAP・サブスク(IA*)/教育・カスタム配布(1E* 等)/
# bare "1"(有料DLと曖昧)は **除外** する。
# ※実レポートで別の初回DLコードが観測されたらここに追記すること。
_DOWNLOAD_PRODUCT_TYPES = {
    "1f",
    "1t",
    "f1",
    "ff",
}


def _is_download_type(value: str) -> bool:
    """Product Type Identifier がアプリ初回DLを表すか判定する。

    表記ゆれ(大文字小文字・前後空白)を吸収。更新(7*)・アプリ内課金/サブスク
    (IA*)・bare "1"・教育/カスタム配布(1E*)等は DL に含めない。
    確定した初回DLコード(_DOWNLOAD_PRODUCT_TYPES)のみ True を返す。
    """
    key = _normalize_key(value)
    if not key:
        return False
    return key in _DOWNLOAD_PRODUCT_TYPES


def parse_asc_sales(raw: bytes) -> dict[str, Any]:
    """App Store Connect Sales レポート(TSV/CSV, gzip可)をパースする。

    戻り値:
        {
          "daily":   DataFrame[date, downloads],
          "country": DataFrame[country, downloads],
          "currency": str,
          "total_downloads": int,
          "columns": {検出した元列名のマップ},
        }

    Product Type Identifier が初回DL系のみを集計する(アプリ内課金は除外)。
    """
    df = load_table(raw)
    if df.empty:
        return _empty_asc()

    col_date = _find_column(df, _ASC_DATE)
    col_units = _find_column(df, _ASC_UNITS)
    col_country = _find_column(df, _ASC_COUNTRY)
    col_ptype = _find_column(df, _ASC_PRODUCT_TYPE)
    col_curr = _find_column(df, _ASC_CURRENCY)

    work = pd.DataFrame()
    work["date"] = _parse_dates(df[col_date]) if col_date else pd.NaT
    work["units"] = _to_numeric(df[col_units]) if col_units else 0.0
    work["country"] = df[col_country].astype(str).str.strip() if col_country else "N/A"
    # 透明性のため元 Product Type も保持(内訳表示・監査用)。
    work["product_type"] = (
        df[col_ptype].astype(str).str.strip() if col_ptype else ""
    )

    # Product Type でDL行を抽出。列が無ければ全行をDL扱い(寛容)。
    if col_ptype:
        mask = df[col_ptype].apply(_is_download_type)
        work = work[mask.to_numpy()]

    currency = "USD"
    if col_curr and not df[col_curr].empty:
        vals = df[col_curr].astype(str).str.strip()
        vals = vals[vals != ""]
        if not vals.empty:
            currency = vals.mode().iat[0]

    daily = (
        work.dropna(subset=["date"])
        .groupby("date", as_index=False)["units"]
        .sum()
        .sort_values("date")
        .rename(columns={"units": "downloads"})
    )

    # 国別集計: 国コードが空/欠損(N/A)の行は除外する(Top10 に N/A を出さない)。
    # 総DL(total_downloads)には含めるため、集計元は work 全体のままにする。
    _blank_country = {"", "nan", "none", "n/a", "na"}
    country_work = work[
        ~work["country"].astype(str).str.strip().str.lower().isin(_blank_country)
    ]
    country = (
        country_work.groupby("country", as_index=False)["units"]
        .sum()
        .sort_values("units", ascending=False)
        .rename(columns={"units": "downloads"})
    )

    # Product Type 別内訳(初回DL確定コードごとの Units 合計)。app 側で任意表示。
    by_product_type = (
        work.groupby("product_type", as_index=False)["units"]
        .sum()
        .sort_values("units", ascending=False)
        .rename(columns={"units": "downloads"})
        .reset_index(drop=True)
    )

    total = int(work["units"].sum())

    return {
        "daily": daily.reset_index(drop=True),
        "country": country.reset_index(drop=True),
        "by_product_type": by_product_type,
        "currency": currency,
        "total_downloads": total,
        "columns": {
            "date": col_date,
            "units": col_units,
            "country": col_country,
            "product_type": col_ptype,
        },
    }


def _empty_asc() -> dict[str, Any]:
    return {
        "daily": pd.DataFrame(columns=["date", "downloads"]),
        "country": pd.DataFrame(columns=["country", "downloads"]),
        "by_product_type": pd.DataFrame(columns=["product_type", "downloads"]),
        "currency": "USD",
        "total_downloads": 0,
        "columns": {},
    }


# ---------------------------------------------------------------------------
# 自己テスト用 CLI (sample_data を通して集計値が出るか確認する)
# ---------------------------------------------------------------------------


def _selftest() -> int:
    """sample_data のサンプルを読み込み、集計値を標準出力に出す。

    streamlit run では動作確認しづらいので、パーサ層をこの CLI で検証する。
    """
    import pathlib

    base = pathlib.Path(__file__).parent / "sample_data"
    admob_path = base / "admob_sample.csv"
    asc_path = base / "asc_sales_sample.tsv"

    print("=" * 60)
    print("PARSER SELF-TEST")
    print("=" * 60)

    ok = True

    # --- AdMob ---
    if admob_path.exists():
        result = parse_admob(admob_path.read_bytes())
        daily = result["daily"]
        country = result["country"]
        total_earn = float(daily["earnings"].sum())
        total_impr = float(daily["impressions"].sum())
        overall_ecpm = (total_earn / total_impr * 1000) if total_impr else 0.0
        print("\n[AdMob]")
        print(f"  検出列: {result['columns']}")
        print(f"  通貨: {result['currency']}")
        print(f"  日次行数: {len(daily)}")
        print(f"  総収益: {total_earn:.2f} {result['currency']}")
        print(f"  総表示回数: {int(total_impr):,}")
        print(f"  全体eCPM: {overall_ecpm:.4f} {result['currency']}")
        print(f"  直近日eCPM: {float(daily['ecpm'].iloc[-1]):.4f}")
        print(f"  国別Top3:\n{country.head(3).to_string(index=False)}")
        assert total_earn > 0, "AdMob 総収益が 0"
        assert total_impr > 0, "AdMob 総表示回数が 0"
        assert overall_ecpm > 0, "AdMob eCPM が 0"
    else:
        print(f"\n[AdMob] サンプル未検出: {admob_path}")
        ok = False

    # --- ASC Sales ---
    if asc_path.exists():
        result = parse_asc_sales(asc_path.read_bytes())
        daily = result["daily"]
        country = result["country"]
        total_dl = result["total_downloads"]
        print("\n[App Store Connect Sales]")
        print(f"  検出列: {result['columns']}")
        print(f"  通貨: {result['currency']}")
        print(f"  日次行数: {len(daily)}")
        print(f"  累計DL(初回DLのみ): {total_dl:,}")
        print(f"  日次DL合計: {int(daily['downloads'].sum()):,}")
        print(f"  国別Top3:\n{country.head(3).to_string(index=False)}")
        assert total_dl > 0, "ASC 累計DLが 0"
        assert int(daily["downloads"].sum()) == total_dl, "日次合計と累計が不一致"
    else:
        print(f"\n[ASC] サンプル未検出: {asc_path}")
        ok = False

    # --- (b) Product Type フィルタ: 初回DLのみ集計・更新/課金/bare"1"は除外 ---
    print("\n[b] Product Type フィルタ検証 (合成TSV)")
    synth_pt = (
        "Product Type Identifier\tUnits\tBegin Date\tCountry Code\n"
        "1F\t100\t06/25/2026\tJP\n"   # 初回DL(iPhone) -> 数える
        "7F\t40\t06/25/2026\tJP\n"    # 更新 -> 除外
        "1\t30\t06/25/2026\tUS\n"     # bare "1"(曖昧) -> 除外
        "IA1\t9\t06/25/2026\tJP\n"    # アプリ内課金 -> 除外
        "1T\t5\t06/25/2026\tUS\n"     # ユニバーサル初回DL -> 数える
    )
    pt = parse_asc_sales(synth_pt.encode("utf-8"))
    pt_total = pt["total_downloads"]
    print(f"  総DL: {pt_total} (期待 105 = 1F:100 + 1T:5, 7F/1/IA1 除外)")
    assert pt_total == 105, f"Product Type フィルタ不正: {pt_total} != 105"
    # 内訳に更新/課金/bare が現れないこと
    seen_types = set(pt["by_product_type"]["product_type"].astype(str))
    print(f"  集計に残った Product Type: {sorted(seen_types)}")
    assert "7F" not in seen_types, "更新(7F)が集計に混入"
    assert "IA1" not in seen_types, "アプリ内課金(IA1)が集計に混入"
    assert "1" not in seen_types, 'bare "1" が集計に混入'
    assert seen_types <= {"1F", "1T"}, f"想定外のコードが残存: {seen_types}"
    print("  -> 初回DLのみ集計 / 更新・課金・bare1 除外 OK")

    # --- (e) 国コード空/N-A行は国別集計から除外(総DLには含む) ---
    print("\n[e] 国コード空行の国別集計除外")
    synth_c = (
        "Product Type Identifier\tUnits\tBegin Date\tCountry Code\n"
        "1F\t100\t06/25/2026\tJP\n"
        "1F\t20\t06/25/2026\t\n"      # 国コード空 -> 国別から除外
        "1F\t15\t06/25/2026\tN/A\n"  # N/A -> 国別から除外
    )
    cc = parse_asc_sales(synth_c.encode("utf-8"))
    countries = set(cc["country"]["country"].astype(str))
    print(f"  国別に出た国: {sorted(countries)} (期待 JP のみ)")
    assert countries == {"JP"}, f"空/N-A 国コードが国別集計に混入: {countries}"
    assert cc["total_downloads"] == 135, (
        f"総DLには空国も含める想定: {cc['total_downloads']} != 135"
    )
    print("  -> 空/N-A 国は国別Top10から除外・総DLには含む OK")

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS ✅" if ok else "MISSING SAMPLES ❌")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("Usage: python parsers.py --selftest")
