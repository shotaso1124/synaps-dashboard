"""パーサ層: AdMob レポート CSV / App Store Connect Sales レポート(TSV/CSV) /
App Store Connect 維持率エクスポート CSV を表記ゆれに寛容に読み取り、
集計しやすい正規化 DataFrame に変換する。

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
# App Store Connect 維持率(Retention)パーサ
# ---------------------------------------------------------------------------
#
# ASC「App分析 → エンゲージメント → 維持率」からエクスポートした CSV を想定。
# 一般的な形式は「行 = コホート日(インストール日) / 列 = Day 0, Day 1, … の
# 維持率マトリクス(ワイド形式)」。ツールによっては
# [日付, 経過日数, 維持率] の3列(ロング形式)で来ることもあるため両対応する。

_RETENTION_DATE_KEYS = [
    "cohortdate",
    "installdate",
    "begindate",
    "startdate",
    "date",
    "cohort",
]
_RETENTION_DATE_JP = ("日付", "コホート")
_RETENTION_UNITS_KEYS = [
    "appunits",
    "cohortsize",
    "totaldevices",
    "installations",
    "installs",
    "devices",
    "units",
]
_RETENTION_UNITS_JP = ("ユニット", "インストール", "デバイス", "台数")
_RETENTION_LONG_DAY_KEYS = [
    "daysafterinstall",
    "dayssinceinstall",
    "daysafterdownload",
    "dayoffset",
    "daynumber",
    "days",
    "day",
]
_RETENTION_LONG_DAY_JP = ("経過日",)
_RETENTION_LONG_VALUE_KEYS = ["retentionrate", "retention", "rate"]
_RETENTION_LONG_VALUE_JP = ("維持率", "リテンション", "継続率")

# 経過日数として妥当な上限(「2026」等の年らしき列名の誤検出防止)。
_DAY_OFFSET_MAX = 366


def _find_column_jp(df: pd.DataFrame, needles: tuple[str, ...]) -> str | None:
    """日本語の部分文字列で列名を探す。

    _normalize_key は非 ASCII を落とすため、日本語ヘッダ("日付" 等)は
    正規化ベースの _find_column では見つけられない。元列名で補完する。
    """
    for c in df.columns:
        name = str(c)
        if any(n in name for n in needles):
            return c
    return None


def _day_offset_from_header(name: str) -> int | None:
    """列名から「インストール後の経過日数」を取り出す。該当しなければ None。

    対応例: "Day 1" / "day7" / "D30" / "Day 28 Retention" / "7 Days" /
            "1日後" / "7日目" など。
    """
    s = str(name).strip()
    # 日本語表記("1日後" "7日目" など)は正規化で数字以外が消えるため元列名で判定。
    m = re.search(r"(\d+)\s*日", s)
    if m:
        n = int(m.group(1))
        return n if 0 <= n <= _DAY_OFFSET_MAX else None
    key = _normalize_key(s)
    if not key:
        return None
    for pattern in (
        r"^day0*(\d+)(?:retention|rate)?$",   # day1 / day30retention
        r"^d0*(\d+)$",                         # d7
        r"^0*(\d+)days?(?:retention|rate)?$",  # 7days
        r"^0*(\d+)$",                          # 正規化で数字のみ残った場合
    ):
        m = re.match(pattern, key)
        if m:
            n = int(m.group(1))
            if 0 <= n <= _DAY_OFFSET_MAX:
                return n
    return None


def _to_percent_series(series: pd.Series) -> pd.Series:
    """維持率セルを % 数値(0〜100 想定)に変換する。空セルは NaN のまま保持。

    _to_numeric と違い 0 埋めしない。未到来コホート・プライバシー閾値未達の
    空セルを 0% と誤認させると、平均・グラフが大きく歪むため。
    """
    s = series.astype(str).str.strip()
    s = s.str.replace("%", "", regex=False).str.replace(",", "", regex=False)
    blank = {"", "-", "–", "—", "nan", "none", "null", "n/a", "na"}
    s = s.mask(s.str.lower().isin(blank))
    return pd.to_numeric(s, errors="coerce")


def parse_asc_retention(raw: bytes) -> dict[str, Any]:
    """App Store Connect の維持率エクスポート CSV/TSV(gzip可)をパースする。

    対応形式(寛容):
      A) ワイド形式: 行 = コホート日 / 列 = "Day 0", "Day 1", … の維持率。
         列名ゆれ("day7" "D30" "1日後" 等)・%文字・空セルに対応。
      B) ロング形式: [日付, 経過日数, 維持率] の3列型。

    戻り値:
        {
          "matrix": DataFrame(index=コホート日, columns=経過日数int,
                              値=維持率%(0〜100) or NaN),
          "long":   DataFrame[cohort_date, day, retention](NaNセルは除外済み),
          "days":   検出した経過日数の昇順リスト,
          "cohort_units": DataFrame[cohort_date, units](App Units 列があれば),
          "columns": {検出した元列名のマップ},
        }

    維持率が 0〜1 の比率スケールで来た場合は 100 倍して % に揃える。
    空セルは NaN のまま保持する(0% と区別するため)。
    """
    df = load_table(raw)
    if df.empty:
        return _empty_retention()

    # --- コホート日列の検出 ---
    col_date = _find_column(df, _RETENTION_DATE_KEYS) or _find_column_jp(
        df, _RETENTION_DATE_JP
    )
    if col_date is None:
        # 最後の手段: 値の8割以上が日付としてパースできる列を日付列とみなす。
        for c in df.columns:
            parsed = _parse_dates(df[c])
            if len(parsed) > 0 and parsed.notna().mean() >= 0.8:
                col_date = c
                break
    if col_date is None:
        return _empty_retention()

    # --- ワイド形式: Day N 列の検出 ---
    day_cols: dict[str, int] = {}
    for c in df.columns:
        if c == col_date:
            continue
        off = _day_offset_from_header(c)
        if off is not None:
            day_cols[c] = off

    col_units: str | None = None
    col_day_long: str | None = None
    col_val_long: str | None = None

    if day_cols:
        fmt = "wide"
        matrix = pd.DataFrame(index=_parse_dates(df[col_date]))
        for c, off in sorted(day_cols.items(), key=lambda kv: kv[1]):
            matrix[off] = _to_percent_series(df[c]).to_numpy()
        matrix = matrix[matrix.index.notna()]
        matrix.index.name = "cohort_date"
        # 同一コホート日の複数行(フィルタ別エクスポート等)は平均に畳む。
        matrix = matrix.groupby(level=0).mean()

        # App Units(コホート台数)列: 日付・Day 列以外から探す。
        rest_cols = [c for c in df.columns if c != col_date and c not in day_cols]
        if rest_cols:
            rest = df[rest_cols]
            col_units = _find_column(rest, _RETENTION_UNITS_KEYS) or _find_column_jp(
                rest, _RETENTION_UNITS_JP
            )
    else:
        fmt = "long"
        col_day_long = _find_column(df, _RETENTION_LONG_DAY_KEYS) or _find_column_jp(
            df, _RETENTION_LONG_DAY_JP
        )
        col_val_long = _find_column(df, _RETENTION_LONG_VALUE_KEYS) or _find_column_jp(
            df, _RETENTION_LONG_VALUE_JP
        )
        if not (col_day_long and col_val_long):
            return _empty_retention()
        work = pd.DataFrame(
            {
                "cohort_date": _parse_dates(df[col_date]),
                "day": pd.to_numeric(
                    df[col_day_long].astype(str).str.extract(r"(\d+)", expand=False),
                    errors="coerce",
                ),
                "retention": _to_percent_series(df[col_val_long]),
            }
        ).dropna(subset=["cohort_date", "day"])
        if work.empty:
            return _empty_retention()
        work["day"] = work["day"].astype(int)
        matrix = work.pivot_table(
            index="cohort_date", columns="day", values="retention", aggfunc="mean"
        )
        matrix.index.name = "cohort_date"

    if matrix.empty or matrix.dropna(how="all").empty:
        return _empty_retention()

    # 経過日数を int に揃えて昇順に整列。
    matrix.columns = [int(c) for c in matrix.columns]
    matrix = matrix.reindex(sorted(matrix.columns), axis=1)

    # 0〜1 の比率スケールで来ていたら % に揃える(0.40 -> 40.0)。
    # ※Day 0(=100%) を含むエクスポートなら max>1 になるため誤変換しない。
    max_val = matrix.max().max()
    if pd.notna(max_val) and 0 < float(max_val) <= 1.0:
        matrix = matrix * 100.0

    # 折れ線・ヒートマップ用のロング形式(NaN セルは落とす)。
    long_df = (
        matrix.reset_index()
        .melt(id_vars="cohort_date", var_name="day", value_name="retention")
        .dropna(subset=["retention"])
    )
    long_df["day"] = long_df["day"].astype(int)
    long_df = long_df.sort_values(["cohort_date", "day"]).reset_index(drop=True)

    # コホート台数(あれば)。
    if col_units:
        units_df = pd.DataFrame(
            {
                "cohort_date": _parse_dates(df[col_date]),
                "units": _to_numeric(df[col_units]),
            }
        ).dropna(subset=["cohort_date"])
        units_df = units_df.groupby("cohort_date", as_index=False)["units"].sum()
    else:
        units_df = pd.DataFrame(columns=["cohort_date", "units"])

    return {
        "matrix": matrix,
        "long": long_df,
        "days": [int(c) for c in matrix.columns],
        "cohort_units": units_df,
        "columns": {
            "date": col_date,
            "units": col_units,
            "format": fmt,
            "day_columns": {str(k): v for k, v in day_cols.items()},
            "long_day": col_day_long,
            "long_value": col_val_long,
        },
    }


def _empty_retention() -> dict[str, Any]:
    return {
        "matrix": pd.DataFrame(),
        "long": pd.DataFrame(columns=["cohort_date", "day", "retention"]),
        "days": [],
        "cohort_units": pd.DataFrame(columns=["cohort_date", "units"]),
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

    # --- (r1) 維持率: ワイド形式(%文字・空セル=NaN 保持・D1/D7/D30 抽出) ---
    print("\n[r1] 維持率パーサ: ワイド形式 (合成CSV)")
    synth_ret = (
        "Date,App Units,Day 0,Day 1,Day 7,Day 14,Day 30\n"
        "2026-06-01,120,100%,40.0%,20.0%,12.5%,8.0%\n"
        "2026-06-02,80,100%,35.0%,15.0%,10.0%,\n"    # D30 未到来 -> NaN
        "2026-06-03,100,100%,45.0%,,,\n"             # D7 以降未到来 -> NaN
    )
    r1 = parse_asc_retention(synth_ret.encode("utf-8"))
    m1 = r1["matrix"]
    print(f"  検出列: format={r1['columns']['format']} days={r1['days']}")
    print(f"  matrix 形状: {m1.shape} (期待 (3, 5))")
    assert r1["days"] == [0, 1, 7, 14, 30], f"経過日検出不正: {r1['days']}"
    assert m1.shape == (3, 5), f"matrix 形状不正: {m1.shape}"
    d1_0601 = float(m1.loc[pd.Timestamp("2026-06-01"), 1])
    d7_0602 = float(m1.loc[pd.Timestamp("2026-06-02"), 7])
    d30_0601 = float(m1.loc[pd.Timestamp("2026-06-01"), 30])
    print(f"  D1(06/01)={d1_0601} D7(06/02)={d7_0602} D30(06/01)={d30_0601}")
    assert d1_0601 == 40.0, f"D1 抽出不正: {d1_0601}"
    assert d7_0602 == 15.0, f"D7 抽出不正: {d7_0602}"
    assert d30_0601 == 8.0, f"D30 抽出不正: {d30_0601}"
    # 空セルは 0 でなく NaN(未到来を 0% と誤認しない)
    assert pd.isna(m1.loc[pd.Timestamp("2026-06-02"), 30]), "未到来セルが NaN でない"
    assert pd.isna(m1.loc[pd.Timestamp("2026-06-03"), 7]), "未到来セルが NaN でない"
    # ヒートマップ用ロング整形: 有効セルのみ(5+4+2=11)・NaN 行なし
    l1 = r1["long"]
    print(f"  long 行数: {len(l1)} (期待 11 = 有効セルのみ)")
    assert len(l1) == 11, f"long 行数不正: {len(l1)}"
    assert not l1["retention"].isna().any(), "long に NaN が混入"
    assert set(l1.columns) == {"cohort_date", "day", "retention"}, "long 列名不正"
    # App Units 検出
    u1 = r1["cohort_units"]
    assert int(u1["units"].sum()) == 300, f"App Units 合計不正: {u1['units'].sum()}"
    print("  -> ワイド形式 / % 除去 / NaN 保持 / long 整形 / App Units OK")

    # --- (r2) 維持率: 日本語ヘッダ + 比率(0〜1)スケールの自動補正 ---
    print("\n[r2] 維持率パーサ: 日本語ヘッダ / 比率スケール")
    synth_jp = (
        "日付,Appユニット,0日後,1日後,7日後\n"
        "2026/06/01,50,100%,30%,10%\n"
        "2026/06/02,60,100%,25%,8%\n"
    )
    r2 = parse_asc_retention(synth_jp.encode("utf-8"))
    assert r2["days"] == [0, 1, 7], f"日本語 Day 列検出不正: {r2['days']}"
    jp_d1 = float(r2["matrix"].loc[pd.Timestamp("2026-06-01"), 1])
    assert jp_d1 == 30.0, f"日本語形式 D1 不正: {jp_d1}"
    assert int(r2["cohort_units"]["units"].sum()) == 110, "日本語 App Units 不正"
    print(f"  日本語ヘッダ: days={r2['days']} D1(06/01)={jp_d1} OK")
    synth_ratio = "Date,Day 1,Day 7\n2026-06-01,0.40,0.20\n2026-06-02,0.35,0.15\n"
    r2b = parse_asc_retention(synth_ratio.encode("utf-8"))
    ratio_d1 = float(r2b["matrix"].loc[pd.Timestamp("2026-06-01"), 1])
    assert ratio_d1 == 40.0, f"比率スケール補正不正: {ratio_d1} != 40.0"
    print(f"  比率スケール 0.40 -> {ratio_d1}% 補正 OK")

    # --- (r3) 維持率: ロング形式([日付, 経過日数, 維持率]) ---
    print("\n[r3] 維持率パーサ: ロング形式")
    synth_long = (
        "Date,Days After Install,Retention\n"
        "2026-06-01,1,40%\n"
        "2026-06-01,7,20%\n"
        "2026-06-02,1,35%\n"
    )
    r3 = parse_asc_retention(synth_long.encode("utf-8"))
    assert r3["columns"]["format"] == "long", "ロング形式が検出されない"
    assert r3["days"] == [1, 7], f"ロング形式 days 不正: {r3['days']}"
    long_d7 = float(r3["matrix"].loc[pd.Timestamp("2026-06-01"), 7])
    assert long_d7 == 20.0, f"ロング形式 D7 不正: {long_d7}"
    assert pd.isna(r3["matrix"].loc[pd.Timestamp("2026-06-02"), 7]), (
        "ロング形式の欠損セルが NaN でない"
    )
    print(f"  ロング形式: days={r3['days']} D7(06/01)={long_d7} OK")

    # --- (r4) 維持率: 解析不能データは空を返す(例外にしない) ---
    print("\n[r4] 維持率パーサ: 解析不能フォールバック")
    r4 = parse_asc_retention(b"foo,bar\n1,2\n")
    assert r4["matrix"].empty, "解析不能なのに matrix が非空"
    assert r4["days"] == [], "解析不能なのに days が非空"
    print("  -> 不明形式は空戻り(UI 側で案内表示) OK")

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS ✅" if ok else "MISSING SAMPLES ❌")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("Usage: python parsers.py --selftest")
