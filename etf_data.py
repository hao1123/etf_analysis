"""免费 ETF 行情源与统一数据接口。

历史行情优先直连东方财富，失败后回退腾讯财经和 AkShare。
实时行情优先腾讯财经，并用东方财富快照补充 IOPV、折溢价和全市场 ETF 列表。
同花顺源用于 ETF 名称、净值和基金元数据补充。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import requests

warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API.*",
    category=UserWarning,
)
try:
    import akshare as ak
except ImportError:
    ak = None


LOGGER = logging.getLogger("etf.data")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def plain_code(code: str) -> str:
    return code.split(".", 1)[0]


def exchange_code(code: str) -> str:
    raw = plain_code(code)
    if code.endswith(".XSHG"):
        return f"sh{raw}"
    if code.endswith(".XSHE"):
        return f"sz{raw}"
    return f"sh{raw}" if raw.startswith(("5", "6")) else f"sz{raw}"


def jq_code(code: str) -> str:
    raw = plain_code(code)
    return f"{raw}.XSHG" if raw.startswith(("5", "6")) else f"{raw}.XSHE"


@dataclass
class Quote:
    code: str
    name: str
    price: float
    previous_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    timestamp: datetime | None
    source: str
    premium_rate: float | None = None

    @property
    def change_pct(self) -> float | None:
        if self.previous_close <= 0:
            return None
        return (self.price / self.previous_close - 1) * 100


class DataSourceError(RuntimeError):
    pass


class MarketDataHub:
    def __init__(self, cache_dir: Path, request_timeout: int = 12) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_timeout = request_timeout
        self.session = requests.Session()
        self.session.trust_env = os.getenv("ETF_USE_SYSTEM_PROXY", "0") == "1"
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.source_events: list[str] = []
        self._em_spot: pd.DataFrame | None = None
        self._ths_spot: pd.DataFrame | None = None
        self._event_lock = threading.Lock()
        self._intraday_lock = threading.Lock()
        self._last_intraday_request = 0.0
        self._history_lock = threading.Lock()
        self._last_history_request = 0.0
        self._provider_failures: dict[str, int] = {}
        self._disabled_until: dict[str, float] = {}

    def _event(self, text: str) -> None:
        with self._event_lock:
            if text not in self.source_events:
                LOGGER.info(text)
                self.source_events.append(text)

    def source_summary(self) -> str:
        if not self.source_events:
            return "本次尚未访问行情源"
        return "\n".join(dict.fromkeys(self.source_events))

    def _retry(self, label: str, fn: Callable[[], pd.DataFrame], attempts: int = 2) -> pd.DataFrame:
        error: Exception | None = None
        for index in range(attempts):
            try:
                result = fn()
                if result is None or result.empty:
                    raise DataSourceError("返回空数据")
                self._event(f"{label}: 成功")
                return result
            except Exception as exc:
                error = exc
                if index + 1 < attempts:
                    time.sleep(0.6 * (index + 1))
        self._event(f"{label}: 失败({type(error).__name__})")
        raise DataSourceError(f"{label}失败: {error}") from error

    def get_history(self, code: str, count: int = 70) -> pd.DataFrame:
        cache = self.cache_dir / f"history_{plain_code(code)}.csv"
        cached = self._read_history_cache(cache)
        required_cache_rows = min(count, 30)
        if (
            cached is not None
            and len(cached) >= required_cache_rows
            and datetime.fromtimestamp(cache.stat().st_mtime).date() == date.today()
        ):
            self._event("当日历史缓存: 成功")
            return cached.tail(count).reset_index(drop=True)
        providers: list[tuple[str, Callable[[], pd.DataFrame], str | None]] = [
            ("东方财富直连历史", lambda: self._history_eastmoney(code, count), "em_history"),
            ("新浪财经历史", lambda: self._history_sina(code, count), None),
            ("腾讯财经历史", lambda: self._history_tencent(code, count), None),
            ("AkShare/东方财富历史", lambda: self._history_akshare(code, count), "ak_history"),
        ]
        errors: list[str] = []
        for label, provider, circuit_key in providers:
            if circuit_key and time.monotonic() < self._disabled_until.get(circuit_key, 0):
                continue
            try:
                frame = self._retry(label, provider, attempts=3)
                frame = normalize_history(frame)
                frame.to_csv(cache, index=False)
                if circuit_key:
                    self._provider_failures[circuit_key] = 0
                return frame.tail(count).reset_index(drop=True)
            except Exception as exc:
                errors.append(str(exc))
                if circuit_key:
                    failures = self._provider_failures.get(circuit_key, 0) + 1
                    self._provider_failures[circuit_key] = failures
                    if failures >= 5:
                        self._disabled_until[circuit_key] = time.monotonic() + 300
        cached = self._read_history_cache(cache)
        if cached is not None:
            self._event("本地历史缓存: 成功")
            return cached.tail(count).reset_index(drop=True)
        raise DataSourceError(f"{code} 无可用历史数据；" + "；".join(errors))

    def get_histories(
        self,
        codes: Iterable[str],
        count: int = 70,
        workers: int = 10,
    ) -> dict[str, pd.DataFrame]:
        unique_codes = list(dict.fromkeys(codes))
        results: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(self.get_history, code, count): code for code in unique_codes}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    results[code] = future.result()
                except Exception as exc:
                    LOGGER.warning("%s 历史行情不可用: %s", code, exc)
        return results

    def get_intraday_histories(
        self,
        codes: Iterable[str],
        target_day: date,
        workers: int = 1,
    ) -> dict[str, pd.DataFrame]:
        unique_codes = list(dict.fromkeys(codes))
        results: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(self.get_intraday_history, code, target_day): code
                for code in unique_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    results[code] = future.result()
                except Exception as exc:
                    LOGGER.warning(
                        "%s %s 分钟行情不可用: %s", code, target_day, exc
                    )
        return results

    def get_intraday_history(
        self, code: str, target_day: date
    ) -> pd.DataFrame:
        cache = self.cache_dir / (
            f"minute_{plain_code(code)}_{target_day.isoformat()}.csv"
        )
        if cache.exists():
            try:
                frame = normalize_intraday(pd.read_csv(cache))
                if not frame.empty:
                    self._event("历史分钟缓存: 成功")
                    return frame
            except Exception:
                pass
        frame = None
        if time.monotonic() >= self._disabled_until.get("em_intraday", 0):
            try:
                frame = self._retry(
                    "东方财富历史分钟",
                    lambda: self._intraday_eastmoney(code),
                    attempts=1,
                )
                self._provider_failures["em_intraday"] = 0
            except Exception:
                failures = self._provider_failures.get("em_intraday", 0) + 1
                self._provider_failures["em_intraday"] = failures
                if failures >= 2:
                    self._disabled_until["em_intraday"] = (
                        time.monotonic() + 300
                    )
        if frame is None:
            frame = self._retry(
                "新浪历史分钟",
                lambda: self._intraday_sina(code),
                attempts=2,
            )
        frame = normalize_intraday(frame)
        for frame_day, day_frame in frame.groupby(frame["time"].dt.date):
            day_cache = self.cache_dir / (
                f"minute_{plain_code(code)}_{frame_day.isoformat()}.csv"
            )
            day_frame.to_csv(day_cache, index=False)
        target = frame[frame["time"].dt.date == target_day].copy()
        if target.empty:
            raise DataSourceError(f"分钟窗口不含 {target_day.isoformat()}")
        return target.reset_index(drop=True)

    def get_quotes(self, codes: Iterable[str]) -> dict[str, Quote]:
        unique_codes = list(dict.fromkeys(codes))
        if not unique_codes:
            return {}
        try:
            quotes = self._quotes_tencent(unique_codes)
            if quotes:
                self._event("腾讯财经实时行情: 成功")
                return quotes
        except Exception as exc:
            self._event(f"腾讯财经实时行情: 失败({type(exc).__name__})")
        frame = self.get_eastmoney_spot()
        return self._quotes_from_eastmoney(frame, unique_codes)

    def get_eastmoney_spot(self, force: bool = False) -> pd.DataFrame:
        if self._em_spot is not None and not force:
            return self._em_spot
        cache = self.cache_dir / "eastmoney_etf_spot.csv"
        try:
            if ak is None:
                raise DataSourceError("未安装 AkShare")
            frame = self._retry("AkShare/东方财富ETF快照", ak.fund_etf_spot_em, attempts=2)
            frame["代码"] = frame["代码"].astype(str).str.zfill(6)
            frame.to_csv(cache, index=False)
            self._em_spot = frame
            return frame
        except Exception:
            try:
                frame = self._retry(
                    "东方财富ETF快照直连", self._eastmoney_spot_direct, attempts=2
                )
                frame.to_csv(cache, index=False)
                self._em_spot = frame
                return frame
            except Exception:
                pass
            try:
                frame = self._retry(
                    "同花顺列表/腾讯财经全市场快照",
                    self._snapshot_from_tencent_ths,
                    attempts=1,
                )
                frame.to_csv(cache, index=False)
                self._em_spot = frame
                return frame
            except Exception:
                pass
            if cache.exists():
                frame = pd.read_csv(cache, dtype={"代码": str})
                frame["代码"] = frame["代码"].str.zfill(6)
                self._event("东方财富ETF快照: 使用本地缓存")
                self._em_spot = frame
                return frame
            raise

    def get_ths_spot(self, force: bool = False) -> pd.DataFrame:
        if self._ths_spot is not None and not force:
            return self._ths_spot
        cache = self.cache_dir / "ths_etf_spot.csv"
        try:
            if ak is None:
                raise DataSourceError("未安装 AkShare")
            frame = self._retry("AkShare/同花顺ETF净值", ak.fund_etf_spot_ths, attempts=2)
            frame["基金代码"] = frame["基金代码"].astype(str).str.zfill(6)
            frame.to_csv(cache, index=False)
            self._ths_spot = frame
            return frame
        except Exception:
            if cache.exists():
                frame = pd.read_csv(cache, dtype={"基金代码": str})
                frame["基金代码"] = frame["基金代码"].str.zfill(6)
                self._event("同花顺ETF净值: 使用本地缓存")
                self._ths_spot = frame
                return frame
            raise

    def get_name_map(self) -> dict[str, str]:
        names: dict[str, str] = {}
        try:
            ths = self.get_ths_spot()
            names.update({
                str(code).zfill(6): str(name)
                for code, name in zip(ths["基金代码"], ths["基金名称"])
                if pd.notna(name)
            })
        except Exception:
            pass
        try:
            em = self.get_eastmoney_spot()
            names.update(dict(zip(em["代码"], em["名称"])))
        except Exception:
            pass
        return names

    def get_trade_dates(self) -> set[date]:
        cache = self.cache_dir / "trade_dates.csv"
        try:
            frame = self._retry(
                "公开节假日交易日历", self._trade_dates_from_holidays, attempts=2
            )
            frame.to_csv(cache, index=False)
        except Exception:
            try:
                if ak is None:
                    raise DataSourceError("未安装 AkShare")
                frame = self._retry(
                    "AkShare交易日历", ak.tool_trade_date_hist_sina, attempts=1
                )
                frame.to_csv(cache, index=False)
            except Exception:
                if not cache.exists():
                    return set()
                frame = pd.read_csv(cache)
                self._event("交易日历: 使用本地缓存")
        column = "trade_date" if "trade_date" in frame.columns else frame.columns[0]
        return set(pd.to_datetime(frame[column]).dt.date)

    def _history_akshare(self, code: str, count: int) -> pd.DataFrame:
        if ak is None:
            raise DataSourceError("未安装 AkShare")
        start = (date.today() - timedelta(days=max(120, count * 3))).strftime("%Y%m%d")
        end = date.today().strftime("%Y%m%d")
        return ak.fund_etf_hist_em(
            symbol=plain_code(code),
            period="daily",
            start_date=start,
            end_date=end,
            adjust="",
        )

    def _eastmoney_spot_direct(self) -> pd.DataFrame:
        fields = (
            "f2,f3,f4,f5,f6,f7,f8,f10,f12,f13,f14,f15,f16,f17,f18,"
            "f20,f21,f30,f31,f32,f33,f34,f35,f38,f124,f297,f402,f441"
        )
        params = {
            "pn": "1",
            "pz": "500",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
            "fields": fields,
        }
        rows: list[dict[str, object]] = []
        total = None
        page = 1
        while total is None or len(rows) < total:
            params["pn"] = str(page)
            response = self.session.get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                params=params,
                timeout=self.request_timeout,
                headers={"Referer": "https://quote.eastmoney.com/"},
            )
            response.raise_for_status()
            payload = response.json().get("data") or {}
            batch = payload.get("diff") or []
            if not batch:
                break
            rows.extend(batch)
            total = int(payload.get("total") or len(rows))
            page += 1
        if not rows:
            raise DataSourceError("东方财富ETF快照为空")
        frame = pd.DataFrame(rows).rename(columns={
            "f12": "代码",
            "f14": "名称",
            "f2": "最新价",
            "f441": "IOPV实时估值",
            "f402": "基金折价率",
            "f4": "涨跌额",
            "f3": "涨跌幅",
            "f5": "成交量",
            "f6": "成交额",
            "f17": "开盘价",
            "f15": "最高价",
            "f16": "最低价",
            "f18": "昨收",
            "f7": "振幅",
            "f8": "换手率",
            "f10": "量比",
            "f33": "委比",
            "f34": "外盘",
            "f35": "内盘",
            "f30": "现手",
            "f31": "买一",
            "f32": "卖一",
            "f38": "最新份额",
            "f21": "流通市值",
            "f20": "总市值",
            "f297": "数据日期",
            "f124": "更新时间",
        })
        for column in [
            "最新价", "IOPV实时估值", "基金折价率", "涨跌额", "涨跌幅",
            "成交量", "成交额", "开盘价", "最高价", "最低价", "昨收",
            "振幅", "换手率", "量比", "委比", "外盘", "内盘", "现手",
            "买一", "卖一", "最新份额", "流通市值", "总市值",
        ]:
            frame[column] = pd.to_numeric(frame.get(column), errors="coerce")
        frame["代码"] = frame["代码"].astype(str).str.zfill(6)
        frame["数据日期"] = pd.to_datetime(
            frame["数据日期"], format="%Y%m%d", errors="coerce"
        )
        frame["更新时间"] = (
            pd.to_datetime(frame["更新时间"], unit="s", errors="coerce")
            .dt.tz_localize("UTC")
            .dt.tz_convert("Asia/Shanghai")
        )
        return frame

    def _snapshot_from_tencent_ths(self) -> pd.DataFrame:
        ths = self.get_ths_spot()
        codes = [jq_code(str(code).zfill(6)) for code in ths["基金代码"]]
        quotes = self._quotes_tencent(codes)
        if not quotes:
            raise DataSourceError("腾讯财经未返回ETF行情")
        rows = []
        for quote in quotes.values():
            rows.append({
                "代码": plain_code(quote.code),
                "名称": quote.name,
                "最新价": quote.price,
                "IOPV实时估值": pd.NA,
                "基金折价率": pd.NA,
                "涨跌额": quote.price - quote.previous_close,
                "涨跌幅": quote.change_pct,
                "成交量": quote.volume,
                "成交额": quote.amount,
                "开盘价": quote.open,
                "最高价": quote.high,
                "最低价": quote.low,
                "昨收": quote.previous_close,
                "数据日期": quote.timestamp.date() if quote.timestamp else pd.NaT,
                "更新时间": quote.timestamp,
            })
        return pd.DataFrame(rows)

    def _trade_dates_from_holidays(self) -> pd.DataFrame:
        years = range(date.today().year - 1, date.today().year + 2)
        holidays: set[date] = set()
        for year in years:
            response = self.session.get(
                f"https://timor.tech/api/holiday/year/{year}",
                timeout=self.request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise DataSourceError(f"{year} 节假日接口返回异常")
            for item in (payload.get("holiday") or {}).values():
                if item.get("holiday") is True:
                    holidays.add(date.fromisoformat(item["date"]))
        start = date(date.today().year - 1, 1, 1)
        end = date(date.today().year + 1, 12, 31)
        days = pd.date_range(start, end, freq="D").date
        trade_dates = [
            day for day in days if day.weekday() < 5 and day not in holidays
        ]
        return pd.DataFrame({"trade_date": trade_dates})

    def _history_eastmoney(self, code: str, count: int) -> pd.DataFrame:
        secid = ("1." if exchange_code(code).startswith("sh") else "0.") + plain_code(code)
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": secid,
            "klt": "101",
            "fqt": "0",
            "lmt": str(count),
            "end": "20500101",
            "iscca": "1",
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
        with self._history_lock:
            wait_seconds = 0.15 - (time.monotonic() - self._last_history_request)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            response = self.session.get(
                url,
                params=params,
                timeout=self.request_timeout,
                headers={"Referer": "https://quote.eastmoney.com/"},
            )
            self._last_history_request = time.monotonic()
        response.raise_for_status()
        data = response.json().get("data") or {}
        rows = [item.split(",") for item in data.get("klines") or []]
        if not rows:
            raise DataSourceError("东方财富无K线")
        columns = ["date", "open", "close", "high", "low", "volume", "amount",
                   "amplitude", "change_pct", "change", "turnover"]
        return pd.DataFrame(rows, columns=columns)

    def _history_tencent(self, code: str, count: int) -> pd.DataFrame:
        symbol = exchange_code(code)
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = {"param": f"{symbol},day,,,{count},qfq"}
        response = self.session.get(
            url,
            params=params,
            timeout=self.request_timeout,
            headers={"Referer": "https://gu.qq.com/"},
        )
        response.raise_for_status()
        payload = response.json()
        data = (payload.get("data") or {}).get(symbol) or {}
        rows = data.get("qfqday") or data.get("day") or []
        if not rows:
            raise DataSourceError("腾讯无K线")
        width = max(len(row) for row in rows)
        columns = ["date", "open", "close", "high", "low", "volume", "amount"][:width]
        return pd.DataFrame([row[:width] for row in rows], columns=columns)

    def _history_sina(self, code: str, count: int) -> pd.DataFrame:
        symbol = exchange_code(code)
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {
            "symbol": symbol,
            "scale": "240",
            "datalen": str(count),
            "ma": "no",
        }
        response = self.session.get(
            url,
            params=params,
            timeout=self.request_timeout,
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        response.raise_for_status()
        text = response.text.strip()
        if not text.startswith("["):
            match = re.search(r"\((\[.*\])\)", text, re.DOTALL)
            if not match:
                raise DataSourceError("新浪历史格式异常")
            text = match.group(1)
        rows_data = json.loads(text)
        if not rows_data:
            raise DataSourceError("新浪无K线")
        frame = pd.DataFrame(rows_data)
        if "day" in frame.columns:
            frame = frame.rename(columns={"day": "date"})
        if "date" not in frame.columns:
            raise DataSourceError("新浪历史缺日期列")
        frame["amount"] = (
            pd.to_numeric(frame.get("volume", 0), errors="coerce")
            * pd.to_numeric(frame.get("close", 0), errors="coerce")
        )
        cols = ["date", "open", "close", "high", "low", "volume", "amount"]
        for col in cols:
            if col not in frame.columns:
                frame[col] = None
        return frame[cols]

    def _intraday_eastmoney(self, code: str) -> pd.DataFrame:
        secid = ("1." if exchange_code(code).startswith("sh") else "0.") + plain_code(code)
        with self._intraday_lock:
            wait_seconds = 0.18 - (
                time.monotonic() - self._last_intraday_request
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            response = self.session.get(
                "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
                params={
                    "secid": secid,
                    "ndays": "5",
                    "iscr": "0",
                    "iscca": "0",
                    "ut": "7eea3edcaed734bea9cbfc24409ed989",
                    "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                },
                timeout=self.request_timeout,
                headers={"Referer": "https://quote.eastmoney.com/"},
            )
            self._last_intraday_request = time.monotonic()
        response.raise_for_status()
        data = response.json().get("data") or {}
        rows = [item.split(",") for item in data.get("trends") or []]
        if not rows:
            raise DataSourceError("东方财富无分钟行情")
        frame = pd.DataFrame(
            rows,
            columns=[
                "time", "open", "close", "high", "low",
                "volume", "amount", "average",
            ],
        )
        frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
        frame["source"] = "东方财富历史1分钟"
        return frame

    def _intraday_sina(self, code: str) -> pd.DataFrame:
        response = self.session.get(
            "https://quotes.sina.cn/cn/api/json_v2.php/"
            "CN_MarketDataService.getKLineData",
            params={
                "symbol": exchange_code(code),
                "scale": "1",
                "ma": "no",
                "datalen": "1023",
            },
            timeout=self.request_timeout,
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise DataSourceError("新浪无分钟行情")
        frame = pd.DataFrame(rows).rename(columns={"day": "time"})
        frame["volume"] = (
            pd.to_numeric(frame["volume"], errors="coerce") / 100
        )
        frame["source"] = "新浪历史1分钟"
        frame["average"] = pd.NA
        return frame

    def _quotes_tencent(self, codes: list[str]) -> dict[str, Quote]:
        result: dict[str, Quote] = {}
        for start in range(0, len(codes), 60):
            chunk = codes[start:start + 60]
            symbols = ",".join(exchange_code(code) for code in chunk)
            response = self.session.get(
                "https://qt.gtimg.cn/q=" + symbols,
                timeout=self.request_timeout,
                headers={"Referer": "https://gu.qq.com/"},
            )
            response.raise_for_status()
            text = response.content.decode("gbk", errors="replace")
            for line in text.splitlines():
                match = re.match(r'v_([^=]+)="(.*)";', line.strip())
                if not match:
                    continue
                fields = match.group(2).split("~")
                if len(fields) < 38 or not fields[2]:
                    continue
                raw = fields[2].zfill(6)
                timestamp = None
                try:
                    timestamp = datetime.strptime(fields[30][:14], "%Y%m%d%H%M%S")
                except (ValueError, IndexError):
                    pass
                result[jq_code(raw)] = Quote(
                    code=jq_code(raw),
                    name=fields[1],
                    price=to_float(fields[3]),
                    previous_close=to_float(fields[4]),
                    open=to_float(fields[5]),
                    high=to_float(fields[33]),
                    low=to_float(fields[34]),
                    volume=to_float(fields[6]),
                    amount=to_float(fields[37]) * 10_000,
                    timestamp=timestamp,
                    source="腾讯财经",
                )
        return result

    def _quotes_from_eastmoney(self, frame: pd.DataFrame, codes: list[str]) -> dict[str, Quote]:
        wanted = {plain_code(code) for code in codes}
        subset = frame[frame["代码"].isin(wanted)]
        result: dict[str, Quote] = {}
        for _, row in subset.iterrows():
            code = jq_code(str(row["代码"]))
            timestamp = None
            raw_time = row.get("更新时间")
            if pd.notna(raw_time):
                timestamp = pd.to_datetime(raw_time).to_pydatetime()
            result[code] = Quote(
                code=code,
                name=str(row.get("名称", code)),
                price=to_float(row.get("最新价")),
                previous_close=to_float(row.get("昨收")),
                open=to_float(row.get("开盘价")),
                high=to_float(row.get("最高价")),
                low=to_float(row.get("最低价")),
                volume=to_float(row.get("成交量")),
                amount=to_float(row.get("成交额")),
                timestamp=timestamp,
                source="AkShare/东方财富",
                premium_rate=to_optional_float(row.get("基金折价率")),
            )
        self._event("AkShare/东方财富实时行情: 成功")
        return result

    @staticmethod
    def _read_history_cache(path: Path) -> pd.DataFrame | None:
        if not path.exists():
            return None
        try:
            frame = normalize_history(pd.read_csv(path))
            if frame.empty:
                return None
            return frame
        except Exception:
            return None


def normalize_history(frame: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
    }
    result = frame.rename(columns=rename).copy()
    required = ["date", "open", "close", "high", "low", "volume"]
    missing = [column for column in required if column not in result.columns]
    if missing:
        raise DataSourceError(f"历史行情缺少字段: {missing}")
    if "amount" not in result.columns:
        result["amount"] = pd.NA
    result["date"] = pd.to_datetime(result["date"]).dt.date
    for column in ["open", "close", "high", "low", "volume", "amount"]:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["date", "close"]).drop_duplicates("date")
    return result.sort_values("date").reset_index(drop=True)


def normalize_intraday(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "时间" in result.columns:
        result = result.rename(columns={
            "时间": "time",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "均价": "average",
        })
    required = ["time", "close", "high", "low", "volume", "amount"]
    missing = [column for column in required if column not in result.columns]
    if missing:
        raise DataSourceError(f"分钟行情缺少字段: {missing}")
    result["time"] = pd.to_datetime(result["time"], errors="coerce")
    for column in ["open", "close", "high", "low", "volume", "amount", "average"]:
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["time", "close"]).drop_duplicates("time")
    return result.sort_values("time").reset_index(drop=True)


def to_float(value: object) -> float:
    try:
        number = float(value)
        return 0.0 if pd.isna(number) else number
    except (TypeError, ValueError):
        return 0.0


def to_optional_float(value: object) -> float | None:
    try:
        number = float(value)
        return None if pd.isna(number) else number
    except (TypeError, ValueError):
        return None
