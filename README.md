# ETF 策略提醒

`etf.py` 是原聚宽策略的提醒版。它保留策略参数、ETF 池和运行时间，
使用 AkShare、腾讯财经、东方财富、同花顺免费数据源，不连接券商，
通过 SMTP 邮件和飞书群 Webhook 推送研究信号。原聚宽代码保存在
`etf_joinquant_original.py`。

策略在 rebalance 信号发出时按当日行情自动更新本地持仓（信号即成交），
止损触发时自动清除持仓，无需手动维护。

## 1. 安装与配置

```bash
cd /Users/pumpkin/zh/code/JoinQuant/etf
PYTHON_BIN="$(pyenv which python3 2>/dev/null || command -v python3)"
"$PYTHON_BIN" -m pip install -r requirements-etf.txt
cp .env.etf.example .env.etf
```

编辑 `.env.etf`：
- **SMTP**：QQ/163 邮箱需先在邮箱设置中启用 SMTP，填写 SMTP 授权码（非登录密码）
- **飞书 Webhook**：`FEISHU_WEBHOOK` 留空则只发邮件；填群机器人 Webhook URL 则同时推送飞书
- **持仓份数**：`HOLDINGS_AMOUNT` 默认 1000，控制信号即成交时的买入份数

检查数据源和配置：

```bash
./run_etf.sh doctor
./run_etf.sh send-test
```

## 2. 持仓自动管理

rebalance 任务会固定使用当日 `13:10` 的1分钟价格和截至该分钟的累计成交量，
不受 GitHub Actions 排队或手动补跑时间影响。信号发出后，程序按该截面价格作为
成本价自动写入持仓：
- **卖出**：信号目标与当前持仓的差集中，旧持仓自动清除
- **买入**：新目标自动以当日行情价记入持仓，份数由 `HOLDINGS_AMOUNT` 控制
- **止损**：触发固定止损线时自动清除对应持仓

如需手动覆盖持仓（如调整真实成交价），仍可使用 CLI：

```bash
./run_etf.sh position set 510300 1000 4.125 --name 沪深300ETF
./run_etf.sh position list
./run_etf.sh position remove 510300
```

## 3. 无通知演练

```bash
./run_etf.sh once morning --dry-run
./run_etf.sh once weak --dry-run
./run_etf.sh once rebalance --dry-run
./run_etf.sh once close --dry-run
```

历史日K回放（会写入 `data/replay/YYYY-MM-DD.log`）：

```bash
./run_etf.sh replay 2026-06-30 2026-07-01
./run_etf.sh replay 2026-07-01 2026-07-02 \
  --reference-log data/logs/log_1.txt
```

历史回放严格过滤目标日之后的数据，并在免费源5日分钟窗口内优先使用目标日
`13:10` 的1分钟价格和累计成交量（东方财富失败时回退新浪）；超出窗口或
单只ETF分钟数据失败时才回退日K收盘近似。强弱状态按历史交易日连续回放。
正常期动态池仍受限于当时的全市场快照不可得，因此历史回放会明确标注未纳入。
提供聚宽日志时，回放会使用日志“第一步排名”中可见的候选池做同池验证；
聚宽正常期日志最多打印前100只，因此该模式用于验证排名和最终目标，不代表完整池归档。

免费源的历史分钟价与聚宽信号时点可能相差一个最小报价单位。只有当原始动量
略高于上限、且价格低一档后的动量不超过原上限时，策略才按一档报价误差放行，
并使用低一档价格对应的得分和R²参与排名；邮件与回放日志会同时保留原始分钟价
得分，不会修改得分上限参数。

## 4. GitHub Actions 部署

项目设计为在 GitHub Actions 上托管运行，避免依赖本地电脑。Workflow 定义在
`.github/workflows/etf.yml`。

### Secret 配置

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret | 说明 |
| --- | --- |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | SMTP 服务器配置 |
| `MAIL_FROM` / `MAIL_TO` | 发件人和收件人 |
| `FEISHU_WEBHOOK` | 飞书群机器人 Webhook URL（可选） |
| `FEISHU_SECRET` | 飞书机器人加签密钥（可选） |
| `HOLDINGS_AMOUNT` | 信号即成交的默认份数，如 `1000` |

### Cron 时间表

UTC 时间，北京时间 = UTC+8：

| UTC cron | 北京时间 | 任务 |
| --- | --- | --- |
| `0 1 * * 1-5` | 09:00 | morning：持仓/浮盈/流动性 |
| `40 1 * * 1-5` | 09:40 | weak：大A强弱/ETF池更新 |
| `10 5 * * 1-5` | 13:10 | rebalance：动量排名/自动调仓 |
| `10 7 * * 1-5` | 15:10 | reset：重置日内缓存 |
| `30 7 * * 1-5` | 15:30 | close：收盘持仓/成交额并归档全市场快照 |

非交易日（周末和中国节假日）由 `etf.py` 内部判断并跳过，job 显示绿色但日志可见 `::notice::非交易日`。

### 状态持久化

每次 workflow run 通过 `actions/cache@v4` 持久化 `data/` 目录：
- `key: etf-data-${{ github.run_id }}`：每次 run 必存新版本
- `restore-keys: etf-data-`：前缀匹配拿最近缓存
- `concurrency.group: etf-strategy`：同 group 排队串行，避免并发覆盖状态

流动性门槛和正常期动态池只读取目标日前最近3份15:30全市场收盘快照，
不会把09:00或09:40的当日部分成交额混入三日均值。仓库内
`bootstrap/spot/` 提供首次部署所需的历史种子快照，因此无需等待3个收盘任务；
`data/cache/` 中同日期的真实15:30归档会自动覆盖对应种子数据。

### 手动触发

Actions 页面 → "ETF Strategy" → Run workflow，可选 job 和 dry_run 开关。

### 注意事项

- GitHub Actions cron 可能延迟 5-15 分钟，不影响执行只影响提醒时效
- 公开仓库 Actions 额度无限；私有仓库每月 2000 分钟（本项目够用）
- Actions Cache 7 天未访问会过期，工作日每天都跑会持续刷新
- 电脑睡眠、合盖问题不存在（云端运行）

## 数据回退

- 实时行情：腾讯财经，失败后回退 AkShare/东方财富。
- 历史行情：统一使用前复权日线；东方财富直连失败后回退腾讯财经、
  AkShare，最后才使用新浪不复权日线。
- 13:10信号：东方财富历史分钟，失败后回退新浪历史分钟；成交量统一换算为股。
- 全市场 ETF、成交额、折溢价：AkShare/东方财富。
- ETF 名称与净值补充：AkShare/同花顺。
- 所有历史数据和全市场快照会写入 `data/cache`，网络失败时可使用最近缓存。

免费源可能临时限流或出现字段延迟。每封邮件都会附本次数据源状态；如果关键
行情缺失，对应 ETF 会跳过计算，不用零值冒充有效数据。
