import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from env_config import load_project_env
load_project_env()
import csv
import json
import math
import os
import re
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple

from binance.client import Client
from binance.helpers import round_step_size
from filelock import FileLock
from langchain.tools import tool as langchain_tool

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from range import (
    TRIGGER_MARKET_EXECUTION_STYLE,
    collect_trigger_actions,
    prepare_trigger_plan_runtime,
    range_plan_execution_style,
    reset_trigger_level,
)

TRADE_LOG_FILE = "/home/coinautomation/trade_history.csv"
CLOCK_JSON_PATH = "/home/coinautomation/clock.json"

MEM_DIR = os.path.join(BASE_DIR, "mem")
RANGE_PLAN_PATH = os.path.join(MEM_DIR, "range_plan.json")
MARKET_REPORT_PATH = os.path.join(BASE_DIR, "Binance", "metrics_report.md")
os.makedirs(MEM_DIR, exist_ok=True)

api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

symbol_info_cache: Dict[str, dict] = {}
account_position_mode: Dict[str, Any] = {}

USDT_FUTURES_NEW_ORDER_DOC = "https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order"
USDT_FUTURES_ERROR_CODE_DOC = "https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code"
SUPPORTED_USDT_FUTURES_ORDER_TYPES = {
    "LIMIT",
    "MARKET",
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
}
COIN_M_NEW_ORDER_DOC = USDT_FUTURES_NEW_ORDER_DOC
COIN_M_ERROR_CODE_DOC = USDT_FUTURES_ERROR_CODE_DOC
SUPPORTED_COIN_M_ORDER_TYPES = SUPPORTED_USDT_FUTURES_ORDER_TYPES
PROTECTIVE_ORDER_TYPES = {
    "STOP",
    "STOP_MARKET",
    "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET",
}
CAPACITY_DIRTY_TOOLS = {
    "set_usdt_futures_leverage",
    "set_usdt_futures_margin_type",
    "transfer_to_usdt_futures",
    "cancel_usdt_futures_order",
    "cancel_all_usdt_futures_orders",
    "close_usdt_futures_position",
}
PRIMARY_EXECUTE_STAGES = {
    "execute_primary_model",
    "execute_primary_retry",
}
OPENING_GUARD_ACTIVE_STAGES = PRIMARY_EXECUTE_STAGES | {
    "execute_followup",
}

_tool_recorder_ctx: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar(
    "coinautomation_tool_recorder",
    default=None,
)
_tool_stage_ctx: ContextVar[str] = ContextVar(
    "coinautomation_tool_stage",
    default="unknown",
)


def set_tool_recorder(recorder: List[Dict[str, Any]]):
    """Bind a per-run tool call recorder for strategy logging."""
    return _tool_recorder_ctx.set(recorder)


def reset_tool_recorder(token) -> None:
    _tool_recorder_ctx.reset(token)


def set_tool_stage(stage: str):
    """Bind the current strategy stage for tool-call recorder metadata."""
    return _tool_stage_ctx.set(str(stage or "unknown"))


def reset_tool_stage(token) -> None:
    _tool_stage_ctx.reset(token)


def _record_tool_call(name: str, args: Dict[str, Any], result: Any) -> None:
    recorder = _tool_recorder_ctx.get()
    if recorder is None:
        return
    tool_call_id = f"tc_{len(recorder) + 1:04d}"
    explanation = ""
    if isinstance(args, dict):
        explanation = str(args.get("explanation", "") or "").strip()
    guard_rule_id = None
    guard_retryable = None
    if isinstance(result, dict):
        guard_rule_id = result.get("guard_rule_id") or result.get("error_class")
        if "retryable" in result:
            guard_retryable = bool(result.get("retryable"))
    recorder.append(
        {
            "id": tool_call_id,
            "tool": name,
            "args": args,
            "result": result,
            "stage": _tool_stage_ctx.get(),
            "guard_rule_id": guard_rule_id,
            "guard_retryable": guard_retryable,
            "tool_call_explanation": explanation,
            "recorded_at": datetime.now().isoformat(),
        }
    )


def _structured_tool_error(
    tool_name: str,
    *,
    error_class: str,
    why_rejected: str,
    model_fix_hint: str,
    exchange_code: Optional[int] = None,
    docs_basis: Optional[str] = None,
    retryable: bool = False,
    defer_until_next_turn: bool = False,
    invalid_args: Optional[Dict[str, Any]] = None,
    allowed_shapes: Optional[List[str]] = None,
    raw_message: Optional[str] = None,
) -> Dict[str, Any]:
    current_stage = _tool_stage_ctx.get()
    res: Dict[str, Any] = {
        "status": "error",
        "tool": tool_name,
        "is_hard_guard": True,
        "guard_rule_id": error_class,
        "guard_stage": current_stage,
        "error_source": "binance_usdt_futures_guard" if exchange_code is None else "binance_usdt_futures_api",
        "error_class": error_class,
        "why_rejected": why_rejected,
        "model_fix_hint": model_fix_hint,
        "retryable": retryable,
        "defer_until_next_turn": defer_until_next_turn,
    }
    if exchange_code is not None:
        res["exchange_code"] = exchange_code
    if docs_basis:
        res["docs_basis"] = docs_basis
    if invalid_args:
        res["invalid_args"] = invalid_args
    if allowed_shapes:
        res["allowed_shapes"] = allowed_shapes
    if raw_message:
        res["message"] = raw_message
    return res


def _require_action_explanation(tool_name: str, explanation: str, call_args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if str(explanation or "").strip():
        return None
    return _structured_tool_error(
        tool_name,
        error_class="missing_action_explanation",
        why_rejected="This state-changing tool requires a non-empty explanation.",
        model_fix_hint=(
            "Provide a short explanation describing why this action is being taken now, "
            "what hypothesis or risk reason it serves, and any key dependency."
        ),
        docs_basis="CoinAutomation action-tool contract: key mutating tools require explanation for auditability.",
        invalid_args=call_args,
        retryable=True,
        defer_until_next_turn=False,
    )


def _extract_exchange_error_code(message: str) -> Optional[int]:
    match = re.search(r"code=(-?\d+)", str(message))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_success_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    status = str(result.get("status", "")).strip().lower()
    return status not in {"error", "failed", "rejected"}


def _is_effective_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    status = str(result.get("status", "")).strip().lower()
    return status not in {"error", "failed", "rejected", "skipped"}


def _normalize_alarm_condition_signature(raw_condition: Any) -> str:
    if isinstance(raw_condition, (dict, list)):
        return json.dumps(raw_condition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text = str(raw_condition or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return re.sub(r"\s+", " ", text)


def _housekeeping_signature(tool_name: str, args: Dict[str, Any]) -> Optional[tuple]:
    if tool_name == "set_alarm":
        return (
            "set_alarm",
            str(args.get("value", "")).strip(),
            str(args.get("unit", "")).strip().lower(),
            str(args.get("prompt", "")).strip(),
            _normalize_alarm_condition_signature(args.get("condition")),
        )
    if tool_name == "cancel_usdt_futures_order":
        return (
            "cancel_usdt_futures_order",
            _get_usdt_futures_symbol(str(args.get("symbol", ""))),
            str(args.get("order_id", "")).strip(),
        )
    if tool_name == "delete_alarm":
        return (
            "delete_alarm",
            str(args.get("alarm_id", "")).strip(),
        )
    return None


def _guard_duplicate_followup_housekeeping(tool_name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    current_stage = _tool_stage_ctx.get()
    if current_stage != "execute_followup":
        return None
    signature = _housekeeping_signature(tool_name, args)
    if signature is None:
        return None
    recorder = _tool_recorder_ctx.get() or []
    for call in recorder:
        if str(call.get("stage", "")) not in {"execute_primary_model", "execute_primary_retry"}:
            continue
        if str(call.get("tool", "")) != tool_name:
            continue
        if not _is_effective_result(call.get("result")):
            continue
        prior_sig = _housekeeping_signature(tool_name, call.get("args", {}) if isinstance(call.get("args"), dict) else {})
        if prior_sig != signature:
            continue
        return _structured_tool_error(
            tool_name,
            error_class="duplicate_followup_action",
            why_rejected="execute_followup attempted to repeat a housekeeping action already completed in execute_primary.",
            model_fix_hint="Skip duplicate action and continue with only remaining follow-up tasks.",
            docs_basis="CoinAutomation followup idempotency guard.",
            retryable=True,
            defer_until_next_turn=False,
            invalid_args=args,
        )
    return None


def _guard_range_plan_conflict(
    tool_name: str,
    *,
    symbol: str,
    order_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    plan = _read_range_plan_state()
    if not _range_plan_is_active(plan):
        return None
    plan_symbol = _get_usdt_futures_symbol(plan.get("symbol", "ETHUSDT"))
    current_symbol = _get_usdt_futures_symbol(symbol)
    if current_symbol != plan_symbol:
        return None
    if tool_name == "trade_usdt_futures":
        return _structured_tool_error(
            tool_name,
            error_class="range_plan_active_conflict",
            why_rejected="A live range automation plan already owns this symbol's managed entry lifecycle.",
            model_fix_hint="Use cancel_range_plan before placing discretionary MARKET/LIMIT entries, or let the active range plan continue managing the symbol.",
            docs_basis="CoinAutomation range plan guard: discretionary opening orders are blocked while active range automation owns the symbol.",
            retryable=False,
            defer_until_next_turn=False,
            invalid_args={"symbol": current_symbol},
        )
    tracked_ids = _tracked_range_order_ids(plan)
    if tool_name == "cancel_all_usdt_futures_orders":
        return _structured_tool_error(
            tool_name,
            error_class="range_plan_active_conflict",
            why_rejected="cancel_all would destroy the tracked order lattice of the active range automation plan.",
            model_fix_hint="Use cancel_range_plan to stop the automation cleanly instead of cancel_all on the managed symbol.",
            docs_basis="CoinAutomation range plan guard: range-plan-managed orders must be cleaned up through cancel_range_plan.",
            retryable=False,
            defer_until_next_turn=False,
            invalid_args={"symbol": current_symbol},
        )
    if tool_name == "cancel_usdt_futures_order" and str(order_id or "").strip() in tracked_ids:
        return _structured_tool_error(
            tool_name,
            error_class="range_plan_active_conflict",
            why_rejected="This order_id belongs to the active range automation plan.",
            model_fix_hint="Use cancel_range_plan to stop the managed order lattice, rather than removing tracked range orders one by one.",
            docs_basis="CoinAutomation range plan guard: tracked range-plan orders must be managed by the range automation lifecycle.",
            retryable=False,
            defer_until_next_turn=False,
            invalid_args={"symbol": current_symbol, "order_id": str(order_id or "")},
        )
    return None


def _detect_retry_duplicate_housekeeping_warning(tool_name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    current_stage = _tool_stage_ctx.get()
    if current_stage != "execute_primary_retry":
        return None
    signature = _housekeeping_signature(tool_name, args)
    if signature is None:
        return None
    recorder = _tool_recorder_ctx.get() or []
    for call in reversed(recorder):
        if str(call.get("stage", "")) not in PRIMARY_EXECUTE_STAGES:
            continue
        if str(call.get("tool", "")) != tool_name:
            continue
        if not _is_effective_result(call.get("result")):
            continue
        prior_sig = _housekeeping_signature(tool_name, call.get("args", {}) if isinstance(call.get("args"), dict) else {})
        if prior_sig != signature:
            continue
        return {
            "warning_class": "retry_duplicate_action_warning",
            "warning": (
                "Retry repeated a housekeeping action that was already completed earlier in this wakeup."
            ),
            "duplicate_of_call_id": str(call.get("id", "")),
            "duplicate_tool": tool_name,
            "duplicate_stage": str(call.get("stage", "")),
        }
    return None


def _get_raw_usdt_futures_positions(symbol: str) -> List[Dict[str, Any]]:
    return list(client.futures_position_information(symbol=symbol))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_margin_type(raw_position: Dict[str, Any]) -> str:
    raw_margin_type = str(raw_position.get("marginType", "") or "").strip().upper()
    if raw_margin_type in {"ISOLATED", "CROSSED", "CROSS"}:
        return "ISOLATED" if raw_margin_type == "ISOLATED" else "CROSSED"

    isolated_flag = raw_position.get("isolated")
    if isinstance(isolated_flag, str):
        isolated_flag = isolated_flag.strip().lower() in {"true", "1", "yes"}
    if isinstance(isolated_flag, bool):
        return "ISOLATED" if isolated_flag else "CROSSED"

    isolated_margin = abs(_safe_float(raw_position.get("isolatedMargin"), 0.0))
    if isolated_margin > 0:
        return "ISOLATED"
    return "CROSSED"


def _extract_position_leverage(raw_position: Optional[Dict[str, Any]], default: int = 20) -> Tuple[int, str]:
    if not isinstance(raw_position, dict):
        return int(default), "default_no_position_row"

    raw_leverage = raw_position.get("leverage")
    if raw_leverage is not None and str(raw_leverage).strip() != "":
        try:
            parsed = int(float(raw_leverage))
            if parsed > 0:
                return parsed, "positionRisk_leverage_field"
        except (TypeError, ValueError):
            pass

    notional_abs = abs(_safe_float(raw_position.get("notional"), 0.0))
    position_initial_margin = _safe_float(raw_position.get("positionInitialMargin"), 0.0)
    if position_initial_margin <= 0:
        position_initial_margin = _safe_float(raw_position.get("initialMargin"), 0.0)
    if notional_abs > 0 and position_initial_margin > 0:
        derived = int(round(notional_abs / position_initial_margin))
        return max(1, derived), "derived_from_notional_over_initial_margin"

    return int(default), "default_missing_in_positionRisk_v3"


def _get_active_position_snapshot(symbol: str) -> Dict[str, Any]:
    positions = _get_raw_usdt_futures_positions(symbol)
    active: Dict[str, Any] = {}
    for pos in positions:
        amt = float(pos.get("positionAmt", 0) or 0)
        if amt == 0:
            continue
        side_key = str(pos.get("positionSide", "BOTH")).upper()
        if side_key == "BOTH":
            side_key = "LONG" if amt > 0 else "SHORT"
        active[side_key] = {
            "symbol": pos.get("symbol", symbol),
            "position_side": side_key,
            "amount": amt,
            "abs_amount": abs(amt),
            "raw": pos,
        }
    return active


def _extract_chart_section(report_text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}\s+chart\s*\n(.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(str(report_text or ""))
    return match.group(1) if match else ""


def _extract_section_numeric(section: str, label: str) -> Optional[float]:
    match = re.search(
        rf"\*\*{re.escape(label)}\*\*:\s*([-+]?\d+(?:\.\d+)?)",
        str(section or ""),
    )
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_section_bollinger_lower(section: str) -> Optional[float]:
    match = re.search(
        r"\*\*Bollinger Bands\*\*:\s*Upper\s+[-+]?\d+(?:\.\d+)?\s*\|\s*Mid\s+[-+]?\d+(?:\.\d+)?\s*\|\s*Lower\s+([-+]?\d+(?:\.\d+)?)",
        str(section or ""),
    )
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_section_momentum_field(section: str, field_name: str) -> str:
    match = re.search(
        rf"\*\*{re.escape(field_name)}\*\*:\s*`([^`]+)`",
        str(section or ""),
    )
    return str(match.group(1)).strip().lower() if match else ""


def _load_market_execution_context() -> Dict[str, Any]:
    if not os.path.exists(MARKET_REPORT_PATH):
        return {}
    try:
        with open(MARKET_REPORT_PATH, "r", encoding="utf-8") as f:
            report_text = f.read()
    except Exception:
        return {}

    metrics: Dict[str, Any] = {}
    for heading, suffix in [("15 minutes", "15m"), ("1h", "1h")]:
        section = _extract_chart_section(report_text, heading)
        if not section:
            continue
        metrics[f"RSI_{suffix}"] = _extract_section_numeric(section, "RSI(14)")
        metrics[f"lower_bb_{suffix}"] = _extract_section_bollinger_lower(section)
        metrics[f"price_momentum_{suffix}"] = _extract_section_momentum_field(section, "price_momentum")
        metrics[f"macd_histo_state_{suffix}"] = _extract_section_momentum_field(section, "macd_histo_state")
    return metrics


def _infer_opening_trade_direction(side: str, position_side: str, is_hedge: bool) -> str:
    side_upper = str(side or "").upper()
    position_side_upper = str(position_side or "").upper()

    # In hedge mode, SELL+LONG and BUY+SHORT are reducing/protective shapes,
    # not new directional entries. Entry-quality guards must ignore them.
    if position_side_upper == "LONG":
        return "long" if side_upper == "BUY" else ""
    if position_side_upper == "SHORT":
        return "short" if side_upper == "SELL" else ""
    if side_upper not in {"BUY", "SELL"}:
        return ""
    if is_hedge:
        return "long" if side_upper == "BUY" else "short"
    return "long" if side_upper == "BUY" else "short"


def _guard_short_entry_quality(
    *,
    symbol: str,
    side: str,
    position_side: str,
    order_type: str,
    price: Optional[float],
    is_hedge: bool,
    call_args: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    direction = _infer_opening_trade_direction(side, position_side, is_hedge)
    if direction != "short":
        return _guard_long_entry_quality(
            symbol=symbol,
            side=side,
            position_side=position_side,
            order_type=order_type,
            price=price,
            is_hedge=is_hedge,
            call_args=call_args,
        )
    if order_type not in {"MARKET", "LIMIT"}:
        return None

    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        current_price = float(ticker[0]["price"]) if isinstance(ticker, list) else float(ticker["price"])
    except Exception:
        current_price = None

    metrics = _load_market_execution_context()
    rsi_15m = metrics.get("RSI_15m")
    rsi_1h = metrics.get("RSI_1h")
    lower_bb_15m = metrics.get("lower_bb_15m")

    if current_price is None or rsi_15m is None:
        return None

    oversold_15m = rsi_15m <= 30
    oversold_1h = rsi_1h is not None and rsi_1h <= 30
    near_lower_band = lower_bb_15m is not None and current_price <= lower_bb_15m * 1.006

    limit_not_pullback = order_type == "LIMIT" and (price is None or price <= current_price * 1.002)
    chase_shape = order_type == "MARKET" or limit_not_pullback
    if not chase_shape:
        return None

    if oversold_15m and (oversold_1h or near_lower_band):
        return _structured_tool_error(
            "trade_usdt_futures",
            error_class="short_entry_quality_violation",
            why_rejected=(
                "Opening short here looks like chasing an already-extended breakdown: "
                "15m is oversold and price is already pressed into the lower-band / multi-timeframe extension zone."
            ),
            model_fix_hint=(
                "Do not market-short into deep oversold extension. Wait for a rebound-fail / lower-high retest, "
                "or place a pullback limit higher instead of selling at the extension low."
            ),
            docs_basis="CoinAutomation short-entry quality guard: no chase-short into oversold extension.",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
    return None


def _guard_long_entry_quality(
    *,
    symbol: str,
    side: str,
    position_side: str,
    order_type: str,
    price: Optional[float],
    is_hedge: bool,
    call_args: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    direction = _infer_opening_trade_direction(side, position_side, is_hedge)
    if direction != "long":
        return None
    if order_type not in {"MARKET", "LIMIT"}:
        return None

    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        current_price = float(ticker[0]["price"]) if isinstance(ticker, list) else float(ticker["price"])
    except Exception:
        current_price = None

    metrics = _load_market_execution_context()
    rsi_15m = metrics.get("RSI_15m")
    rsi_1h = metrics.get("RSI_1h")
    macd_histo_state_15m = str(metrics.get("macd_histo_state_15m", "") or "").lower()
    macd_histo_state_1h = str(metrics.get("macd_histo_state_1h", "") or "").lower()
    price_momentum_1h = str(metrics.get("price_momentum_1h", "") or "").lower()

    if current_price is None or rsi_15m is None:
        return None

    limit_not_pullback = order_type == "LIMIT" and (price is None or price >= current_price * 0.998)
    chase_shape = order_type == "MARKET" or limit_not_pullback
    if not chase_shape:
        return None

    weak_1h_backdrop = (
        rsi_1h is not None
        and rsi_1h < 50
        and (
            "bear" in macd_histo_state_1h
            or "contracting" in macd_histo_state_1h
            or "down" in price_momentum_1h
        )
    )
    shallow_15m_bounce = rsi_15m < 45
    if weak_1h_backdrop and shallow_15m_bounce:
        return _structured_tool_error(
            "trade_usdt_futures",
            error_class="long_entry_quality_violation",
            why_rejected=(
                "Opening long here looks like buying the first shallow bounce before 1h structure/momentum repaired. "
                "15m rebound is still weak while the 1h backdrop remains unrepaired."
            ),
            model_fix_hint=(
                "Do not use a market / near-market long to buy the first shallow rebound. "
                "Wait for clearer reclaim evidence such as higher-low + hold, reclaim + retest acceptance, "
                "or explicit 1h momentum repair before opening long."
            ),
            docs_basis="CoinAutomation long-entry quality guard: no first-bounce long against unrepaired 1h weakness.",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )

    overextended_15m = rsi_15m >= 68
    overheated_1h = rsi_1h is not None and rsi_1h >= 65
    weak_follow_through = "contracting" in macd_histo_state_15m
    if overextended_15m and (overheated_1h or weak_follow_through):
        return _structured_tool_error(
            "trade_usdt_futures",
            error_class="long_entry_quality_violation",
            why_rejected=(
                "Opening long here looks like chasing a weak reclaim / late extension: "
                "15m is already stretched and the follow-through quality is weakening."
            ),
            model_fix_hint=(
                "Do not market-long into a late extension. "
                "Wait for reclaim + retest hold, or use a pullback limit lower instead of paying up at the extension high."
            ),
            docs_basis="CoinAutomation long-entry quality guard: no chase-long into weak reclaim / stretched extension.",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
    return None


def _find_protective_target(position_snapshot: Dict[str, Any], side: str, position_side: str) -> Optional[Dict[str, Any]]:
    if position_side in {"LONG", "SHORT"}:
        target = position_snapshot.get(position_side)
        if not target:
            return None
        expected_side = "SELL" if position_side == "LONG" else "BUY"
        return target if side == expected_side else None

    if position_side == "BOTH":
        if len(position_snapshot) != 1:
            return None
        only_target = next(iter(position_snapshot.values()))
        expected_side = "SELL" if only_target["position_side"] == "LONG" else "BUY"
        return only_target if side == expected_side else None

    return None


def _is_successful_opening_trade_call(call: Dict[str, Any], symbol: str) -> bool:
    if str(call.get("tool", "")) != "trade_usdt_futures":
        return False
    args = call.get("args", {})
    if _get_usdt_futures_symbol(str(args.get("symbol", symbol))) != symbol:
        return False
    if str(args.get("order_type", "")).upper() not in {"MARKET", "LIMIT"}:
        return False
    return _is_success_result(call.get("result"))


def _classify_binance_order_error(
    tool_name: str,
    message: str,
    order_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    code = _extract_exchange_error_code(message)
    if code == -1116:
        return _structured_tool_error(
            tool_name,
            error_class="unsupported_order_type",
            why_rejected="Binance USD(S)-M New Order does not accept this order_type for /fapi/v1/order.",
            model_fix_hint="Use one of LIMIT, MARKET, STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET.",
            exchange_code=code,
            docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}) and Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}#-1116-invalid_order_type).",
            retryable=True,
            invalid_args=order_args or {},
            allowed_shapes=list(sorted(SUPPORTED_USDT_FUTURES_ORDER_TYPES)),
            raw_message=message,
        )
    if code == -1128:
        return _structured_tool_error(
            tool_name,
            error_class="optional_params_bad_combo",
            why_rejected="The optional parameter combination is invalid for Binance USD(S)-M order placement.",
            model_fix_hint="Pick one legal shape only: either closePosition=true without quantity, or quantity + reduce_only=true/false with a directionally consistent position_side.",
            exchange_code=code,
            docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}) and Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}#-1128-optional_params_bad_combo).",
            retryable=True,
            invalid_args=order_args or {},
            raw_message=message,
        )
    if code == -4060:
        return _structured_tool_error(
            tool_name,
            error_class="invalid_position_side",
            why_rejected="The supplied position_side is invalid for the current Binance position mode.",
            model_fix_hint="Use LONG or SHORT in Hedge Mode; use BOTH only when the account is in One-way Mode and the order is not a directional hedge-side protective order.",
            exchange_code=code,
            docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}) and Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}#-4060-invalid_position_side).",
            retryable=True,
            invalid_args=order_args or {},
            raw_message=message,
        )
    if code == -4061:
        return _structured_tool_error(
            tool_name,
            error_class="position_side_not_match",
            why_rejected="The order position_side does not match the user's current Binance position mode or order direction.",
            model_fix_hint="Align side and position_side with the live held position. LONG protection must be SELL+LONG; SHORT protection must be BUY+SHORT.",
            exchange_code=code,
            docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}) and Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}#-4061-position_side_not_match).",
            retryable=True,
            invalid_args=order_args or {},
            raw_message=message,
        )
    if code == -2022:
        return _structured_tool_error(
            tool_name,
            error_class="reduce_only_rejected",
            why_rejected="Binance rejected the reduce-only order.",
            model_fix_hint="Re-check current position size, side, and open protective orders before submitting another reduce-only or close-position order.",
            exchange_code=code,
            docs_basis=f"Binance Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}#-2022-reduce_only_reject).",
            retryable=False,
            invalid_args=order_args or {},
            raw_message=message,
        )
    if code == -2026:
        return _structured_tool_error(
            tool_name,
            error_class="reduce_only_order_type_not_supported",
            why_rejected="This order type is not supported together with reduce_only under Binance USD(S)-M rules.",
            model_fix_hint="Use STOP_MARKET or TAKE_PROFIT_MARKET with a legal reduce-only or closePosition shape instead of the unsupported combination.",
            exchange_code=code,
            docs_basis=f"Binance Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}#-2026-reduce_only_order_type_not_supported).",
            retryable=True,
            invalid_args=order_args or {},
            raw_message=message,
        )
    if code in {-2011, -2013}:
        return {
            "status": "skipped",
            "tool": tool_name,
            "error_source": "binance_usdt_futures_api",
            "error_class": "order_not_found",
            "exchange_code": code,
            "why_rejected": "Binance reports that the order no longer exists.",
            "model_fix_hint": "Treat the order as already gone (filled, canceled, or expired) and continue with state refresh instead of retrying cancellation.",
            "retryable": False,
            "defer_until_next_turn": False,
            "docs_basis": f"Binance Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}#-2011-cancel_rejected).",
            "message": message,
        }
    return _structured_tool_error(
        tool_name,
        error_class="exchange_rejected",
        why_rejected="Binance rejected the request.",
        model_fix_hint="Read exchange_code/message, refresh live state, and re-plan with a legal parameter shape instead of blind retry.",
        exchange_code=code,
        docs_basis=f"Binance USD(S)-M Error Codes ({USDT_FUTURES_ERROR_CODE_DOC}).",
        retryable=False,
        invalid_args=order_args or {},
        raw_message=message,
    )


def _guard_capacity_sequence_for_opening(symbol: str) -> Optional[Dict[str, Any]]:
    recorder = _tool_recorder_ctx.get() or []
    current_stage = _tool_stage_ctx.get()
    if current_stage not in OPENING_GUARD_ACTIVE_STAGES:
        return None

    successful_openings = [
        call for call in recorder
        if _is_successful_opening_trade_call(call, symbol)
    ]
    if successful_openings:
        return _structured_tool_error(
            "trade_usdt_futures",
            error_class="opening_sequence_already_consumed",
            why_rejected="A successful opening sequence for this symbol already exists in the current wakeup.",
            model_fix_hint="Do not submit another same-wakeup opening trade. Continue with verification, protection management, cleanup, or set_alarm.",
            docs_basis="CoinAutomation production guard: one opening sequence per symbol per wakeup.",
            retryable=False,
            defer_until_next_turn=True,
        )

    opening_attempts = [
        call for call in recorder
        if str(call.get("tool", "")) == "trade_usdt_futures"
        and _get_usdt_futures_symbol(str(call.get("args", {}).get("symbol", symbol))) == symbol
        and str(call.get("args", {}).get("order_type", "")).upper() in {"MARKET", "LIMIT"}
    ]
    if len(opening_attempts) >= 2:
        return _structured_tool_error(
            "trade_usdt_futures",
            error_class="opening_retry_exhausted",
            why_rejected="Opening retry budget is exhausted for the current wakeup.",
            model_fix_hint="Switch to blocked/wait, set an alarm, and retry only in the next wakeup after re-planning.",
            docs_basis="CoinAutomation production guard: allow at most one same-wakeup parameter repair retry for an opening trade.",
            retryable=False,
            defer_until_next_turn=True,
        )
    if current_stage in PRIMARY_EXECUTE_STAGES:
        for call in recorder:
            if str(call.get("stage", "")) not in PRIMARY_EXECUTE_STAGES:
                continue
            if str(call.get("tool", "")) in CAPACITY_DIRTY_TOOLS and _is_success_result(call.get("result")):
                return _structured_tool_error(
                    "trade_usdt_futures",
                    error_class="capacity_refresh_required",
                    why_rejected="A capacity-changing action already succeeded in execute_primary; opening must wait for refreshed precheck.",
                    model_fix_hint="Stop this batch and rely on the orchestrator-refreshed precheck before any opening trade.",
                    docs_basis="CoinAutomation production guard: capacity-changing actions require refresh_precheck before dependent follow-up.",
                    retryable=False,
                    defer_until_next_turn=False,
                )
    return None


def _is_protective_open_order(order: Dict[str, Any]) -> bool:
    order_type = str(order.get("type", order.get("orderType", "")) or "").upper()
    if order_type not in PROTECTIVE_ORDER_TYPES:
        return False
    position_side = str(order.get("positionSide", "BOTH")).upper()
    side = str(order.get("side", "")).upper()
    return (side == "SELL" and position_side == "LONG") or (side == "BUY" and position_side == "SHORT")


def _normalize_open_order_row(order: Dict[str, Any], source: str) -> Dict[str, Any]:
    row = dict(order)
    order_id = row.get("orderId")
    algo_id = row.get("algoId")
    order_ref = str(order_id or algo_id or row.get("clientOrderId") or row.get("clientAlgoId") or "")
    order_type = str(row.get("type", row.get("orderType", "")) or "").upper()
    status = str(row.get("status", row.get("algoStatus", "")) or "").upper()
    position_side = str(row.get("positionSide", row.get("position_side", "BOTH")) or "BOTH").upper()
    stop_price = row.get("stopPrice", row.get("triggerPrice"))
    price = row.get("price", "0")
    orig_qty = row.get("origQty", row.get("quantity", "0"))
    executed_qty = row.get("executedQty", row.get("cumQty", "0"))

    row["orderId"] = order_ref
    if algo_id not in (None, ""):
        row["algoId"] = algo_id
    row["order_ref"] = order_ref
    row["type"] = order_type
    row["orderType"] = order_type
    row["status"] = status
    row["positionSide"] = position_side
    row["price"] = price if price not in (None, "") else "0"
    row["origQty"] = orig_qty if orig_qty not in (None, "") else "0"
    row["executedQty"] = executed_qty if executed_qty not in (None, "") else "0"
    if stop_price not in (None, ""):
        row["stopPrice"] = stop_price
        row.setdefault("triggerPrice", stop_price)
    row["closePosition"] = bool(row.get("closePosition", False))
    row["order_source"] = source
    row["is_conditional"] = source == "conditional"
    return row


def _fetch_conditional_usdt_futures_open_orders(symbol: Optional[str]) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {}
    if symbol:
        params["symbol"] = symbol
    try:
        raw = client.futures_get_open_orders(conditional=True, **params)
    except Exception as primary_exc:
        try:
            raw = client.futures_get_open_algo_orders(**params)
        except Exception:
            raise primary_exc
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _fetch_all_usdt_futures_open_orders(symbol: Optional[str]) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {}
    if symbol:
        params["symbol"] = symbol
    standard_raw = client.futures_get_open_orders(**params)
    conditional_raw = _fetch_conditional_usdt_futures_open_orders(symbol)

    merged: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()

    for source, rows in (("standard", standard_raw), ("conditional", conditional_raw)):
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_open_order_row(item, source)
            dedupe_key = (str(normalized.get("order_ref", "") or ""), source)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(normalized)
    return merged


def _open_order_matches_ref(order: Dict[str, Any], order_ref: str) -> bool:
    order_ref = str(order_ref or "").strip()
    if not order_ref:
        return False
    return any(
        str(order.get(key, "") or "").strip() == order_ref
        for key in ("orderId", "algoId", "order_ref")
    )


def _find_open_order_by_ref(open_orders: List[Dict[str, Any]], order_ref: str) -> Optional[Dict[str, Any]]:
    for order in open_orders:
        if isinstance(order, dict) and _open_order_matches_ref(order, order_ref):
            return order
    return None


def log_trade(cmd_name: str, args: Dict[str, Any], res: Any, explanation: str = "") -> None:
    """记录交易到 CSV"""
    file_exists = os.path.isfile(TRADE_LOG_FILE)
    with open(TRADE_LOG_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "command", "args", "result_summary"])

        if isinstance(res, dict):
            order_id = res.get("orderId")
            status = res.get("status", "N/A")
            filled_qty = res.get("executedQty", "0")
            summary = f"orderId={order_id} status={status} filled_qty={filled_qty}"
        else:
            summary = str(res)
        if explanation:
            summary = f"{summary} | explanation={explanation}"
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            cmd_name,
            str(args),
            summary,
        ])


def _get_usdt_futures_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return "ETHUSDT"
    if s.endswith("_PERP"):
        s = s[:-5]
    if "_" in s:
        s = s.split("_", 1)[0]
    if s.endswith("USDT"):
        return s
    if s.endswith("USD"):
        return f"{s[:-3]}USDT"
    return f"{s}USDT"


def _normalize_order_quantity(quantity: float, filters: Dict[str, Any]) -> float:
    step_size = float(filters.get("step_size", 0) or 0)
    qty_precision = int(filters.get("qty_precision", 8) or 8)
    qty = float(quantity)
    if step_size > 0:
        qty = math.floor((qty + 1e-12) / step_size) * step_size
    return round(max(qty, 0.0), qty_precision)


def _normalize_order_price(price: float, filters: Dict[str, Any]) -> float:
    tick_size = float(filters.get("tick_size", 0) or 0)
    price_precision = int(filters.get("price_precision", 8) or 8)
    normalized = float(price)
    if tick_size > 0:
        normalized = float(round_step_size(normalized, tick_size))
    return round(max(normalized, 0.0), price_precision)


def _range_plan_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_range_breakout_policy() -> Dict[str, Any]:
    return {
        "mode": "confirm_then_exit",
        "confirm_seconds": 180,
        "extended_breakout_pct": 0.25,
    }


def _default_range_breakout_watch() -> Dict[str, Any]:
    return {
        "status": "idle",
        "side": "",
        "first_breach_at": "",
        "confirm_deadline_at": "",
        "breach_price": 0.0,
        "extended_lower": 0.0,
        "extended_upper": 0.0,
        "canceled_entry_order_ids": [],
        "false_breakout_count": 0,
        "last_resolution": "",
        "last_resolution_at": "",
    }


def _default_range_plan(*, source: str = "system") -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": 1,
            "updated_at": _range_plan_now(),
            "source": source,
        },
        "status": "inactive",
        "symbol": "ETHUSDT",
        "mode": "range_automation",
        "execution_style": "maker_grid_legacy",
        "position_mode": "unknown",
        "suppress_order_fill_wakeup": True,
        "lower_breakout": 0.0,
        "upper_breakout": 0.0,
        "expires_at": "",
        "activation_reason": "",
        "breakout_policy": _default_range_breakout_policy(),
        "breakout_watch": _default_range_breakout_watch(),
        "long_levels": [],
        "short_levels": [],
        "audit": {
            "last_sync_at": "",
            "last_price": 0.0,
            "last_error": "",
            "recent_order_ids": [],
            "event": {
                "key": "",
                "type": "",
                "content": "",
                "created_at": "",
                "sent_at": "",
            },
        },
    }


def _read_range_plan_state() -> Dict[str, Any]:
    if not os.path.exists(RANGE_PLAN_PATH):
        return _default_range_plan(source="missing_file")
    lock = FileLock(f"{RANGE_PLAN_PATH}.lock", timeout=10)
    with lock:
        try:
            with open(RANGE_PLAN_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return _default_range_plan(source="read_error")
    if not isinstance(payload, dict):
        return _default_range_plan(source="invalid_payload")
    merged = _default_range_plan(source="read")
    for key, value in payload.items():
        if key in {"meta", "audit"} and isinstance(value, dict):
            merged[key].update(value)
        elif key in {"breakout_policy", "breakout_watch"} and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    if not isinstance(merged.get("long_levels"), list):
        merged["long_levels"] = []
    if not isinstance(merged.get("short_levels"), list):
        merged["short_levels"] = []
    if not isinstance(merged.get("breakout_policy"), dict):
        merged["breakout_policy"] = _default_range_breakout_policy()
    if not isinstance(merged.get("breakout_watch"), dict):
        merged["breakout_watch"] = _default_range_breakout_watch()
    return merged


def _write_range_plan_state(plan: Dict[str, Any]) -> None:
    payload = _default_range_plan(source="write")
    for key, value in dict(plan or {}).items():
        if key in {"meta", "audit"} and isinstance(value, dict):
            payload[key].update(value)
        elif key in {"breakout_policy", "breakout_watch"} and isinstance(value, dict):
            payload[key].update(value)
        else:
            payload[key] = value
    payload["meta"]["updated_at"] = _range_plan_now()
    lock = FileLock(f"{RANGE_PLAN_PATH}.lock", timeout=10)
    temp_path = f"{RANGE_PLAN_PATH}.tmp"
    with lock:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, RANGE_PLAN_PATH)


def read_range_plan_state() -> Dict[str, Any]:
    """Read the persisted range-automation plan for strategy-side routing."""
    return _read_range_plan_state()


def _range_plan_is_active(plan: Dict[str, Any]) -> bool:
    if not isinstance(plan, dict):
        return False
    if str(plan.get("status", "")).strip().lower() != "active":
        return False
    expires_at = str(plan.get("expires_at", "") or "").strip()
    if not expires_at:
        return False
    try:
        return datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S") > datetime.now()
    except Exception:
        return False


def _tracked_range_order_ids(plan: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    tracked: Dict[str, Dict[str, str]] = {}
    for side_name, levels in (("long", plan.get("long_levels", [])), ("short", plan.get("short_levels", []))):
        for level in levels if isinstance(levels, list) else []:
            if not isinstance(level, dict):
                continue
            level_id = str(level.get("level_id", "") or "")
            entry_order_id = str(level.get("entry_order_id", "") or "")
            exit_order_id = str(level.get("exit_order_id", "") or "")
            if entry_order_id:
                tracked[entry_order_id] = {"bucket": side_name, "leg": "entry", "level_id": level_id}
            if exit_order_id:
                tracked[exit_order_id] = {"bucket": side_name, "leg": "exit", "level_id": level_id}
    recent_order_ids = (plan.get("audit", {}) or {}).get("recent_order_ids", [])
    for item in recent_order_ids if isinstance(recent_order_ids, list) else []:
        if not isinstance(item, dict):
            continue
        order_id = str(item.get("order_id", "") or "").strip()
        if not order_id or order_id in tracked:
            continue
        tracked[order_id] = {
            "bucket": str(item.get("bucket", "") or ""),
            "leg": str(item.get("leg", "") or ""),
            "level_id": str(item.get("level_id", "") or ""),
        }
    return tracked


def classify_range_fill_batch(fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan = _read_range_plan_state()
    if not _range_plan_is_active(plan):
        return {"active": False, "suppress": False, "reason": "range_plan_inactive"}
    tracked = _tracked_range_order_ids(plan)
    unknown_order_ids: List[str] = []
    known_order_ids: List[str] = []
    for item in fills if isinstance(fills, list) else []:
        if not isinstance(item, dict):
            continue
        order_id = str(item.get("orderId", "") or "").strip()
        if not order_id:
            continue
        if order_id in tracked:
            known_order_ids.append(order_id)
        else:
            unknown_order_ids.append(order_id)
    if unknown_order_ids:
        return {
            "active": True,
            "suppress": False,
            "reason": "unexpected_fill_during_range_plan",
            "unknown_order_ids": sorted(set(unknown_order_ids)),
            "known_order_ids": sorted(set(known_order_ids)),
        }
    return {
        "active": True,
        "suppress": True,
        "reason": "tracked_range_plan_fills",
        "known_order_ids": sorted(set(known_order_ids)),
    }


def _set_range_plan_event(plan: Dict[str, Any], event_type: str, content: str) -> None:
    audit = plan.setdefault("audit", {})
    event = audit.setdefault("event", {})
    key = f"{event_type}:{plan.get('status', '')}:{_range_plan_now()}"
    event.update(
        {
            "key": key,
            "type": event_type,
            "content": str(content or ""),
            "created_at": _range_plan_now(),
            "sent_at": "",
        }
    )


def acknowledge_range_plan_event(event_key: str) -> Dict[str, Any]:
    plan = _read_range_plan_state()
    event = (plan.get("audit", {}) or {}).get("event", {})
    if not isinstance(event, dict) or str(event.get("key", "")).strip() != str(event_key or "").strip():
        return {"status": "skipped", "message": "event key mismatch"}
    event["sent_at"] = _range_plan_now()
    _write_range_plan_state(plan)
    return {"status": "success", "message": f"range plan event acknowledged: {event_key}"}


def _fetch_mark_price(symbol: str) -> float:
    ticker = client.futures_mark_price(symbol=symbol)
    return float(ticker.get("markPrice", 0.0))


def _fetch_order_status(symbol: str, order_id: str) -> Dict[str, Any]:
    try:
        return client.futures_get_order(symbol=symbol, orderId=order_id)
    except Exception as e:
        if "-2011" in str(e) or "-2013" in str(e) or "Unknown order" in str(e):
            return {"status": "NOT_FOUND", "orderId": order_id}
        raise


def _submit_range_limit_order(
    *,
    symbol: str,
    side: str,
    position_side: str,
    price: float,
    quantity: float,
    explanation: str,
) -> Dict[str, Any]:
    filters = get_symbol_filters(symbol)
    if not isinstance(filters, dict) or filters.get("status") == "error":
        raise RuntimeError(f"Unable to load symbol filters for {symbol}")
    normalized_price = _normalize_order_price(price, filters)
    normalized_qty = _normalize_order_quantity(quantity, filters)
    if normalized_qty <= 0:
        raise RuntimeError("range plan quantity normalized to zero")
    params = {
        "symbol": symbol,
        "side": side,
        "positionSide": position_side,
        "type": "LIMIT",
        "timeInForce": "GTX",
        "price": normalized_price,
        "quantity": normalized_qty,
    }
    res = client.futures_create_order(**params)
    log_trade(
        "range_plan_order",
        {
            "symbol": symbol,
            "side": side,
            "position_side": position_side,
            "price": normalized_price,
            "quantity": normalized_qty,
        },
        res,
        explanation=explanation,
    )
    return res


def _cancel_range_order(symbol: str, order_id: str, explanation: str) -> Dict[str, Any]:
    res = client.futures_cancel_order(symbol=symbol, orderId=order_id)
    log_trade(
        "range_plan_cancel",
        {"symbol": symbol, "order_id": order_id},
        res,
        explanation=explanation,
    )
    return res


def _normalize_range_levels(
    raw_levels: Any,
    *,
    bucket: str,
    lower_breakout: float,
    upper_breakout: float,
    filters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    if isinstance(raw_levels, str):
        raw_levels = json.loads(raw_levels)
    if not isinstance(raw_levels, list):
        raise ValueError(f"{bucket}_levels must be a JSON list")
    normalized_levels: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_levels):
        if not isinstance(item, dict):
            raise ValueError(f"{bucket}_levels[{idx}] must be an object")
        entry_price = _normalize_order_price(float(item.get("entry_price")), filters)
        exit_price = _normalize_order_price(float(item.get("exit_price")), filters)
        quantity = _normalize_order_quantity(float(item.get("quantity")), filters)
        if quantity <= 0:
            raise ValueError(f"{bucket}_levels[{idx}] quantity normalized to zero")
        if not (lower_breakout < entry_price < upper_breakout):
            raise ValueError(f"{bucket}_levels[{idx}] entry_price must stay strictly inside breakout bounds")
        if not (lower_breakout < exit_price < upper_breakout):
            raise ValueError(f"{bucket}_levels[{idx}] exit_price must stay strictly inside breakout bounds")
        if bucket == "long" and not (entry_price < exit_price):
            raise ValueError(f"{bucket}_levels[{idx}] requires entry_price < exit_price")
        if bucket == "short" and not (entry_price > exit_price):
            raise ValueError(f"{bucket}_levels[{idx}] requires entry_price > exit_price")
        normalized_levels.append(
            {
                "level_id": f"{bucket}_{idx + 1}",
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "entry_order_id": "",
                "entry_status": "idle",
                "exit_order_id": "",
                "exit_status": "idle",
                "filled_qty": 0.0,
                "completed_cycles": 0,
            }
        )
    return normalized_levels


def _active_live_positions(symbol: str) -> Dict[str, float]:
    positions = client.futures_position_information(symbol=symbol)
    result = {"LONG": 0.0, "SHORT": 0.0}
    for row in positions:
        if row.get("symbol") != symbol:
            continue
        position_side = str(row.get("positionSide", "BOTH")).upper()
        amount = float(row.get("positionAmt", 0.0) or 0.0)
        if position_side == "LONG" and amount > 0:
            result["LONG"] = amount
        elif position_side == "SHORT" and amount < 0:
            result["SHORT"] = abs(amount)
        elif position_side == "BOTH":
            if amount > 0:
                result["LONG"] = amount
            elif amount < 0:
                result["SHORT"] = abs(amount)
    return result


def _reset_range_level(level: Dict[str, Any]) -> None:
    level["entry_order_id"] = ""
    level["entry_status"] = "idle"
    level["exit_order_id"] = ""
    level["exit_status"] = "idle"
    level["filled_qty"] = 0.0
    level["completed_cycles"] = int(level.get("completed_cycles", 0) or 0) + 1


def _range_plan_position_mode(plan: Dict[str, Any]) -> str:
    raw = str(plan.get("position_mode", "") or "").strip().lower()
    if raw in {"hedge", "one_way"}:
        return raw
    return "hedge" if _get_position_mode() else "one_way"


def _range_plan_order_position_side(plan: Dict[str, Any], bucket: str) -> str:
    if _range_plan_position_mode(plan) == "hedge":
        return "LONG" if bucket == "long" else "SHORT"
    return "BOTH"


def _range_plan_breakout_policy(plan: Dict[str, Any]) -> Dict[str, Any]:
    policy = plan.get("breakout_policy", {})
    if not isinstance(policy, dict):
        policy = {}
    merged = _default_range_breakout_policy()
    merged.update(policy)
    try:
        merged["confirm_seconds"] = max(30, int(float(merged.get("confirm_seconds", 180) or 180)))
    except Exception:
        merged["confirm_seconds"] = 180
    try:
        merged["extended_breakout_pct"] = max(0.05, float(merged.get("extended_breakout_pct", 0.25) or 0.25))
    except Exception:
        merged["extended_breakout_pct"] = 0.25
    return merged


def _append_recent_range_order_id(
    plan: Dict[str, Any],
    *,
    order_id: str,
    bucket: str,
    leg: str,
    level_id: str,
) -> None:
    order_id = str(order_id or "").strip()
    if not order_id:
        return
    audit = plan.setdefault("audit", {})
    recent = audit.setdefault("recent_order_ids", [])
    if not isinstance(recent, list):
        recent = []
        audit["recent_order_ids"] = recent
    recent.append(
        {
            "order_id": order_id,
            "bucket": str(bucket or ""),
            "leg": str(leg or ""),
            "level_id": str(level_id or ""),
            "recorded_at": _range_plan_now(),
        }
    )
    if len(recent) > 40:
        audit["recent_order_ids"] = recent[-40:]


def _range_plan_breakout_watch(plan: Dict[str, Any]) -> Dict[str, Any]:
    watch = plan.get("breakout_watch", {})
    if not isinstance(watch, dict):
        watch = {}
    merged = _default_range_breakout_watch()
    merged.update(watch)
    if not isinstance(merged.get("canceled_entry_order_ids"), list):
        merged["canceled_entry_order_ids"] = []
    return merged


def _submit_range_market_order(
    *,
    plan: Dict[str, Any],
    symbol: str,
    bucket: str,
    leg: str,
    level: Dict[str, Any],
    side: str,
    quantity: float,
    explanation: str,
    reduce_only: bool = False,
) -> Dict[str, Any]:
    filters = get_symbol_filters(symbol)
    if not isinstance(filters, dict) or filters.get("status") == "error":
        raise RuntimeError(f"Unable to load symbol filters for {symbol}")
    normalized_qty = _normalize_order_quantity(quantity, filters)
    if normalized_qty <= 0:
        raise RuntimeError("range plan market quantity normalized to zero")
    params: Dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "positionSide": _range_plan_order_position_side(plan, bucket),
        "type": "MARKET",
        "quantity": normalized_qty,
    }
    if _range_plan_position_mode(plan) == "one_way" and reduce_only:
        params["reduceOnly"] = "true"
    res = client.futures_create_order(**params)
    log_trade(
        "range_plan_market_order",
        {
            "symbol": symbol,
            "bucket": bucket,
            "leg": leg,
            "level_id": str(level.get("level_id", "") or ""),
            "side": side,
            "position_side": params["positionSide"],
            "quantity": normalized_qty,
            "reduce_only": bool(reduce_only),
        },
        res,
        explanation=explanation,
    )
    _append_recent_range_order_id(
        plan,
        order_id=str(res.get("orderId", "") or ""),
        bucket=bucket,
        leg=leg,
        level_id=str(level.get("level_id", "") or ""),
    )
    return res


def _sync_trigger_market_range_plan(plan: Dict[str, Any], symbol: str, mark_price: float) -> bool:
    changed = prepare_trigger_plan_runtime(plan, mark_price)
    live_positions = _active_live_positions(symbol)
    actions, runtime_changed = collect_trigger_actions(
        plan,
        mark_price,
        live_positions,
        _range_plan_position_mode(plan),
    )
    changed = changed or runtime_changed
    if not actions:
        return changed

    bucket_map = {
        "long": plan.get("long_levels", []) if isinstance(plan.get("long_levels"), list) else [],
        "short": plan.get("short_levels", []) if isinstance(plan.get("short_levels"), list) else [],
    }

    for action in actions:
        bucket = str(action.get("bucket", "") or "")
        level_id = str(action.get("level_id", "") or "")
        levels = bucket_map.get(bucket, [])
        level = next(
            (item for item in levels if isinstance(item, dict) and str(item.get("level_id", "") or "") == level_id),
            None,
        )
        if level is None:
            continue

        action_name = str(action.get("action", "") or "")
        if action_name == "reset_level":
            reset_trigger_level(level, bucket, mark_price)
            changed = True
            continue

        if action_name == "entry_market":
            side = "BUY" if bucket == "long" else "SELL"
            entry_price = float(level.get("entry_price", 0.0) or 0.0)
            res = _submit_range_market_order(
                plan=plan,
                symbol=symbol,
                bucket=bucket,
                leg="entry",
                level=level,
                side=side,
                quantity=float(action.get("quantity", 0.0) or 0.0),
                explanation=(
                    f"Range trigger-market entry for {bucket} level {level_id}: "
                    f"mark price {mark_price:.2f} touched entry trigger {entry_price:.2f}, so the local range executor opened the leg immediately."
                ),
                reduce_only=False,
            )
            filters = get_symbol_filters(symbol)
            filled_qty = _normalize_order_quantity(
                float(res.get("executedQty", action.get("quantity", 0.0)) or action.get("quantity", 0.0) or 0.0),
                filters or {},
            )
            if filled_qty <= 0:
                filled_qty = _normalize_order_quantity(float(action.get("quantity", 0.0) or 0.0), filters or {})
            level["entry_order_id"] = str(res.get("orderId", "") or "")
            level["entry_status"] = "filled"
            level["filled_qty"] = filled_qty
            level["exit_status"] = "armed"
            level["last_entry_at"] = _range_plan_now()
            level["last_entry_price"] = mark_price
            changed = True
            live_positions = _active_live_positions(symbol)
            continue

        if action_name == "exit_market":
            quantity = float(action.get("quantity", 0.0) or 0.0)
            if quantity <= 0:
                reset_trigger_level(level, bucket, mark_price)
                changed = True
                continue
            side = "SELL" if bucket == "long" else "BUY"
            exit_price = float(level.get("exit_price", 0.0) or 0.0)
            res = _submit_range_market_order(
                plan=plan,
                symbol=symbol,
                bucket=bucket,
                leg="exit",
                level=level,
                side=side,
                quantity=quantity,
                explanation=(
                    f"Range trigger-market exit for {bucket} level {level_id}: "
                    f"mark price {mark_price:.2f} reached exit trigger {exit_price:.2f}, so the local range executor closed the leg immediately."
                ),
                reduce_only=True,
            )
            level["exit_order_id"] = str(res.get("orderId", "") or "")
            level["last_exit_at"] = _range_plan_now()
            level["last_exit_price"] = mark_price
            reset_trigger_level(level, bucket, mark_price)
            changed = True
            live_positions = _active_live_positions(symbol)

    return changed


def _sync_range_levels_for_bucket(
    *,
    plan: Dict[str, Any],
    bucket: str,
    open_orders_by_id: Dict[str, Dict[str, Any]],
) -> bool:
    changed = False
    symbol = str(plan.get("symbol", "ETHUSDT") or "ETHUSDT")
    levels = plan.get(f"{bucket}_levels", [])
    for level in levels if isinstance(levels, list) else []:
        if not isinstance(level, dict):
            continue
        entry_order_id = str(level.get("entry_order_id", "") or "")
        exit_order_id = str(level.get("exit_order_id", "") or "")

        if exit_order_id and exit_order_id not in open_orders_by_id:
            exit_order = _fetch_order_status(symbol, exit_order_id)
            exit_status = str(exit_order.get("status", "") or "").upper()
            if exit_status == "FILLED":
                _reset_range_level(level)
                changed = True
                continue
            if exit_status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "NOT_FOUND"}:
                level["exit_order_id"] = ""
                level["exit_status"] = "idle"
                changed = True

        if entry_order_id and entry_order_id not in open_orders_by_id:
            entry_order = _fetch_order_status(symbol, entry_order_id)
            entry_status = str(entry_order.get("status", "") or "").upper()
            if entry_status == "FILLED":
                filters = get_symbol_filters(symbol)
                filled_qty = _normalize_order_quantity(float(entry_order.get("executedQty", level.get("quantity", 0.0)) or 0.0), filters or {})
                if filled_qty <= 0:
                    filled_qty = float(level.get("quantity", 0.0) or 0.0)
                level["filled_qty"] = filled_qty
                level["entry_order_id"] = ""
                level["entry_status"] = "filled"
                changed = True
            elif entry_status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "NOT_FOUND"}:
                level["entry_order_id"] = ""
                level["entry_status"] = "idle"
                changed = True

        if str(level.get("entry_status", "") or "") == "filled" and not str(level.get("exit_order_id", "") or ""):
            filled_qty = float(level.get("filled_qty", 0.0) or 0.0)
            if filled_qty > 0:
                if bucket == "long":
                    side = "SELL"
                else:
                    side = "BUY"
                position_side = _range_plan_order_position_side(plan, bucket)
                explanation = (
                    f"Range automation paired exit for {bucket} level {level.get('level_id')}: "
                    f"filled entry at {level.get('entry_price')} and re-listing the opposite-side take-profit at {level.get('exit_price')}."
                )
                res = _submit_range_limit_order(
                    symbol=symbol,
                    side=side,
                    position_side=position_side,
                    price=float(level.get("exit_price", 0.0) or 0.0),
                    quantity=filled_qty,
                    explanation=explanation,
                )
                level["exit_order_id"] = str(res.get("orderId", "") or "")
                level["exit_status"] = "pending"
                changed = True

        if (
            str(level.get("entry_status", "") or "") == "idle"
            and not str(level.get("entry_order_id", "") or "")
            and not str(level.get("exit_order_id", "") or "")
        ):
            if bucket == "long":
                side = "BUY"
            else:
                side = "SELL"
            position_side = _range_plan_order_position_side(plan, bucket)
            explanation = (
                f"Range automation entry for {bucket} level {level.get('level_id')}: "
                f"re-arm passive range order at {level.get('entry_price')} inside the active sideways band."
            )
            res = _submit_range_limit_order(
                symbol=symbol,
                side=side,
                position_side=position_side,
                price=float(level.get("entry_price", 0.0) or 0.0),
                quantity=float(level.get("quantity", 0.0) or 0.0),
                explanation=explanation,
            )
            level["entry_order_id"] = str(res.get("orderId", "") or "")
            level["entry_status"] = "pending"
            changed = True
    return changed


def _cancel_all_tracked_range_orders(plan: Dict[str, Any], reason: str) -> List[str]:
    canceled: List[str] = []
    symbol = str(plan.get("symbol", "ETHUSDT") or "ETHUSDT")
    tracked = _tracked_range_order_ids(plan)
    open_orders = client.futures_get_open_orders(symbol=symbol)
    open_order_ids = {str(item.get("orderId")) for item in open_orders if isinstance(item, dict)}
    for order_id in tracked.keys():
        if order_id and order_id in open_order_ids:
            try:
                _cancel_range_order(
                    symbol,
                    order_id,
                    explanation=f"Range automation cleanup: {reason}",
                )
                canceled.append(order_id)
            except Exception:
                continue
    for bucket in ("long_levels", "short_levels"):
        levels = plan.get(bucket, [])
        for level in levels if isinstance(levels, list) else []:
            if not isinstance(level, dict):
                continue
            level["entry_order_id"] = ""
            level["entry_status"] = "idle"
            level["exit_order_id"] = ""
            level["exit_status"] = "idle"
    return canceled


def _cancel_range_entry_orders_for_breakout_watch(plan: Dict[str, Any], reason: str) -> List[str]:
    canceled: List[str] = []
    symbol = str(plan.get("symbol", "ETHUSDT") or "ETHUSDT")
    open_orders = client.futures_get_open_orders(symbol=symbol)
    open_order_ids = {str(item.get("orderId")) for item in open_orders if isinstance(item, dict)}
    for bucket in ("long_levels", "short_levels"):
        levels = plan.get(bucket, [])
        for level in levels if isinstance(levels, list) else []:
            if not isinstance(level, dict):
                continue
            entry_order_id = str(level.get("entry_order_id", "") or "")
            if entry_order_id and entry_order_id in open_order_ids:
                try:
                    _cancel_range_order(
                        symbol,
                        entry_order_id,
                        explanation=f"Range breakout watch: {reason}",
                    )
                    canceled.append(entry_order_id)
                except Exception:
                    continue
            level["entry_order_id"] = ""
            if str(level.get("entry_status", "") or "") == "pending":
                level["entry_status"] = "idle"
    return canceled


def _close_range_positions_if_needed(symbol: str, reason: str) -> List[str]:
    closed: List[str] = []
    live_positions = _active_live_positions(symbol)
    if _get_position_mode():
        if live_positions["LONG"] > 0:
            close_usdt_futures_position(
                symbol=symbol,
                explanation=reason,
                position_side="LONG",
            )
            closed.append("LONG")
        if live_positions["SHORT"] > 0:
            close_usdt_futures_position(
                symbol=symbol,
                explanation=reason,
                position_side="SHORT",
            )
            closed.append("SHORT")
    elif live_positions["LONG"] > 0 or live_positions["SHORT"] > 0:
        close_usdt_futures_position(
            symbol=symbol,
            explanation=reason,
            position_side="BOTH",
        )
        closed.append("LONG" if live_positions["LONG"] > 0 else "SHORT")
    return closed


def _range_plan_summary(plan: Dict[str, Any]) -> Dict[str, Any]:
    tracked = _tracked_range_order_ids(plan)
    return {
        "status": str(plan.get("status", "") or ""),
        "symbol": str(plan.get("symbol", "") or ""),
        "execution_style": str(plan.get("execution_style", "") or ""),
        "position_mode": str(plan.get("position_mode", "") or ""),
        "lower_breakout": float(plan.get("lower_breakout", 0.0) or 0.0),
        "upper_breakout": float(plan.get("upper_breakout", 0.0) or 0.0),
        "expires_at": str(plan.get("expires_at", "") or ""),
        "long_level_count": len(plan.get("long_levels", []) if isinstance(plan.get("long_levels"), list) else []),
        "short_level_count": len(plan.get("short_levels", []) if isinstance(plan.get("short_levels"), list) else []),
        "tracked_order_ids": sorted(tracked.keys()),
        "suppress_order_fill_wakeup": bool(plan.get("suppress_order_fill_wakeup", True)),
        "breakout_policy": _range_plan_breakout_policy(plan),
        "breakout_watch": _range_plan_breakout_watch(plan),
        "audit": plan.get("audit", {}),
    }


def get_range_plan(symbol: str = "ETHUSDT", explanation: str = ""):
    """查询当前 sideways range automation 计划与挂单生命周期状态；explanation 用于说明为何读取该计划。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "explanation": explanation}
    plan = _read_range_plan_state()
    if str(plan.get("symbol", "") or "") != symbol and _range_plan_is_active(plan):
        res = {"status": "inactive", "message": f"{symbol} 当前无 active range plan。", "plan": _range_plan_summary(plan)}
    else:
        res = {"status": str(plan.get("status", "inactive") or "inactive"), "plan": plan}
    _record_tool_call("get_range_plan", call_args, res)
    return res


def maintain_range_plan() -> Dict[str, Any]:
    plan = _read_range_plan_state()
    if not isinstance(plan, dict) or str(plan.get("status", "")).strip().lower() != "active":
        event = (plan.get("audit", {}) or {}).get("event", {})
        if isinstance(event, dict) and str(event.get("key", "")).strip() and not str(event.get("sent_at", "")).strip():
            return {"status": str(plan.get("status", "") or ""), "event": dict(event)}
        return {"status": str(plan.get("status", "inactive") or "inactive")}

    symbol = _get_usdt_futures_symbol(plan.get("symbol", "ETHUSDT"))
    plan["symbol"] = symbol
    lower_breakout = float(plan.get("lower_breakout", 0.0) or 0.0)
    upper_breakout = float(plan.get("upper_breakout", 0.0) or 0.0)

    try:
        expires_at = datetime.strptime(str(plan.get("expires_at", "") or ""), "%Y-%m-%d %H:%M:%S")
    except Exception:
        plan["status"] = "error"
        plan.setdefault("audit", {})["last_error"] = "invalid expires_at format"
        _set_range_plan_event(plan, "range_plan_exception", "Range automation plan has invalid expires_at and was stopped.")
        _write_range_plan_state(plan)
        return {"status": "error", "event": dict(plan.get("audit", {}).get("event", {}))}

    if datetime.now() >= expires_at:
        _cancel_all_tracked_range_orders(plan, "plan expired")
        plan["status"] = "expired"
        _set_range_plan_event(
            plan,
            "range_plan_expired",
            (
                f"Range automation plan expired at {plan.get('expires_at')}. "
                "Tracked range entry/exit orders were canceled. Re-evaluate whether the market is still sideways."
            ),
        )
        _write_range_plan_state(plan)
        return {"status": "expired", "event": dict(plan.get("audit", {}).get("event", {}))}

    try:
        mark_price = _fetch_mark_price(symbol)
        audit = plan.setdefault("audit", {})
        audit["last_sync_at"] = _range_plan_now()
        audit["last_price"] = mark_price
        audit["last_error"] = ""
        breakout_policy = _range_plan_breakout_policy(plan)
        breakout_watch = _range_plan_breakout_watch(plan)
        range_width = max(upper_breakout - lower_breakout, 0.0)
        extended_lower = lower_breakout - range_width * float(breakout_policy.get("extended_breakout_pct", 0.25) or 0.25)
        extended_upper = upper_breakout + range_width * float(breakout_policy.get("extended_breakout_pct", 0.25) or 0.25)

        def _confirm_breakout(*, reason: str) -> Dict[str, Any]:
            _cancel_all_tracked_range_orders(plan, f"breakout confirmed ({reason})")
            closed_sides = _close_range_positions_if_needed(
                symbol,
                reason=(
                    f"Range automation confirmed breakout: mark price {mark_price:.2f} invalidated "
                    f"[{lower_breakout:.2f}, {upper_breakout:.2f}] ({reason}) and range positions must be flattened."
                ),
            )
            plan["status"] = "breakout"
            breakout_watch["status"] = "confirmed"
            breakout_watch["last_resolution"] = reason
            breakout_watch["last_resolution_at"] = _range_plan_now()
            plan["breakout_watch"] = breakout_watch
            _set_range_plan_event(
                plan,
                "range_breakout",
                (
                    f"Range automation confirmed breakout: mark price {mark_price:.2f} invalidated breakout band "
                    f"[{lower_breakout:.2f}, {upper_breakout:.2f}] via {reason}. "
                    f"Tracked orders were canceled and closed_sides={closed_sides or ['none']}."
                ),
            )
            _write_range_plan_state(plan)
            return {"status": "breakout", "event": dict(plan.get("audit", {}).get("event", {}))}

        if str(breakout_watch.get("status", "") or "").strip().lower() == "pending":
            confirm_deadline_at = str(breakout_watch.get("confirm_deadline_at", "") or "").strip()
            deadline_dt = None
            if confirm_deadline_at:
                try:
                    deadline_dt = datetime.strptime(confirm_deadline_at, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    deadline_dt = None

            if lower_breakout < mark_price < upper_breakout:
                breakout_watch["status"] = "idle"
                breakout_watch["last_resolution"] = "false_breakout_reverted"
                breakout_watch["last_resolution_at"] = _range_plan_now()
                breakout_watch["false_breakout_count"] = int(breakout_watch.get("false_breakout_count", 0) or 0) + 1
                breakout_watch["side"] = ""
                breakout_watch["first_breach_at"] = ""
                breakout_watch["confirm_deadline_at"] = ""
                breakout_watch["breach_price"] = 0.0
                breakout_watch["extended_lower"] = 0.0
                breakout_watch["extended_upper"] = 0.0
                breakout_watch["canceled_entry_order_ids"] = []
                plan["breakout_watch"] = breakout_watch
                _set_range_plan_event(
                    plan,
                    "range_breakout_reverted",
                    (
                        f"Range automation breakout watch reverted: price returned inside [{lower_breakout:.2f}, {upper_breakout:.2f}] "
                        f"before confirmation. Continue range plan and consider widening the box only if false breakouts repeat."
                    ),
                )
                _write_range_plan_state(plan)
            elif mark_price <= extended_lower or mark_price >= extended_upper:
                return _confirm_breakout(reason="extended_breakout_threshold")
            elif deadline_dt is not None and datetime.now() >= deadline_dt:
                return _confirm_breakout(reason="breakout_confirmation_timeout")
            else:
                plan["breakout_watch"] = breakout_watch
                _write_range_plan_state(plan)
                return {"status": "active", "summary": _range_plan_summary(plan)}

        if mark_price <= lower_breakout or mark_price >= upper_breakout:
            canceled_entry_order_ids = _cancel_range_entry_orders_for_breakout_watch(
                plan,
                "first breach detected while waiting for confirmation",
            )
            breakout_watch = _default_range_breakout_watch()
            breakout_watch["status"] = "pending"
            breakout_watch["side"] = "lower" if mark_price <= lower_breakout else "upper"
            breakout_watch["first_breach_at"] = _range_plan_now()
            breakout_watch["confirm_deadline_at"] = (
                datetime.now() + timedelta(seconds=int(breakout_policy.get("confirm_seconds", 180) or 180))
            ).strftime("%Y-%m-%d %H:%M:%S")
            breakout_watch["breach_price"] = round(mark_price, 8)
            breakout_watch["extended_lower"] = round(extended_lower, 8)
            breakout_watch["extended_upper"] = round(extended_upper, 8)
            breakout_watch["canceled_entry_order_ids"] = canceled_entry_order_ids
            prior_false_breakouts = _range_plan_breakout_watch(plan).get("false_breakout_count", 0)
            breakout_watch["false_breakout_count"] = int(prior_false_breakouts or 0)
            plan["breakout_watch"] = breakout_watch
            _set_range_plan_event(
                plan,
                "range_breakout_watch",
                (
                    f"Range automation touched breakout band at {mark_price:.2f}, but exit is now confirmation-based. "
                    f"Fresh entry exposure was paused and the plan entered breakout_watch until {breakout_watch['confirm_deadline_at']}. "
                    f"If price re-enters [{lower_breakout:.2f}, {upper_breakout:.2f}], treat it as a false breakout and continue range. "
                    f"If price keeps expanding beyond [{extended_lower:.2f}, {extended_upper:.2f}] or stays outside through the deadline, "
                    "the breakout will be confirmed and the plan will exit."
                ),
            )
            _write_range_plan_state(plan)
            return {"status": "active", "event": dict(plan.get("audit", {}).get("event", {})), "summary": _range_plan_summary(plan)}

        changed = False
        if range_plan_execution_style(plan) == TRIGGER_MARKET_EXECUTION_STYLE:
            changed = _sync_trigger_market_range_plan(plan, symbol, mark_price) or changed
        else:
            open_orders = client.futures_get_open_orders(symbol=symbol)
            open_orders_by_id = {
                str(item.get("orderId")): item
                for item in open_orders
                if isinstance(item, dict) and item.get("orderId") is not None
            }
            changed = _sync_range_levels_for_bucket(plan=plan, bucket="long", open_orders_by_id=open_orders_by_id) or changed
            changed = _sync_range_levels_for_bucket(plan=plan, bucket="short", open_orders_by_id=open_orders_by_id) or changed
        if changed:
            _write_range_plan_state(plan)
        return {"status": "active", "summary": _range_plan_summary(plan)}
    except Exception as e:
        plan["status"] = "error"
        plan.setdefault("audit", {})["last_error"] = f"{type(e).__name__}: {e}"
        _set_range_plan_event(
            plan,
            "range_plan_exception",
            f"Range automation maintenance failed: {type(e).__name__}: {e}",
        )
        _write_range_plan_state(plan)
        return {"status": "error", "event": dict(plan.get("audit", {}).get("event", {}))}


def set_range_plan(
    symbol: str,
    lower_breakout: float,
    upper_breakout: float,
    long_levels: Any,
    short_levels: Any,
    expires_at: str,
    explanation: str,
):
    """创建或替换一个 sideways range automation 计划，由执行层自动维护价格触发、循环收割与突破失效。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {
        "symbol": symbol,
        "lower_breakout": lower_breakout,
        "upper_breakout": upper_breakout,
        "long_levels": long_levels,
        "short_levels": short_levels,
        "expires_at": expires_at,
        "explanation": explanation,
    }
    explanation_error = _require_action_explanation("set_range_plan", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("set_range_plan", call_args, explanation_error)
        return explanation_error

    is_hedge_mode = _get_position_mode()

    filters = get_symbol_filters(symbol)
    if not isinstance(filters, dict) or filters.get("status") == "error":
        res = _structured_tool_error(
            "set_range_plan",
            error_class="symbol_filters_unavailable",
            why_rejected=f"无法获取 {symbol} 交易规格，不能安全规范化 range levels。",
            model_fix_hint="不要启动 range plan；等待交易规格刷新后重新评估，或设置复核 alarm。",
            invalid_args=call_args,
            retryable=False,
            defer_until_next_turn=True,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    lower_breakout = _normalize_order_price(float(lower_breakout), filters)
    upper_breakout = _normalize_order_price(float(upper_breakout), filters)
    if not lower_breakout < upper_breakout:
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_bounds_invalid",
            why_rejected="lower_breakout 必须小于 upper_breakout。",
            model_fix_hint="重新计算 hard_bounds，确保 lower_breakout < upper_breakout，且所有 inside-band levels 位于两者之间。",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    try:
        expiry_dt = datetime.strptime(str(expires_at or "").strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_expiry_invalid",
            why_rejected="expires_at 必须是 YYYY-MM-DD HH:MM:SS。",
            model_fix_hint="用北京时间墙上时间写一个未来 expiry，例如 2026-05-05 12:30:00；不要写自然语言或 ISO T 格式。",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res
    if expiry_dt <= datetime.now():
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_expiry_not_future",
            why_rejected="expires_at 必须是未来时间。",
            model_fix_hint="把 expires_at 改成晚于当前时间的 YYYY-MM-DD HH:MM:SS；若没有足够窗口，改为 set_alarm 复核而不是启动计划。",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    existing_plan = _read_range_plan_state()
    if _range_plan_is_active(existing_plan):
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_plan_active_conflict",
            why_rejected="An active range automation plan already exists.",
            model_fix_hint="Cancel the current range plan first or wait for breakout/expiry before replacing it.",
            docs_basis="CoinAutomation range plan guard: only one active range plan may own the symbol at a time.",
            invalid_args=call_args,
            retryable=False,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    live_positions = _active_live_positions(symbol)
    if live_positions["LONG"] > 0 or live_positions["SHORT"] > 0:
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_plan_active_conflict",
            why_rejected="Range automation must start from flat so the plan owns the full position lifecycle.",
            model_fix_hint="Flatten existing LONG/SHORT exposure first, then create the range plan.",
            docs_basis="CoinAutomation range plan guard: sideways automation cannot inherit pre-existing positions.",
            invalid_args=call_args,
            retryable=False,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    open_orders = get_usdt_futures_open_orders(symbol)
    if isinstance(open_orders, dict) and str(open_orders.get("status", "")).strip().lower() == "error":
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_open_orders_unknown",
            why_rejected=f"无法确认 {symbol} 当前挂单状态，不能确保 range plan 从 clean order book 启动。",
            model_fix_hint="不要启动 range plan；先等待 open orders 查询恢复，或设置 alarm 稍后复核。",
            invalid_args=call_args,
            retryable=False,
            defer_until_next_turn=True,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res
    if isinstance(open_orders, list) and open_orders:
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_plan_active_conflict",
            why_rejected="Range automation requires a clean order book for the symbol before activation.",
            model_fix_hint="Cancel stale manual/protective orders first, then set the range plan from a clean symbol state.",
            docs_basis="CoinAutomation range plan guard: range automation must not inherit unrelated pending orders.",
            invalid_args=call_args,
            retryable=False,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    try:
        normalized_long_levels = _normalize_range_levels(
            long_levels,
            bucket="long",
            lower_breakout=lower_breakout,
            upper_breakout=upper_breakout,
            filters=filters,
        )
        normalized_short_levels = _normalize_range_levels(
            short_levels,
            bucket="short",
            lower_breakout=lower_breakout,
            upper_breakout=upper_breakout,
            filters=filters,
        )
    except Exception as e:
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_levels_invalid",
            why_rejected=str(e),
            model_fix_hint="修正 long_levels/short_levels：entry/exit 必须在 hard_bounds 内，long entry < exit，short entry > exit，quantity 按 step_size 对齐。",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    if not normalized_long_levels and not normalized_short_levels:
        res = _structured_tool_error(
            "set_range_plan",
            error_class="range_levels_missing",
            why_rejected="至少需要一组 long_levels 或 short_levels。",
            model_fix_hint="如果决定 start range_decision，就必须提供可执行的一侧或双侧 levels；否则把 range_decision 改为 decline 并给出 reason_code。",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_range_plan", call_args, res)
        return res

    range_width = upper_breakout - lower_breakout
    width_pct = (range_width / upper_breakout) * 100.0 if upper_breakout > 0 else 0.0
    breakout_policy = _default_range_breakout_policy()
    if width_pct <= 0.9:
        breakout_policy["confirm_seconds"] = 240
        breakout_policy["extended_breakout_pct"] = 0.33
    elif width_pct <= 1.3:
        breakout_policy["confirm_seconds"] = 180
        breakout_policy["extended_breakout_pct"] = 0.28
    else:
        breakout_policy["confirm_seconds"] = 120
        breakout_policy["extended_breakout_pct"] = 0.22

    plan = _default_range_plan(source="set_range_plan")
    plan.update(
        {
            "status": "active",
            "symbol": symbol,
            "execution_style": TRIGGER_MARKET_EXECUTION_STYLE,
            "lower_breakout": lower_breakout,
            "upper_breakout": upper_breakout,
            "expires_at": expiry_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "activation_reason": explanation,
            "position_mode": "hedge" if is_hedge_mode else "one_way",
            "breakout_policy": breakout_policy,
            "breakout_watch": _default_range_breakout_watch(),
            "long_levels": normalized_long_levels,
            "short_levels": normalized_short_levels,
            "suppress_order_fill_wakeup": True,
        }
    )
    _write_range_plan_state(plan)
    maintenance = maintain_range_plan()
    res = {
        "status": "success",
        "message": "range automation plan activated",
        "plan": _read_range_plan_state(),
        "maintenance": maintenance,
    }
    _record_tool_call("set_range_plan", call_args, res)
    return res


def cancel_range_plan(symbol: str, explanation: str, close_positions: bool = False):
    """取消当前 active range automation 计划，并清理其托管挂单；可选一并平掉 range 仓位。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {
        "symbol": symbol,
        "explanation": explanation,
        "close_positions": close_positions,
    }
    explanation_error = _require_action_explanation("cancel_range_plan", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("cancel_range_plan", call_args, explanation_error)
        return explanation_error

    plan = _read_range_plan_state()
    if not _range_plan_is_active(plan):
        res = {"status": "skipped", "message": "当前无 active range plan"}
        _record_tool_call("cancel_range_plan", call_args, res)
        return res
    if str(plan.get("symbol", "") or "") != symbol:
        res = {"status": "skipped", "message": f"{symbol} 不是当前 active range plan 的符号"}
        _record_tool_call("cancel_range_plan", call_args, res)
        return res

    canceled_order_ids = _cancel_all_tracked_range_orders(plan, "manual range plan cancellation")
    closed_sides: List[str] = []
    if close_positions:
        closed_sides = _close_range_positions_if_needed(
            symbol,
            reason=f"Manual range plan cancellation: {explanation}",
        )

    plan["status"] = "canceled"
    plan["audit"]["last_sync_at"] = _range_plan_now()
    plan["audit"]["last_error"] = ""
    _write_range_plan_state(plan)
    res = {
        "status": "success",
        "message": "range automation plan canceled",
        "canceled_order_ids": canceled_order_ids,
        "closed_sides": closed_sides,
        "plan": _range_plan_summary(plan),
    }
    _record_tool_call("cancel_range_plan", call_args, res)
    return res


_get_coin_futures_symbol = _get_usdt_futures_symbol
_get_raw_coin_futures_positions = _get_raw_usdt_futures_positions


def get_symbol_filters(symbol: str):
    symbol = _get_usdt_futures_symbol(symbol)
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]

    try:
        info = client.futures_exchange_info()
        sym_info = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        if not sym_info:
            return None

        filters = {
            "price_precision": int(sym_info["pricePrecision"]),
            "qty_precision": int(sym_info["quantityPrecision"]),
            "tick_size": 0.0,
            "step_size": 0.0,
            "base_asset": sym_info["baseAsset"],
            "margin_asset": sym_info.get("marginAsset", sym_info.get("quoteAsset", "USDT")),
            "quote_asset": sym_info.get("quoteAsset", "USDT"),
        }

        for f in sym_info["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                filters["tick_size"] = float(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                filters["step_size"] = float(f["stepSize"])

        symbol_info_cache[symbol] = filters
        return filters
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _get_position_mode() -> bool:
    try:
        res = client.futures_get_position_mode()
        raw_mode = res.get("dualSidePosition", False)
        if isinstance(raw_mode, str):
            is_hedge = raw_mode.strip().lower() in {"true", "1", "yes"}
        else:
            is_hedge = bool(raw_mode)
        account_position_mode["is_hedge"] = is_hedge
        account_position_mode["checked_at"] = datetime.now().isoformat()
        account_position_mode["source"] = "exchange_api"
        return is_hedge
    except Exception:
        account_position_mode["source"] = "fallback_cache_after_error"
        return bool(account_position_mode.get("is_hedge", False))


def get_spot_balance(explanation: str = "") -> List[Dict[str, Any]]:
    """获取现货账户非零资产余额列表（free 或 locked 大于 0）；explanation 用于说明为何需要现货余额。"""
    account = client.get_account()
    balances = account.get("balances", [])
    result = [b for b in balances if float(b["free"]) > 0 or float(b["locked"]) > 0]
    _record_tool_call("get_spot_balance", {"explanation": explanation}, result)
    return result


def get_usdt_futures_position(symbol: str, explanation: str = "") -> Dict[str, Any]:
    """查询 U 本位合约持仓，返回方向、数量、开仓价、标记价、清算价和杠杆等信息；explanation 用于说明本次查询目的。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "explanation": explanation}
    is_hedge = _get_position_mode()

    positions = client.futures_position_information(symbol=symbol)
    active_positions = [p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0]

    if not active_positions:
        fallback_positions = [p for p in positions if p["symbol"] == symbol]
        if is_hedge:
            hedge_rows = [
                p for p in fallback_positions
                if str(p.get("positionSide", "")).upper() in {"LONG", "SHORT"}
            ]
            if hedge_rows:
                fallback_positions = hedge_rows
        if not fallback_positions:
            result = {
                "status": "flat",
                "positions": [],
                "is_hedge_mode": is_hedge,
                "position_mode_source": str(account_position_mode.get("source", "unknown")),
                "position_mode_checked_at": str(account_position_mode.get("checked_at", "")),
                "message": f"{symbol} 当前无活跃持仓",
                "note": "空仓不是错误；positions 为空表示当前该交易对没有活跃仓位。",
                "position_api_schema_hint": "python-binance futures_position_information -> /fapi/v3/positionRisk (leverage/marginType may be absent).",
                "leverage_sources": [],
            }
            _record_tool_call("get_usdt_futures_position", call_args, result)
            return result
        active_positions = fallback_positions

    results: List[Dict[str, Any]] = []
    leverage_sources: List[str] = []
    for pos in active_positions:
        leverage, leverage_source = _extract_position_leverage(pos, default=20)
        leverage_sources.append(leverage_source)
        results.append(
            {
                "symbol": pos["symbol"],
                "position_side": pos.get("positionSide", "BOTH"),
                "position_amount": float(pos["positionAmt"]),
                "entry_price": float(pos["entryPrice"]),
                "mark_price": float(pos["markPrice"]),
                "unrealized_profit": float(pos["unRealizedProfit"]),
                "liquidation_price": float(pos["liquidationPrice"]),
                "leverage": leverage,
                "margin_type": _normalize_margin_type(pos),
                "isolated_margin": _safe_float(pos.get("isolatedMargin"), 0.0),
                "leverage_source": leverage_source,
            }
        )

    result = {
        "positions": results,
        "is_hedge_mode": is_hedge,
        "position_mode_source": str(account_position_mode.get("source", "unknown")),
        "position_mode_checked_at": str(account_position_mode.get("checked_at", "")),
        "note": "position_amount 为正表示多头，为负表示空头。在双向模式下，LONG 为正，SHORT 为负。quantity 语义为标的数量。",
        "position_api_schema_hint": "python-binance futures_position_information -> /fapi/v3/positionRisk (leverage/marginType may be absent).",
        "leverage_sources": sorted(list(set(leverage_sources))),
    }
    _record_tool_call("get_usdt_futures_position", call_args, result)
    return result


def get_usdt_futures_account(explanation: str = "") -> Dict[str, Any]:
    """查询 U 本位合约账户资产余额，仅返回余额大于 0 的资产；explanation 用于说明本次查询目的。"""
    balances = client.futures_account_balance()
    active_balances = [b for b in balances if float(b["balance"]) > 0]
    result = {"balances": active_balances, "note": "U 本位合约通常使用 USDT/USDC 等稳定币作为保证金。"}
    _record_tool_call("get_usdt_futures_account", {"explanation": explanation}, result)
    return result


def get_usdt_futures_max_open_position(symbol: str, explanation: str = "") -> Dict[str, Any]:
    """估算 U 本位当前可开标的数量与平仓后潜在可开数量；explanation 用于说明容量查询目的。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "explanation": explanation}
    filters = get_symbol_filters(symbol)
    if not filters or isinstance(filters, dict) and filters.get("status") == "error":
        result = _structured_tool_error(
            "get_usdt_futures_max_open_position",
            error_class="symbol_filters_unavailable",
            why_rejected="无法获取交易对信息，不能可靠估算最大可开数量。",
            model_fix_hint="不要基于缺失的容量数据开仓；等待下一轮市场/交易所状态刷新后重新做 precheck。",
            invalid_args=call_args,
            retryable=False,
            defer_until_next_turn=True,
        )
        _record_tool_call("get_usdt_futures_max_open_position", call_args, result)
        return result

    margin_asset = filters["margin_asset"]
    acc_info = client.futures_account()
    asset_data = next((a for a in acc_info["assets"] if a["asset"] == margin_asset), None)
    if not asset_data:
        result = _structured_tool_error(
            "get_usdt_futures_max_open_position",
            error_class="margin_asset_missing",
            why_rejected=f"未在账户中找到保证金资产 {margin_asset}，不能可靠估算最大可开数量。",
            model_fix_hint="把执行状态标记为 blocked/wait，不要开仓；等待账户资产刷新或改用账户实际支持的 U 本位保证金资产。",
            invalid_args=call_args,
            retryable=False,
            defer_until_next_turn=True,
        )
        _record_tool_call("get_usdt_futures_max_open_position", call_args, result)
        return result

    available_margin = float(asset_data["availableBalance"])
    ticker = client.futures_symbol_ticker(symbol=symbol)
    price = float(ticker[0]["price"]) if isinstance(ticker, list) else float(ticker["price"])
    pos_info = client.futures_position_information(symbol=symbol)
    pos = next((p for p in pos_info if p["symbol"] == symbol), None)
    leverage, leverage_source = _extract_position_leverage(pos, default=20)

    margin_balance = float(asset_data["marginBalance"])
    max_qty_now = _normalize_order_quantity((available_margin * leverage) / max(price, 1e-9), filters)
    max_qty_potential = _normalize_order_quantity((margin_balance * leverage) / max(price, 1e-9), filters)

    result = {
        "symbol": symbol,
        "available_margin": available_margin,
        "margin_balance": margin_balance,
        "margin_asset": margin_asset,
        "leverage": leverage,
        "leverage_source": leverage_source,
        "current_price": price,
        "max_quantity": max_qty_now,
        "potential_max_if_flat": max_qty_potential,
        "step_size": float(filters.get("step_size", 0) or 0),
        "note": (
            f"当前可增持约 {max_qty_now} {filters['base_asset']}。若平仓后，理论最高可开约 {max_qty_potential} {filters['base_asset']}。 "
            f"数量按 step_size={filters.get('step_size', 0)} 向下取整。"
        ),
    }
    _record_tool_call("get_usdt_futures_max_open_position", call_args, result)
    return result


def transfer_to_usdt_futures(
    asset: str,
    amount: float,
    direction: Literal["TO_FUTURES", "FROM_FUTURES"],
    explanation: str,
):
    """在现货与 U 本位合约账户之间划转资金。"""
    call_args = {"asset": asset, "amount": amount, "direction": direction, "explanation": explanation}
    explanation_error = _require_action_explanation("transfer_to_usdt_futures", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("transfer_to_usdt_futures", call_args, explanation_error)
        return explanation_error
    transfer_type = 1 if direction == "TO_FUTURES" else 2
    res = client.futures_account_transfer(asset=asset, amount=amount, type=transfer_type)
    log_trade("transfer_futures", call_args, res, explanation=explanation)
    _record_tool_call("transfer_to_usdt_futures", call_args, res)
    return res


def set_usdt_futures_leverage(symbol: str, leverage: int, explanation: str):
    """设置 U 本位合约指定交易对杠杆倍数。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "leverage": leverage, "explanation": explanation}
    explanation_error = _require_action_explanation("set_usdt_futures_leverage", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("set_usdt_futures_leverage", call_args, explanation_error)
        return explanation_error
    res = client.futures_change_leverage(symbol=symbol, leverage=leverage)
    log_trade("set_futures_leverage", call_args, res, explanation=explanation)
    _record_tool_call("set_usdt_futures_leverage", call_args, res)
    return res


def set_usdt_futures_margin_type(symbol: str, margin_type: Literal["ISOLATED", "CROSSED"], explanation: str):
    """设置 U 本位合约指定交易对保证金模式（逐仓或全仓）。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "margin_type": margin_type, "explanation": explanation}
    explanation_error = _require_action_explanation("set_usdt_futures_margin_type", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("set_usdt_futures_margin_type", call_args, explanation_error)
        return explanation_error
    res = client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
    log_trade("set_futures_margin_type", call_args, res, explanation=explanation)
    _record_tool_call("set_usdt_futures_margin_type", call_args, res)
    return res


def get_usdt_futures_open_orders(symbol: str = None, explanation: str = ""):
    """查询 U 本位合约当前挂单（含 conditional/algo 保护单）；传 symbol 时仅返回该交易对挂单；explanation 用于说明查询目的。"""
    args = {"symbol": symbol, "explanation": explanation}
    if symbol:
        symbol = _get_usdt_futures_symbol(symbol)
        args["symbol"] = symbol
    try:
        res = _fetch_all_usdt_futures_open_orders(symbol)
    except Exception as e:
        res = _structured_tool_error(
            "get_usdt_futures_open_orders",
            error_class="open_orders_query_failed",
            why_rejected="无法查询当前挂单，不能可靠判断保护单、撤单或 range 清理状态。",
            model_fix_hint="不要基于未知挂单状态执行撤单/改单/保护单替换；等待刷新或改为只设置复核 alarm。",
            raw_message=str(e),
            invalid_args=args,
            retryable=False,
            defer_until_next_turn=True,
        )
    _record_tool_call("get_usdt_futures_open_orders", args, res)
    return res


def cancel_usdt_futures_order(symbol: str, order_id: str, explanation: str):
    """撤销 U 本位单笔挂单；order_id 必须为纯数字字符串，可对应普通订单或 conditional/algo 订单。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "order_id": order_id, "explanation": explanation}
    explanation_error = _require_action_explanation("cancel_usdt_futures_order", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("cancel_usdt_futures_order", call_args, explanation_error)
        return explanation_error
    range_plan_guard = _guard_range_plan_conflict(
        "cancel_usdt_futures_order",
        symbol=symbol,
        order_id=order_id,
    )
    if range_plan_guard is not None:
        _record_tool_call("cancel_usdt_futures_order", call_args, range_plan_guard)
        return range_plan_guard
    duplicate_guard = _guard_duplicate_followup_housekeeping("cancel_usdt_futures_order", call_args)
    if duplicate_guard:
        _record_tool_call("cancel_usdt_futures_order", call_args, duplicate_guard)
        return duplicate_guard
    retry_duplicate_warning = _detect_retry_duplicate_housekeeping_warning("cancel_usdt_futures_order", call_args)
    try:
        if not str(order_id).isdigit():
            res = _structured_tool_error(
                "cancel_coin_futures_order",
                error_class="invalid_order_id",
                why_rejected="order_id must be a numeric string.",
                model_fix_hint="Pass the Binance numeric order_id exactly as returned by get_usdt_futures_order/get_usdt_futures_open_orders.",
                docs_basis="CoinAutomation production guard: order_id is constrained to Binance numeric order ids.",
                invalid_args=call_args,
            )
        else:
            active_positions = _get_active_position_snapshot(symbol)
            target_order = None
            open_orders: List[Dict[str, Any]] = []
            if active_positions:
                open_orders = _fetch_all_usdt_futures_open_orders(symbol)
                target_order = _find_open_order_by_ref(open_orders, order_id)
                if target_order and _is_protective_open_order(target_order):
                    target_side = str(target_order.get("positionSide", "BOTH")).upper()
                    matching_protection = [
                        order
                        for order in open_orders
                        if _is_protective_open_order(order)
                        and str(order.get("positionSide", "BOTH")).upper() == target_side
                    ]
                    if target_side in active_positions and len(matching_protection) <= 1:
                        res = _structured_tool_error(
                            "cancel_usdt_futures_order",
                            error_class="last_verified_protection_guard",
                            why_rejected="This order is the last verified protective order covering a live position.",
                            model_fix_hint="Keep the old protective order active until a replacement protective order is submitted and verified, or close/reduce the live position first.",
                            docs_basis="CoinAutomation production guard: do not cancel the last live protective order while the position remains open.",
                            invalid_args=call_args,
                            retryable=False,
                            defer_until_next_turn=True,
                        )
                        _record_tool_call("cancel_usdt_futures_order", call_args, res)
                        return res
            else:
                try:
                    open_orders = _fetch_all_usdt_futures_open_orders(symbol)
                    target_order = _find_open_order_by_ref(open_orders, order_id)
                except Exception:
                    open_orders = []
                    target_order = None
            if target_order and bool(target_order.get("is_conditional")):
                try:
                    res = client.futures_cancel_algo_order(symbol=symbol, algoId=order_id)
                except Exception as e:
                    classified = _classify_binance_order_error("cancel_usdt_futures_order", str(e), call_args)
                    if (
                        isinstance(classified, dict)
                        and str(classified.get("error_class", "")).strip() == "order_not_found"
                        and not active_positions
                    ):
                        try:
                            refreshed_orders = _fetch_all_usdt_futures_open_orders(symbol)
                        except Exception:
                            refreshed_orders = []
                        still_open = _find_open_order_by_ref(refreshed_orders, order_id)
                        if still_open and bool(still_open.get("is_conditional")):
                            conditional_res = client.futures_cancel_all_algo_open_orders(symbol=symbol)
                            remaining_orders = _fetch_all_usdt_futures_open_orders(symbol)
                            if _find_open_order_by_ref(remaining_orders, order_id) is None:
                                res = {
                                    "status": "success",
                                    "symbol": symbol,
                                    "order_id": str(order_id),
                                    "cleanup_mode": "conditional_cancel_all_fallback",
                                    "message": "单笔 conditional 撤单返回 order_not_found，但 open orders 仍可见同一保护单；已在同一工具内执行 conditional cleanup fallback。",
                                    "conditional_orders": conditional_res,
                                }
                            else:
                                res = classified
                                res["post_refresh_open_order_count"] = len(remaining_orders)
                        else:
                            res = classified
                    else:
                        res = classified
            else:
                res = client.futures_cancel_order(symbol=symbol, orderId=order_id)
            log_trade("cancel_futures", call_args, res, explanation=explanation)
    except Exception as e:
        res = _classify_binance_order_error("cancel_usdt_futures_order", str(e), call_args)
    if isinstance(res, dict) and retry_duplicate_warning:
        res["_retry_warning"] = retry_duplicate_warning
    _record_tool_call("cancel_usdt_futures_order", call_args, res)
    return res


def cancel_all_usdt_futures_orders(symbol: str, explanation: str):
    """撤销 U 本位指定交易对全部挂单（含 conditional/algo 订单）。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "explanation": explanation}
    explanation_error = _require_action_explanation("cancel_all_usdt_futures_orders", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("cancel_all_usdt_futures_orders", call_args, explanation_error)
        return explanation_error
    range_plan_guard = _guard_range_plan_conflict(
        "cancel_all_usdt_futures_orders",
        symbol=symbol,
    )
    if range_plan_guard is not None:
        _record_tool_call("cancel_all_usdt_futures_orders", call_args, range_plan_guard)
        return range_plan_guard
    try:
        active_positions = _get_active_position_snapshot(symbol)
        if active_positions:
            open_orders = _fetch_all_usdt_futures_open_orders(symbol)
            protective_orders = [order for order in open_orders if _is_protective_open_order(order)]
            for pos_side in active_positions.keys():
                if any(str(order.get("positionSide", "BOTH")).upper() == pos_side for order in protective_orders):
                    res = _structured_tool_error(
                        "cancel_all_usdt_futures_orders",
                        error_class="live_protection_cancel_all_blocked",
                        why_rejected="cancel_all would remove the last verified protective coverage for a live position.",
                        model_fix_hint="Do not use cancel_all while a live position still depends on existing protective orders. Verify a replacement protection first, or close/reduce the position before cleanup.",
                        docs_basis="CoinAutomation production guard: cancel_all is blocked when live positions still rely on existing protective orders.",
                        invalid_args=call_args,
                        retryable=False,
                        defer_until_next_turn=True,
                    )
                    _record_tool_call("cancel_all_usdt_futures_orders", call_args, res)
                    return res
        standard_res = client.futures_cancel_all_open_orders(symbol=symbol)
        conditional_res = client.futures_cancel_all_algo_open_orders(symbol=symbol)
        res = {
            "symbol": symbol,
            "standard_orders": standard_res,
            "conditional_orders": conditional_res,
        }
        log_trade("cancel_all_futures", call_args, res, explanation=explanation)
    except Exception as e:
        res = _classify_binance_order_error("cancel_all_usdt_futures_orders", str(e), call_args)
    _record_tool_call("cancel_all_usdt_futures_orders", call_args, res)
    return res


def trade_usdt_futures(
    symbol: str,
    side: Literal["BUY", "SELL"],
    explanation: str,
    quantity: float = None,
    price: float = None,
    order_type: Literal[
        "LIMIT",
        "MARKET",
        "STOP",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    ] = "MARKET",
    stop_price: float = None,
    reduce_only: bool = False,
    position_side: Literal["LONG", "SHORT", "BOTH"] = "BOTH",
    time_in_force: Literal["GTC", "IOC", "FOK", "GTX"] = "GTC",
    callback_rate: float = None,
    activation_price: float = None,
    close_position: bool = False,
):
    """提交 U 本位合约订单。

    保护单语义必须二选一：
    - 全平保护：`order_type=STOP_MARKET` 或 `TAKE_PROFIT_MARKET`，`close_position=true`，且不要传 `quantity`。
    - 部分保护：传 `quantity`，并保持 `close_position=false`。

    在双向模式下：
    - 保护 LONG：`side=SELL` 且 `position_side=LONG`
    - 保护 SHORT：`side=BUY` 且 `position_side=SHORT`
    """
    symbol = _get_usdt_futures_symbol(symbol)
    order_type = str(order_type or "MARKET").upper()
    side = str(side or "").upper()
    position_side = str(position_side or "BOTH").upper()
    reduce_only = bool(reduce_only)
    close_position = bool(close_position)
    call_args = {
        "symbol": symbol,
        "side": side,
        "explanation": explanation,
        "quantity": quantity,
        "price": price,
        "order_type": order_type,
        "stop_price": stop_price,
        "reduce_only": reduce_only,
        "position_side": position_side,
        "time_in_force": time_in_force,
        "callback_rate": callback_rate,
        "activation_price": activation_price,
        "close_position": close_position,
    }
    explanation_error = _require_action_explanation("trade_usdt_futures", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("trade_usdt_futures", call_args, explanation_error)
        return explanation_error
    is_hedge = _get_position_mode()
    filters = get_symbol_filters(symbol)

    if order_type not in SUPPORTED_USDT_FUTURES_ORDER_TYPES:
        res = _structured_tool_error(
            "trade_usdt_futures",
            error_class="unsupported_order_type",
            why_rejected="This order_type is not supported by Binance USD(S)-M /fapi/v1/order.",
            model_fix_hint="Use LIMIT, MARKET, STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET, or TRAILING_STOP_MARKET.",
            docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}). LIMIT_MAKER is not exposed in this production tool.",
            retryable=True,
            invalid_args=call_args,
            allowed_shapes=list(sorted(SUPPORTED_USDT_FUTURES_ORDER_TYPES)),
        )
        _record_tool_call("trade_usdt_futures", call_args, res)
        return res

    if side not in {"BUY", "SELL"}:
        res = _structured_tool_error(
            "trade_usdt_futures",
            error_class="invalid_side",
            why_rejected="side must be BUY or SELL.",
            model_fix_hint="Use BUY for entering/covering and SELL for exiting/shorting according to the intended position direction.",
            docs_basis="CoinAutomation production guard: side must be BUY or SELL.",
            invalid_args=call_args,
        )
        _record_tool_call("trade_usdt_futures", call_args, res)
        return res

    if position_side not in {"LONG", "SHORT", "BOTH"}:
        res = _structured_tool_error(
            "trade_usdt_futures",
            error_class="invalid_position_side",
            why_rejected="position_side must be LONG, SHORT, or BOTH.",
            model_fix_hint="Use LONG or SHORT in hedge mode, and use BOTH only in one-way mode.",
            docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
            invalid_args=call_args,
        )
        _record_tool_call("trade_usdt_futures", call_args, res)
        return res

    is_protective_order_type = order_type in PROTECTIVE_ORDER_TYPES
    live_positions = _get_active_position_snapshot(symbol) if is_protective_order_type else {}
    protective_target = _find_protective_target(live_positions, side, position_side) if is_protective_order_type else None
    is_protective_trade = bool(protective_target)

    if order_type in {"MARKET", "LIMIT"} and not is_protective_trade:
        range_plan_guard = _guard_range_plan_conflict(
            "trade_usdt_futures",
            symbol=symbol,
        )
        if range_plan_guard is not None:
            _record_tool_call("trade_usdt_futures", call_args, range_plan_guard)
            return range_plan_guard
        opening_guard = _guard_capacity_sequence_for_opening(symbol)
        if opening_guard is not None:
            _record_tool_call("trade_usdt_futures", call_args, opening_guard)
            return opening_guard
        quality_guard = _guard_short_entry_quality(
            symbol=symbol,
            side=side,
            position_side=position_side,
            order_type=order_type,
            price=price,
            is_hedge=is_hedge,
            call_args=call_args,
        )
        if quality_guard is not None:
            _record_tool_call("trade_usdt_futures", call_args, quality_guard)
            return quality_guard

    if filters and isinstance(filters, dict) and filters.get("tick_size"):
        if price is not None:
            price = round_step_size(price, filters["tick_size"])
        if stop_price is not None:
            stop_price = round_step_size(stop_price, filters["tick_size"])
        if activation_price is not None:
            activation_price = round_step_size(activation_price, filters["tick_size"])

    params: Dict[str, Any] = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
    }
    if quantity is not None:
        normalized_quantity = _normalize_order_quantity(quantity, filters or {})
        if normalized_quantity <= 0:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="quantity_too_small",
                why_rejected="quantity rounds down to zero under the symbol step size.",
                model_fix_hint="Increase quantity to at least one valid step_size increment before submitting.",
                docs_basis="CoinAutomation USD(S)-M precision guard.",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        quantity = normalized_quantity
        params["quantity"] = quantity

    if is_protective_trade:
        if is_hedge and position_side == "BOTH":
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="protective_position_side_required",
                why_rejected="Hedge Mode protective orders must target a concrete live side, not BOTH.",
                model_fix_hint="For LONG protection use SELL + position_side=LONG. For SHORT protection use BUY + position_side=SHORT.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}) and CoinAutomation protective-order guard.",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        if close_position and order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="close_position_order_type_invalid",
                why_rejected="close_position=true is only supported here for STOP_MARKET or TAKE_PROFIT_MARKET.",
                model_fix_hint="Use STOP_MARKET/TAKE_PROFIT_MARKET for full-close protection, or use a quantity-based protective order without close_position.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        if close_position and quantity is not None:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="close_position_with_quantity",
                why_rejected="close_position=true must not be combined with quantity.",
                model_fix_hint="For full-close protection, omit quantity. For partial protection, set quantity and do not use close_position.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        if close_position and reduce_only:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="close_position_with_reduce_only",
                why_rejected="close_position=true and reduce_only=true must not be combined in one protective order request.",
                model_fix_hint="Choose one legal protective shape only: close_position=true without quantity, or quantity-based protection without close_position.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        if not close_position and quantity is None:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="protective_quantity_missing",
                why_rejected="A quantity-based protective order must include quantity unless close_position=true is used.",
                model_fix_hint="Set quantity for a partial protective order, or switch to close_position=true for a full-close STOP_MARKET/TAKE_PROFIT_MARKET.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        if quantity is not None and protective_target and float(quantity) > float(protective_target["abs_amount"]) + 1e-12:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="protective_quantity_exceeds_position",
                why_rejected="The protective quantity exceeds the currently held live position size.",
                model_fix_hint="Keep quantity less than or equal to the live position size, or use close_position=true for a full-close market trigger.",
                docs_basis="CoinAutomation protective-order guard: protective quantity must not exceed the live position size.",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        if is_hedge and reduce_only:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="hedge_mode_reduce_only_not_supported",
                why_rejected="This production tool does not allow reduce_only in Hedge Mode for protective orders because Binance USD(S)-M frequently rejects or complicates that combination.",
                model_fix_hint="In Hedge Mode, submit quantity-based protective orders with a directionally correct position_side, or use close_position=true for full-close STOP_MARKET/TAKE_PROFIT_MARKET.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}) and observed API rejection pattern (-2022/-2026/-1128).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
    elif is_protective_order_type and (reduce_only or close_position):
        res = _structured_tool_error(
            "trade_usdt_futures",
            error_class="protective_semantic_mismatch",
            why_rejected="The request claims protective semantics, but side/position_side do not match any live position that can be reduced or closed.",
            model_fix_hint="Protect LONG with SELL + LONG. Protect SHORT with BUY + SHORT. If there is no live position, do not set reduce_only/close_position.",
            docs_basis="CoinAutomation protective-order guard: protective orders must align with a live position.",
            invalid_args=call_args,
            retryable=True,
        )
        _record_tool_call("trade_usdt_futures", call_args, res)
        return res

    if is_hedge:
        if position_side == "BOTH":
            params["positionSide"] = "LONG" if side == "BUY" else "SHORT"
        else:
            params["positionSide"] = position_side
    else:
        params["positionSide"] = "BOTH"
        if reduce_only and not close_position:
            params["reduceOnly"] = "true"

    if order_type == "LIMIT":
        if price is None:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="limit_price_missing",
                why_rejected="LIMIT orders require price.",
                model_fix_hint="Provide a limit price rounded to the symbol tick size.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        params["price"] = price
        params["timeInForce"] = time_in_force
    elif order_type in ["STOP", "TAKE_PROFIT"]:
        if price is None or stop_price is None:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="trigger_price_missing",
                why_rejected=f"{order_type} requires both price and stop_price.",
                model_fix_hint="Provide both the trigger stop_price and the working limit price.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        params["price"] = price
        params["stopPrice"] = stop_price
        params["timeInForce"] = time_in_force
    elif order_type in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
        if stop_price is None:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="stop_price_missing",
                why_rejected=f"{order_type} requires stop_price.",
                model_fix_hint="Provide a stop_price rounded to the symbol tick size.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        params["stopPrice"] = stop_price
        if close_position:
            params["closePosition"] = "true"
            params.pop("quantity", None)
            params.pop("reduceOnly", None)
        elif quantity is None:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="protective_quantity_missing",
                why_rejected=f"{order_type} requires quantity unless close_position=true is used.",
                model_fix_hint="Set quantity for partial protection or use close_position=true for full-close protection.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
    elif order_type == "TRAILING_STOP_MARKET":
        if callback_rate is None or quantity is None:
            res = _structured_tool_error(
                "trade_usdt_futures",
                error_class="trailing_params_missing",
                why_rejected="TRAILING_STOP_MARKET requires callback_rate and quantity.",
                model_fix_hint="Provide callback_rate and quantity. Do not use close_position=true with TRAILING_STOP_MARKET in this production tool.",
                docs_basis=f"Binance USD(S)-M New Order ({USDT_FUTURES_NEW_ORDER_DOC}).",
                invalid_args=call_args,
                retryable=True,
            )
            _record_tool_call("trade_usdt_futures", call_args, res)
            return res
        params["callbackRate"] = callback_rate
        if activation_price is not None:
            params["activationPrice"] = activation_price

    try:
        res = client.futures_create_order(**params)
        log_trade(
            "trade_futures",
            {"symbol": symbol, "side": side, "quantity": quantity, "type": order_type},
            res,
            explanation=explanation,
        )
    except Exception as e:
        res = _classify_binance_order_error("trade_usdt_futures", str(e), call_args)

    _record_tool_call(
        "trade_usdt_futures",
        call_args,
        res,
    )
    return res


def modify_usdt_futures_order(
    symbol: str,
    explanation: str,
    order_id: int = None,
    orig_client_order_id: str = None,
    side: Literal["BUY", "SELL"] = None,
    quantity: float = None,
    price: float = None,
):
    """修改 U 本位 LIMIT 挂单的价格或数量（交易所会重排队）。"""
    symbol = _get_usdt_futures_symbol(symbol)
    filters = get_symbol_filters(symbol)

    params: Dict[str, Any] = {"symbol": symbol}
    call_args: Dict[str, Any] = {"symbol": symbol, "explanation": explanation}
    explanation_error = _require_action_explanation("modify_usdt_futures_order", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("modify_usdt_futures_order", call_args, explanation_error)
        return explanation_error
    if order_id:
        params["orderId"] = order_id
        call_args["order_id"] = order_id
    if orig_client_order_id:
        params["origClientOrderId"] = orig_client_order_id
        call_args["orig_client_order_id"] = orig_client_order_id

    try:
        live_order = None
        open_orders: List[Dict[str, Any]] = []
        target_ref = str(order_id or "").strip()
        client_ref = str(orig_client_order_id or "").strip()
        if target_ref or client_ref:
            try:
                open_orders = _fetch_all_usdt_futures_open_orders(symbol)
            except Exception:
                open_orders = []
            if target_ref:
                live_order = _find_open_order_by_ref(open_orders, target_ref)
            if live_order is None and client_ref:
                live_order = next(
                    (
                        order
                        for order in open_orders
                        if str(order.get("clientOrderId", "") or "").strip() == client_ref
                        or str(order.get("clientAlgoId", "") or "").strip() == client_ref
                    ),
                    None,
                )

        if live_order is not None:
            order_type = str(live_order.get("type", live_order.get("orderType", "")) or "").upper()
            if bool(live_order.get("is_conditional")) or order_type != "LIMIT":
                res = _structured_tool_error(
                    "modify_usdt_futures_order",
                    error_class="modify_non_limit_order",
                    why_rejected="This tool only supports live standard LIMIT orders; the referenced order is conditional/protective or otherwise non-LIMIT.",
                    model_fix_hint=(
                        "Do not use modify_usdt_futures_order for STOP/TAKE_PROFIT/conditional protection. "
                        "Cancel the old protective order intentionally, then submit a fresh legal protective order."
                    ),
                    docs_basis="CoinAutomation execution contract: modify_usdt_futures_order is limited to standard LIMIT orders.",
                    invalid_args=call_args,
                    retryable=True,
                    defer_until_next_turn=False,
                )
                _record_tool_call("modify_usdt_futures_order", call_args, res)
                return res
            if not side:
                params["side"] = str(live_order.get("side", "") or "").upper()
        elif not side and order_id:
            historical_order = client.futures_get_order(symbol=symbol, orderId=order_id)
            order_type = str(historical_order.get("type", historical_order.get("origType", "")) or "").upper()
            if order_type and order_type != "LIMIT":
                res = _structured_tool_error(
                    "modify_usdt_futures_order",
                    error_class="modify_non_limit_order",
                    why_rejected="This tool only supports LIMIT orders; the referenced Binance order is not LIMIT.",
                    model_fix_hint=(
                        "Use modify_usdt_futures_order only for pending LIMIT orders. "
                        "For conditional/protective orders, cancel and recreate the order with the intended parameters."
                    ),
                    docs_basis="CoinAutomation execution contract: modify_usdt_futures_order is limited to standard LIMIT orders.",
                    invalid_args=call_args,
                    retryable=True,
                    defer_until_next_turn=False,
                )
                _record_tool_call("modify_usdt_futures_order", call_args, res)
                return res
            params["side"] = historical_order["side"]
        elif side:
            params["side"] = side
        if params.get("side") is not None:
            call_args["side"] = params.get("side")

        if quantity is not None:
            normalized_quantity = _normalize_order_quantity(quantity, filters or {})
            params["quantity"] = normalized_quantity
            call_args["quantity"] = normalized_quantity

        if price is not None:
            if filters and isinstance(filters, dict) and filters.get("tick_size"):
                price = round_step_size(price, filters["tick_size"])
            params["price"] = price
            call_args["price"] = price

        res = client.futures_modify_order(**params)
        log_trade("modify_futures", call_args, res, explanation=explanation)
    except Exception as e:
        res = _classify_binance_order_error("modify_usdt_futures_order", str(e), call_args)

    _record_tool_call("modify_usdt_futures_order", call_args, res)
    return res


def get_usdt_futures_order(symbol: str, order_id: str, explanation: str = ""):
    """查询 U 本位单笔订单详情；order_id 必须为纯数字字符串；explanation 用于说明查询目的。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {"symbol": symbol, "order_id": order_id, "explanation": explanation}
    try:
        if not str(order_id).isdigit():
            res = _structured_tool_error(
                "get_usdt_futures_order",
                error_class="invalid_order_id",
                why_rejected="order_id 必须为纯数字字符串。",
                model_fix_hint="从 get_usdt_futures_open_orders 或交易所返回中复制 numeric orderId；不要传 alarm_id、client id 或自然语言。",
                invalid_args=call_args,
                retryable=True,
                defer_until_next_turn=False,
            )
        else:
            open_orders = _fetch_all_usdt_futures_open_orders(symbol)
            live_order = _find_open_order_by_ref(open_orders, order_id)
            if live_order:
                res = dict(live_order)
                res["query_source"] = "open_orders_snapshot"
            else:
                res = client.futures_get_order(symbol=symbol, orderId=order_id)
    except Exception as e:
        if "-2011" in str(e) or "-2013" in str(e) or "Unknown order" in str(e):
            res = {"status": "not_found", "message": "订单不存在", "query_source": "exchange_order_endpoint"}
        else:
            res = _structured_tool_error(
                "get_usdt_futures_order",
                error_class="order_query_failed",
                why_rejected="订单查询失败，不能可靠确认该订单状态。",
                model_fix_hint="不要把订单描述为已成交/已撤/仍挂；先用 open orders + position 刷新状态，或等待下一轮复核。",
                raw_message=str(e),
                invalid_args=call_args,
                retryable=False,
                defer_until_next_turn=True,
            )

    _record_tool_call("get_usdt_futures_order", call_args, res)
    return res


def close_usdt_futures_position(
    symbol: str,
    explanation: str,
    quantity: float = None,
    position_side: Literal["LONG", "SHORT", "BOTH"] = "BOTH",
):
    """按方向执行 U 本位减仓/平仓（MARKET），未传 quantity 默认尽量全平。"""
    symbol = _get_usdt_futures_symbol(symbol)
    call_args = {
        "symbol": symbol,
        "explanation": explanation,
        "quantity": quantity,
        "position_side": position_side,
    }
    explanation_error = _require_action_explanation("close_usdt_futures_position", explanation, call_args)
    if explanation_error is not None:
        _record_tool_call("close_usdt_futures_position", call_args, explanation_error)
        return explanation_error
    try:
        is_hedge = _get_position_mode()
        pos_info = client.futures_position_information(symbol=symbol)
        filters = get_symbol_filters(symbol)

        if is_hedge:
            if position_side == "BOTH":
                active = [p for p in pos_info if p["symbol"] == symbol and float(p["positionAmt"]) != 0]
                if not active:
                    res = {"status": "skipped", "message": "当前无活跃持仓"}
                    _record_tool_call("close_usdt_futures_position", call_args, res)
                    return res
                target_pos = active[0]
            else:
                target_pos = next(
                    (p for p in pos_info if p["symbol"] == symbol and p["positionSide"] == position_side),
                    None,
                )
        else:
            target_pos = next((p for p in pos_info if p["symbol"] == symbol), None)

        if not target_pos or float(target_pos["positionAmt"]) == 0:
            res = {"status": "skipped", "message": "未找到对应的活跃持仓"}
            _record_tool_call("close_usdt_futures_position", call_args, res)
            return res

        current_qty = float(target_pos["positionAmt"])
        side = "SELL" if current_qty > 0 else "BUY"
        if quantity is None:
            quantity = abs(current_qty)
        else:
            quantity = min(float(quantity), abs(current_qty))
        quantity = _normalize_order_quantity(quantity, filters or {})
        if quantity <= 0:
            res = {"status": "skipped", "message": "平仓数量低于最小 step_size，未执行"}
            _record_tool_call("close_usdt_futures_position", call_args, res)
            return res
        call_args["quantity"] = quantity
        call_args["position_side"] = target_pos["positionSide"] if is_hedge else "BOTH"

        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }

        if is_hedge:
            params["positionSide"] = target_pos["positionSide"]
        else:
            params["positionSide"] = "BOTH"
            params["reduceOnly"] = "true"

        res = client.futures_create_order(**params)
        log_trade("close_futures", {"symbol": symbol, "quantity": quantity}, res, explanation=explanation)
    except Exception as e:
        exchange_code = _extract_exchange_error_code(str(e))
        if exchange_code is not None:
            res = _classify_binance_order_error("close_usdt_futures_position", str(e), call_args)
        else:
            res = _structured_tool_error(
                "close_usdt_futures_position",
                error_class="position_close_failed",
                why_rejected="Unable to confirm or close the live position due to an execution-layer failure.",
                model_fix_hint=(
                    "Refresh live position and open orders before describing the position as closed. "
                    "If live state cannot be refreshed reliably, defer trading changes to the next wakeup."
                ),
                invalid_args=call_args,
                raw_message=str(e),
                retryable=False,
                defer_until_next_turn=True,
            )
    _record_tool_call(
        "close_usdt_futures_position",
        call_args,
        res,
    )
    return res


def calculate_expression(expression: str, explanation: str = ""):
    """执行受限四则运算表达式计算（仅数字与运算符）；explanation 用于说明这次计算服务于哪个风险/价格判断。"""
    call_args = {"expression": expression, "explanation": explanation}
    if not re.match(r"^[0-9+\-*/().\s**]+$", expression):
        res = _structured_tool_error(
            "calculate_expression",
            error_class="expression_invalid",
            why_rejected="表达式包含非法字符；该工具只接受数字、括号和四则运算符。",
            model_fix_hint="只传纯数学表达式，例如 2364.96*1.002；不要传自然语言、变量名或单位。",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("calculate_expression", call_args, res)
        return res
    try:
        result = eval(expression, {"__builtins__": None}, {})
        res = {"expression": expression, "result": float(result)}
    except Exception as e:
        res = _structured_tool_error(
            "calculate_expression",
            error_class="expression_eval_failed",
            why_rejected=str(e),
            model_fix_hint="检查括号、除零和数字格式；如果计算不是必须，直接用已知市场数据重写计划。",
            invalid_args=call_args,
            retryable=True,
            defer_until_next_turn=False,
        )
    _record_tool_call("calculate_expression", call_args, res)
    return res


# Backward-compatible aliases for historical scripts/log readers during the U-margined migration.
get_coin_futures_position = get_usdt_futures_position
get_coin_futures_account = get_usdt_futures_account
get_coin_futures_max_open_position = get_usdt_futures_max_open_position
transfer_to_coin_futures = transfer_to_usdt_futures
set_coin_futures_leverage = set_usdt_futures_leverage
set_coin_futures_margin_type = set_usdt_futures_margin_type
get_coin_futures_open_orders = get_usdt_futures_open_orders
cancel_coin_futures_order = cancel_usdt_futures_order
cancel_all_coin_futures_orders = cancel_all_usdt_futures_orders
trade_coin_futures = trade_usdt_futures
modify_coin_futures_order = modify_usdt_futures_order
get_coin_futures_order = get_usdt_futures_order
close_coin_futures_position = close_usdt_futures_position


ALARM_METRIC_ALIASES = {
    "price": "price",
    "PRICE": "price",
    "price_15m": "price",
    "PRICE_15M": "price",
    "price_1h": "price_1h",
    "PRICE_1H": "price_1h",
    "RSI": "RSI_15m",
    "RSI_15m": "RSI_15m",
    "RSI_1h": "RSI_1h",
    "MACD": "MACD_HISTO_15m",
    "MACD_HISTO": "MACD_HISTO_15m",
    "MACD_HISTO_15m": "MACD_HISTO_15m",
    "MACD_HISTO_1h": "MACD_HISTO_1h",
}


def _normalize_alarm_metric(raw_metric: Any):
    metric = ALARM_METRIC_ALIASES.get(str(raw_metric or "").strip())
    if metric is None:
        supported = ", ".join(sorted(ALARM_METRIC_ALIASES.keys()))
        return None, f"condition.metric 不支持，允许: {supported}"
    return metric, None


def _normalize_alarm_operator(raw_operator: Any):
    operator = str(raw_operator or "").strip()
    operator_aliases = {
        ">": ">",
        "<": "<",
        ">=": ">=",
        "<=": "<=",
        "gt": ">",
        "lt": "<",
        "gte": ">=",
        "lte": "<=",
    }
    normalized = operator_aliases.get(operator)
    if normalized is None:
        return None, "condition.operator 只允许 > / < / >= / <="
    return normalized, None


def _normalize_alarm_expr_text(raw_expr: Any):
    text = str(raw_expr or "").strip()
    if not text:
        return ""
    text = re.sub(r"\bAND\b", "&", text, flags=re.IGNORECASE)
    text = re.sub(r"\bOR\b", "|", text, flags=re.IGNORECASE)
    return text


def _tokenize_alarm_expr(expr: str):
    token_pattern = re.compile(
        r"\s*("
        r"\(|\)|\&|\||>=|<=|>|<|"
        r"[A-Za-z_][A-Za-z0-9_]*|"
        r"-?\d+(?:\.\d+)?"
        r")"
    )
    tokens = []
    idx = 0
    text = _normalize_alarm_expr_text(expr)
    while idx < len(text):
        match = token_pattern.match(text, idx)
        if not match:
            return None, f"condition.expr 在位置 {idx} 存在非法字符"
        token = match.group(1)
        tokens.append(token)
        idx = match.end()
    return tokens, None


def _parse_alarm_expr(tokens: List[str]):
    idx = 0

    def parse_atom():
        nonlocal idx
        if idx >= len(tokens):
            raise ValueError("条件表达式不完整")
        token = tokens[idx]
        if token == "(":
            idx += 1
            node = parse_or()
            if idx >= len(tokens) or tokens[idx] != ")":
                raise ValueError("条件表达式缺少右括号")
            idx += 1
            return node

        metric, metric_error = _normalize_alarm_metric(token)
        if metric_error:
            raise ValueError(metric_error)
        idx += 1
        if idx >= len(tokens):
            raise ValueError("条件表达式缺少比较符")
        operator, operator_error = _normalize_alarm_operator(tokens[idx])
        if operator_error:
            raise ValueError(operator_error)
        idx += 1
        if idx >= len(tokens):
            raise ValueError("条件表达式缺少阈值")
        try:
            value = float(tokens[idx])
        except ValueError as exc:
            raise ValueError("条件表达式阈值必须是数值") from exc
        idx += 1
        return {"metric": metric, "operator": operator, "value": value}

    def parse_and():
        nonlocal idx
        node = parse_atom()
        children = [node]
        while idx < len(tokens) and tokens[idx] == "&":
            idx += 1
            children.append(parse_atom())
        if len(children) == 1:
            return children[0]
        return {"op": "and", "children": children}

    def parse_or():
        nonlocal idx
        node = parse_and()
        children = [node]
        while idx < len(tokens) and tokens[idx] == "|":
            idx += 1
            children.append(parse_and())
        if len(children) == 1:
            return children[0]
        return {"op": "or", "children": children}

    tree = parse_or()
    if idx != len(tokens):
        raise ValueError("条件表达式存在未解析尾部")
    return tree


def _normalize_alarm_condition(condition: Any):
    if condition is None:
        return None, None
    if isinstance(condition, str):
        raw_text = condition.strip()
        if raw_text.startswith("{"):
            try:
                decoded = json.loads(raw_text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                condition = decoded
    if isinstance(condition, str):
        condition = {"expr": condition}
    if not isinstance(condition, dict):
        return None, "condition 必须是 JSON object 或表达式字符串"

    if "expr" in condition:
        expr = _normalize_alarm_expr_text(condition.get("expr", ""))
        if not expr:
            return None, "condition.expr 不能为空"
        tokens, token_error = _tokenize_alarm_expr(expr)
        if token_error:
            return None, token_error
        try:
            tree = _parse_alarm_expr(tokens)
        except ValueError as exc:
            return None, str(exc)
        return {"expr": expr, "tree": tree}, None

    raw_metric = str(condition.get("metric", "")).strip()
    raw_operator = str(condition.get("operator", "")).strip()
    raw_value = condition.get("value")

    metric, metric_error = _normalize_alarm_metric(raw_metric)
    if metric_error:
        return None, metric_error
    operator, operator_error = _normalize_alarm_operator(raw_operator)
    if operator_error:
        return None, operator_error
    try:
        numeric_value = float(raw_value)
    except (TypeError, ValueError):
        return None, "condition.value 必须是数值"

    return {
        "metric": metric,
        "operator": operator,
        "value": numeric_value,
    }, None


def set_alarm(
    value: float,
    unit: Literal["min", "h", "d"],
    prompt: str,
    explanation: str,
    condition: Any = None,
):
    """创建闹钟并写入 clock.json。时间必填；condition 选填，命中则在截止时间前提前触发。"""
    normalized_condition, condition_error = _normalize_alarm_condition(condition)
    tool_args = {"value": value, "unit": unit, "prompt": prompt, "explanation": explanation}
    if condition is not None:
        tool_args["condition"] = condition
    explanation_error = _require_action_explanation("set_alarm", explanation, tool_args)
    if explanation_error is not None:
        _record_tool_call("set_alarm", tool_args, explanation_error)
        return explanation_error
    duplicate_guard = _guard_duplicate_followup_housekeeping("set_alarm", tool_args)
    if duplicate_guard:
        _record_tool_call("set_alarm", tool_args, duplicate_guard)
        return duplicate_guard
    retry_duplicate_warning = _detect_retry_duplicate_housekeeping_warning("set_alarm", tool_args)

    now = datetime.now()
    delta = None
    if unit == "min":
        delta = timedelta(minutes=value)
    elif unit == "h":
        delta = timedelta(hours=value)
    elif unit == "d":
        delta = timedelta(days=value)
    if delta is None:
        res = _structured_tool_error(
            "set_alarm",
            error_class="alarm_unit_invalid",
            why_rejected="unit 只允许 min / h / d。",
            model_fix_hint="把 unit 改成 min、h 或 d，并保持 value 为数值。",
            invalid_args=tool_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_alarm", tool_args, res)
        return res
    if condition_error:
        res = _structured_tool_error(
            "set_alarm",
            error_class="alarm_condition_invalid",
            why_rejected=condition_error,
            model_fix_hint="condition 必须是 JSON object。单条件用 metric/operator/value；复合条件用 {\"expr\":\"price>123&RSI<45\"}。",
            invalid_args=tool_args,
            retryable=True,
            defer_until_next_turn=False,
        )
        _record_tool_call("set_alarm", tool_args, res)
        return res

    trigger_time = now + delta
    alarm_id = f"ALARM_{trigger_time.strftime('%Y%m%d%H%M%S')}"
    alarm = {
        "id": alarm_id,
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "trigger_time": trigger_time.strftime("%Y-%m-%d %H:%M:%S"),
        "prompt": prompt,
        "status": "pending",
    }
    if normalized_condition is not None:
        alarm["condition"] = normalized_condition

    lock = FileLock(f"{CLOCK_JSON_PATH}.lock", timeout=10)
    with lock:
        alarms: List[Dict[str, Any]] = []
        if os.path.exists(CLOCK_JSON_PATH):
            with open(CLOCK_JSON_PATH, "r", encoding="utf-8") as f:
                raw = f.read().strip()
                if raw:
                    alarms = json.loads(raw)
        alarms.append(alarm)
        with open(CLOCK_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(alarms, f, indent=2, ensure_ascii=False)

    res = {
        "status": "success",
        "id": alarm_id,
        "created_at": alarm["created_at"],
        "trigger_time": alarm["trigger_time"],
        "mode": "conditional" if normalized_condition is not None else "time_only",
    }
    if normalized_condition is not None:
        res["condition"] = normalized_condition
    if retry_duplicate_warning:
        res["_retry_warning"] = retry_duplicate_warning
    _record_tool_call("set_alarm", tool_args, res)
    return res


def delete_alarm(alarm_id: str, explanation: str):
    """按 alarm_id 删除闹钟。"""
    tool_args = {"alarm_id": alarm_id, "explanation": explanation}
    explanation_error = _require_action_explanation("delete_alarm", explanation, tool_args)
    if explanation_error is not None:
        _record_tool_call("delete_alarm", tool_args, explanation_error)
        return explanation_error
    duplicate_guard = _guard_duplicate_followup_housekeeping("delete_alarm", tool_args)
    if duplicate_guard:
        _record_tool_call("delete_alarm", tool_args, duplicate_guard)
        return duplicate_guard
    retry_duplicate_warning = _detect_retry_duplicate_housekeeping_warning("delete_alarm", tool_args)
    if not os.path.exists(CLOCK_JSON_PATH):
        res = _structured_tool_error(
            "delete_alarm",
            error_class="alarm_not_found",
            why_rejected="clock.json 不存在，当前没有可删除闹钟。",
            model_fix_hint="不要继续删除；改为基于当前 Pending Alarms 状态重写计划，必要时设置新的 set_alarm。",
            invalid_args=tool_args,
            retryable=False,
            defer_until_next_turn=False,
        )
        _record_tool_call("delete_alarm", tool_args, res)
        return res

    lock = FileLock(f"{CLOCK_JSON_PATH}.lock", timeout=10)
    with lock:
        with open(CLOCK_JSON_PATH, "r", encoding="utf-8") as f:
            alarms = json.load(f)
        alarms = [a for a in alarms if a.get("id") != alarm_id]
        with open(CLOCK_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(alarms, f, indent=2, ensure_ascii=False)

    res = {"status": "success", "message": f"闹钟 {alarm_id} 已删除"}
    if retry_duplicate_warning:
        res["_retry_warning"] = retry_duplicate_warning
    _record_tool_call("delete_alarm", tool_args, res)
    return res


TOOL_FUNCTIONS = [
    get_spot_balance,
    get_usdt_futures_position,
    get_usdt_futures_account,
    get_usdt_futures_max_open_position,
    get_range_plan,
    transfer_to_usdt_futures,
    set_usdt_futures_leverage,
    set_usdt_futures_margin_type,
    get_usdt_futures_open_orders,
    cancel_usdt_futures_order,
    cancel_all_usdt_futures_orders,
    trade_usdt_futures,
    modify_usdt_futures_order,
    get_usdt_futures_order,
    close_usdt_futures_position,
    set_range_plan,
    cancel_range_plan,
    calculate_expression,
    set_alarm,
    delete_alarm,
]


TOOL_DESCRIPTION_ADDENDA = {
    "get_spot_balance": (
        "Tool purpose:\n"
        "- Read-only spot balance snapshot. Use only when spot funding context matters; USD(S)-M futures capacity still comes from futures tools.\n"
        "- Optional explanation: pass a short reason for the query so the audit trail can distinguish evidence gathering from action."
    ),
    "get_usdt_futures_position": (
        "Tool purpose:\n"
        "- Read-only live USD(S)-M position authority: side, size, entry, mark, liquidation and leverage/margin metadata.\n"
        "- Optional explanation: pass why you need the position snapshot, especially before close/protection/attribution.\n"
        "Hard guard contract:\n"
        "- Related decision guard: precheck_gate_violation. Before any new opening trade, this tool must be used together with "
        "get_usdt_futures_account and get_usdt_futures_max_open_position in the same wakeup flow.\n"
        "- Do not assume flat/long/short from memory. Use this tool as the live position authority before close/reduce/protection logic.\n"
        "- If the prompt already injected [PRECHECK RESULTS] or [REFRESHED PRECHECK RESULTS], do not repeat the fixed precheck trio in execute."
    ),
    "get_usdt_futures_account": (
        "Tool purpose:\n"
        "- Read-only live USD(S)-M margin account snapshot. Use it for futures margin assets, not spot intuition.\n"
        "- Optional explanation: pass why the account snapshot is needed for the current decision.\n"
        "Hard guard contract:\n"
        "- Related decision guard: precheck_gate_violation. Use together with get_usdt_futures_position and get_usdt_futures_max_open_position before any new opening trade.\n"
        "- This is the live margin-account authority for capacity reasoning. Do not replace it with spot-balance intuition.\n"
        "- If refreshed precheck is already injected, reuse that snapshot instead of repeating the fixed trio in execute."
    ),
    "get_usdt_futures_max_open_position": (
        "Tool purpose:\n"
        "- Read-only futures capacity estimate: max base-asset quantity available now and potential max if flat.\n"
        "- Optional explanation: pass the capacity question being answered.\n"
        "Hard guard contract:\n"
        "- Related decision guard: precheck_gate_violation. Use together with get_usdt_futures_position and get_usdt_futures_account before any new opening trade.\n"
        "- quantity means base-asset quantity, not integer contract count. New entries must keep a safety buffer and must not exceed max_quantity.\n"
        "- After any successful capacity-changing action (transfer / leverage / margin-type / cancel / close), opening must wait for refreshed precheck; do not keep using stale max_quantity."
    ),
    "get_range_plan": (
        "Tool purpose:\n"
        "- Read-only local range automation state, including active/inactive status, armed levels, expiry and breakout watch.\n"
        "- Optional explanation: pass why range ownership matters for this decision.\n"
        "Guard-aware usage:\n"
        "- Use this as the live authority for sideways range automation state.\n"
        "- If an active range plan exists, do not assume ordinary discretionary MARKET/LIMIT entry flow still owns the symbol.\n"
        "- Read this before replacing, canceling, or auditing a range automation session."
    ),
    "transfer_to_usdt_futures": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty and state why the transfer is needed now.\n"
        "- This is a capacity-changing action. After it succeeds, do not open a new position in the same batch until refreshed precheck is injected.\n"
        "- If the entry depends on transferred funds arriving, do not parallel this tool with trade_usdt_futures."
    ),
    "set_usdt_futures_leverage": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- This is a capacity-changing action. After it succeeds, do not open a new position in the same batch until refreshed precheck is injected.\n"
        "- Use only when leverage change is part of a specific position-management plan, not as a decorative action."
    ),
    "set_usdt_futures_margin_type": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- This is a capacity-changing action. After it succeeds, do not open a new position in the same batch until refreshed precheck is injected.\n"
        "- User preference is normally CROSSED. If switching to ISOLATED, your explanation must justify the deviation."
    ),
    "get_usdt_futures_open_orders": (
        "Tool purpose:\n"
        "- Read-only live Binance open-order snapshot. This is the authority for actual exchange orders, not local armed range levels.\n"
        "- Optional explanation: pass whether you are checking protection, cleanup, fill attribution or stale orders.\n"
        "Guard-aware usage:\n"
        "- Use this as the live open-order authority before cancel/replace/protection cleanup.\n"
        "- When protection coverage matters, reconcile this output with get_usdt_futures_position before canceling orders.\n"
        "- Do not infer open orders from memory snapshots."
    ),
    "cancel_usdt_futures_order": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- Related runtime guard: invalid_order_id. order_id must be the Binance numeric order id as a digit-only string.\n"
        "- Related runtime guard: last_verified_protection_guard. Do not cancel the last verified protective order covering a live position unless replacement protection is already verified or the position is being closed/reduced first.\n"
        "- Related wakeup guard: duplicate_followup_action. Do not repeat the same successful cancel in execute_followup or retry with identical target/args.\n"
        "- If Binance returns -2011/order_not_found, treat the order as already gone and move on to state refresh."
    ),
    "cancel_all_usdt_futures_orders": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- Related runtime guard: live_protection_cancel_all_blocked. Do not use cancel_all if a live position still depends on current protective coverage.\n"
        "- Related runtime guard: range_plan_active_conflict. If a range plan owns the symbol, stop it with cancel_range_plan instead of cancel_all.\n"
        "- This is a capacity-changing action. After it succeeds, do not open a new position in the same batch until refreshed precheck is injected."
    ),
    "trade_usdt_futures": (
        "Hard guard contract:\n"
        "- Related runtime guards for opening flow: opening_sequence_already_consumed, opening_retry_exhausted, capacity_refresh_required.\n"
        "- Related runtime guard: range_plan_active_conflict. Discretionary MARKET/LIMIT entries are blocked while an active range plan owns the symbol.\n"
        "- Same wakeup normally allows only one successful MARKET/LIMIT opening sequence per symbol. If one opening already succeeded, do not open again in the same wakeup.\n"
        "- Opening retry budget is at most one conservative repair retry. Attempt 2 may only fix parameter shape, shrink quantity after refreshed state, or downgrade to a smaller probe. Never use retry to increase risk.\n"
        "- Before any new opening MARKET/LIMIT trade, the precheck trio must already exist in the same wakeup flow: get_usdt_futures_position + get_usdt_futures_account + get_usdt_futures_max_open_position.\n"
        "- After any successful capacity-changing action (transfer / leverage / margin-type / cancel / close), opening must wait for refreshed precheck.\n"
        "- Related runtime guards for parameters: unsupported_order_type, invalid_side, invalid_position_side, optional_params_bad_combo, position_side_not_match, reduce_only_rejected, reduce_only_order_type_not_supported, exchange_rejected.\n"
        "- order_type only supports LIMIT, MARKET, STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET.\n"
        "- side only BUY or SELL. position_side only LONG / SHORT / BOTH.\n"
        "- Related runtime guards for protective orders: protective_position_side_required, close_position_order_type_invalid, close_position_with_quantity, close_position_with_reduce_only, protective_quantity_missing, protective_quantity_exceeds_position, hedge_mode_reduce_only_not_supported, protective_semantic_mismatch, limit_price_missing, trigger_price_missing, stop_price_missing, trailing_params_missing.\n"
        "- Protective LONG must be SELL + position_side=LONG. Protective SHORT must be BUY + position_side=SHORT. In hedge mode, never use BOTH for protective orders.\n"
        "- close_position=true is only legal for STOP_MARKET or TAKE_PROFIT_MARKET full-close protection. Do not combine close_position=true with quantity or reduce_only=true.\n"
        "- TRAILING_STOP_MARKET requires callback_rate and quantity. This production tool does not support close_position=true for trailing stops.\n"
        "- Related entry-quality guards: long_entry_quality_violation and short_entry_quality_violation.\n"
        "- Do not market-long / near-market long into the first shallow bounce of unrepaired 1h weakness or into a late stretched reclaim. Do not market-short / near-market short into deep oversold breakdown extension.\n"
        "- explanation must be non-empty and should state the setup, risk reason, and why this is legal now."
    ),
    "modify_usdt_futures_order": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- This tool is only for modifying live standard LIMIT orders. Do not use it for STOP/TAKE_PROFIT/conditional protective orders.\n"
        "- Do not treat modify as a substitute for illegal cancel-and-reopen sequences inside the same wakeup when capacity state is stale."
    ),
    "get_usdt_futures_order": (
        "Tool purpose:\n"
        "- Read-only single-order verification. Use it to verify a numeric Binance order id before claiming fill/cancel status.\n"
        "- Optional explanation: pass the exact status question you are verifying.\n"
        "Guard-aware usage:\n"
        "- order_id must be the Binance numeric order id as a digit-only string.\n"
        "- Use this tool to verify fill/cancel state before describing an order as actually executed or gone.\n"
        "- If the response indicates order_not_found/-2011, treat the order as already gone and reconcile with live position/open-order state instead of blind retry."
    ),
    "close_usdt_futures_position": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- This is a capacity-changing action. After it succeeds, do not open a new position in the same batch until refreshed precheck is injected.\n"
        "- Use this tool for real reduce/close when risk compression or hypothesis invalidation requires action. Do not describe a close as completed unless live position state is later verified."
    ),
    "set_range_plan": (
        "Hard guard contract:\n"
        "- explanation must be non-empty and should state why the market is sideways enough to delegate execution to the range engine now.\n"
        "- This tool is only for sideways automation. It requires a flat symbol and a clean open-order book for that symbol.\n"
        "- The execution layer supports both Hedge Mode and One-way Mode. In One-way Mode it will use position_side=BOTH and manage one net position at a time.\n"
        "- The current production style is trigger-based range execution: you define bounds and inside-band levels, then the local range executor watches live price and fires the entry/exit lifecycle for you. Do not redesign levels just to satisfy Post-Only microstructure.\n"
        "- Breakout handling is two-stage, not instant exit: the first hard-bound breach enters breakout_watch, pauses fresh entries, and waits for re-entry or confirmation timeout / extended-breakout expansion before fully stopping the plan.\n"
        "- All long/short entry and exit prices must stay strictly inside the breakout bounds. long entry must be below its exit; short entry must be above its exit.\n"
        "- Use this instead of manually spraying many LIMIT orders when you want execution-layer range automation to own the full lifecycle."
    ),
    "cancel_range_plan": (
        "Hard guard contract:\n"
        "- explanation must be non-empty and should state why the sideways automation is being stopped now.\n"
        "- Use this to stop the managed range lattice cleanly. Do not manually cancel tracked range orders one by one.\n"
        "- close_positions=true is allowed when you want to flatten residual range exposure while stopping the plan."
    ),
    "calculate_expression": (
        "Tool purpose:\n"
        "- Read-only arithmetic helper for explicit price, quantity, ratio or risk math.\n"
        "- Optional explanation: pass what calculation is being audited.\n"
        "Guard-aware usage:\n"
        "- Use for explicit quantity / risk / ratio math instead of mental arithmetic.\n"
        "- Allowed input is restricted arithmetic only; do not pass natural-language text."
    ),
    "set_alarm": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- Related decision guards: missing_structured_wait_alarm and historical_alarm_confused_as_live.\n"
        "- If your reasoning writes a concrete future wait condition (price / RSI / MACD threshold, reclaim, retest, confirmation), you are expected to call this tool in the same wakeup unless you explicitly cite a real live alarm id from Pending Alarms.\n"
        "- Prefer condition for threshold-based waits. condition must be a standard JSON object or expression shape accepted by the tool.\n"
        "- Do not repeat an identical successful set_alarm in execute_followup or retry with the same args; duplicate housekeeping is blocked/warned."
    ),
    "delete_alarm": (
        "Hard guard contract:\n"
        "- Related runtime guard: missing_action_explanation. explanation must be non-empty.\n"
        "- Use only for real cleanup of obsolete future alarms. Do not delete a live alarm unless your explanation states what replaced it or why the wait is no longer needed.\n"
        "- Do not repeat an identical successful delete_alarm in execute_followup or retry with the same args; duplicate housekeeping is blocked/warned."
    ),
}


def _build_langchain_tool(fn):
    desc = (fn.__doc__ or "").strip()
    if not desc:
        raise ValueError(f"Tool function {fn.__name__} is missing a docstring description.")
    addendum = TOOL_DESCRIPTION_ADDENDA.get(fn.__name__)
    if addendum:
        desc = f"{desc}\n\n{addendum}"
    return langchain_tool(fn, description=desc)


LANGCHAIN_TOOLS = [_build_langchain_tool(fn) for fn in TOOL_FUNCTIONS]
LANGCHAIN_TOOLS_BY_NAME = {tool_obj.name: tool_obj for tool_obj in LANGCHAIN_TOOLS}


def get_langchain_tools(tool_names: Optional[List[str]] = None):
    """Return LangChain tool objects (all by default)."""
    if tool_names is None:
        return LANGCHAIN_TOOLS

    missing = [name for name in tool_names if name not in LANGCHAIN_TOOLS_BY_NAME]
    if missing:
        raise ValueError(f"Unknown tool names requested: {missing}")
    return [LANGCHAIN_TOOLS_BY_NAME[name] for name in tool_names]


if __name__ == "__main__":
    print("execution/main.py 已迁移为 LangChain 同进程工具模块，不再提供 FastMCP 服务入口。")
