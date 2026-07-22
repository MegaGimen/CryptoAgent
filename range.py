from typing import Any, Dict, List, Tuple

TRIGGER_MARKET_EXECUTION_STYLE = "trigger_market_v1"
LEGACY_MAKER_GRID_EXECUTION_STYLE = "maker_grid_legacy"


def range_plan_execution_style(plan: Dict[str, Any]) -> str:
    raw = str((plan or {}).get("execution_style", "") or "").strip().lower()
    if raw == TRIGGER_MARKET_EXECUTION_STYLE:
        return TRIGGER_MARKET_EXECUTION_STYLE
    return LEGACY_MAKER_GRID_EXECUTION_STYLE


def _entry_reset_condition(bucket: str, mark_price: float, entry_price: float) -> bool:
    if bucket == "long":
        return mark_price > entry_price
    return mark_price < entry_price


def _entry_trigger_condition(bucket: str, mark_price: float, entry_price: float) -> bool:
    if bucket == "long":
        return mark_price <= entry_price
    return mark_price >= entry_price


def _exit_trigger_condition(bucket: str, mark_price: float, exit_price: float) -> bool:
    if bucket == "long":
        return mark_price >= exit_price
    return mark_price <= exit_price


def _side_key(bucket: str) -> str:
    return "LONG" if bucket == "long" else "SHORT"


def _ensure_trigger_level_runtime(level: Dict[str, Any], bucket: str, current_price: float) -> bool:
    changed = False
    if not isinstance(level, dict):
        return False

    if not str(level.get("level_id", "") or "").strip():
        level["level_id"] = f"{bucket}_unknown"
        changed = True

    level.setdefault("quantity", 0.0)
    level.setdefault("filled_qty", 0.0)
    level.setdefault("completed_cycles", 0)
    level.setdefault("entry_order_id", "")
    level.setdefault("exit_order_id", "")
    level.setdefault("last_entry_at", "")
    level.setdefault("last_exit_at", "")
    level.setdefault("last_entry_price", 0.0)
    level.setdefault("last_exit_price", 0.0)

    entry_status = str(level.get("entry_status", "") or "").strip().lower()
    exit_status = str(level.get("exit_status", "") or "").strip().lower()

    if entry_status not in {"armed", "waiting_reset", "filled"}:
        entry_status = "armed" if _entry_reset_condition(bucket, current_price, float(level.get("entry_price", 0.0) or 0.0)) else "waiting_reset"
        level["entry_status"] = entry_status
        changed = True

    if entry_status == "filled":
        if exit_status != "armed":
            level["exit_status"] = "armed"
            changed = True
    else:
        if exit_status not in {"idle", ""}:
            level["exit_status"] = "idle"
            changed = True

    return changed


def prepare_trigger_plan_runtime(plan: Dict[str, Any], current_price: float) -> bool:
    changed = False
    for bucket_key, bucket_name in (("long_levels", "long"), ("short_levels", "short")):
        levels = plan.get(bucket_key, [])
        if not isinstance(levels, list):
            continue
        for level in levels:
            changed = _ensure_trigger_level_runtime(level, bucket_name, current_price) or changed
            if not isinstance(level, dict):
                continue
            if str(level.get("entry_status", "") or "").strip().lower() == "waiting_reset":
                if _entry_reset_condition(bucket_name, current_price, float(level.get("entry_price", 0.0) or 0.0)):
                    level["entry_status"] = "armed"
                    changed = True
    return changed


def reset_trigger_level(level: Dict[str, Any], bucket: str, current_price: float) -> None:
    entry_price = float(level.get("entry_price", 0.0) or 0.0)
    level["entry_status"] = "armed" if _entry_reset_condition(bucket, current_price, entry_price) else "waiting_reset"
    level["exit_status"] = "idle"
    level["entry_order_id"] = ""
    level["exit_order_id"] = ""
    level["filled_qty"] = 0.0
    level["completed_cycles"] = int(level.get("completed_cycles", 0) or 0) + 1


def collect_trigger_actions(
    plan: Dict[str, Any],
    current_price: float,
    live_positions: Dict[str, float],
    position_mode: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    changed = False
    actions: List[Dict[str, Any]] = []

    for bucket_key, bucket_name in (("long_levels", "long"), ("short_levels", "short")):
        levels = plan.get(bucket_key, [])
        if not isinstance(levels, list):
            continue
        for level in levels:
            if not isinstance(level, dict):
                continue
            changed = _ensure_trigger_level_runtime(level, bucket_name, current_price) or changed
            entry_status = str(level.get("entry_status", "") or "").strip().lower()
            side_key = _side_key(bucket_name)
            current_side_qty = float(live_positions.get(side_key, 0.0) or 0.0)

            if entry_status == "waiting_reset":
                if _entry_reset_condition(bucket_name, current_price, float(level.get("entry_price", 0.0) or 0.0)):
                    level["entry_status"] = "armed"
                    changed = True
                continue

            if entry_status == "filled":
                filled_qty = float(level.get("filled_qty", 0.0) or 0.0)
                if current_side_qty <= 0 or filled_qty <= 0:
                    actions.append({
                        "action": "reset_level",
                        "bucket": bucket_name,
                        "level_id": str(level.get("level_id", "") or ""),
                    })
                    continue
                if _exit_trigger_condition(bucket_name, current_price, float(level.get("exit_price", 0.0) or 0.0)):
                    actions.append({
                        "action": "exit_market",
                        "bucket": bucket_name,
                        "level_id": str(level.get("level_id", "") or ""),
                        "quantity": min(filled_qty, current_side_qty),
                    })
                continue

            if entry_status != "armed":
                continue

            if position_mode == "one_way":
                opposing_key = "SHORT" if side_key == "LONG" else "LONG"
                if float(live_positions.get(opposing_key, 0.0) or 0.0) > 0:
                    continue

            if _entry_trigger_condition(bucket_name, current_price, float(level.get("entry_price", 0.0) or 0.0)):
                actions.append({
                    "action": "entry_market",
                    "bucket": bucket_name,
                    "level_id": str(level.get("level_id", "") or ""),
                    "quantity": float(level.get("quantity", 0.0) or 0.0),
                })

    priority = {"reset_level": 0, "exit_market": 1, "entry_market": 2}
    actions.sort(key=lambda item: (priority.get(str(item.get("action", "")), 9), str(item.get("level_id", ""))))
    return actions, changed
