# Polymarket（Long Hunter 生产链路）

本目录只保留 BTC Long Hunter 需要的预测市场采集与协议输出：

- `monitor_markets.py`：轮询 Gamma，写入 `monitor.json`，并触发 First/SecondGate
- `protocol.py`：读取 `gated.json`，按分数阈值格式化为长线研究上下文

门控脚本在仓库根目录：`run_first_gate.py` / `run_second_gate.py`。
