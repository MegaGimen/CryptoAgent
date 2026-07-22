# CoinAutomation · short

`short` 分支是 **ETH/USDT 日内短线执行交易员**：事件驱动唤醒，经 LangGraph 状态机完成观察、开平仓、保护单与记忆维护。

## 做什么

- 消费多周期行情（`Binance/`）、账户状态、已门控新闻与 Polymarket、可选巨鲸信号
- 通过 `execution/main.py` 的 U 本位合约工具真实下单 / 平仓 / 保护单 / range plan
- 用 `mem/short.json`、`mem/long.json` 等结构化记忆保持跨轮一致性
- 日审与止损反思写入长期/执行反思文件，供后续短线回合读取

## 不做什么

- 不做 BTC 长线「只预测不下单」的 Long Hunter（见 `long` 分支）
- 不把 News/Polymarket 门控模型当成交易员；门控只维护高质量背景池

## 核心入口

| 组件 | 路径 | 说明 |
|------|------|------|
| 策略主循环 | `strategy.py` | 唤醒合并、状态机、提示词组装、守卫与落盘 |
| 执行工具 | `execution/main.py` | U 本位交易 / 查询 / 闹钟 / range |
| 短线提示词 | `prompts/agentprompt.md` | 系统侧执行合同 |
| 行情监控 | `Binance/Binance.py` | 指标报告与告警 |
| 新闻采集 | `news/news.py` | → First/SecondGate → `news/gated.json` |
| Polymarket | `polymarket/monitor_markets.py` | → Gate → `polymarket/gated.json` |
| 巨鲸（可选） | `whale/whalealert/protocol.py` | `ENABLE_WHALE_MONITOR=true` 时 WebSocket 唤醒 |
| 复盘 / 报表 | `report.py`、`gentestreport.py` | 可视化与审计 |

## 数据流（简图）

```text
行情/成交/闹钟/新闻/Poly/(可选)巨鲸
        ↓
   strategy 唤醒合并
        ↓
 load account → news → polymarket → memory → compose_prompt
        ↓
 propose → execute(tools) → verify
        ↓
 logs/<ts>/ + mem/*.json + clock.json
```

## 配置

1. `cp .env.example .env`，填写密钥（**仅根目录 `.env`**）
2. 运行时统一通过 `env_config.load_project_env()` 加载
3. 开发约定见 `AGENTS.md`

## 常用启动

```bash
# 策略主进程（生产短线）
python strategy.py

# Polymarket 监控（需常驻，才会刷新 gated 池）
python polymarket/monitor_markets.py

# 新闻采集（需常驻）
python news/news.py
```

## 分支关系

| 分支 | 角色 |
|------|------|
| `short` | ETH 日内执行交易员（本分支） |
| `long` | BTC Long Hunter，prediction-only 长线研究 |
