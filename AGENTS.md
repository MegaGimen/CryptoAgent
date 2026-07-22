# Long Hunter 开发指南（long 分支）

本分支是 **BTC/USDT Long Hunter**：prediction-only 长线研究 agent，**禁止下单与持仓管理**。

## 职责边界

- 输出：≥30 天叙事、结构化价格预测、驱动因素、失效条件
- 不输出：具体开平仓指令、保护单、range plan、交易所下单
- First/SecondGate 只做信息准入/淘汰，不得给出买卖价位地图

## 核心文件

| 路径 | 作用 |
|------|------|
| `strategy.py` | 周审调度、上下文组装、模型调用、预测落盘 |
| `prompts/long_hunter_prompt.md` | 长线预测主提示词 |
| `news/` + `run_*_gate.py` | 新闻采集与双层门控 → `news/gated.json` |
| `polymarket/` + gates | 预测市场采集与门控 → `polymarket/gated.json` |
| `report.py` / `templates/` | Long Hunter Console |
| `env_config.py` | 仅加载仓库根目录 `.env` |

## 密钥

- 全项目只使用根目录 `.env`（见 `.env.example`）
- 经 `env_config.load_project_env()` 加载
- 禁止再建 `Binance/.env` 等分散密钥文件

## 产物

- `logs/<run_id>/input.md`、`output.json`、`prediction.json`、`context/`
- `mem/predictions.json` 追加历史预测
- 可选 `snapshot/` 冻结上下文

## 与 short 的关系

| 分支 | 角色 |
|------|------|
| `long` | BTC 长线预测（本分支） |
| `short` | ETH 日内执行交易员 |
