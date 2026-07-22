# CoinAutomation · long

`long` 分支是 **BTC/USDT Long Hunter**：prediction-only 的长线研究 agent，输出 ≥30 天叙事与结构化价格预测，**不下单、不持仓管理**。

## 做什么

- 汇总 BTC 日线/月线结构、News/Polymarket 门控池、CryptoNews 巨鲸摘要、Quant Research Overlay
- 结合历史预测记录，生成长期叙事、驱动因素、失效条件与价格区间
- 通过 Long Hunter Console（`report.py`）查看预测、冻结上下文、Gate 进度与追问

## 不做什么

- 不做日内开平仓或保护单（见 `short` 分支）
- 门控模型（First/SecondGate）只做信息准入/淘汰，不给具体买卖价位

## 核心入口

| 组件 | 路径 | 说明 |
|------|------|------|
| Long Hunter 主流程 | `strategy.py` | 周审调度、上下文组装、预测落盘 |
| 提示词 | `prompts/long_hunter_prompt.md` | 长线预测合同 |
| 新闻 | `news/news.py` + Gates | → `news/gated.json`（BTC 相关） |
| Polymarket | `polymarket/monitor_markets.py` + Gates | → `polymarket/gated.json` |
| 巨鲸摘要 | `strategy.py` 内联 CryptoNews whale-summary API | 注入 `{{whale_summary}}`（不依赖 `whale/` 目录） |
| Quant Overlay | `strategy.py` → `build_quant_research_context` | 结构/回测统计辅助证据 |
| Console | `report.py` + `templates/index.html` | 预测与门控可视化 |

## 产物

每轮写入：

- `logs/<run_id>/input.md` / `output.json` / `prediction.json` / `context/`
- 追加 `mem/predictions.json`
- 可选冻结快照：`snapshot/news|polymarket|whale/`

## 数据流（简图）

```text
BTC 行情结构 + news/gated + polymarket/gated + whale-summary + quant overlay
        ↓
   LongHunterManager.run_once
        ↓
   长线叙事 + 结构化预测
        ↓
 logs/ + mem/predictions.json + Console
```

## 配置

1. `cp .env.example .env`，填写密钥（**仅根目录 `.env`**）
2. 关键项示例：`LLMAPIKEY`、`CRYPTONEWS_API_TOKEN` / `LONG_HUNTER_WHALE_API_TOKEN`、Binance 只读行情密钥等
3. 统一经 `env_config.load_project_env()` 加载

## 常用启动

```bash
# Long Hunter 主进程（周审 / 手动触发逻辑在 strategy 内）
python strategy.py

# 维持门控池（需常驻）
python news/news.py
python polymarket/monitor_markets.py

# Console
python report.py
```

## 分支关系

| 分支 | 角色 |
|------|------|
| `long` | BTC Long Hunter，只预测（本分支） |
| `short` | ETH 日内执行交易员，可真实下单 |
