# ETF 策略迁移 GitHub Actions 改造方案

## Context

当前项目是本地 macOS daemon 模式，按交易时间表调度任务并通过 SMTP 发送邮件提醒。用户希望：

1. **解决合盖睡眠问题**：迁移到 GitHub Actions 云端运行，不依赖本地电脑
2. **新增飞书通知**：除邮件外，支持飞书群 Webhook 推送消息
3. **自动持仓管理**：rebalance 信号即成交，按当日 quote.price 作为 avg_cost 自动写入持仓；卖出/止损自动清除，无需手动 `position set`

用户已确认：公开仓库、默认持仓 1000 份（可经 HOLDINGS_AMOUNT 调整）、废弃本地 daemon 模式、放弃盘中每分钟止损（cron 无法做到）、状态用 GitHub Actions Cache 持久化。

## 改造范围

### 一、新增文件

#### 1. `.github/workflows/etf.yml`（核心调度）

5 个 cron 触发器（北京时间 → UTC，周一至周五）：

| 北京时间 | UTC cron | 任务 |
|---|---|---|
| 09:00 | `0 1 * * 1-5` | morning |
| 09:40 | `40 1 * * 1-5` | weak |
| 13:10 | `10 5 * * 1-5` | rebalance |
| 15:10 | `10 7 * * 1-5` | reset |
| 15:30 | `30 7 * * 1-5` | close |

关键设计：
- `concurrency.group: etf-strategy` + `cancel-in-progress: false`：同 group 排队串行，绝不并发，避免状态覆盖
- `env.TZ: Asia/Shanghai`：让 `datetime.now()` 直接取北京时间
- `actions/cache@v4`：`path: data`，`key: etf-data-${{ github.run_id }}`（每次必存），`restore-keys: etf-data-`（前缀匹配拿最近版本）
- Secret 注入：`SMTP_HOST/PORT/USER/PASSWORD`、`MAIL_FROM/TO`、`SMTP_SSL=1`、`FEISHU_WEBHOOK`、`HOLDINGS_AMOUNT`
- `workflow_dispatch` 支持手动触发（inputs: job 选择 + dry_run 开关）
- 步骤：checkout → setup-python 3.12 → cache restore → pip install -r requirements-etf.txt → `python etf.py once "$JOB" $DRY_FLAG` → cache save

#### 2. `.gitignore`

排除：`.env.etf`、`__pycache__/`、`data/logs/`、`data/replay/`、`*.pyc`、`.DS_Store`
**保留**：`data/etf_state.json`（初始版本，首次 run 后由 Cache 接管）、`data/cache/`（行情缓存，可选排除，但保留可减少首次 run 请求量）

### 二、修改文件

#### 1. `etf.py` 主程序

**a. 顶部新增 `import requests`**（当前未导入，飞书通知需要）

**b. 新增 `FeishuNotifier` 类**（紧随 `Mailer` 类之后，约 [etf.py:193](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L193)）：
- `__init__`：读取 `FEISHU_WEBHOOK` 和可选 `FEISHU_SECRET`（加签机器人）
- `send(subject, body, dry_run)`：未配置 webhook 时静默 return；构造 `{"msg_type":"text","content":{"text":f"{subject}\n\n{body}"}}`；可选加签；`dry_run` 时 print 不发送；`requests.post(...).raise_for_status()`
- 个人群机器人默认无签名，`FEISHU_SECRET` 留空即可

**c. `LocalETFStrategy.__init__` 新增 `self.feishu = FeishuNotifier()`**

**d. 新增 `_notify(subject, body, dry_run)` 方法**：
```python
def _notify(self, subject, body, dry_run=False):
    self.mailer.send(subject, body, dry_run)
    self.feishu.send(subject, body, dry_run)
```

**e. 替换所有 `self.mailer.send(...)` 为 `self._notify(...)`**：共 5 处（morning、weak_and_pool、rebalance、close、stop_loss）

**f. 修改 `rebalance`（[etf.py:303-380](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L303-380)）**：
在第 342 行 `holds = ...` 之后、第 344 行 `lines = [...]` 之前插入持仓自动更新：
```python
holdings_amount = int(os.getenv("HOLDINGS_AMOUNT", "1000"))
trades = []
for code in sells:
    self.state.remove_position(code)
    trades.append(f"卖出 {self._name(code)} @ {quotes[code].price:.3f}" if code in quotes else f"卖出 {code}")
for code in buys:
    q = quotes.get(code)
    if q and q.price > 0:
        self.state.set_position(Position(code, holdings_amount, q.price, q.name))
        trades.append(f"买入 {q.name}({plain_code(code)}) @ {q.price:.3f} x{holdings_amount}")
```
注意 `quotes` 字典已包含所有 pool 报价（[etf.py:306](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L306)），defensive 回退时也单独取过 quote（[etf.py:328](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L328)）。

正文第 373 行 `"以上仅为策略提醒，不代表已成交。完成手工交易后请更新本地持仓。"` 改为 `"已按当日行情自动更新持仓（份数 {holdings_amount}）。"`，并新增 `trades` 列表展示。

**g. 修改 `stop_loss`（[etf.py:382-422](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L382-422)）**：
在第 414 行 `sent[alert_key] = now.isoformat()` 之前增加自动卖出：
```python
self.state.remove_position(position.code)
```
正文第 409 行 `"这是止损提醒，不会自动卖出。"` 改为 `"已自动卖出并清除持仓。"`

**h. 修改 `send-test` 命令（[etf.py:1278-1283](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L1278-1283)）**：扩展为同时发飞书：
```python
subject = "[ETF策略] 通知测试"
body = f"ETF策略通知配置有效。\n测试时间：{datetime.now():%Y-%m-%d %H:%M:%S}"
strategy._notify(subject, body)
```

**i. 修改 `main()` 的 `once` 分支（[etf.py:1294+](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L1294)）**：执行 job 前加交易日检查：
```python
calendar = strategy.data.get_trade_dates()
is_trade_day = now.date() in calendar if calendar else now.weekday() < 5
if not is_trade_day:
    print(f"::notice::非交易日 {now.date()}，跳过 {args.job}")
    return 0
```
`send-test`/`doctor`/`replay`/`position` 不受此检查影响（它们位于 `once` 分支之前）。

**j. 删除以下代码**（废弃本地 daemon 模式）：
- `run_daemon` 函数（[etf.py:1138-1180](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L1138-1180)）
- `JOB_WINDOWS` 字典（[etf.py:1129-1135](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L1129-1135)）
- `is_market_session` 函数（grep 定位后删除，仅 daemon 使用）
- `build_parser` 中的 `sub.add_parser("daemon", ...)`（[etf.py:1245](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L1245)）
- `main()` 中 `if args.command == "daemon":` 分支（[etf.py:1273-1275](file:///Users/pumpkin/zh/code/JoinQuant/etf/etf.py#L1273-1275)）

**k. `run_etf.sh` 保留**：本地测试仍可用（`./run_etf.sh once morning --dry-run`）

#### 2. `.env.etf.example`

新增（追加到现有内容之后）：
```
# 飞书群机器人 Webhook（可选，未配置则只发邮件）
FEISHU_WEBHOOK=
# 飞书机器人加签密钥（可选，仅当机器人启用签名校验时填写）
FEISHU_SECRET=
# 信号即成交时的默认持仓份数
HOLDINGS_AMOUNT=1000
```

#### 3. `ETF_LOCAL_README.md`

- 新增 "GitHub Actions 部署" 章节：Secret 配置清单、workflow_dispatch 手动触发、Actions Cache 工作原理、cron 时间表
- 删除 "常驻运行" 章节中 LaunchAgent 相关内容（或改为"本地备选方案"）
- 删除 macOS 相关说明（合盖问题已不存在）
- 更新运行时间表说明（不再有盘中每分钟止损）

#### 4. 删除文件

- `install_etf_service.sh`（不再需要 LaunchAgent）
- `uninstall_etf_service.sh`

### 三、不动文件

- `etf_config.py`：`SCHEDULES` 字典仍用于日志展示，`StrategyConfig` 无需改动
- `etf_data.py`：数据源回退链足够（东财→腾讯→AkShare），`ETF_USE_SYSTEM_PROXY=0` 默认适配 GitHub Actions
- `requirements-etf.txt`：无需新增依赖（飞书 Webhook 只用 requests，已在列表中）

## 数据源海外可达性评估

| 源 | 海外可达 | 说明 |
|---|---|---|
| 东方财富 `push2his/push2.eastmoney.com` | 优 | 历史首选，无 IP 限制 |
| 腾讯 `web.ifzq.gtimg.cn` | 优 | 实时首选 |
| 新浪 `quotes.sina.cn` | 优 | 分钟回退 |
| AkShare（底层东财/新浪） | 良 | 偶发限流，作为回退 |
| `timor.tech` 节假日 | 中 | 有 AkShare sina 回退兜底 |

现有回退链足够，无需新增数据源。`data/cache` 缓存历史 CSV，可显著减少跨次 run 请求量。

## 验证方案

### 1. 本地测试飞书通知
```bash
cp .env.etf.example .env.etf
# 编辑 .env.etf 填入 FEISHU_WEBHOOK
./run_etf.sh send-test
# 应同时收到邮件和飞书消息
```

### 2. 本地测试自动持仓
```bash
./run_etf.sh once rebalance --dry-run
# 检查输出中是否含"已按当日行情自动更新持仓"
# 去掉 --dry-run 实际执行后：
./run_etf.sh position list
# 应看到新写入的持仓
```

### 3. GitHub Actions 测试
- 推送到 GitHub 公开仓库
- 配置 Secrets：SMTP_HOST/PORT/USER/PASSWORD、MAIL_FROM/TO、FEISHU_WEBHOOK、HOLDINGS_AMOUNT
- Actions 页面 → workflow_dispatch → 选 `morning` + `dry_run` 手动触发
- 检查日志：依赖安装成功、cache 命中、飞书/邮件发出
- 非 dry-run 跑 `rebalance`，检查 Actions Cache 已保存

### 4. 状态持久化验证
- 连续两次 workflow_dispatch 跑 `position list`
- 第二次应能看到第一次 `rebalance` 写入的持仓
- 在 Actions 日志搜 `Cache restored` / `Cache saved`

### 5. 交易日过滤验证
- 周末手动触发，确认日志输出 `::notice::非交易日` 并正常退出（job 绿色但跳过）

## 风险与权衡

| 风险 | 应对 |
|---|---|
| GitHub Actions cron 延迟 5-15 分钟 | `once` 模式不校验 JOB_WINDOWS，延迟只影响提醒时效 |
| 公开仓库代码泄露 | `.env.etf` 在 .gitignore，Secret 不入库 |
| Actions Cache 7 天过期 | 工作日每天都跑，cache 持续刷新；长假后首次 run 重新拉取 |
| 信号即成交滑价 | 用 `quote.price` 作 `avg_cost`，与真实成交价有滑价，用户已知悉 |
| AkShare 海外限流 | 已有东财/腾讯回退链兜底 |
| concurrency 排队放大延迟 | 保证状态一致性的必要代价，可接受 |

## 实施步骤顺序

1. 修改 `etf.py`：新增 FeishuNotifier + _notify + 自动持仓 + 交易日检查 + 删除 daemon 相关代码
2. 修改 `.env.etf.example`：新增飞书和持仓配置
3. 新增 `.gitignore`
4. 删除 `install_etf_service.sh`、`uninstall_etf_service.sh`
5. 新增 `.github/workflows/etf.yml`
6. 修改 `ETF_LOCAL_README.md`
7. 本地测试：`./run_etf.sh send-test` + `./run_etf.sh once rebalance --dry-run`
8. `git init` + 推送到 GitHub 公开仓库
9. 配置 Secrets，workflow_dispatch 手动触发验证
