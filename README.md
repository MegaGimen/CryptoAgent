# CoinAutomation · short

`short` 分支是 **ETH/USDT 日内短线执行交易员**：事件驱动唤醒，经 LangGraph 状态机完成观察、开平仓、保护单与记忆维护。

各能力基本是**独立进程/模块**：行情、新闻、Polymarket、巨鲸、QQ、复盘控制台与策略主循环分开启动；策略通过读文件 / 协议函数 / 同进程工具消费它们。

## 配置（所有组件共用）

```bash
cp .env.example .env   # 只使用仓库根目录 .env
```

运行时统一经 `env_config.load_project_env()` 加载。开发约定见 `AGENTS.md`。

---

## 组件启动一览

生产常驻建议至少拉起：**Binance + news + polymarket + strategy**；QQ / report / whale 按需。

| 组件 | 启动命令 | 常驻？ | 依赖密钥 / 说明 |
|------|----------|--------|-----------------|
| 策略主循环 | `python strategy.py` | 是 | `LLMAPIKEY` 等；同进程加载 `execution` 工具 |
| Binance 行情/账户 | `python Binance/Binance.py` | 是 | `BINANCE_API_KEY` / `SECRET`；写 `metrics_report.md`、`account.md`、alerts/fills |
| 新闻采集+门控 | `python news/news.py` | 是 | `CRYPTONEWS_API_TOKEN`；内嵌 First/SecondGate |
| Polymarket 监控+门控 | `python polymarket/monitor_markets.py` | 是 | LLM（Gate）；写 `monitor.json` → `gated.json` |
| 巨鲸 WhaleAlert | 见下方 | 可选 | `WHALE_ALERT_API_KEY`；默认随 strategy 开关 |
| QQ 推送 | `python qq.py` | 可选 | `QQ_BOT_*`；HTTP `9182` |
| 复盘 Console | `python report.py` | 可选 | Binance 密钥；HTTP `1520` |
| FirstGate（手动） | `python run_first_gate.py` | 否 | 一般由 news/poly 自动触发 |
| SecondGate（手动） | `python run_second_gate.py` | 否 | 一般每 3h 由采集进程触发 |
| 执行层 | 不单独启动 | — | 被 `strategy` 同进程 import |
| 仓位审计 | `python positions.py <days>` | 否 | Binance 密钥 |
| 日志审计报告 | `python gentestreport.py <start> <end>` | 否 | 读 `logs/` |
| 错误汇总 | `python error.py` | 否 | 刷新 `errors.json` |
| Log Lookup MCP | `python log_lookup_mcp.py` | 可选 | 默认 stdio；`--transport sse --port 18123` |
| 记忆重置 | `python reset.py` | 否 | 重建 `mem/*.json` 骨架 |
| 一键重启（systemd） | `./restart.sh` | — | 依赖本机 unit：`execMCP` / `Binance` / `news` / `god` / `polymarket` |

---

## 各组件说明与启动方式

### 1. 策略主循环 · `strategy.py`

短线大脑：合并唤醒事件 → 组 prompt → 决策 / 调工具 → 落盘 `logs/<ts>/`。

```bash
python strategy.py
```

- 巨鲸：若 `.env` 中 `ENABLE_WHALE_MONITOR=true`，主进程内会跑 `whale_monitor()`（不必再单独起 whale）
- 执行：同进程加载 `execution/main.py` 的 LangChain tools（**不要**再当独立 FastMCP 服务起）

### 2. Binance 行情与账户 · `Binance/Binance.py`

独立刷新 ETH 多周期指标、账户报告、成交/风险告警。

```bash
python Binance/Binance.py
```

- 标的：`ETHUSDT`
- 周期：约每 15s 一轮（异常时短睡重试）
- 产物：`Binance/metrics_report.md`、`Binance/account.md`、`alerts.json`、`fills.json` 等

### 3. 新闻 · `news/news.py`

独立拉 CryptoNews，并在进程内触发门控。

```bash
python news/news.py
```

- 轮询：约每 10 分钟
- 有增量时：`run_news_gate()` → `news/gated.json`
- 约每 3 小时：`run_news_second_gate()` 清洗过期项
- 策略侧通过 `news/protocol.py` 读 gated 池注入 prompt

### 4. Polymarket · `polymarket/monitor_markets.py`

独立轮询 Gamma，并在进程内触发门控。

```bash
python polymarket/monitor_markets.py
```

- 轮询：默认约 5s（本分支硬编码间隔）
- 更新后：`run_poly_gate()` → `polymarket/gated.json`
- 约每 3 小时：`run_poly_second_gate()`
- 策略侧通过 `polymarket/protocol.py` 注入 prompt

### 5. 巨鲸 · `whale/whalealert/protocol.py`

WhaleAlert WebSocket；**两套用法二选一即可**。

**推荐（随策略）：**

```bash
# .env
ENABLE_WHALE_MONITOR=true
WHALE_ALERT_API_KEY=...
python strategy.py
```

**单独调试协议：**

```bash
python whale/whalealert/protocol.py
```

单独跑只维持连接与本地 `whale_data.json`，不会自动进策略队列；生产唤醒依赖 strategy 内的 `whale_monitor`。

### 6. QQ 推送 · `qq.py`

独立机器人 + 本地推送 HTTP。

```bash
python qq.py
```

- 需：`QQ_BOT_APPID` / `QQ_BOT_SECRET` 等
- Flask：`0.0.0.0:9182`

### 7. 复盘 Console · `report.py`

独立 Web 复盘（K 线、事件、记忆差异等）。

```bash
python report.py
```

- 地址：`http://0.0.0.0:1520`

### 8. 门控脚本（通常不必常驻）

采集进程已内嵌调用；仅手工补跑或排障时用：

```bash
python run_first_gate.py    # 新闻 + Polymarket FirstGate
python run_second_gate.py   # 新闻 + Polymarket SecondGate
```

### 9. 执行层 · `execution/main.py`

**不是独立守护进程。** 生产由 `strategy.py` 同进程引用工具函数。

```bash
python execution/main.py
# 只会打印「已迁移为 LangChain 同进程工具模块」，无服务端口
```

### 10. 辅助工具（按需）

```bash
python positions.py 3                 # 近 N 日仓位闭环审计
python gentestreport.py <start> <end> # 区间审计报告
python error.py                       # 汇总工具/API 错误
python log_lookup_mcp.py              # 日志查询 MCP（stdio）
python log_lookup_mcp.py --transport sse --host 0.0.0.0 --port 18123
python reset.py                       # 重置结构化记忆骨架
./restart.sh                          # systemd 一键重启（需本机已配置同名 unit）
```

---

## 推荐启动顺序

```text
1) Binance/Binance.py
2) news/news.py
3) polymarket/monitor_markets.py
4) （可选）qq.py / report.py
5) strategy.py          # 最后起，避免上下文文件还是空的
```

## 数据流（简图）

```text
Binance / news / polymarket / (whale) / alarm / fill
        ↓ 写盘或事件
   strategy.py 唤醒合并
        ↓
 propose → execute(execution tools) → verify
        ↓
 logs/<ts>/ + mem/*.json + clock.json
```

## 分支关系

| 分支 | 角色 |
|------|------|
| `short` | ETH 日内执行交易员（本分支） |
| `long` | BTC Long Hunter，prediction-only 长线研究 |
