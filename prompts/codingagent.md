# Coding Agent Long-Term Notes

## 2026-04-17 Stable Pattern
- 新增技能：`skills/backtest-decision-audit`。
- 标准流程：接收两个时间点 -> 格式化为 `YYYYMMDD_HHMMSS` -> 执行 `python gentestreport.py <start> <end>` -> 读取 `backtest_report.txt` -> 输出结构化“决策合理性审计报告”。
- 报告必须覆盖：趋势环境、风险阈值与仓位纪律、执行原子性、证伪闭环、改进清单。
- `experience.md` 若出现错误经验，仅允许清空，不允许逐条修改。
