# CoinAutomation · long

`long` 分支是 **BTC/USDT Long Hunter**：prediction-only 的长线研究 agent，输出 ≥30 天叙事与结构化价格预测，**不下单、不持仓管理**。

能力同样拆成独立组件：行情刷新、新闻门控、Polymarket 门控、Long Hunter 主循环、Console 分开启动。巨鲸**不是**独立进程，由 `strategy.py` 内联调用 CryptoNews whale-summary API。

## 配置（所有组件共用）

```bash
cp .env.example .env   # 只使用仓库根目录 .env
```

关键变量示例：`LLMAPIKEY`、`CRYPTONEWS_API_TOKEN` / `LONG_HUNTER_WHALE_API_TOKEN`、`BINANCE_API_KEY`（行情）等。统一经 `env_config.load_project_env()` 加载。约定见 `AGENTS.md`。

---

## 组件启动一览

生产常驻建议：**Binance + news + polymarket + strategy**；Console 按需。

| 组件 | 启动命令 | 常驻？ | 依赖 / 说明 |
|------|----------|--------|-------------|
| Long Hunter 主循环 | `python strategy.py` | 是 | LLM；内联 whale-summary + quant overlay |
| Binance 行情 | `python Binance/Binance.py` | 建议 | `BINANCE_*`；本分支标的 `BTCUSDT` |
| 新闻采集+门控 | `python news/news.py` | 是 | `CRYPTONEWS_API_TOKEN`；内嵌 Gates |
| Polymarket 监控+门控 | `python polymarket/monitor_markets.py` | 是 | LLM（Gate）；间隔可用环境变量配置 |
| Long Hunter Console | `python report.py` | 可选 | HTTP `1520`；可手动触发预测 |
| FirstGate（手动） | `python run_first_gate.py` | 否 | 一般由 news/poly 自动触发 |
| SecondGate（手动） | `python run_second_gate.py` | 否 | 一般每 3h 由采集进程触发 |
| 巨鲸摘要 | 无独立启动 | — | `strategy` 内调 API，需 `LONG_HUNTER_WHALE_API_TOKEN` |
| 搜索 / RAG | 无独立启动 | — | `search_tool.py` / `simple_rag.py` 供 Gate 调用 |

---

## 各组件说明与启动方式

### 1. Long Hunter 主循环 · `strategy.py`

周审调度 + 组装上下文（市场结构、gated 新闻/Poly、whale-summary、quant overlay）→ 写预测产物。

```bash
python strategy.py
```

- 默认周审：周六（`LONG_HUNTER_WEEKDAY=5`）`LONG_HUNTER_TIME`（默认 `08:10`，北京时间语义）
- 标的：`LONG_HUNTER_SYMBOL`（默认 `BTCUSDT`）
- 产物：`logs/<run_id>/`、`mem/predictions.json`、可选 `snapshot/`
- **不会**启动交易执行层；本分支无 `execution/` / `whale/` 守护进程

也可在 Console 内触发手动跑（见 `report.py` / `trigger_manual_long_hunter`）。

### 2. Binance 行情 · `Binance/Binance.py`

独立刷新 BTC 多周期指标报告（供本地观察；Long Hunter 自身也会拉 K 线做结构/量化上下文）。

```bash
python Binance/Binance.py
```

- 标的：`BTCUSDT`（本分支）
- 周期：约每 15s 一轮
- 产物：`Binance/metrics_report.md` 等

### 3. 新闻 · `news/news.py`

独立拉 CryptoNews（BTC），进程内跑门控。

```bash
python news/news.py
```

- 轮询：约每 10 分钟
- 增量 → FirstGate → `news/gated.json`
- 约每 3 小时 → SecondGate 清洗
- Long Hunter 读 `news/gated.json` + `news/protocol.py` 文本

### 4. Polymarket · `polymarket/monitor_markets.py`

独立轮询 Gamma，进程内跑门控。

```bash
python polymarket/monitor_markets.py
```

- 间隔：`POLY_MONITOR_INTERVAL_SECONDS`（默认 30，最小 5）
- 有新 event 时 FirstGate → `polymarket/gated.json`
- 约每 3 小时 SecondGate
- 协议分数阈值：`POLYMARKET_PROTOCOL_MIN_SCORE`（`protocol.py`）

### 5. 巨鲸摘要（非独立组件）

无 `whale/` 目录进程。`strategy.py` 在组上下文时请求：

`https://cryptonews-api.com/api/v1/whale-summary`

```bash
# .env 中配置即可，随 strategy 自动使用
LONG_HUNTER_WHALE_API_TOKEN=...
# 兼容也可用 CRYPTONEWS_API_TOKEN 作来源（以你本地 .env 为准）
```

### 6. Long Hunter Console · `report.py`

独立 Web：预测列表、冻结上下文、Gate 进度、手动触发等。

```bash
python report.py
```

- 地址：`http://0.0.0.0:1520`

### 7. 门控脚本（通常不必常驻）

```bash
python run_first_gate.py     # 新闻 + Polymarket FirstGate
python run_second_gate.py    # 新闻 + Polymarket SecondGate
```

Gate 会用到 `search_tool.py` / `simple_rag.py`（随脚本 import，无需单独起服务）。

---

## 推荐启动顺序

```text
1) Binance/Binance.py
2) news/news.py
3) polymarket/monitor_markets.py
4) （可选）report.py
5) strategy.py          # Long Hunter 周审 / 常驻调度
```

## 数据流（简图）

```text
BTC 结构(K线) + news/gated + polymarket/gated + whale-summary API + quant overlay
        ↓
   LongHunterManager.run_once / weekly_monitor
        ↓
 叙事 + 结构化预测
        ↓
 logs/ + mem/predictions.json + Console
```

## 分支关系

| 分支 | 角色 |
|------|------|
| `long` | BTC Long Hunter，只预测（本分支） |
| `short` | ETH 日内执行交易员，可真实下单 |
