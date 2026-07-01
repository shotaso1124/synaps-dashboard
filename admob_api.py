"""AdMob Reporting API から広告収益を自動取得する層。

代表が OAuth2 の client_id / client_secret / refresh_token を Secrets に投入すれば、
CSV アップロード無しで **広告収益・表示回数・国別・eCPM** が自動で入る。

- 認証: refresh_token から都度アクセストークンを更新する
  (OAuth2 grant_type=refresh_token / サービスアカウント非対応)。
- 取得: POST /v1/accounts/{account}/networkReport:generate
  (reportSpec: dateRange / dimensions[DATE,COUNTRY] / metrics[...])。
- ★収益は micros(実額×1,000,000)で返る → 1,000,000 で割って通貨額へ。
- eCPM は AdMob メトリクスに無いため 収益 / 表示回数 × 1000 で自算出。
- 集計結果は parsers.parse_admob と同形にまとめ、既存の可視化に流し込む。

Streamlit に依存しない純粋なロジック層(Secrets 取得のみ st.secrets を任意利用)。
実 API を叩かないモック検証は `python admob_api.py --selftest` を参照。

初回の refresh_token 取得は `python admob_auth.py`(1回だけ代表が実行)で行う。
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from parsers import _empty_admob, _safe_ecpm

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# OAuth2 トークンエンドポイント(refresh_token → access_token)。
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
# AdMob Reporting API のスコープ(読み取り専用)。
ADMOB_SCOPE = "https://www.googleapis.com/auth/admob.readonly"
# networkReport:generate のエンドポイント基底。{account} を差し込む。
ADMOB_BASE_URL = "https://admob.googleapis.com/v1"
# Publisher ID のデフォルト。API 上の account 名は "ca-app-" を外した形。
#   Publisher ID: ca-app-pub-3967754936311621 → account: pub-3967754936311621
DEFAULT_PUBLISHER_ID = "pub-3967754936311621"
DEFAULT_LOOKBACK_DAYS = 30
# AdMob レポートは太平洋時間(PT/America/Los_Angeles)で日次確定する。
ADMOB_TIMEZONE = ZoneInfo("America/Los_Angeles")
# 当日/前日は未確定なことが多い。終端をこの日数だけ後ろへずらす。
FETCH_END_OFFSET_DAYS = 1
# 収益 micros → 通貨額 の除数。
MICROS_PER_UNIT = 1_000_000
CACHE_PATH = os.path.join(os.path.dirname(__file__), "data", "admob_revenue.parquet")
# キャッシュのメタ/国別/通貨を保存する JSON(daily は parquet/csv 側にも保存)。
CACHE_META_PATH = os.path.join(os.path.dirname(__file__), "data", "admob_revenue.json")
_HTTP_TIMEOUT = 30


class AdMobAuthError(RuntimeError):
    """AdMob 認証(401/403)に失敗したことを表す明示的な例外。

    refresh_token / access_token / client_secret はメッセージに含めない
    (UI に安全に表示できる)。
    """


# ---------------------------------------------------------------------------
# Secrets / 認証情報の取得
# ---------------------------------------------------------------------------


def _secret(key: str) -> str:
    """st.secrets 優先、無ければ環境変数から取得する。

    Streamlit が無い / secrets.toml が無い環境(CLI・CI)でも例外を出さず、
    環境変数 ADMOB_* にフォールバックする。
    """
    # 1) Streamlit Secrets(存在すれば)
    try:
        import streamlit as st  # 遅延 import: 非 Streamlit 環境を壊さない

        try:
            val = st.secrets.get(key)  # type: ignore[attr-defined]
            if val:
                return str(val)
        except Exception:  # noqa: BLE001 — secrets.toml が無い等
            pass
    except Exception:  # noqa: BLE001 — streamlit 未インストール
        pass
    # 2) 環境変数
    return str(os.environ.get(key, ""))


def get_credentials() -> dict[str, str]:
    """AdMob 認証情報を Secrets / 環境変数から収集する。

    戻り値のキー: client_id / client_secret / refresh_token / publisher_id。
    publisher_id は未設定ならデフォルト(pub-3967754936311621)。
    欠けていても例外は出さない(has_credentials で判定する)。
    """
    pub = _secret("ADMOB_PUBLISHER_ID").strip() or DEFAULT_PUBLISHER_ID
    return {
        "client_id": _secret("ADMOB_CLIENT_ID"),
        "client_secret": _secret("ADMOB_CLIENT_SECRET"),
        "refresh_token": _secret("ADMOB_REFRESH_TOKEN"),
        "publisher_id": _normalize_account(pub),
    }


def _normalize_account(publisher_id: str) -> str:
    """Publisher ID を API の account 名(pub-...)に正規化する。

    "ca-app-pub-3967754936311621" のような AdMob アプリ用の表記から
    先頭の "ca-app-" を外し、API が期待する "pub-3967754936311621" にする。
    既に "pub-" 始まりならそのまま返す。
    """
    pid = str(publisher_id).strip()
    if pid.startswith("ca-app-"):
        pid = pid[len("ca-app-") :]
    return pid


def has_credentials(creds: dict[str, str] | None = None) -> bool:
    """自動取得に必要な OAuth3 項目が全て揃っているか。

    publisher_id はデフォルトがあるため必須判定には含めない。
    """
    c = creds if creds is not None else get_credentials()
    return all(c.get(k) for k in ("client_id", "client_secret", "refresh_token"))


# ---------------------------------------------------------------------------
# OAuth2 アクセストークン更新
# ---------------------------------------------------------------------------


def _build_token_request(creds: dict[str, str]) -> dict[str, str]:
    """トークン更新リクエストの form body を組み立てる(テストで検証可能に分離)。

    grant_type=refresh_token に client_id / client_secret / refresh_token を付与する。
    """
    return {
        "grant_type": "refresh_token",
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
    }


def _get_access_token(
    creds: dict[str, str], *, session: requests.Session | None = None
) -> str:
    """refresh_token から短命のアクセストークンを取得する。

    認証失敗(400/401/403)は AdMobAuthError に変換する
    (client_secret / refresh_token / access_token は絶対にメッセージへ出さない)。
    """
    data = _build_token_request(creds)
    http = session or requests
    resp = http.post(OAUTH_TOKEN_URL, data=data, timeout=_HTTP_TIMEOUT)

    # refresh_token 失効・client 情報誤りは 400/401 で返る。明示メッセージに変換。
    if resp.status_code in (400, 401, 403):
        raise AdMobAuthError(
            "AdMob の認証(アクセストークン更新)に失敗しました(HTTP "
            f"{resp.status_code})。Secrets の認証情報 "
            "(ADMOB_CLIENT_ID / ADMOB_CLIENT_SECRET / ADMOB_REFRESH_TOKEN) "
            "が正しいか、refresh_token が失効していないか確認してください。"
        )
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as exc:  # JSON でない応答
        raise AdMobAuthError(
            "AdMob 認証応答の解析に失敗しました。Secrets の認証情報を確認してください。"
        ) from exc

    token = payload.get("access_token")
    if not token:
        raise AdMobAuthError(
            "AdMob 認証応答に access_token がありません。"
            "Secrets の認証情報を確認してください。"
        )
    return str(token)


# ---------------------------------------------------------------------------
# networkReport:generate 取得
# ---------------------------------------------------------------------------


def _date_to_spec(d: date) -> dict[str, int]:
    """date を reportSpec 用の {year, month, day} に変換する。"""
    return {"year": d.year, "month": d.month, "day": d.day}


def _build_report_spec(
    start: date,
    end: date,
    *,
    currency_code: str | None = None,
) -> dict[str, Any]:
    """networkReport:generate の reportSpec body を組み立てる(テストで検証可能に分離)。

    dimensions は DATE / COUNTRY、metrics は ESTIMATED_EARNINGS / IMPRESSIONS / CLICKS。
    currency_code を渡すと localizationSettings.currencyCode を付与する。
    """
    spec: dict[str, Any] = {
        "dateRange": {
            "startDate": _date_to_spec(start),
            "endDate": _date_to_spec(end),
        },
        "dimensions": ["DATE", "COUNTRY"],
        "metrics": ["ESTIMATED_EARNINGS", "IMPRESSIONS", "CLICKS"],
    }
    if currency_code:
        spec["localizationSettings"] = {"currencyCode": currency_code}
    return spec


def fetch_report(
    start: date,
    end: date,
    creds: dict[str, str],
    *,
    currency_code: str | None = None,
    access_token: str | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """指定期間の networkReport を生成し、レスポンス行のリストを返す。

    レスポンスは JSON 行の配列(先頭 header / 中間 row / 末尾 footer)。
    そのまま parse_report に渡せる形で返す。
    認証エラー(401/403)は AdMobAuthError に変換する(トークンは出さない)。
    その他の HTTP エラーは requests の例外として送出する。
    """
    account = _normalize_account(creds.get("publisher_id") or DEFAULT_PUBLISHER_ID)
    token = access_token or _get_access_token(creds, session=session)
    url = f"{ADMOB_BASE_URL}/accounts/{account}/networkReport:generate"
    body = {"reportSpec": _build_report_spec(start, end, currency_code=currency_code)}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    http = session or requests
    resp = http.post(url, json=body, headers=headers, timeout=_HTTP_TIMEOUT)

    # 認証・認可の失敗はスタックトレースでなく明示メッセージに変換する。
    # トークン本文は絶対にメッセージへ出さない。
    if resp.status_code in (401, 403):
        raise AdMobAuthError(
            "AdMob の認証に失敗しました(HTTP "
            f"{resp.status_code})。Secrets の認証情報 "
            "(ADMOB_CLIENT_ID / ADMOB_CLIENT_SECRET / ADMOB_REFRESH_TOKEN) "
            "が正しいか、AdMob アカウント(Publisher ID)へのアクセス権を確認してください。"
        )
    resp.raise_for_status()

    payload = resp.json()
    # レスポンスは行の配列。念のため dict 単体で返るケースも許容する。
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return []


# ---------------------------------------------------------------------------
# レポート行のパース(micros 変換・集計・eCPM 算出)
# ---------------------------------------------------------------------------


def _micros_to_units(micros: Any) -> float:
    """micros(実額×1,000,000)を通貨額へ変換する。空/不正は 0.0。"""
    try:
        return float(micros) / MICROS_PER_UNIT
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any) -> float:
    """integerValue 等の数値文字列を float 化する。空/不正は 0.0。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _extract_currency(rows: list[dict[str, Any]]) -> str | None:
    """header / footer から通貨コードを拾う。無ければ None。

    AdMob は header.reportHeader.localizationSettings.currencyCode か
    footer.reportFooter.matchedRequests 等に情報を持つ。ここでは
    localizationSettings.currencyCode を最優先で探す。
    """
    for row in rows:
        for key in ("header", "footer"):
            block = row.get(key) if isinstance(row, dict) else None
            if not isinstance(block, dict):
                continue
            loc = block.get("localizationSettings")
            if isinstance(loc, dict) and loc.get("currencyCode"):
                return str(loc["currencyCode"])
    return None


def parse_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """networkReport の行配列を parse_admob 互換の dict に変換する。

    - 各 row.row.dimensionValues.DATE.value(YYYYMMDD) / COUNTRY.value と
      row.row.metricValues.ESTIMATED_EARNINGS.microsValue / IMPRESSIONS / CLICKS を抽出。
    - ★収益は micros → 1,000,000 で割って通貨額へ。
    - DATE で日次集計、COUNTRY で国別集計。
    - eCPM = 収益 / 表示回数 × 1000 を parsers._safe_ecpm で自算出。

    戻り値: {daily, country, currency, columns}(parse_admob と同形)。
    """
    records: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        row = entry.get("row")
        if not isinstance(row, dict):
            continue  # header / footer はスキップ
        dims = row.get("dimensionValues", {}) or {}
        mets = row.get("metricValues", {}) or {}

        date_val = (dims.get("DATE", {}) or {}).get("value", "")
        country_val = (dims.get("COUNTRY", {}) or {}).get("value", "") or "N/A"
        earn_micros = (mets.get("ESTIMATED_EARNINGS", {}) or {}).get("microsValue", 0)
        # IMPRESSIONS / CLICKS は integerValue で返る(micros ではない)。
        impr = (mets.get("IMPRESSIONS", {}) or {}).get("integerValue", 0)
        clicks = (mets.get("CLICKS", {}) or {}).get("integerValue", 0)

        records.append(
            {
                "date": str(date_val),
                "country": str(country_val),
                "earnings": _micros_to_units(earn_micros),
                "impressions": _int_value(impr),
                "clicks": _int_value(clicks),
            }
        )

    currency = _extract_currency(rows) or "USD"

    if not records:
        empty = _empty_admob()
        empty["currency"] = currency
        return empty

    work = pd.DataFrame(records)
    # DATE は YYYYMMDD 文字列。datetime へ。
    work["date"] = pd.to_datetime(work["date"], format="%Y%m%d", errors="coerce")

    # 日次集計(国をまたいで合算)。
    daily = (
        work.dropna(subset=["date"])
        .groupby("date", as_index=False)[["earnings", "impressions", "clicks"]]
        .sum()
        .sort_values("date")
    )
    daily["ecpm"] = _safe_ecpm(daily["earnings"], daily["impressions"])

    # 国別集計。
    country = (
        work.groupby("country", as_index=False)[["earnings", "impressions", "clicks"]]
        .sum()
        .sort_values("earnings", ascending=False)
    )

    return {
        "daily": daily.reset_index(drop=True),
        "country": country.reset_index(drop=True),
        "currency": currency,
        "columns": {"source": "admob_api"},
    }


# ---------------------------------------------------------------------------
# 直近 N 日の収益取得(日次 / 国別)
# ---------------------------------------------------------------------------


def fetch_revenue(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    end_date: date | None = None,
    currency_code: str | None = None,
    creds: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """直近 lookback_days 日分の広告収益を取得して集計する。

    networkReport は期間指定で1回叩けば全日返るため、日付ループは不要。
    アクセストークンは refresh_token から都度更新する。

    戻り値は parse_admob と同形:
        {daily, country, currency, columns, meta}
    meta には取得範囲・行数・最終取得時刻を入れる。
    """
    c = creds if creds is not None else get_credentials()
    if not has_credentials(c):
        raise RuntimeError(
            "AdMob 認証情報が不足しています"
            "(ADMOB_CLIENT_ID / ADMOB_CLIENT_SECRET / ADMOB_REFRESH_TOKEN)。"
        )

    # 基準日は太平洋時間(PT)。AdMob は PT で確定するため。
    # 未確定になりやすい当日/前日を避けて終端をずらす。
    if end_date is not None:
        end = end_date
    else:
        pt_today = datetime.now(ADMOB_TIMEZONE).date()
        end = pt_today - timedelta(days=FETCH_END_OFFSET_DAYS)
    start = end - timedelta(days=max(lookback_days - 1, 0))

    own_session = session is None
    http = session or requests.Session()
    try:
        rows = fetch_report(
            start, end, c, currency_code=currency_code, session=http
        )
    finally:
        if own_session:
            http.close()

    result = parse_report(rows)
    result["meta"] = {
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "rows": len(rows),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return result


# ---------------------------------------------------------------------------
# ローカルキャッシュ
# ---------------------------------------------------------------------------


def _daily_to_records(daily: pd.DataFrame) -> list[dict[str, Any]]:
    """daily(date, earnings, impressions, clicks, ecpm)を JSON 化可能なレコードに変換する。"""
    if daily is None or daily.empty:
        return []
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    for col in ("earnings", "impressions", "clicks", "ecpm"):
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0.0)
    cols = [c for c in ("date", "earnings", "impressions", "clicks", "ecpm") if c in d.columns]
    return d[cols].to_dict("records")


def save_cache(
    result: dict[str, Any],
    path: str = CACHE_PATH,
    meta_path: str = CACHE_META_PATH,
) -> str | None:
    """取得結果を丸ごとローカル保存する。data/ は .gitignore 済み。

    daily は parquet(不可なら csv)で、country/currency/meta は JSON で保存する。
    JSON にも daily を含めるので、load_cache は JSON 単体からも完全復元できる。

    保存できた daily のパスを返す(daily 空・全書き込み不能なら None)。
    """
    daily = result.get("daily")
    country = result.get("country")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # --- JSON メタ(country/currency/meta + daily のバックアップ) ---
    payload = {
        "daily": _daily_to_records(daily),
        "country": (
            country.to_dict("records")
            if isinstance(country, pd.DataFrame) and not country.empty
            else []
        ),
        "currency": result.get("currency", "USD"),
        "meta": result.get("meta", {}),
    }
    try:
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 — メタ保存失敗は致命ではない
        pass

    if daily is None or daily.empty:
        return None

    # --- daily 本体(parquet 優先、pyarrow 無ければ csv) ---
    try:
        daily.to_parquet(path, index=False)
        return path
    except Exception:  # noqa: BLE001 — pyarrow 未導入等は csv へ
        csv_path = os.path.splitext(path)[0] + ".csv"
        try:
            daily.to_csv(csv_path, index=False)
            return csv_path
        except Exception:  # noqa: BLE001
            return None


def load_cache(
    path: str = CACHE_PATH, meta_path: str = CACHE_META_PATH
) -> dict[str, Any] | None:
    """前回保存した取得結果を parse_admob 互換の dict で復元する。

    JSON メタ(daily/country/currency/meta)があればそれを最優先で復元。
    無ければ parquet/csv の daily 単体から最小復元する。どちらも無ければ None。
    """
    # --- 1) JSON メタから完全復元 ---
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                payload = json.load(fh)
            daily = pd.DataFrame(payload.get("daily", []))
            if not daily.empty:
                daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
                for col in ("earnings", "impressions", "clicks", "ecpm"):
                    if col in daily.columns:
                        daily[col] = pd.to_numeric(
                            daily[col], errors="coerce"
                        ).fillna(0.0)
            else:
                daily = pd.DataFrame(
                    columns=["date", "earnings", "impressions", "clicks", "ecpm"]
                )
            country = pd.DataFrame(payload.get("country", []))
            if country.empty:
                country = pd.DataFrame(
                    columns=["country", "earnings", "impressions", "clicks"]
                )
            meta = dict(payload.get("meta", {}))
            meta["source"] = "cache"
            return {
                "daily": daily.reset_index(drop=True),
                "country": country.reset_index(drop=True),
                "currency": payload.get("currency", "USD"),
                "columns": {"source": "cache"},
                "meta": meta,
            }
        except Exception:  # noqa: BLE001 — 壊れた JSON は parquet/csv へフォールバック
            pass

    # --- 2) daily 単体(parquet / csv)から最小復元 ---
    for p in (path, os.path.splitext(path)[0] + ".csv"):
        if os.path.exists(p):
            try:
                daily = pd.read_parquet(p) if p.endswith(".parquet") else pd.read_csv(p)
                if "date" in daily.columns:
                    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
                return {
                    "daily": daily.reset_index(drop=True),
                    "country": pd.DataFrame(
                        columns=["country", "earnings", "impressions", "clicks"]
                    ),
                    "currency": "USD",
                    "columns": {"source": "cache"},
                    "meta": {"source": "cache"},
                }
            except Exception:  # noqa: BLE001
                continue
    return None


# ---------------------------------------------------------------------------
# モック自己テスト(実 OAuth / 実 API を叩かない)
# ---------------------------------------------------------------------------


def _selftest() -> int:  # pragma: no cover — 手動検証用
    """実 API を叩かずに micros 変換・集計・eCPM・トークン更新整形を検証する。"""
    from unittest import mock

    print("=" * 60)
    print("ADMOB_API MOCK SELF-TEST (no real API call)")
    print("=" * 60)

    ok = True

    # 合成レスポンス(header / row×複数 / footer)。
    #   収益 micros: 1,234,560 → 1.23456 通貨額 になること(★micros 変換)。
    synthetic_rows = [
        {
            "header": {
                "dateRange": {
                    "startDate": {"year": 2026, "month": 6, "day": 25},
                    "endDate": {"year": 2026, "month": 6, "day": 26},
                },
                "localizationSettings": {"currencyCode": "JPY"},
            }
        },
        {
            "row": {
                "dimensionValues": {
                    "DATE": {"value": "20260625"},
                    "COUNTRY": {"value": "JP"},
                },
                "metricValues": {
                    "ESTIMATED_EARNINGS": {"microsValue": "1234560"},  # → 1.23456
                    "IMPRESSIONS": {"integerValue": "1000"},
                    "CLICKS": {"integerValue": "20"},
                },
            }
        },
        {
            "row": {
                "dimensionValues": {
                    "DATE": {"value": "20260625"},
                    "COUNTRY": {"value": "US"},
                },
                "metricValues": {
                    "ESTIMATED_EARNINGS": {"microsValue": "2000000"},  # → 2.0
                    "IMPRESSIONS": {"integerValue": "1000"},
                    "CLICKS": {"integerValue": "10"},
                },
            }
        },
        {
            "row": {
                "dimensionValues": {
                    "DATE": {"value": "20260626"},
                    "COUNTRY": {"value": "JP"},
                },
                "metricValues": {
                    "ESTIMATED_EARNINGS": {"microsValue": "500000"},  # → 0.5
                    "IMPRESSIONS": {"integerValue": "500"},
                    "CLICKS": {"integerValue": "5"},
                },
            }
        },
        {"footer": {"matchingRowCount": "3"}},
    ]

    parsed = parse_report(synthetic_rows)
    daily = parsed["daily"]
    country = parsed["country"]

    # --- (a) micros 変換: microsValue=1234560 → 1.23456 ---
    print("\n[a] micros 変換 (microsValue → 通貨額)")
    single = _micros_to_units("1234560")
    print(f"  _micros_to_units('1234560') = {single} (期待 1.23456)")
    assert abs(single - 1.23456) < 1e-9, f"micros 変換が不正: {single}"
    # 0625 の JP 行の収益が 1.23456 で入っていること
    jp_0625 = float(
        country.loc[country["country"] == "JP", "earnings"].iloc[0]
    )
    # JP は 0625(1.23456) + 0626(0.5) = 1.73456
    print(f"  国別 JP 収益: {jp_0625} (期待 1.73456 = 1.23456+0.5)")
    assert abs(jp_0625 - 1.73456) < 1e-9, f"JP 収益 micros 集計が不正: {jp_0625}"
    print("  -> micros → 通貨額 変換 OK (÷1,000,000)")

    # --- (b) 日次/国別集計と eCPM(=収益/表示×1000) が手計算と一致 ---
    print("\n[b] 日次/国別集計と eCPM 算出")
    # 0625: 収益 1.23456+2.0=3.23456, 表示 1000+1000=2000 → eCPM=3.23456/2000*1000=1.61728
    d0625 = daily.loc[daily["date"] == pd.Timestamp("2026-06-25")].iloc[0]
    earn_0625 = float(d0625["earnings"])
    impr_0625 = float(d0625["impressions"])
    ecpm_0625 = float(d0625["ecpm"])
    expected_ecpm = round(3.23456 / 2000 * 1000, 4)
    print(f"  06/25 収益={earn_0625} 表示={int(impr_0625)} eCPM={ecpm_0625}")
    print(f"       期待 収益=3.23456 表示=2000 eCPM={expected_ecpm}")
    assert abs(earn_0625 - 3.23456) < 1e-9, f"06/25 収益集計が不正: {earn_0625}"
    assert int(impr_0625) == 2000, f"06/25 表示回数集計が不正: {impr_0625}"
    assert abs(ecpm_0625 - expected_ecpm) < 1e-9, f"eCPM 算出が不正: {ecpm_0625}"
    # 通貨がレスポンスの currencyCode(JPY)から取れていること
    print(f"  通貨: {parsed['currency']} (期待 JPY)")
    assert parsed["currency"] == "JPY", f"通貨コードが取れていない: {parsed['currency']}"
    # クリック集計も確認
    clk_0625 = float(d0625["clicks"])
    assert int(clk_0625) == 30, f"06/25 クリック集計が不正: {clk_0625}"
    print("  -> 日次/国別集計・eCPM・通貨・クリック OK")

    # --- reportSpec / account 正規化の検証 ---
    print("\n[reportSpec] body 整形 & account 正規化")
    spec = _build_report_spec(date(2026, 6, 25), date(2026, 6, 26), currency_code="JPY")
    print(f"  dimensions={spec['dimensions']} metrics={spec['metrics']}")
    assert spec["dimensions"] == ["DATE", "COUNTRY"], "dimensions が不正"
    assert spec["metrics"] == [
        "ESTIMATED_EARNINGS",
        "IMPRESSIONS",
        "CLICKS",
    ], "metrics が不正"
    assert spec["dateRange"]["startDate"] == {"year": 2026, "month": 6, "day": 25}
    assert spec["dateRange"]["endDate"] == {"year": 2026, "month": 6, "day": 26}
    assert spec["localizationSettings"]["currencyCode"] == "JPY"
    acct = _normalize_account("ca-app-pub-3967754936311621")
    print(f"  account 正規化: ca-app-pub-... → {acct} (期待 pub-3967754936311621)")
    assert acct == "pub-3967754936311621", f"account 正規化が不正: {acct}"
    assert _normalize_account("pub-123") == "pub-123", "既に pub- のものは不変であるべき"
    print("  -> reportSpec body / account 正規化 OK")

    # --- (c) アクセストークン更新の整形 & トークン非漏洩(モック) ---
    print("\n[c] アクセストークン更新の整形 (grant_type=refresh_token, モック)")
    creds = {
        "client_id": "CLIENT-ID-123.apps.googleusercontent.com",
        "client_secret": "SUPER_SECRET_CLIENT_SECRET",
        "refresh_token": "SUPER_SECRET_REFRESH_TOKEN",
        "publisher_id": "pub-3967754936311621",
    }
    body = _build_token_request(creds)
    print(f"  body キー: {sorted(body.keys())}")
    assert body["grant_type"] == "refresh_token", "grant_type が refresh_token でない"
    assert body["client_id"] == creds["client_id"], "client_id 不一致"
    assert body["client_secret"] == creds["client_secret"], "client_secret 不一致"
    assert body["refresh_token"] == creds["refresh_token"], "refresh_token 不一致"

    fake_token_resp = mock.Mock()
    fake_token_resp.status_code = 200
    fake_token_resp.json.return_value = {
        "access_token": "SECRET_ACCESS_TOKEN_XYZ",
        "expires_in": 3599,
        "token_type": "Bearer",
    }
    fake_session = mock.Mock()
    fake_session.post.return_value = fake_token_resp
    token = _get_access_token(creds, session=fake_session)
    # POST が正しい URL / data で呼ばれたこと
    call = fake_session.post.call_args
    assert call.args[0] == OAUTH_TOKEN_URL, "トークン URL が不正"
    sent = call.kwargs.get("data")
    assert sent["grant_type"] == "refresh_token", "送信 body の grant_type が不正"
    assert token == "SECRET_ACCESS_TOKEN_XYZ", "access_token が取れていない"
    print("  -> grant_type=refresh_token / client_id/secret/refresh_token 付与 OK")

    # --- (c-2) refresh_token / access_token / client_secret が例外・UI に漏れない ---
    print("\n[c-2] トークン/シークレット非漏洩 (401 経路)")
    fake_401 = mock.Mock()
    fake_401.status_code = 401
    fake_session_401 = mock.Mock()
    fake_session_401.post.return_value = fake_401
    raised = False
    try:
        _get_access_token(creds, session=fake_session_401)
    except AdMobAuthError as exc:
        raised = True
        msg = str(exc)
        print(f"  AdMobAuthError: {msg}")
        assert creds["client_secret"] not in msg, "client_secret がメッセージに漏洩"
        assert creds["refresh_token"] not in msg, "refresh_token がメッセージに漏洩"
        assert "SECRET_ACCESS_TOKEN_XYZ" not in msg, "access_token がメッセージに漏洩"
        assert "401" in msg, "HTTP ステータスがメッセージに無い"
        assert ("認証" in msg) or ("ADMOB" in msg), "認証確認の案内が無い"
    assert raised, "401 なのに AdMobAuthError が送出されなかった"
    print("  -> トークン更新 401 は明示エラー・シークレット非漏洩 OK")

    # --- (d) fetch_report 401 → AdMobAuthError 明示メッセージ・トークン非漏洩 ---
    print("\n[d] レポート取得 401 → AdMobAuthError (モック)")
    fake_report_401 = mock.Mock()
    fake_report_401.status_code = 403
    fake_session_r = mock.Mock()
    fake_session_r.post.return_value = fake_report_401
    raised = False
    try:
        # access_token を渡してトークン更新をスキップ → レポート POST が 403
        fetch_report(
            date(2026, 6, 25),
            date(2026, 6, 26),
            creds,
            access_token="SECRET_ACCESS_TOKEN_XYZ",
            session=fake_session_r,
        )
    except AdMobAuthError as exc:
        raised = True
        msg = str(exc)
        print(f"  AdMobAuthError: {msg}")
        assert "SECRET_ACCESS_TOKEN_XYZ" not in msg, "access_token がメッセージに漏洩"
        assert creds["client_secret"] not in msg, "client_secret がメッセージに漏洩"
        assert creds["refresh_token"] not in msg, "refresh_token がメッセージに漏洩"
        assert "403" in msg, "HTTP ステータスがメッセージに無い"
        assert ("認証" in msg) or ("Publisher" in msg) or ("ADMOB" in msg), (
            "認証確認の案内が無い"
        )
    assert raised, "403 なのに AdMobAuthError が送出されなかった"
    fake_report_401.raise_for_status.assert_not_called()  # スタックトレース経由でない
    # POST 先 URL に account(pub-...)が入っていること
    r_call = fake_session_r.post.call_args
    posted_url = r_call.args[0]
    print(f"  POST URL: {posted_url}")
    assert "pub-3967754936311621" in posted_url, "URL に account が入っていない"
    assert posted_url.endswith("networkReport:generate"), "エンドポイントが不正"
    print("  -> レポート 403 は明示エラー・トークン非漏洩・URL に account OK")

    # --- (e) cache: save→load 往復で daily+country+currency が復元される ---
    import tempfile

    print("\n[e] キャッシュ save→load 往復")
    with tempfile.TemporaryDirectory() as tmp:
        cache_p = os.path.join(tmp, "admob_revenue.parquet")
        meta_p = os.path.join(tmp, "admob_revenue.json")
        # 上で作った parsed をそのままキャッシュ
        parsed_with_meta = dict(parsed)
        parsed_with_meta["meta"] = {"fetched_at": "2026-06-27 09:00:00"}
        save_cache(parsed_with_meta, path=cache_p, meta_path=meta_p)
        loaded = load_cache(path=cache_p, meta_path=meta_p)
        assert loaded is not None, "load_cache が None を返した"
        assert not loaded["daily"].empty, "daily が復元されない"
        loaded_earn = float(loaded["daily"]["earnings"].sum())
        orig_earn = float(daily["earnings"].sum())
        print(f"  復元 総収益={loaded_earn:.5f} (元 {orig_earn:.5f})")
        assert abs(loaded_earn - orig_earn) < 1e-6, "daily 収益合計が不一致"
        assert loaded["currency"] == "JPY", "currency が復元されない"
        jp_c = float(
            loaded["country"].loc[
                loaded["country"]["country"] == "JP", "earnings"
            ].iloc[0]
        )
        assert abs(jp_c - 1.73456) < 1e-9, f"country(JP) が復元されない: {jp_c}"
        assert loaded["meta"].get("fetched_at") == "2026-06-27 09:00:00", (
            "meta.fetched_at が復元されない"
        )
        # eCPM 列も残っていること
        assert "ecpm" in loaded["daily"].columns, "eCPM 列が復元されない"
        print(
            f"  復元: 収益={loaded_earn:.5f} daily行={len(loaded['daily'])} "
            f"JP={jp_c:.5f} currency={loaded['currency']}"
        )
    print("  -> daily+country+currency+meta+eCPM 復元 OK")

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("Usage: python admob_api.py --selftest")
