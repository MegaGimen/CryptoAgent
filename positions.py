import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from env_config import load_project_env
load_project_env()

from binance import Client


BASE_DIR = Path(__file__).resolve().parent
BJ_TZ = timezone(timedelta(hours=8))


def load_env() -> None:
    from env_config import load_project_env
    load_project_env()


def to_coin_symbol(symbol: str) -> str:
    s = symbol.upper().replace("USDT", "").replace("USD", "")
    return f"{s}USD_PERP"


def build_client() -> Client:
    load_env()
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Missing BINANCE_API_KEY or BINANCE_API_SECRET in .env")
    return Client(api_key, api_secret)


def ms_to_bj_iso(ts_ms: Optional[int]) -> Optional[str]:
    if not ts_ms:
        return None
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=BJ_TZ).isoformat()


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fetch_recent_trades(client: Client, days: float, symbol: str = "ETHUSDT") -> List[Dict[str, Any]]:
    if days <= 0:
        raise ValueError("days must be > 0")

    coin_symbol = to_coin_symbol(symbol)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_trades: List[Dict[str, Any]] = []
    cursor = start_ms
    # Keep each query window <= 24h for stability with Binance userTrades constraints.
    step_ms = 24 * 60 * 60 * 1000

    while cursor <= end_ms:
        window_end = min(cursor + step_ms - 1, end_ms)
        chunk = client.futures_coin_account_trades(
            symbol=coin_symbol,
            startTime=cursor,
            endTime=window_end,
            limit=100,
        )
        if chunk:
            all_trades.extend(chunk)
        cursor = window_end + 1

    all_trades = [t for t in all_trades if int(t.get("time", 0)) >= start_ms]
    all_trades.sort(key=lambda x: (int(x.get("time", 0)), int(x.get("id", 0))))

    return all_trades


def fetch_order_map(client: Client, days: float, symbol: str = "ETHUSDT") -> Dict[int, Dict[str, Any]]:
    coin_symbol = to_coin_symbol(symbol)
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)

    orders: List[Dict[str, Any]] = []
    next_order_id: Optional[int] = None
    page_limit = 100
    # Coin-M all orders endpoint accepts max limit=100, so page by orderId.
    while True:
        params: Dict[str, Any] = {"symbol": coin_symbol, "limit": page_limit}
        if next_order_id is not None:
            params["orderId"] = next_order_id
        page = client.futures_coin_get_all_orders(**params)
        if not page:
            break
        orders.extend(page)
        if len(page) < page_limit:
            break
        last_order_id = int(page[-1].get("orderId", 0) or 0)
        if last_order_id <= 0:
            break
        next_order_id = last_order_id + 1
    order_map: Dict[int, Dict[str, Any]] = {}
    for order in orders:
        order_id_raw = order.get("orderId")
        if order_id_raw is None:
            continue
        order_time = int(order.get("time", 0) or 0)
        # Keep relevant recent orders; include older ones if they were updated recently.
        update_time = int(order.get("updateTime", 0) or 0)
        if order_time < start_ms and update_time < start_ms:
            continue
        order_map[int(order_id_raw)] = order
    return order_map


def enrich_trades_with_order_time(
    trades: List[Dict[str, Any]], order_map: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for t in trades:
        row = dict(t)
        order_id = int(row.get("orderId", 0) or 0)
        t_ms = int(row.get("time", 0) or 0)
        order = order_map.get(order_id, {})
        order_create_ms = int(order.get("time", 0) or 0) if order else None
        order_update_ms = int(order.get("updateTime", 0) or 0) if order else None
        row["fill_time_bj"] = ms_to_bj_iso(t_ms)
        row["order_create_time_bj"] = ms_to_bj_iso(order_create_ms)
        row["order_update_time_bj"] = ms_to_bj_iso(order_update_ms)
        enriched.append(row)
    return enriched


def build_orders_summary(trades: List[Dict[str, Any]], order_map: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[int, Dict[str, Any]] = {}
    for t in trades:
        order_id = int(t.get("orderId", 0) or 0)
        t_ms = int(t.get("time", 0) or 0)
        g = grouped.get(order_id)
        if g is None:
            order = order_map.get(order_id, {})
            g = {
                "order_id": order_id,
                "symbol": t.get("symbol"),
                "side": t.get("side") or order.get("side"),
                "position_side": t.get("positionSide") or order.get("positionSide"),
                "status": order.get("status"),
                "order_type": order.get("type"),
                "order_create_time_bj": ms_to_bj_iso(int(order.get("time", 0) or 0)) if order else None,
                "order_update_time_bj": ms_to_bj_iso(int(order.get("updateTime", 0) or 0)) if order else None,
                "first_fill_time_bj": ms_to_bj_iso(t_ms),
                "last_fill_time_bj": ms_to_bj_iso(t_ms),
                "fill_count": 0,
                "total_qty": Decimal("0"),
                "total_base_qty": Decimal("0"),
                "sum_realized_pnl": Decimal("0"),
                "sum_commission": Decimal("0"),
                "sum_price_x_qty": Decimal("0"),
            }
            grouped[order_id] = g

        g["fill_count"] += 1
        g["total_qty"] += _to_decimal(t.get("qty"))
        g["total_base_qty"] += _to_decimal(t.get("baseQty"))
        g["sum_realized_pnl"] += _to_decimal(t.get("realizedPnl"))
        g["sum_commission"] += _to_decimal(t.get("commission"))
        g["sum_price_x_qty"] += _to_decimal(t.get("price")) * _to_decimal(t.get("qty"))

        first_iso = g["first_fill_time_bj"]
        last_iso = g["last_fill_time_bj"]
        cur_iso = ms_to_bj_iso(t_ms)
        if first_iso is None or (cur_iso is not None and cur_iso < first_iso):
            g["first_fill_time_bj"] = cur_iso
        if last_iso is None or (cur_iso is not None and cur_iso > last_iso):
            g["last_fill_time_bj"] = cur_iso

    summary: List[Dict[str, Any]] = []
    for oid, g in grouped.items():
        total_qty = g["total_qty"]
        avg_price = (g["sum_price_x_qty"] / total_qty) if total_qty != 0 else Decimal("0")
        summary.append(
            {
                "order_id": oid,
                "symbol": g["symbol"],
                "side": g["side"],
                "position_side": g["position_side"],
                "status": g["status"],
                "order_type": g["order_type"],
                "order_create_time_bj": g["order_create_time_bj"],
                "order_update_time_bj": g["order_update_time_bj"],
                "first_fill_time_bj": g["first_fill_time_bj"],
                "last_fill_time_bj": g["last_fill_time_bj"],
                "fill_count": g["fill_count"],
                "total_qty": str(total_qty),
                "total_base_qty": str(g["total_base_qty"]),
                "avg_fill_price": str(avg_price),
                "sum_realized_pnl": str(g["sum_realized_pnl"]),
                "sum_commission": str(g["sum_commission"]),
            }
        )

    summary.sort(
        key=lambda x: (
            x.get("last_fill_time_bj") or "",
            int(x.get("order_id", 0)),
        )
    )
    return summary


def fetch_income_history(
    client: Client,
    days: float,
    symbol: str = "ETHUSDT",
    income_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    coin_symbol = to_coin_symbol(symbol)
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    results: List[Dict[str, Any]] = []
    cursor = start_ms
    limit = 100
    while cursor <= end_ms:
        params: Dict[str, Any] = {
            "symbol": coin_symbol,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": limit,
        }
        if income_type:
            params["incomeType"] = income_type
        page = client.futures_coin_income_history(**params)
        if not page:
            break
        results.extend(page)
        last_time = max(_to_int(x.get("time"), 0) for x in page)
        if last_time <= cursor:
            break
        cursor = last_time + 1
        if len(page) < limit:
            break

    filtered = [x for x in results if start_ms <= _to_int(x.get("time"), 0) <= end_ms]
    filtered.sort(key=lambda x: (_to_int(x.get("time"), 0), str(x.get("tranId", ""))))
    return filtered


def fetch_position_leverage_map(client: Client) -> Dict[Tuple[str, str], Decimal]:
    rows = client.futures_coin_position_information()
    mp: Dict[Tuple[str, str], Decimal] = {}
    for r in rows:
        symbol = str(r.get("symbol", ""))
        side = str(r.get("positionSide", "BOTH")).upper()
        lev = _to_decimal(r.get("leverage"))
        if symbol and lev > 0:
            mp[(symbol, side)] = lev
    return mp


def _position_delta(side: str, position_side: str, qty: Decimal) -> Decimal:
    s = (side or "").upper()
    ps = (position_side or "").upper()
    if ps == "LONG":
        return qty if s == "BUY" else -qty
    if ps == "SHORT":
        return qty if s == "SELL" else -qty
    # one-way mode fallback (BOTH): BUY increases net long, SELL decreases
    return qty if s == "BUY" else -qty


def _position_direction(position_side: str, initial_delta: Decimal) -> str:
    ps = (position_side or "").upper()
    if ps == "LONG":
        return "BUY"
    if ps == "SHORT":
        return "SELL"
    return "BUY" if initial_delta > 0 else "SELL"


def build_position_history_like(
    trades: List[Dict[str, Any]],
    order_map: Dict[int, Dict[str, Any]],
    funding_income: List[Dict[str, Any]],
    position_leverage_map: Dict[Tuple[str, str], Decimal],
) -> List[Dict[str, Any]]:
    state: Dict[Tuple[str, str], Dict[str, Any]] = {}
    completed: List[Dict[str, Any]] = []

    funding_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for row in funding_income:
        sym = str(row.get("symbol", ""))
        funding_by_symbol.setdefault(sym, []).append(row)

    def start_new_position(
        key: Tuple[str, str],
        symbol: str,
        position_side: str,
        t_ms: int,
        delta: Decimal,
        price: Decimal,
        qty: Decimal,
        rpnl: Decimal,
        commission: Decimal,
        leverage: Decimal,
    ) -> None:
        direction = _position_direction(position_side, delta)
        state[key] = {
            "symbol": symbol,
            "position_side": position_side,
            "direction": direction,
            "open_time_ms": t_ms,
            "close_time_ms": t_ms,
            "position_qty": qty,
            "highest_oi": qty,
            "open_qty": qty,
            "open_notional": price * qty,
            "close_qty": Decimal("0"),
            "close_notional": Decimal("0"),
            "realized_pnl": rpnl,
            "commission": commission,
            "leverage": leverage if leverage > 0 else Decimal("0"),
        }

    for t in trades:
        symbol = str(t.get("symbol", ""))
        position_side = str(t.get("positionSide", "BOTH"))
        side = str(t.get("side", ""))
        qty = _to_decimal(t.get("qty"))
        price = _to_decimal(t.get("price"))
        rpnl = _to_decimal(t.get("realizedPnl"))
        commission = _to_decimal(t.get("commission"))
        t_ms = _to_int(t.get("time"), 0)
        order_id = _to_int(t.get("orderId"), 0)
        order = order_map.get(order_id, {})
        leverage = _to_decimal(order.get("leverage"))

        if qty <= 0:
            continue

        key = (symbol, position_side)
        delta = _position_delta(side, position_side, qty)
        pos = state.get(key)

        if pos is None:
            if delta <= 0:
                # carry-in close: position opened before lookback window
                continue
            start_new_position(
                key=key,
                symbol=symbol,
                position_side=position_side,
                t_ms=t_ms,
                delta=delta,
                price=price,
                qty=qty,
                rpnl=rpnl,
                commission=commission,
                leverage=leverage,
            )
            continue

        old_qty = _to_decimal(pos["position_qty"])
        new_qty = old_qty + delta
        pos["close_time_ms"] = max(_to_int(pos["close_time_ms"], 0), t_ms)
        pos["realized_pnl"] += rpnl
        pos["commission"] += commission
        if leverage > 0 and _to_decimal(pos["leverage"]) <= 0:
            pos["leverage"] = leverage

        if delta > 0:
            pos["open_qty"] += qty
            pos["open_notional"] += price * qty
        else:
            reduce_qty = qty if qty <= old_qty else old_qty
            if reduce_qty > 0:
                pos["close_qty"] += reduce_qty
                pos["close_notional"] += price * reduce_qty

        if new_qty < 0:
            new_qty = Decimal("0")
        pos["position_qty"] = new_qty
        if new_qty > pos["highest_oi"]:
            pos["highest_oi"] = new_qty

        if new_qty == 0:
            open_qty = _to_decimal(pos["open_qty"])
            close_qty = _to_decimal(pos["close_qty"])
            open_notional = _to_decimal(pos["open_notional"])
            close_notional = _to_decimal(pos["close_notional"])
            avg_open = (open_notional / open_qty) if open_qty > 0 else Decimal("0")
            avg_close = (close_notional / close_qty) if close_qty > 0 else Decimal("0")
            direction = str(pos["direction"])

            if avg_open > 0:
                if direction == "BUY":
                    price_return_pct = (avg_close - avg_open) / avg_open * Decimal("100")
                else:
                    price_return_pct = (avg_open - avg_close) / avg_open * Decimal("100")
            else:
                price_return_pct = Decimal("0")

            leverage_used = _to_decimal(pos["leverage"])
            roi_source = "trade_order_leverage"
            if leverage_used <= 1:
                key_exact = (str(pos["symbol"]), str(pos["position_side"]).upper())
                key_both = (str(pos["symbol"]), "BOTH")
                mapped = position_leverage_map.get(key_exact, Decimal("0"))
                if mapped <= 0:
                    mapped = position_leverage_map.get(key_both, Decimal("0"))
                    if mapped > 0:
                        roi_source = "position_information_both_leverage"
                else:
                    roi_source = "position_information_leverage"
                if mapped > 1:
                    leverage_used = mapped
            if leverage_used <= 0:
                leverage_used = Decimal("1")
                roi_source = "price_return_only_fallback"

            estimated_roi_pct = price_return_pct * leverage_used

            funding_fee = Decimal("0")
            symbol_funding = funding_by_symbol.get(str(pos["symbol"]), [])
            open_ms = _to_int(pos["open_time_ms"], 0)
            close_ms = _to_int(pos["close_time_ms"], 0)
            for inc in symbol_funding:
                inc_t = _to_int(inc.get("time"), 0)
                if open_ms <= inc_t <= close_ms:
                    funding_fee += _to_decimal(inc.get("income"))

            completed.append(
                {
                    "symbol": pos["symbol"],
                    "direction": direction,
                    "position_side": pos["position_side"],
                    "open_time_bj": ms_to_bj_iso(open_ms),
                    "all_close_time_bj": ms_to_bj_iso(close_ms),
                    "open_price_avg": str(avg_open),
                    "close_price_avg": str(avg_close),
                    "closed_qty": str(close_qty),
                    "max_oi_qty": str(pos["highest_oi"]),
                    "realized_pnl_eth": str(pos["realized_pnl"]),
                    "funding_fee_eth": str(funding_fee),
                    "commission_eth": str(pos["commission"]),
                    "net_pnl_after_fee_eth": str(pos["realized_pnl"] + funding_fee - pos["commission"]),
                    "price_return_pct": str(price_return_pct),
                    "leverage_used": str(leverage_used),
                    "return_rate_pct_estimate": f"{estimated_roi_pct}%",
                    "roi_source": roi_source,
                    "is_complete_closed_position": True,
                }
            )
            del state[key]

    completed.sort(key=lambda x: (x.get("all_close_time_bj") or "", x.get("open_time_bj") or ""))
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Coin-M position-history-like records for audit.")
    parser.add_argument("days", type=float, help="Lookback days, e.g. 1 or 3.5")
    parser.add_argument("--symbol", default="ETHUSDT", help="Base symbol, default ETHUSDT")
    parser.add_argument(
        "--out",
        default=None,
        help="Optional output JSON file path. Default: orders_last_<days>_days.json",
    )
    args = parser.parse_args()

    client = build_client()
    trades = fetch_recent_trades(client, args.days, args.symbol)
    order_map = fetch_order_map(client, args.days, args.symbol)
    funding_income = fetch_income_history(client, args.days, args.symbol, income_type="FUNDING_FEE")
    position_leverage_map = fetch_position_leverage_map(client)
    enriched_trades = enrich_trades_with_order_time(trades, order_map)
    orders_summary = build_orders_summary(enriched_trades, order_map)
    position_history_like = build_position_history_like(
        enriched_trades, order_map, funding_income, position_leverage_map
    )

    out_path = args.out or f"positions_last_{str(args.days).replace('.', '_')}_days.json"
    payload = position_history_like

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(position_history_like)} position records to {out_path}")
    if enriched_trades:
        first_ts = datetime.fromtimestamp(int(enriched_trades[0]["time"]) / 1000, tz=BJ_TZ).isoformat()
        last_ts = datetime.fromtimestamp(int(enriched_trades[-1]["time"]) / 1000, tz=BJ_TZ).isoformat()
        print(f"Time range (Asia/Shanghai): {first_ts} -> {last_ts}")


if __name__ == "__main__":
    main()
