---
name: position-backtest-review
description: 基于 positions.py 对最近 3 天或用户指定 N 天仓位闭环进行回测复盘与绩效总结，并强制结合 log_lookup MCP 日志证据做全链路归因。Use when the user asks to evaluate recent trading performance, analyze position-level PnL/ROI/win-loss patterns, or produce a multi-factor summary that must correlate positions with model execution logs.
---

# Position Backtest Review

1. 运行仓位提取脚本。

```bash
python /home/coinautomation/positions.py <days>
```

2. 读取输出文件：`positions_last_<days>_days.json`。

3. 基于列表做统计（至少包含以下项目）：
- 总仓位数
- 胜率（`realized_pnl_eth > 0`）
- 总 `realized_pnl_eth`
- 总 `net_pnl_after_fee_eth`
- 多头与空头分别的次数与净盈亏
- 最大单笔亏损与最大单笔盈利
- 平均回报率（从 `return_rate_pct_estimate` 去掉 `%` 后按数值统计）

4. 识别异常与重点样本：
- 按亏损绝对值排序列出前 3 笔
- 按收益绝对值排序列出前 3 笔
- 标记 `roi_source` 非 `position_information_leverage` 的记录并说明口径风险

5. 必须执行日志取证链路（不要跳过）：
- 先调用 `list_log_times` 获取可用日志时间全集。
- 将仓位时间（`open_time_bj` / `all_close_time_bj`）转成 `YYYYMMDD_HHMMSS` 候选值。
- 先按时间范围过滤候选日志（最近 N 天）；再从中选每笔仓位最近的日志点。
- 对每个重点样本调用：
  - `get_nearest_input_md(time_str=...)`
  - `get_nearest_output_json(time_str=...)`
- 从日志提取：当轮决策意图、工具调用序列、reanswer/guard 拦截、最终执行动作、是否出现一致性修正。
- 给出“市场因素 vs 策略执行因素”拆分结论，并标注证据来源时间戳。

6. 输出总结时使用固定结构：
- `时间范围`
- `核心绩效指标`
- `关键亏损样本`
- `关键盈利样本`
- `模型执行质量观察`
- `可执行改进建议（最多 5 条）`

7. 约束：
- 只基于真实产物（positions 脚本输出 + 日志）；不要使用任何“预期文件/展望文件”。
- 时间口径统一北京时间。
- 明确区分“事实”与“推断”。
- 禁止只复述 `positions_last_*_days.json`；必须包含日志证据与归因结论。
- 若日志缺失，必须显式写出“缺失哪些日志、对结论造成的影响”。
