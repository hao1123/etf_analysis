#!/usr/bin/env python3
"""通过 GitHub API 触发 ETF Strategy workflow_dispatch。

用于替代 GitHub Actions cron（免费账户 cron 可能延迟或跳过）。
可在外部 cron 服务（cron-job.org、本地 crontab）中调度。

用法：
    GITHUB_TOKEN=ghp_xxx python trigger_actions.py morning
    GITHUB_TOKEN=ghp_xxx python trigger_actions.py weak --dry-run

 cron-job.org 配置：
    URL: https://api.github.com/repos/hao1123/etf_analysis/actions/workflows/etf.yml/dispatches
    Method: POST
    Headers:
        Accept: application/vnd.github+json
        Authorization: Bearer ghp_xxx
        Content-Type: application/json
    Body (morning): {"ref":"main","inputs":{"job":"morning","dry_run":"false"}}
    Body (weak):    {"ref":"main","inputs":{"job":"weak","dry_run":"false"}}
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests


REPO = "hao1123/etf_analysis"
WORKFLOW_FILE = "etf.yml"
API_URL = (
    f"https://api.github.com/repos/{REPO}/actions/workflows/"
    f"{WORKFLOW_FILE}/dispatches"
)


def trigger(job: str, dry_run: bool = False, token: str | None = None) -> bool:
    token = token or os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        print("错误：未设置 GITHUB_TOKEN 环境变量", file=sys.stderr)
        print(
            "请前往 https://github.com/settings/tokens 创建 PAT（需 repo 权限）",
            file=sys.stderr,
        )
        return False
    payload = {
        "ref": "main",
        "inputs": {"job": job, "dry_run": "true" if dry_run else "false"},
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    print(f"触发 {job} (dry_run={dry_run})...")
    response = requests.post(API_URL, json=payload, headers=headers, timeout=15)
    if response.status_code == 204:
        print(f"成功：{job} 已触发")
        return True
    print(
        f"失败：HTTP {response.status_code} - {response.text}",
        file=sys.stderr,
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="触发 ETF Strategy workflow")
    parser.add_argument(
        "job",
        choices=["morning", "weak", "rebalance", "reset", "close", "stop"],
        help="要执行的任务",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Dry-run 模式"
    )
    args = parser.parse_args()
    return 0 if trigger(args.job, args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
