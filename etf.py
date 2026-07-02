#!/usr/bin/env python3
"""本地 ETF 动量策略与邮件提醒。

该程序不连接券商、不自动下单。它在原聚宽策略的时间点读取免费行情，
根据本地持仓生成研究信号，并通过 SMTP 发送提醒。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import smtplib
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as datetime_time
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import requests

from etf_config import StrategyConfig
from etf_data import MarketDataHub, Quote, jq_code, plain_code, to_float


BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("etf")

INDEXES = {
    "沪深300": "000300.XSHG",
    "中小综指": "399101.XSHE",
    "创业板指": "399006.XSHE",
    "中证A500": "000510.XSHG",
}

FUND_COMPANIES = sorted({
    "易方达", "广发", "华夏", "华安", "嘉实", "富国", "招商", "鹏华",
    "南方", "汇添富", "国泰", "平安", "银华", "天弘", "建信", "工银",
    "华泰柏瑞", "博时", "景顺长城", "景顺", "华宝", "申万菱信", "万家",
    "中欧", "永赢", "大成", "海富通", "摩根", "中信", "中银",
}, key=len, reverse=True)

NOISE_WORDS = sorted({
    "ETF基金", "ETF联接", "ETF", "LOF基金", "LOF", "基金", "指数", "联接",
    "增强", "龙头", "主题", "产业", "策略", "场内", "A类", "C类",
}, key=len, reverse=True)

EXCLUDED_DYNAMIC_KEYWORDS = sorted({
    "300", "500", "1000", "2000", "800", "A50", "A100", "A500",
    "沪深", "中证", "上证", "深证", "深成", "MSCI", "ESG",
    "短融", "可转债", "转债", "利率债", "国债", "地债", "政金债",
    "国开债", "信用债", "企业债", "公司债", "城投债", "美元债", "债",
    "货币", "现金", "快线", "快钱", "自由现金流",
}, key=len, reverse=True)


@dataclass
class Position:
    code: str
    amount: int
    avg_cost: float
    name: str = ""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    @staticmethod
    def default() -> dict[str, Any]:
        return {
            "version": 1,
            "positions": {},
            "weak": {"active": False, "start_date": None, "days": 0},
            "liquidity_threshold": None,
            "snapshot_liquid_codes": [],
            "filtered_pool": [],
            "name_map": {},
            "last_jobs": {},
            "stop_alerts": {},
            "daily_records": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("状态文件损坏，使用空状态: %s", self.path)
            return self.default()
        default = self.default()
        default.update(data)
        return default

    def save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.path)

    def positions(self) -> list[Position]:
        result = []
        for raw in self.data.get("positions", {}).values():
            try:
                result.append(Position(
                    code=jq_code(raw["code"]),
                    amount=int(raw["amount"]),
                    avg_cost=float(raw["avg_cost"]),
                    name=str(raw.get("name", "")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def set_position(self, position: Position) -> None:
        self.data["positions"][plain_code(position.code)] = asdict(position)
        self.save()

    def remove_position(self, code: str) -> None:
        self.data["positions"].pop(plain_code(code), None)
        self.save()

    def job_done(self, job: str, day: date) -> bool:
        return self.data.get("last_jobs", {}).get(job) == day.isoformat()

    def mark_job(self, job: str, day: date) -> None:
        self.data.setdefault("last_jobs", {})[job] = day.isoformat()
        self.save()


class Mailer:
    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "465"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.sender = os.getenv("MAIL_FROM", self.user)
        self.recipients = [
            item.strip()
            for item in os.getenv("MAIL_TO", "").replace(";", ",").split(",")
            if item.strip()
        ]
        self.use_ssl = env_bool("SMTP_SSL", self.port == 465)
        self.starttls = env_bool("SMTP_STARTTLS", self.port == 587)

    def configuration_errors(self) -> list[str]:
        fields = {
            "SMTP_HOST": self.host,
            "SMTP_USER": self.user,
            "SMTP_PASSWORD": self.password,
            "MAIL_FROM": self.sender,
            "MAIL_TO": self.recipients,
        }
        return [name for name, value in fields.items() if not value]

    def send(self, subject: str, body: str, dry_run: bool = False) -> None:
        if dry_run:
            print(f"\n===== {subject} =====\n{body}\n")
            return
        errors = self.configuration_errors()
        if errors:
            LOGGER.warning("邮件配置不完整，跳过邮件发送: %s", ", ".join(errors))
            return
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(body)
        if self.use_ssl:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.host, self.port, timeout=20
            )
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=20)
        with client:
            client.ehlo()
            if self.starttls and not self.use_ssl:
                client.starttls()
                client.ehlo()
            client.login(self.user, self.password)
            client.send_message(message)
        LOGGER.info("邮件已发送: %s", subject)


class FeishuNotifier:
    def __init__(self) -> None:
        self.webhook = os.getenv("FEISHU_WEBHOOK", "").strip()
        self.secret = os.getenv("FEISHU_SECRET", "").strip()

    def send(self, subject: str, body: str, dry_run: bool = False) -> None:
        if not self.webhook:
            return
        text = f"{subject}\n\n{body}"
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = self._sign(timestamp)
        if dry_run:
            print(f"\n===== [飞书] {subject} =====\n{text[:500]}\n")
            return
        response = requests.post(self.webhook, json=payload, timeout=15)
        response.raise_for_status()
        LOGGER.info("飞书通知已发送: %s", subject)

    def _sign(self, timestamp: str) -> str:
        import base64
        import hashlib
        import hmac

        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")


class LocalETFStrategy:
    def __init__(
        self,
        config: StrategyConfig,
        data: MarketDataHub,
        state: StateStore,
        mailer: Mailer,
    ) -> None:
        self.config = config
        self.data = data
        self.state = state
        self.mailer = mailer
        self.feishu = FeishuNotifier()

    def _notify(self, subject: str, body: str, dry_run: bool = False) -> None:
        self.mailer.send(subject, body, dry_run)
        self.feishu.send(subject, body, dry_run)

    def morning(self, now: datetime, dry_run: bool = False) -> str:
        name_map = self.data.get_name_map()
        self.state.data["name_map"] = name_map
        threshold, snapshot_date, total_amount = self._liquidity_threshold()
        self.state.data["liquidity_threshold"] = threshold
        try:
            snapshot = self.data.get_eastmoney_spot()
            amounts = pd.to_numeric(snapshot["成交额"], errors="coerce").fillna(0)
            self.state.data["snapshot_liquid_codes"] = [
                str(code).zfill(6)
                for code in snapshot.loc[amounts > threshold, "代码"].tolist()
            ]
        except Exception:
            pass
        self.state.save()

        positions = self.state.positions()
        quotes = self.data.get_quotes([item.code for item in positions])
        lines = [
            f"运行时间：{now:%Y-%m-%d %H:%M:%S}",
            "策略模式：本地提醒，不自动下单",
            "",
            "【持仓检查】",
        ]
        if not positions:
            lines.append("当前本地持仓为空。")
        for position in positions:
            quote = quotes.get(position.code)
            name = self._name(position.code, position.name)
            if quote:
                pnl = (quote.price / position.avg_cost - 1) * 100 if position.avg_cost else 0
                lines.append(
                    f"{name}({plain_code(position.code)}) {position.amount}份，"
                    f"成本 {position.avg_cost:.3f}，现价 {quote.price:.3f}，"
                    f"浮动 {pnl:+.2f}%"
                )
            else:
                lines.append(f"{name}({plain_code(position.code)}) 行情暂不可用")
        lines.extend([
            "",
            "【流动性门槛】",
            f"全市场ETF快照日期：{snapshot_date or '未知'}",
            f"快照总成交额：{total_amount / 1e8:.2f}亿元",
            f"筛选门槛：{threshold / 1e4:.0f}万元",
            "说明：本地免费源以最近一份全市场快照近似聚宽的3日均值门槛。",
            "",
            "【数据源】",
            self.data.source_summary(),
        ])
        body = "\n".join(lines)
        self._notify(f"[ETF策略 09:00] 晨间检查 {now:%Y-%m-%d}", body, dry_run)
        return body

    def weak_and_pool(self, now: datetime, dry_run: bool = False) -> str:
        index_lines, is_weak = self._update_weak_state(now.date())
        threshold = self.state.data.get("liquidity_threshold")
        if not threshold:
            threshold, _, _ = self._liquidity_threshold()
            self.state.data["liquidity_threshold"] = threshold

        if is_weak:
            histories = self.data.get_histories(self.config.global_pool, count=8)
            selected = self._filter_by_liquidity(
                self.config.global_pool, histories, float(threshold), now.date()
            )
            pool_note = f"走弱期仅保留全球/海外池，共 {len(selected)} 只"
        else:
            histories = self.data.get_histories(self.config.fixed_pool, count=8)
            fixed = self._filter_by_liquidity(
                self.config.fixed_pool, histories, float(threshold), now.date()
            )
            dynamic = self._dynamic_pool(float(threshold))
            selected = sorted(set(fixed + dynamic))
            pool_note = (
                f"正常期固定池 {len(fixed)} 只 + 动态池 {len(dynamic)} 只，"
                f"合并后 {len(selected)} 只"
            )
        self.state.data["filtered_pool"] = selected
        self.state.save()
        body = "\n".join([
            f"运行时间：{now:%Y-%m-%d %H:%M:%S}",
            "",
            "【大A状态】",
            *index_lines,
            f"最终状态：{'走弱期' if is_weak else '正常期'}",
            "",
            "【ETF池】",
            pool_note,
            f"流动性门槛：{float(threshold) / 1e4:.0f}万元",
            "",
            "【数据源】",
            self.data.source_summary(),
        ])
        self._notify(f"[ETF策略 09:40] 状态与ETF池 {now:%Y-%m-%d}", body, dry_run)
        return body

    def rebalance(self, now: datetime, dry_run: bool = False) -> str:
        pool = self.state.data.get("filtered_pool") or list(self.config.fixed_pool)
        histories = self.data.get_histories(pool, count=70)
        quotes = self.data.get_quotes(pool)
        metrics = []
        for code in pool:
            history = histories.get(code)
            quote = quotes.get(code)
            if history is None or quote is None or quote.price <= 0:
                continue
            metric = self._metrics(code, history, quote, now)
            if metric:
                metrics.append(metric)
        name_map = self.state.data.setdefault("name_map", {})
        name_map.update({
            plain_code(code): quote.name
            for code, quote in quotes.items()
            if quote.name
        })
        self.state.save()
        metrics.sort(key=lambda item: item["momentum_score"], reverse=True)
        filtered = [item for item in metrics if self._passes(item)]
        top_10 = filtered[:10]
        targets = self._select_targets(top_10)
        if not targets:
            defensive = self.data.get_quotes([self.config.defensive_etf])
            if self.config.defensive_etf in defensive:
                quote = defensive[self.config.defensive_etf]
                quotes.update(defensive)
                targets = [{
                    "etf": self.config.defensive_etf,
                    "etf_name": quote.name,
                    "momentum_score": 0.0,
                    "defensive": True,
                }]

        current = {position.code for position in self.state.positions()}
        target_codes = {item["etf"] for item in targets}
        sells = sorted(current - target_codes)
        buys = sorted(target_codes - current)
        holds = sorted(current & target_codes)

        holdings_amount = int(os.getenv("HOLDINGS_AMOUNT", "1000"))
        trades: list[str] = []
        if not dry_run:
            for code in sells:
                self.state.remove_position(code)
                q = quotes.get(code)
                trades.append(
                    f"卖出 {self._name(code)} @ {q.price:.3f}"
                    if q and q.price > 0
                    else f"卖出 {code}"
                )
            for item in targets:
                code = item["etf"]
                if code in buys:
                    q = quotes.get(code)
                    if q and q.price > 0:
                        self.state.set_position(
                            Position(
                                code, holdings_amount, q.price,
                                item.get("etf_name", ""),
                            )
                        )
                        trades.append(
                            f"买入 {item.get('etf_name', plain_code(code))}"
                            f"({plain_code(code)}) @ {q.price:.3f} x{holdings_amount}"
                        )

        lines = [
            f"运行时间：{now:%Y-%m-%d %H:%M:%S}",
            f"市场状态：{'走弱期' if self._is_weak else '正常期'}",
            f"参与计算：{len(metrics)} 只；通过全部过滤：{len(filtered)} 只",
            "",
            "【通过过滤的前10名】",
        ]
        if not top_10:
            lines.append("无符合全部条件的ETF。")
        for index, item in enumerate(top_10, 1):
            lines.append(
                f"{index}. {item['etf_name']}({plain_code(item['etf'])}) "
                f"得分 {item['momentum_score']:.4f}，R² {item['r_squared']:.3f}，"
                f"量比 {fmt_optional(item['volume_ratio'])}，"
                f"拉普拉斯斜率 {item['laplace_slope']:.4f}"
            )
        lines.extend(["", "【最终目标】"])
        for item in targets:
            suffix = "（防御）" if item.get("defensive") else ""
            lines.append(f"{item['etf_name']}({plain_code(item['etf'])}){suffix}")
        if not targets:
            lines.append("空仓观察")
        lines.extend([
            "",
            "【与本地持仓的差异】",
            f"卖出：{self._codes_text(sells)}",
            f"买入：{self._codes_text(buys)}",
            f"继续持有：{self._codes_text(holds)}",
        ])
        if trades:
            lines.append("")
            lines.append("【已自动更新的持仓】")
            lines.extend(f"  {trade}" for trade in trades)
            lines.append(f"已按当日行情自动更新持仓（份数 {holdings_amount}）。")
        elif not dry_run:
            lines.append("")
            lines.append("无调仓，持仓未变。")
        lines.extend([
            "",
            "【数据源】",
            self.data.source_summary(),
        ])
        body = "\n".join(lines)
        self._notify(f"[ETF策略 13:10] 调仓提醒 {now:%Y-%m-%d}", body, dry_run)
        return body

    def stop_loss(self, now: datetime, dry_run: bool = False) -> list[str]:
        positions = self.state.positions()
        if not positions:
            return []
        quotes = self.data.get_quotes([item.code for item in positions])
        sent = self.state.data.setdefault("stop_alerts", {})
        today = now.date().isoformat()
        alerts = []
        for position in positions:
            quote = quotes.get(position.code)
            if not quote or quote.price <= 0 or position.avg_cost <= 0:
                continue
            alert_key = f"{today}:{plain_code(position.code)}:fixed"
            stop_price = position.avg_cost * self.config.fixed_stop_loss_ratio
            if quote.price <= stop_price and alert_key not in sent:
                loss = (quote.price / position.avg_cost - 1) * 100
                if dry_run:
                    action_note = "模拟触发止损，未实际卖出（dry-run）。"
                else:
                    self.state.remove_position(position.code)
                    action_note = "已自动卖出并清除持仓。"
                body = "\n".join([
                    f"触发时间：{now:%Y-%m-%d %H:%M:%S}",
                    f"ETF：{self._name(position.code, position.name)}"
                    f"({plain_code(position.code)})",
                    f"持仓：{position.amount}份",
                    f"成本价：{position.avg_cost:.3f}",
                    f"当前价：{quote.price:.3f}",
                    f"固定止损线：{stop_price:.3f}",
                    f"相对成本跌幅：{loss:.2f}%",
                    f"行情源：{quote.source}",
                    "",
                    action_note,
                ])
                self._notify(
                    f"[ETF策略 止损] {quote.name} {loss:.2f}%", body, dry_run
                )
                sent[alert_key] = now.isoformat()
                alerts.append(body)
        sent_keys = sorted(sent)
        if len(sent_keys) > 300:
            self.state.data["stop_alerts"] = {
                key: sent[key] for key in sent_keys[-300:]
            }
        self.state.save()
        return alerts

    def reset(self, now: datetime, dry_run: bool = False) -> str:
        self.data._em_spot = None
        self.data._ths_spot = None
        message = f"{now:%Y-%m-%d %H:%M:%S} 日内缓存已重置"
        LOGGER.info(message)
        if dry_run:
            print(message)
        return message

    def close(self, now: datetime, dry_run: bool = False) -> str:
        positions = self.state.positions()
        quotes = self.data.get_quotes([item.code for item in positions])
        lines = [
            f"运行时间：{now:%Y-%m-%d %H:%M:%S}",
            "",
            "【收盘持仓与成交额】",
        ]
        record = {"date": now.date().isoformat(), "positions": []}
        if not positions:
            lines.append("无本地持仓。")
        for position in positions:
            quote = quotes.get(position.code)
            if not quote:
                lines.append(f"{self._name(position.code)} 行情暂不可用")
                continue
            pnl = (quote.price / position.avg_cost - 1) * 100 if position.avg_cost else 0
            lines.append(
                f"{quote.name}({plain_code(position.code)}) 收盘/最新 {quote.price:.3f}，"
                f"浮动 {pnl:+.2f}%，当日成交额 {quote.amount / 1e8:.2f}亿元"
            )
            record["positions"].append({
                "code": position.code,
                "name": quote.name,
                "price": quote.price,
                "amount": quote.amount,
            })
        records = self.state.data.setdefault("daily_records", [])
        records = [item for item in records if item.get("date") != record["date"]]
        records.append(record)
        self.state.data["daily_records"] = records[-250:]
        self.state.save()
        lines.extend(["", "【数据源】", self.data.source_summary()])
        body = "\n".join(lines)
        self._notify(f"[ETF策略 15:30] 收盘记录 {now:%Y-%m-%d}", body, dry_run)
        return body

    def replay_day(self, target_day: date) -> str:
        """使用目标日及以前的日K生成无前视历史回放日志。"""
        histories = self.data.get_histories(
            list(self.config.fixed_pool) + list(INDEXES.values()),
            count=120,
        )
        weak_lines, is_weak = self._weak_state_from_histories(
            histories, target_day
        )
        replay_now = datetime.combine(target_day, datetime_time(13, 10))
        replay_pool = (
            list(self.config.global_pool)
            if is_weak
            else list(self.config.fixed_pool)
        )
        intraday_histories = self.data.get_intraday_histories(
            replay_pool, target_day
        )
        metrics = []
        quotes: dict[str, Quote] = {}
        daily_closes: dict[str, tuple[float, float]] = {}
        skipped = 0
        exact_minute_count = 0
        close_fallback_count = 0

        for code in replay_pool:
            frame = histories.get(code)
            if frame is None:
                skipped += 1
                continue
            day_rows = frame[frame["date"] == target_day]
            previous = frame[frame["date"] < target_day]
            if day_rows.empty or previous.empty:
                skipped += 1
                continue
            row = day_rows.iloc[-1]
            previous_close = to_float(previous.iloc[-1]["close"])
            daily_closes[code] = (
                to_float(row["close"]),
                previous_close,
            )
            minute_frame = intraday_histories.get(code)
            minute_rows = pd.DataFrame()
            if minute_frame is not None:
                minute_rows = minute_frame[
                    minute_frame["time"] <= replay_now
                ]
            if not minute_rows.empty:
                last_minute = minute_rows.iloc[-1]
                first_minute = minute_rows.iloc[0]
                minute_open = to_float(first_minute.get("open"))
                if minute_open <= 0:
                    minute_open = to_float(first_minute["close"])
                price = to_float(last_minute["close"])
                open_price = minute_open
                high = to_float(minute_rows["high"].max())
                low = to_float(minute_rows["low"].min())
                volume = to_float(minute_rows["volume"].sum())
                amount = to_float(minute_rows["amount"].sum())
                timestamp = last_minute["time"].to_pydatetime()
                source = str(
                    last_minute.get("source", "历史1分钟")
                )
                exact_minute_count += 1
            else:
                full_day_volume = to_float(row["volume"])
                price = to_float(row["close"])
                open_price = to_float(row["open"])
                high = to_float(row["high"])
                low = to_float(row["low"])
                volume = full_day_volume * 130 / 240
                amount = to_float(row.get("amount"))
                timestamp = replay_now
                source = "历史日K收盘近似"
                close_fallback_count += 1
            quote = Quote(
                code=code,
                name=self._name(code),
                price=price,
                previous_close=previous_close,
                open=open_price,
                high=high,
                low=low,
                volume=volume,
                amount=amount,
                timestamp=timestamp,
                source=source,
            )
            metric = self._metrics(code, frame, quote, replay_now)
            if metric:
                quotes[code] = quote
                metrics.append(metric)
            else:
                skipped += 1

        weak_state = self.state.data.setdefault(
            "weak", {"active": False, "start_date": None, "days": 0}
        )
        old_weak_active = bool(weak_state.get("active"))
        weak_state["active"] = is_weak
        try:
            metrics.sort(
                key=lambda item: item["momentum_score"], reverse=True
            )
            filtered = [item for item in metrics if self._passes(item)]
        finally:
            weak_state["active"] = old_weak_active
        top_10 = filtered[:10]
        targets = top_10[:self.config.holdings_num]

        lines = [
            f"ETF策略历史回放日志：{target_day:%Y-%m-%d}",
            "=" * 64,
            "",
            "【回放口径】",
            "仅使用目标日及以前的历史日K，不使用目标日之后行情。",
            "09:40强弱状态按历史交易日连续回放，不再孤立判断单日。",
            "13:10优先使用免费源历史1分钟收盘价和截至该分钟的累计成交量。",
            f"分钟精确：{exact_minute_count}只；日K收盘回退："
            f"{close_fallback_count}只。",
            (
                "走弱期按原策略仅扫描全球/海外池；历史全市场流动性门槛"
                "仍无法完整复原。"
                if is_weak else
                "正常期使用固定池；动态池依赖当时的全市场快照，"
                "本次仍未纳入。"
            ),
            "",
            "【09:00 晨间】",
            f"本次候选池：{len(replay_pool)}只",
            "历史持仓：未提供，不生成历史持仓盈亏。",
            "",
            "【09:40 大A状态】",
            *weak_lines,
            f"最终状态：{'走弱期' if is_weak else '正常期'}",
            "",
            "【13:10 动量与过滤】",
            f"成功计算：{len(metrics)}只",
            f"跳过/数据不足：{skipped}只",
            f"通过全部过滤：{len(filtered)}只",
        ]
        if not top_10:
            lines.append("无ETF通过全部过滤。")
        for index, item in enumerate(top_10, 1):
            quote = quotes[item["etf"]]
            change = quote.change_pct
            lines.append(
                f"{index}. {item['etf_name']}({plain_code(item['etf'])}) "
                f"13:10价 {quote.price:.3f}，涨跌 "
                f"{change:+.2f}% ，得分 {item['momentum_score']:.4f}，"
                f"R² {item['r_squared']:.3f}，量比 "
                f"{fmt_optional(item['volume_ratio'])}，"
                f"拉普拉斯斜率 {item['laplace_slope']:.4f}，"
                f"来源 {quote.source}"
            )
        lines.extend(["", "【策略目标】"])
        if targets:
            for item in targets:
                lines.append(
                    f"{item['etf_name']}({plain_code(item['etf'])})，"
                    f"动量得分 {item['momentum_score']:.4f}"
                )
        else:
            lines.append(
                f"无风险资产目标；实盘逻辑将检查防御ETF "
                f"{plain_code(self.config.defensive_etf)}。"
            )
        lines.extend(["", "【15:30 收盘记录】"])
        for item in targets:
            close_price, previous_close = daily_closes[item["etf"]]
            close_change = (
                (close_price / previous_close - 1) * 100
                if previous_close > 0 else 0
            )
            lines.append(
                f"{item['etf_name']}({plain_code(item['etf'])}) "
                f"收盘 {close_price:.3f}，当日涨跌 {close_change:+.2f}%"
            )
        if not targets:
            lines.append("无目标ETF。")
        lines.extend(["", "【数据源】", self.data.source_summary()])

        body = "\n".join(lines)
        output_dir = BASE_DIR / "data" / "replay"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{target_day.isoformat()}.log"
        output_path.write_text(body + "\n", encoding="utf-8")
        LOGGER.info("历史回放日志已生成: %s", output_path)
        print(f"\n===== {target_day.isoformat()} 历史回放 =====\n{body}\n")
        return body

    @property
    def _is_weak(self) -> bool:
        return bool(self.state.data.get("weak", {}).get("active"))

    def _liquidity_threshold(self) -> tuple[float, str | None, float]:
        try:
            frame = self.data.get_eastmoney_spot()
            amount = pd.to_numeric(frame["成交额"], errors="coerce").fillna(0)
            total = float(amount[amount > 0].sum())
            threshold = total / 15_000 if total > 0 else 0
            threshold = threshold or self.config.fallback_liquidity_threshold
            dates = frame.get("数据日期")
            snapshot_date = None
            if dates is not None and not dates.dropna().empty:
                snapshot_date = str(dates.dropna().iloc[0])
            return threshold, snapshot_date, total
        except Exception as exc:
            LOGGER.warning("流动性门槛回退: %s", exc)
            return self.config.fallback_liquidity_threshold, None, 0.0

    def _update_weak_state(self, today: date) -> tuple[list[str], bool]:
        histories = self.data.get_histories(INDEXES.values(), count=30, workers=4)
        lines, above, below = self._weak_metrics_from_histories(histories, today)
        weak = self.state.data.setdefault(
            "weak", {"active": False, "start_date": None, "days": 0}
        )
        active = bool(weak.get("active"))
        start = parse_date(weak.get("start_date"))
        days = weekdays_between(start, today) if active and start else 0
        if active:
            if days >= self.config.max_weak_days or above >= 3:
                active, start, days = False, None, 0
        elif below >= 3:
            active, start, days = True, today, 0
        weak.update({
            "active": active,
            "start_date": start.isoformat() if start else None,
            "days": days,
        })
        lines.append(
            f"低于MA：{below}/4；站上MA：{above}/4；"
            f"走弱持续：{days}/{self.config.max_weak_days}个交易日"
        )
        self.state.save()
        return lines, active

    def _weak_state_from_histories(
        self, histories: dict[str, pd.DataFrame], today: date
    ) -> tuple[list[str], bool]:
        first_index = histories.get(next(iter(INDEXES.values())))
        if first_index is None:
            lines, above, below = self._weak_metrics_from_histories(
                histories, today
            )
            lines.append(f"低于MA：{below}/4；站上MA：{above}/4")
            return lines, below >= 3

        execution_dates = sorted({
            value
            for value in first_index["date"].tolist()
            if value <= today
        } | {today})
        active = False
        start_date = None
        transition = "未触发走弱期"
        target_above = 0
        target_below = 0
        for run_day in execution_dates:
            _, above, below = self._weak_metrics_from_histories(
                histories, run_day
            )
            if above + below < len(INDEXES):
                continue
            if active:
                weak_days = sum(
                    start_date <= item <= run_day
                    for item in execution_dates
                ) if start_date else 0
                if weak_days >= self.config.max_weak_days:
                    active = False
                    start_date = None
                    transition = f"{run_day:%m-%d} 达到最长周期退出"
                elif above >= 3:
                    active = False
                    start_date = None
                    transition = f"{run_day:%m-%d} 站上3/4，退出走弱期"
                elif below >= 3:
                    start_date = run_day
                    transition = f"{run_day:%m-%d} 再次触发，延续走弱期"
                else:
                    transition = f"{run_day:%m-%d} 未满足退出条件，延续走弱期"
            elif below >= 3:
                active = True
                start_date = run_day
                transition = f"{run_day:%m-%d} 低于3/4，进入走弱期"
            if run_day == today:
                target_above = above
                target_below = below

        lines, _, _ = self._weak_metrics_from_histories(histories, today)
        lines.append(
            f"低于MA：{target_below}/4；站上MA：{target_above}/4"
        )
        lines.append(f"状态路径：{transition}")
        return lines, active

    def _weak_metrics_from_histories(
        self, histories: dict[str, pd.DataFrame], today: date
    ) -> tuple[list[str], int, int]:
        lines = []
        above = 0
        below = 0
        for name, code in INDEXES.items():
            frame = histories.get(code)
            if frame is None:
                lines.append(f"{name}：数据不足")
                continue
            completed = frame[frame["date"] < today]
            closes = completed["close"].dropna().to_numpy(dtype=float)
            if len(closes) < self.config.weak_period_ma_lookback:
                lines.append(f"{name}：数据不足")
                continue
            price = closes[-1]
            ma = closes[-self.config.weak_period_ma_lookback:].mean()
            status = "站上" if price > ma else "低于" if price < ma else "持平"
            above += int(price > ma)
            below += int(price < ma)
            lines.append(
                f"{name}：{price:.2f} / MA{self.config.weak_period_ma_lookback} "
                f"{ma:.2f}，{status}"
            )
        return lines, above, below

    def _filter_by_liquidity(
        self,
        codes: tuple[str, ...] | list[str],
        histories: dict[str, pd.DataFrame],
        threshold: float,
        today: date,
    ) -> list[str]:
        selected = []
        snapshot_liquid = set(self.state.data.get("snapshot_liquid_codes", []))
        if not snapshot_liquid:
            try:
                snapshot = self.data.get_eastmoney_spot()
                amounts = pd.to_numeric(
                    snapshot["成交额"], errors="coerce"
                ).fillna(0)
                snapshot_liquid = {
                    str(code).zfill(6)
                    for code in snapshot.loc[amounts > threshold, "代码"].tolist()
                }
            except Exception:
                pass
        for code in codes:
            frame = histories.get(code)
            if frame is None:
                continue
            completed = frame[frame["date"] < today].tail(3)
            amounts = pd.to_numeric(completed["amount"], errors="coerce").dropna()
            has_history_amount = len(amounts) > 0
            if (
                has_history_amount and float(amounts.mean()) > threshold
            ) or (
                not has_history_amount and plain_code(code) in snapshot_liquid
            ):
                selected.append(code)
        return selected

    def _dynamic_pool(self, threshold: float) -> list[str]:
        try:
            frame = self.data.get_eastmoney_spot().copy()
        except Exception:
            return []
        frame["成交额"] = pd.to_numeric(frame["成交额"], errors="coerce").fillna(0)
        frame = frame[frame["成交额"] > threshold].nlargest(
            self.config.dynamic_prefilter_limit, "成交额"
        )
        groups: dict[str, dict[str, Any]] = {}
        for _, row in frame.iterrows():
            raw_code = str(row["代码"]).zfill(6)
            name = str(row["名称"])
            if any(keyword in name for keyword in EXCLUDED_DYNAMIC_KEYWORDS):
                continue
            cleaned = clean_fund_name(name)
            if not cleaned:
                continue
            prefix = special_prefix(name)
            key = f"{prefix}:{cleaned[:2]}"
            candidate = {
                "code": jq_code(raw_code),
                "amount": to_float(row["成交额"]),
            }
            if key not in groups or candidate["amount"] > groups[key]["amount"]:
                groups[key] = candidate
        ranked = sorted(groups.values(), key=lambda item: item["amount"], reverse=True)
        return [item["code"] for item in ranked[:self.config.dynamic_pool_limit]]

    def _metrics(
        self,
        code: str,
        history: pd.DataFrame,
        quote: Quote,
        now: datetime,
    ) -> dict[str, Any] | None:
        completed = history[history["date"] < now.date()].tail(
            max(self.config.lookback_days, self.config.volume_lookback,
                self.config.ma_lookback) + 20
        )
        closes = completed["close"].dropna().to_numpy(dtype=float)
        volumes = completed["volume"].dropna().to_numpy(dtype=float)
        if len(closes) < self.config.lookback_days or quote.price <= 0:
            return None
        prices = np.append(closes, quote.price)
        momentum, annualized, r_squared = calculate_momentum_score(
            prices, self.config.lookback_days
        )
        if momentum is None:
            return None
        volume_ratio = calculate_volume_ratio(
            volumes, quote.volume, now, self.config.volume_lookback
        )
        day_ratios = [
            prices[-1] / prices[-2],
            prices[-2] / prices[-3],
            prices[-3] / prices[-4],
        ]
        ma_value = float(np.mean(prices[-self.config.ma_lookback:]))
        laplace_values = laplace_filter(prices, self.config.laplace_s_param)
        laplace_slope = float(laplace_values[-1] - laplace_values[-2])
        return {
            "etf": code,
            "etf_name": quote.name or self._name(code),
            "momentum_score": momentum,
            "annualized_returns": annualized,
            "r_squared": r_squared,
            "current_price": quote.price,
            "volume_ratio": volume_ratio,
            "premium_rate": quote.premium_rate,
            "ma_value": ma_value,
            "laplace_slope": laplace_slope,
            "passed_momentum": (
                self.config.min_score_threshold
                <= momentum
                <= self.config.max_score_threshold
            ),
            "passed_r2": r_squared > self.config.r2_threshold,
            "passed_ma": quote.price > ma_value * self.config.ma_threshold,
            "passed_volume": (
                volume_ratio is not None
                and volume_ratio < self.config.volume_threshold
            ),
            "passed_loss": min(day_ratios) >= self.config.loss_ratio,
            "passed_premium": (
                quote.premium_rate is not None
                and quote.premium_rate <= self.config.max_premium_rate
            ),
            "passed_laplace": (
                quote.price > laplace_values[-1]
                and laplace_slope > self.config.laplace_min_slope
            ),
        }

    def _passes(self, item: dict[str, Any]) -> bool:
        required = [item["passed_momentum"]]
        if self.config.enable_r2_filter and not self._is_weak:
            required.append(item["passed_r2"])
        if self.config.enable_ma_filter and self._is_weak:
            required.append(item["passed_ma"])
        if self.config.enable_volume_check:
            required.append(item["passed_volume"])
        if self.config.enable_loss_filter:
            required.append(item["passed_loss"])
        if self.config.enable_premium_filter:
            required.append(item["passed_premium"])
        if self.config.enable_laplace_filter:
            required.append(item["passed_laplace"])
        return all(required)

    def _select_targets(self, top_10: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not top_10:
            return []
        if len(top_10) >= self.config.holdings_num:
            reference = top_10[self.config.holdings_num - 1]["momentum_score"]
            ratio = 1.0 if self._is_weak else self.config.score_threshold_ratio
            candidates = [
                item for item in top_10
                if item["momentum_score"] >= reference * ratio
            ]
        else:
            candidates = top_10
        current = [position.code for position in self.state.positions()]
        candidate_map = {item["etf"]: item for item in candidates}
        retained = [candidate_map[code] for code in current if code in candidate_map]
        if len(retained) >= self.config.holdings_num:
            return sorted(
                retained, key=lambda item: item["momentum_score"], reverse=True
            )[:self.config.holdings_num]
        remaining = [item for item in candidates if item["etf"] not in current]
        need = self.config.holdings_num - len(retained)
        return retained + remaining[:need]

    def _name(self, code: str, fallback: str = "") -> str:
        return (
            fallback
            or self.state.data.get("name_map", {}).get(plain_code(code))
            or plain_code(code)
        )

    def _codes_text(self, codes: list[str]) -> str:
        if not codes:
            return "无"
        return "、".join(
            f"{self._name(code)}({plain_code(code)})" for code in codes
        )


def calculate_momentum_score(
    price_series: np.ndarray, lookback_days: int
) -> tuple[float | None, float | None, float | None]:
    if len(price_series) < lookback_days + 1:
        return None, None, None
    recent = np.asarray(price_series[-(lookback_days + 1):], dtype=float)
    if np.any(recent <= 0) or np.any(~np.isfinite(recent)):
        return None, None, None
    y = np.log(recent)
    x = np.arange(len(y), dtype=float)
    weights = np.linspace(1, 2, len(y))
    squared_weights = weights ** 2
    x_bar = np.sum(squared_weights * x) / np.sum(squared_weights)
    y_bar = np.sum(squared_weights * y) / np.sum(squared_weights)
    dx = x - x_bar
    dy = y - y_bar
    variance_x = np.sum(squared_weights * dx ** 2)
    if variance_x == 0:
        return 0.0, 0.0, 0.0
    slope = np.sum(squared_weights * dx * dy) / variance_x
    intercept = y_bar - slope * x_bar
    annualized = math.exp(slope * 250) - 1
    prediction = slope * x + intercept
    residual = np.sum(weights * (y - prediction) ** 2)
    total = np.sum(weights * (y - np.mean(y)) ** 2)
    r_squared = 1 - residual / total if total else 0.0
    return annualized * r_squared, annualized, r_squared


def laplace_filter(price: np.ndarray, s: float = 0.05) -> np.ndarray:
    alpha = 1 - np.exp(-s)
    result = np.zeros(len(price))
    result[0] = price[0]
    for index in range(1, len(price)):
        result[index] = alpha * price[index] + (1 - alpha) * result[index - 1]
    return result


def calculate_volume_ratio(
    hist_volumes: np.ndarray,
    today_volume: float,
    now: datetime,
    lookback_days: int,
) -> float | None:
    if len(hist_volumes) < lookback_days:
        return None
    recent = np.asarray(hist_volumes[-lookback_days:], dtype=float)
    if np.any(~np.isfinite(recent)) or np.any(recent <= 0):
        return None
    elapsed = (now.hour - 9) * 60 + now.minute - 30
    if now.hour >= 13:
        elapsed -= 90
    elapsed = max(1, min(elapsed, 240))
    projected = today_volume * 240 / elapsed
    average = float(np.mean(recent))
    return projected / average if average > 0 else None


def clean_fund_name(name: str) -> str:
    result = name
    for word in FUND_COMPANIES:
        result = result.replace(word, "")
    for word in NOISE_WORDS:
        result = result.replace(word, "")
    return result.strip()


def special_prefix(name: str) -> str:
    if any(word in name for word in ("恒生", "港股", "H股", "香港", "中概")):
        return "香港"
    if "科创" in name:
        return "科创"
    if "创业" in name:
        return "创业"
    if any(word in name for word in ("标普", "纳指", "纳斯达克")):
        return "美指"
    return "普通"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def weekdays_between(start: date | None, end: date) -> int:
    if start is None or start >= end:
        return 0
    return int(np.busday_count(start.isoformat(), end.isoformat()))


def fmt_optional(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def configure_logging() -> None:
    log_dir = BASE_DIR / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_dir / "etf.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def build_strategy() -> LocalETFStrategy:
    load_env_file(BASE_DIR / ".env.etf")
    config = StrategyConfig(
        state_path=BASE_DIR / "data" / "etf_state.json",
        cache_dir=BASE_DIR / "data" / "cache",
    )
    state = StateStore(config.state_path)
    data = MarketDataHub(config.cache_dir)
    return LocalETFStrategy(config, data, state, Mailer())


def run_doctor(strategy: LocalETFStrategy) -> int:
    print("【依赖】")
    print(f"Python: {sys.version.split()[0]}")
    try:
        import akshare
        print(f"AkShare: {akshare.__version__}")
    except ImportError:
        print("AkShare: 未安装")
    print("\n【行情源】")
    checks: list[tuple[str, Callable[[], Any]]] = [
        ("腾讯财经实时", lambda: strategy.data.get_quotes(["510300.XSHG"])),
        ("全市场ETF快照", strategy.data.get_eastmoney_spot),
        ("同花顺ETF净值", strategy.data.get_ths_spot),
        ("东方财富/腾讯历史回退链", lambda: strategy.data.get_history("510300.XSHG", 10)),
        ("交易日历", strategy.data.get_trade_dates),
    ]
    failures = 0
    for name, check in checks:
        try:
            result = check()
            size = len(result) if hasattr(result, "__len__") else 1
            if size == 0:
                raise RuntimeError("返回空数据")
            print(f"通过  {name}（{size}条）")
        except Exception as exc:
            failures += 1
            print(f"失败  {name}：{exc}")
    errors = strategy.mailer.configuration_errors()
    print("\n【邮件】")
    print("配置完整" if not errors else "缺少: " + ", ".join(errors))
    print("\n【状态文件】")
    print(strategy.state.path)
    return 1 if failures else 0


def position_command(strategy: LocalETFStrategy, args: argparse.Namespace) -> int:
    if args.position_action == "list":
        positions = strategy.state.positions()
        if not positions:
            print("当前本地持仓为空")
        for item in positions:
            print(
                f"{plain_code(item.code)} {item.name or '-'} "
                f"{item.amount}份 成本{item.avg_cost:.3f}"
            )
        return 0
    if args.position_action == "set":
        code = jq_code(args.code)
        name = args.name or strategy.state.data.get("name_map", {}).get(
            plain_code(code), ""
        )
        strategy.state.set_position(Position(code, args.amount, args.cost, name))
        print(f"已更新持仓: {plain_code(code)} {args.amount}份 成本{args.cost:.3f}")
        return 0
    strategy.state.remove_position(args.code)
    print(f"已删除持仓: {plain_code(args.code)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地ETF策略邮件提醒")
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("once", help="立即执行一个时间点")
    once.add_argument(
        "job", choices=["morning", "weak", "rebalance", "reset", "close", "stop"]
    )
    once.add_argument("--dry-run", action="store_true", help="打印正文，不发邮件")
    sub.add_parser("doctor", help="检查依赖、行情源和邮件配置")
    sub.add_parser("send-test", help="发送测试邮件")
    replay = sub.add_parser("replay", help="按历史日K回放指定交易日")
    replay.add_argument("dates", nargs="+", help="日期，如 2026-06-30")
    positions = sub.add_parser("position", help="维护实际持仓")
    position_sub = positions.add_subparsers(dest="position_action", required=True)
    position_sub.add_parser("list")
    set_parser = position_sub.add_parser("set")
    set_parser.add_argument("code", help="ETF代码，如 510300")
    set_parser.add_argument("amount", type=int, help="持有份数")
    set_parser.add_argument("cost", type=float, help="平均成本")
    set_parser.add_argument("--name", default="", help="ETF名称")
    remove_parser = position_sub.add_parser("remove")
    remove_parser.add_argument("code")
    return parser


def main() -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    strategy = build_strategy()
    if args.command == "doctor":
        return run_doctor(strategy)
    if args.command == "send-test":
        strategy._notify(
            "[ETF策略] 通知测试",
            f"ETF策略通知配置有效。\n测试时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        )
        return 0
    if args.command == "replay":
        for raw_date in args.dates:
            try:
                target_day = date.fromisoformat(raw_date)
            except ValueError as exc:
                parser.error(f"无效日期 {raw_date}: {exc}")
            strategy.replay_day(target_day)
        return 0
    if args.command == "position":
        return position_command(strategy, args)
    now = datetime.now()
    calendar = strategy.data.get_trade_dates()
    is_trade_day = now.date() in calendar if calendar else now.weekday() < 5
    if not is_trade_day:
        print(f"::notice::非交易日 {now.date()}，跳过 {args.job}")
        return 0
    if args.job == "stop":
        strategy.stop_loss(now, args.dry_run)
    else:
        jobs = {
            "morning": strategy.morning,
            "weak": strategy.weak_and_pool,
            "rebalance": strategy.rebalance,
            "reset": strategy.reset,
            "close": strategy.close,
        }
        jobs[args.job](now, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
