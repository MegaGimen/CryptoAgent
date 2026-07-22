# Binance Execution Tools (Coin-M Futures)

Production runtime has migrated to in-process LangChain tools.  
`execution/main.py` now exposes callable Python tool functions for the strategy layer (no FastMCP runtime entry in production path).

## Runtime

Import tools directly from `execution/main.py`:

```python
from execution.main import get_langchain_tools

tools = get_langchain_tools()
```

## Tooling (Coin-M only)

1. `get_spot_balance()`
- Query non-zero spot balances.

2. `get_coin_futures_position(symbol)`
- Query Coin-M positions (side/amount/entry/liq/leverage/margin mode).

3. `get_coin_futures_account()`
- Query Coin-M margin asset balances.

4. `get_coin_futures_max_open_position(symbol)`
- Estimate max open contracts from available margin, leverage, mark price, contract size.

5. `transfer_to_coin_futures(asset, amount, direction, explanation)`
- Transfer between spot and Coin-M account.
- `direction`: `TO_FUTURES` / `FROM_FUTURES`.
- `explanation`: required audit reason for the transfer.

6. `set_coin_futures_leverage(symbol, leverage, explanation)`
- Set leverage for Coin-M symbol.
- `explanation`: required audit reason for the leverage change.

7. `set_coin_futures_margin_type(symbol, margin_type, explanation)`
- Set margin mode.
- `margin_type`: `ISOLATED` / `CROSSED`.
- `explanation`: required audit reason for the margin-mode change.

8. `get_coin_futures_open_orders(symbol?)`
- Query open Coin-M orders.

9. `get_coin_futures_order(symbol, order_id)`
- Query one Coin-M order.

10. `cancel_coin_futures_order(symbol, order_id, explanation)`
- Cancel one Coin-M order.
- `explanation`: required audit reason for the cancel action.

11. `cancel_all_coin_futures_orders(symbol, explanation)`
- Cancel all open orders on symbol.
- `explanation`: required audit reason for the cancel-all action.

12. `modify_coin_futures_order(symbol, explanation, order_id?, orig_client_order_id?, side?, quantity?, price?)`
- 修改现有 LIMIT 订单的价格或数量。
- 注意：修改后的订单会重新排队。
- `explanation`: 必填，说明改价/改量原因。

13. `trade_coin_futures(symbol, side, explanation, quantity?, price?, order_type, stop_price?, reduce_only?, position_side?, time_in_force?, callback_rate?, activation_price?, close_position?)`
- 币本位下单主工具。
- `explanation`: 必填，说明本次下单的触发条件/风控理由。
- `quantity`: 标的数量（按交易所 `step_size` 对齐）。当 `close_position=True` 时可不传。
- `order_type`:
    - `MARKET`: 市价单
    - `LIMIT`: 限价单（需 `price`）
    - `STOP` / `TAKE_PROFIT`: 限价止损/止盈（需 `price` 和 `stop_price`）
    - `STOP_MARKET` / `TAKE_PROFIT_MARKET`: 市价止盈止损（需 `stop_price`）
    - `TRAILING_STOP_MARKET`: 跟踪委托（需 `callback_rate`，可选 `activation_price`）
- `time_in_force`: `GTC` (一直有效), `IOC` (立即成交否则取消), `FOK` (全部成交否则取消), `GTX` (只做 Maker)
- `callback_rate`: 跟踪委托回调幅度 (0.1 到 5.0，代表 0.1% 到 5%)
- `close_position`: `True` 表示“全平仓”模式，用于市价止盈止损单，无需传 `quantity`。

13. `close_coin_futures_position(symbol, explanation, quantity?, position_side?)`
- Flatten Coin-M position by side.
- `explanation`: required audit reason for the close/reduce action.

14. `calculate_expression(expression)`
- Safe arithmetic helper.

15. `set_alarm(value, unit, prompt, explanation)` / `delete_alarm(alarm_id, explanation)`
- Alarm scheduler for strategy wakeups.
- `explanation`: required audit reason for setting/deleting the alarm.

## Integration

`strategy.py` injects these tools into LangChain/LangGraph agent nodes directly in-process.
All trading semantics are Coin-M futures; legacy margin-spot tool names are deprecated.
