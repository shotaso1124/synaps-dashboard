"""App Store Connect Sales & Trends API から DL数を自動取得する層。

代表が Issuer ID / Key ID / .p8 秘密鍵 / Vendor Number を Secrets に投入すれば、
CSV アップロード無しで **ダウンロード数・国別** が自動で入る。

- 認証: JWT(ES256) を毎リクエスト前に生成(有効期限 <= 20分)。
- 取得: GET /v1/salesReports (DAILY / SALES / SUMMARY, gzip-TSV)。
- 集計: 取得した gzip-TSV を parsers.parse_asc_sales に流し込み、
  既存の可視化(daily / country / total_downloads)と同じ形にまとめる。

Streamlit に依存しない純粋なロジック層(Secrets 取得のみ st.secrets を任意利用)。
実 API を叩かないモック検証は `python asc_api.py --selftest` を参照。
"""

from __future__ import annotations

import gzip
import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import jwt  # PyJWT[crypto]
import pandas as pd
import requests

from parsers import _empty_asc, parse_asc_sales

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

ASC_BASE_URL = "https://api.appstoreconnect.apple.com/v1/salesReports"
ASC_AUDIENCE = "appstoreconnect-v1"
# JWT 有効期限: ASC は最大20分。安全側に倒して 19 分にする。
JWT_TTL_SECONDS = 19 * 60
DEFAULT_LOOKBACK_DAYS = 30
# App Store Connect の Sales レポートは太平洋時間(PT)で日次確定する。
# JST 基準だと当日/前日が常に未確定(404)になるため PT 基準日を使う。
ASC_TIMEZONE = ZoneInfo("America/Los_Angeles")
# ASC の当日/前日は未確定なことが多い。ループ開始をこの日数だけ後ろへずらす。
FETCH_END_OFFSET_DAYS = 1
CACHE_PATH = os.path.join(os.path.dirname(__file__), "data", "asc_downloads.parquet")
# キャッシュのメタ/国別/総数を保存する JSON(daily は parquet/csv 側)。
CACHE_META_PATH = os.path.join(os.path.dirname(__file__), "data", "asc_downloads.json")
_HTTP_TIMEOUT = 30


class ASCAuthError(RuntimeError):
    """ASC 認証(401/403)に失敗したことを表す明示的な例外。

    トークンや鍵本文はメッセージに含めない(UI に安全に表示できる)。
    """


# ---------------------------------------------------------------------------
# Secrets / 認証情報の取得
# ---------------------------------------------------------------------------


def _secret(key: str) -> str:
    """st.secrets 優先、無ければ環境変数から取得する。

    Streamlit が無い / secrets.toml が無い環境(CLI・CI)でも例外を出さず、
    環境変数 ASC_* にフォールバックする。
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


def _load_p8(raw: str) -> str:
    r""".p8 秘密鍵本文を正規化する。

    環境変数経由だと改行が "\n" というリテラル2文字で来ることがあるため、
    実際の改行に復元する。secrets.toml の三重引用符なら既に実改行。
    """
    key = raw.strip()
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")
    return key


def get_credentials() -> dict[str, str]:
    """ASC 認証情報を Secrets / 環境変数から収集する。

    戻り値のキー: issuer_id / key_id / p8 / vendor_number。
    欠けていても例外は出さない(has_credentials で判定する)。
    """
    return {
        "issuer_id": _secret("ASC_ISSUER_ID"),
        "key_id": _secret("ASC_KEY_ID"),
        "p8": _load_p8(_secret("ASC_P8")),
        "vendor_number": _secret("ASC_VENDOR_NUMBER"),
    }


def has_credentials(creds: dict[str, str] | None = None) -> bool:
    """自動取得に必要な4項目が全て揃っているか。"""
    c = creds if creds is not None else get_credentials()
    return all(c.get(k) for k in ("issuer_id", "key_id", "p8", "vendor_number"))


# ---------------------------------------------------------------------------
# JWT(ES256)生成
# ---------------------------------------------------------------------------


def generate_jwt(
    issuer_id: str, key_id: str, private_key: str, *, ttl_seconds: int = JWT_TTL_SECONDS
) -> str:
    """App Store Connect API 用の ES256 署名済み JWT を生成する。

    Args:
        issuer_id:   Issuer ID(payload の iss)。
        key_id:      Key ID(ヘッダの kid)。
        private_key: .p8 の秘密鍵本文(PEM, EC P-256)。
        ttl_seconds: 有効期限(秒)。ASC 上限 20 分。

    Returns:
        署名済み JWT 文字列。毎リクエスト前に呼ぶ想定(短命)。
    """
    now = int(time.time())
    headers = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + min(ttl_seconds, JWT_TTL_SECONDS),
        "aud": ASC_AUDIENCE,
    }
    token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    # PyJWT>=2 は str を返すが、古い版の bytes も許容する。
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


# ---------------------------------------------------------------------------
# 1日分の Sales レポート取得
# ---------------------------------------------------------------------------


def _build_sales_params(report_date: date, vendor_number: str) -> dict[str, str]:
    """salesReports 用のクエリ params を組み立てる(テストで検証可能に分離)。

    重要: `filter[version]` は付けない。version は Subscription 系レポート専用で、
    SALES / SUMMARY に付与すると ASC が 400 (PARAMETER_ERROR) を返し全取得が失敗する。
    """
    return {
        "filter[frequency]": "DAILY",
        "filter[reportType]": "SALES",
        "filter[reportSubType]": "SUMMARY",
        "filter[vendorNumber]": vendor_number,
        "filter[reportDate]": report_date.isoformat(),
    }


def fetch_report_bytes(
    report_date: date, creds: dict[str, str], *, session: requests.Session | None = None
) -> bytes | None:
    """指定日の DAILY / SALES / SUMMARY レポートを gzip 解凍済み TSV bytes で返す。

    データが未確定・欠損(404)の日は None を返す(呼び出し側でスキップ)。
    認証エラー(401/403)は ASCAuthError に変換する(鍵/トークンは出さない)。
    その他の HTTP エラーは requests の例外として送出する。
    """
    token = generate_jwt(creds["issuer_id"], creds["key_id"], creds["p8"])
    params = _build_sales_params(report_date, creds["vendor_number"])
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/a-gzip"}

    http = session or requests
    resp = http.get(
        ASC_BASE_URL, params=params, headers=headers, timeout=_HTTP_TIMEOUT
    )

    # 認証・認可の失敗はスタックトレースでなく明示メッセージに変換する。
    # トークン本文・鍵は絶対にメッセージへ出さない。
    if resp.status_code in (401, 403):
        raise ASCAuthError(
            "App Store Connect の認証に失敗しました(HTTP "
            f"{resp.status_code})。Secrets の認証情報 "
            "(Issuer ID / Key ID / Vendor番号 / .p8 秘密鍵) が正しいか確認してください。"
        )

    # 未確定日 / データ無しは 404。全体を落とさずスキップさせる。
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    body = resp.content
    if not body:
        return None
    # レスポンスは gzip。念のため magic number を見てから解凍する。
    if body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except OSError:
            return None
    return body


# ---------------------------------------------------------------------------
# 日付範囲ループ取得 + 集計
# ---------------------------------------------------------------------------


def _daterange(start: date, end: date):
    """start..end(両端含む)を1日ずつ yield する。"""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def fetch_downloads(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    end_date: date | None = None,
    creds: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """直近 lookback_days 日分の DL レポートを取得して集計する。

    各日の gzip-TSV を parsers.parse_asc_sales に通し、
    daily(date, downloads) を縦結合 → 再集計して国別も合算する。
    当日分など未確定日(404/空)は **例外を出さずスキップ** して継続する。

    戻り値は parse_asc_sales と同形:
        {daily, country, currency, total_downloads, columns, meta}
    meta には取得成功日数・スキップ日数・最終取得時刻を入れる。
    """
    c = creds if creds is not None else get_credentials()
    if not has_credentials(c):
        raise RuntimeError(
            "ASC 認証情報が不足しています(ASC_ISSUER_ID/ASC_KEY_ID/ASC_P8/ASC_VENDOR_NUMBER)。"
        )

    # 基準日は太平洋時間(PT)。ASC Sales は PT で確定するため。
    # end 未指定なら「PT の今日 − FETCH_END_OFFSET_DAYS」を終端にして、
    # 未確定になりやすい当日/前日を最初から避ける(404 は下でスキップ許容)。
    if end_date is not None:
        end = end_date
    else:
        pt_today = datetime.now(ASC_TIMEZONE).date()
        end = pt_today - timedelta(days=FETCH_END_OFFSET_DAYS)
    start = end - timedelta(days=max(lookback_days - 1, 0))

    daily_frames: list[pd.DataFrame] = []
    country_frames: list[pd.DataFrame] = []
    currency = "USD"
    fetched_days = 0
    skipped_days = 0
    errors: list[str] = []

    own_session = session is None
    http = session or requests.Session()
    try:
        for d in _daterange(start, end):
            try:
                tsv = fetch_report_bytes(d, c, session=http)
            except ASCAuthError:
                # 認証失敗は日ごとにスキップせず即座に上へ伝える(全日 401 で
                # 無言スキップして 0 件になるのを防ぐ)。
                raise
            except requests.RequestException as exc:  # ネットワーク/一時エラー
                skipped_days += 1
                errors.append(f"{d.isoformat()}: {exc}")
                continue
            if tsv is None:  # 未確定 / 欠損日
                skipped_days += 1
                continue

            parsed = parse_asc_sales(tsv)
            if not parsed["daily"].empty:
                daily_frames.append(parsed["daily"])
            if not parsed["country"].empty:
                country_frames.append(parsed["country"])
            if parsed.get("currency"):
                currency = parsed["currency"]
            fetched_days += 1
    finally:
        if own_session:
            http.close()

    result = _aggregate(daily_frames, country_frames, currency)
    result["meta"] = {
        "range_start": start.isoformat(),
        "range_end": end.isoformat(),
        "fetched_days": fetched_days,
        "skipped_days": skipped_days,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "errors": errors,
    }
    return result


def _aggregate(
    daily_frames: list[pd.DataFrame],
    country_frames: list[pd.DataFrame],
    currency: str,
) -> dict[str, Any]:
    """複数日分の daily / country を結合して再集計する。"""
    if not daily_frames and not country_frames:
        empty = _empty_asc()
        return empty

    if daily_frames:
        daily = (
            pd.concat(daily_frames, ignore_index=True)
            .groupby("date", as_index=False)["downloads"]
            .sum()
            .sort_values("date")
            .reset_index(drop=True)
        )
    else:
        daily = pd.DataFrame(columns=["date", "downloads"])

    if country_frames:
        country = (
            pd.concat(country_frames, ignore_index=True)
            .groupby("country", as_index=False)["downloads"]
            .sum()
            .sort_values("downloads", ascending=False)
            .reset_index(drop=True)
        )
    else:
        country = pd.DataFrame(columns=["country", "downloads"])

    total = int(daily["downloads"].sum()) if not daily.empty else 0
    return {
        "daily": daily,
        "country": country,
        "currency": currency,
        "total_downloads": total,
        "columns": {"source": "asc_api"},
    }


# ---------------------------------------------------------------------------
# ローカルキャッシュ
# ---------------------------------------------------------------------------


def _daily_to_records(daily: pd.DataFrame) -> list[dict[str, Any]]:
    """daily(date, downloads)を JSON 化可能なレコード列に変換する。"""
    if daily is None or daily.empty:
        return []
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    d["downloads"] = pd.to_numeric(d["downloads"], errors="coerce").fillna(0)
    return d[["date", "downloads"]].to_dict("records")


def save_cache(
    result: dict[str, Any],
    path: str = CACHE_PATH,
    meta_path: str = CACHE_META_PATH,
) -> str | None:
    """取得結果を丸ごとローカル保存する。data/ は .gitignore 済み。

    daily は parquet(不可なら csv)で、country/total_downloads/currency/meta は
    JSON で保存する。JSON にも daily を含めるので、load_cache は JSON 単体からも
    完全復元できる(将来 cron 取得データの読み込み土台にもなる)。

    保存できた daily のパスを返す(daily 空・全書き込み不能なら None)。
    """
    daily = result.get("daily")
    country = result.get("country")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # --- JSON メタ(country/total/currency/meta + daily のバックアップ) ---
    payload = {
        "daily": _daily_to_records(daily),
        "country": (
            country.to_dict("records")
            if isinstance(country, pd.DataFrame) and not country.empty
            else []
        ),
        "currency": result.get("currency", "USD"),
        "total_downloads": int(result.get("total_downloads", 0) or 0),
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
    """前回保存した取得結果を parse_asc_sales 互換の dict で復元する。

    JSON メタ(daily/country/total/currency/meta)があればそれを最優先で復元。
    無ければ parquet/csv の daily 単体から最小復元する。どちらも無ければ None。
    二重計上を避けるため、保存済みの集計値をそのまま返す(再取得・再集計しない)。
    """
    # --- 1) JSON メタから完全復元 ---
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as fh:
                payload = json.load(fh)
            daily = pd.DataFrame(payload.get("daily", []))
            if not daily.empty:
                daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
                daily["downloads"] = pd.to_numeric(
                    daily["downloads"], errors="coerce"
                ).fillna(0)
            else:
                daily = pd.DataFrame(columns=["date", "downloads"])
            country = pd.DataFrame(payload.get("country", []))
            if country.empty:
                country = pd.DataFrame(columns=["country", "downloads"])
            meta = dict(payload.get("meta", {}))
            meta["source"] = "cache"
            return {
                "daily": daily.reset_index(drop=True),
                "country": country.reset_index(drop=True),
                "currency": payload.get("currency", "USD"),
                "total_downloads": int(payload.get("total_downloads", 0) or 0),
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
                total = int(daily["downloads"].sum()) if "downloads" in daily else 0
                return {
                    "daily": daily.reset_index(drop=True),
                    "country": pd.DataFrame(columns=["country", "downloads"]),
                    "currency": "USD",
                    "total_downloads": total,
                    "columns": {"source": "cache"},
                    "meta": {"source": "cache"},
                }
            except Exception:  # noqa: BLE001
                continue
    return None


# ---------------------------------------------------------------------------
# モック自己テスト(実キー不要 — その場で EC 鍵を生成して検証する)
# ---------------------------------------------------------------------------


def _selftest() -> int:  # pragma: no cover — 手動検証用
    """実 API を叩かずに JWT 構造と gzip-TSV 集計を検証する。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    print("=" * 60)
    print("ASC_API MOCK SELF-TEST (no real API call)")
    print("=" * 60)

    ok = True

    # --- (1) JWT: その場で生成した EC P-256 鍵で署名し、decode で検証 ---
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = priv.public_key()

    token = generate_jwt("ISSUER-123", "KEY-ABC", pem)
    header = jwt.get_unverified_header(token)
    decoded = jwt.decode(token, pub, algorithms=["ES256"], audience=ASC_AUDIENCE)

    print("\n[1] JWT 構造")
    print(f"  header: {header}")
    print(f"  payload: iss={decoded['iss']} aud={decoded['aud']}")
    print(f"  exp-iat(秒): {decoded['exp'] - decoded['iat']}")
    assert header["alg"] == "ES256", "alg が ES256 でない"
    assert header["kid"] == "KEY-ABC", "kid 不一致"
    assert header["typ"] == "JWT", "typ が JWT でない"
    assert decoded["iss"] == "ISSUER-123", "iss 不一致"
    assert decoded["aud"] == ASC_AUDIENCE, "aud 不一致"
    ttl = decoded["exp"] - decoded["iat"]
    assert 0 < ttl <= 20 * 60, "exp が 20分超"
    print("  -> JWT 構造 OK (alg/kid/typ/iss/aud/exp<=20min)")

    # --- (2) 合成 gzip-TSV を parse_asc_sales に通し集計値を検証 ---
    synthetic_tsv = (
        "Provider\tSKU\tTitle\tProduct Type Identifier\tUnits\t"
        "Developer Proceeds\tBegin Date\tEnd Date\tCountry Code\n"
        "APPLE\tsynaps.app\tSynaps\t1F\t100\t0.00\t06/25/2026\t06/25/2026\tJP\n"
        "APPLE\tsynaps.app\tSynaps\t1F\t20\t0.00\t06/25/2026\t06/25/2026\tUS\n"
        "APPLE\tsynaps.app\tSynaps\t7F\t5\t0.00\t06/25/2026\t06/25/2026\tJP\n"
        "APPLE\tsynaps.premium\tPremium\tIA1\t9\t900.00\t06/25/2026\t06/25/2026\tJP\n"
        "APPLE\tsynaps.app\tSynaps\t1F\t30\t0.00\t06/26/2026\t06/26/2026\tJP\n"
    )
    gz = gzip.compress(synthetic_tsv.encode("utf-8"))

    parsed = parse_asc_sales(gz)  # gzip も parse_asc_sales が解凍対応
    total = parsed["total_downloads"]
    jp = int(
        parsed["country"].loc[parsed["country"]["country"] == "JP", "downloads"].iloc[0]
    )
    us = int(
        parsed["country"].loc[parsed["country"]["country"] == "US", "downloads"].iloc[0]
    )

    print("\n[2] 合成 gzip-TSV の集計 (1F=DL / IA*=除外)")
    print(f"  daily 行数: {len(parsed['daily'])}")
    print(f"  総DL: {total}  (期待 150 = 100+20+30, 7F/IA1 は除外)")
    print(f"  国別 JP: {jp} (期待 130 = 100+30)  US: {us} (期待 20)")
    assert total == 150, f"総DL 期待150 != {total} (Product Type フィルタ不正)"
    assert jp == 130, f"JP 期待130 != {jp}"
    assert us == 20, f"US 期待20 != {us}"
    print("  -> Units/1F フィルタ/国別集計 OK")

    # --- (3) 複数日結合の再集計 (_aggregate) ---
    df_a = pd.DataFrame(
        {"date": pd.to_datetime(["2026-06-25", "2026-06-26"]), "downloads": [120, 30]}
    )
    df_b = pd.DataFrame({"date": pd.to_datetime(["2026-06-26"]), "downloads": [40]})
    c_a = pd.DataFrame({"country": ["JP", "US"], "downloads": [100, 20]})
    c_b = pd.DataFrame({"country": ["JP"], "downloads": [40]})
    agg = _aggregate([df_a, df_b], [c_a, c_b], "JPY")
    merged_0626 = int(
        agg["daily"].loc[
            agg["daily"]["date"] == pd.Timestamp("2026-06-26"), "downloads"
        ].iloc[0]
    )
    jp_agg = int(
        agg["country"].loc[agg["country"]["country"] == "JP", "downloads"].iloc[0]
    )
    print("\n[3] 複数日結合の再集計 (_aggregate)")
    print(f"  06/26 統合DL: {merged_0626} (期待 70 = 30+40)")
    print(f"  総DL: {agg['total_downloads']} (期待 190)  JP合算: {jp_agg} (期待 140)")
    assert merged_0626 == 70, "同日結合の合算が不正"
    assert agg["total_downloads"] == 190, "総DL 合算が不正"
    assert jp_agg == 140, "国別合算が不正"
    print("  -> 複数日/国別の結合再集計 OK")

    # --- (a) salesReports params に filter[version] が含まれないこと ---
    params = _build_sales_params(date(2026, 6, 25), "VENDOR-999")
    print("\n[a] salesReports params の検証")
    print(f"  params キー: {sorted(params.keys())}")
    assert "filter[version]" not in params, (
        "filter[version] が残存(SALES/SUMMARY では 400 になる致命バグ)"
    )
    assert params["filter[reportType]"] == "SALES", "reportType が SALES でない"
    assert params["filter[reportSubType]"] == "SUMMARY", "reportSubType が SUMMARY でない"
    assert params["filter[frequency]"] == "DAILY", "frequency が DAILY でない"
    assert params["filter[vendorNumber]"] == "VENDOR-999", "vendorNumber 不一致"
    assert params["filter[reportDate]"] == "2026-06-25", "reportDate 不一致"
    print("  -> filter[version] 無し / 必須キー正常 OK")

    # --- (c) 401 応答で明示的な認証エラー・鍵/トークンを漏らさない ---
    from unittest import mock

    print("\n[c] 401 応答のハンドリング (モック)")
    secret_p8 = "-----BEGIN PRIVATE KEY-----\nSUPERSECRET_KEY_BODY\n-----END PRIVATE KEY-----"
    fake_resp = mock.Mock()
    fake_resp.status_code = 401
    fake_session = mock.Mock()
    fake_session.get.return_value = fake_resp
    creds_401 = {
        "issuer_id": "ISSUER-123",
        "key_id": "KEY-ABC",
        "p8": pem,  # 署名可能な実 PEM(JWT 生成を通すため)
        "vendor_number": "VENDOR-999",
    }
    raised = False
    try:
        fetch_report_bytes(date(2026, 6, 25), creds_401, session=fake_session)
    except ASCAuthError as exc:
        raised = True
        msg = str(exc)
        print(f"  ASCAuthError: {msg}")
        # 生成した JWT がメッセージに漏れていないこと
        token_leaked = generate_jwt(
            creds_401["issuer_id"], creds_401["key_id"], creds_401["p8"]
        )
        assert token_leaked not in msg, "トークンがエラーメッセージに漏洩"
        assert "PRIVATE KEY" not in msg, "鍵らしき文字列がメッセージに漏洩"
        assert "BEGIN" not in msg, "PEM ヘッダがメッセージに漏洩"
        assert secret_p8 not in msg, "秘密鍵本文がメッセージに漏洩"
        assert "401" in msg, "HTTP ステータスがメッセージに無い"
        assert ("認証" in msg) or ("Issuer" in msg), "認証確認の案内が無い"
    assert raised, "401 なのに ASCAuthError が送出されなかった"
    fake_resp.raise_for_status.assert_not_called()  # スタックトレース経由でない
    print("  -> 401 は明示エラーに変換・鍵/トークン非漏洩 OK")

    # --- (d) cache: save→load 往復で daily+country+total が復元される ---
    import tempfile

    print("\n[d] キャッシュ save→load 往復")
    with tempfile.TemporaryDirectory() as tmp:
        cache_p = os.path.join(tmp, "asc_downloads.parquet")
        meta_p = os.path.join(tmp, "asc_downloads.json")
        result_to_cache = {
            "daily": pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-06-25", "2026-06-26"]),
                    "downloads": [120, 70],
                }
            ),
            "country": pd.DataFrame(
                {"country": ["JP", "US"], "downloads": [150, 40]}
            ),
            "currency": "JPY",
            "total_downloads": 190,
            "meta": {"fetched_at": "2026-06-27 09:00:00"},
        }
        save_cache(result_to_cache, path=cache_p, meta_path=meta_p)
        loaded = load_cache(path=cache_p, meta_path=meta_p)
        assert loaded is not None, "load_cache が None を返した"
        assert int(loaded["total_downloads"]) == 190, "total_downloads が復元されない"
        assert not loaded["daily"].empty, "daily が復元されない"
        assert int(loaded["daily"]["downloads"].sum()) == 190, "daily 合計が不一致"
        jp_c = int(
            loaded["country"].loc[
                loaded["country"]["country"] == "JP", "downloads"
            ].iloc[0]
        )
        assert jp_c == 150, f"country(JP) が復元されない: {jp_c}"
        assert loaded["currency"] == "JPY", "currency が復元されない"
        assert loaded["meta"].get("fetched_at") == "2026-06-27 09:00:00", (
            "meta.fetched_at が復元されない"
        )
        print(
            f"  復元: total={loaded['total_downloads']} "
            f"daily行={len(loaded['daily'])} JP={jp_c} "
            f"currency={loaded['currency']}"
        )
    print("  -> daily+country+total+currency+meta 復元 OK(二重計上なし)")

    print("\n" + "=" * 60)
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("Usage: python asc_api.py --selftest")
