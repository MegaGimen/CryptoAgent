from env_config import load_project_env
load_project_env()

import difflib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from binance.client import Client
from plotly.subplots import make_subplots
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

# --- CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYMBOL = "ETHUSDT"
INTERVAL = Client.KLINE_INTERVAL_15MINUTE
LOG_DIR = os.path.join(BASE_DIR, "logs")
CST_OFFSET = timedelta(hours=8)
UTC_TZ = timezone.utc

def get_klines(symbol, interval, start_time, end_time):
    """Fetch K-line data from Binance for a specific range."""
    print(f"Fetching candles for {symbol} from {start_time} to {end_time}...")
    start_ts = int(start_time.replace(tzinfo=UTC_TZ).timestamp() * 1000)
    end_ts = int(end_time.replace(tzinfo=UTC_TZ).timestamp() * 1000)
    interval_ms_map = {
        Client.KLINE_INTERVAL_15MINUTE: 15 * 60 * 1000,
        Client.KLINE_INTERVAL_1HOUR: 60 * 60 * 1000,
        Client.KLINE_INTERVAL_4HOUR: 4 * 60 * 60 * 1000,
        Client.KLINE_INTERVAL_1DAY: 24 * 60 * 60 * 1000,
    }
    step_ms = interval_ms_map.get(interval, 15 * 60 * 1000)

    all_klines = []
    cursor = start_ts
    while cursor < end_ts:
        batch = client.get_klines(
            symbol=symbol,
            interval=interval,
            startTime=cursor,
            endTime=end_ts,
            limit=1000,
        )
        if not batch:
            break
        all_klines.extend(batch)
        last_open_ts = int(batch[-1][0])
        next_cursor = last_open_ts + step_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1000:
            break

    if not all_klines:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    deduped = {}
    for row in all_klines:
        deduped[int(row[0])] = row
    klines = [deduped[k] for k in sorted(deduped.keys())]

    df = pd.DataFrame(
        klines,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_asset_volume",
            "number_of_trades",
            "taker_buy_base_asset_volume",
            "taker_buy_quote_asset_volume",
            "ignore",
        ],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df

def extract_user_prompt_block(input_md: str) -> str:
    if not input_md:
        return ""
    marker = "--- USER PROMPT ---"
    if marker not in input_md:
        return input_md
    return input_md.split(marker, 1)[1]

def shorten_blob(text, limit=280):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

def parse_jsonish(value):
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except Exception:
            return {}
    return {}

def load_context_json(folder_path: str, filename: str):
    path = os.path.join(folder_path, "context", filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None

def load_structured_long_context(folder_path: str) -> Dict[str, Any]:
    raw_inputs = load_context_json(folder_path, "raw_long_inputs.json")
    kept_inputs = load_context_json(folder_path, "kept_long_inputs.json")
    dropped_inputs = load_context_json(folder_path, "dropped_long_inputs.json")
    long_view = load_context_json(folder_path, "long_horizon_view.json")
    long_reflection = load_context_json(folder_path, "long_reflection.json")

    raw_count = 0
    if isinstance(raw_inputs, dict):
        raw_count += len(raw_inputs.get("news_items", [])) if isinstance(raw_inputs.get("news_items", []), list) else 0
        raw_count += len(raw_inputs.get("polymarket_items", [])) if isinstance(raw_inputs.get("polymarket_items", []), list) else 0

    return {
        "long_horizon_view": long_view if isinstance(long_view, dict) else None,
        "long_inputs_raw": raw_inputs if isinstance(raw_inputs, dict) else None,
        "long_inputs_kept": kept_inputs if isinstance(kept_inputs, dict) else None,
        "long_inputs_dropped": dropped_inputs if isinstance(dropped_inputs, dict) else None,
        "long_reflection": long_reflection if isinstance(long_reflection, dict) else None,
        "long_gating_stats": {
            "raw_count": raw_count,
            "kept_count": len(kept_inputs.get("items", [])) if isinstance(kept_inputs, dict) and isinstance(kept_inputs.get("items", []), list) else 0,
            "dropped_count": len(dropped_inputs.get("items", [])) if isinstance(dropped_inputs, dict) and isinstance(dropped_inputs.get("items", []), list) else 0,
            "has_structured_context": any(isinstance(item, dict) for item in [raw_inputs, kept_inputs, dropped_inputs, long_view, long_reflection]),
        },
    }

def annotate_tool_calls_with_trace(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    annotated = []
    for call in tool_calls:
        item = dict(call)
        item["stage"] = str(item.get("stage", "") or "unknown")
        item["stage_source"] = "local_fallback"
        annotated.append(item)
    return annotated

def extract_event_type_and_list(content_md: str, fallback_type: str = "unknown"):
    event_type_str = (fallback_type or "unknown").strip() or "unknown"
    event_type_list = []

    if content_md:
        event_markers = re.findall(r"Event\s+\d+\s+\((.*?)\):", content_md, flags=re.IGNORECASE)
        if event_markers:
            event_type_list = [x.strip() for x in event_markers if x.strip()]
            event_type_str = " & ".join(event_type_list)
        else:
            type_match = re.search(r"^Type:\s*(.*?)\s*$", content_md, re.MULTILINE)
            if type_match:
                event_type_str = type_match.group(1).strip() or event_type_str
            else:
                event_type_line = re.search(r"^Event Type:\s*(.*?)\s*$", content_md, re.MULTILINE)
                if event_type_line:
                    event_type_str = event_type_line.group(1).strip() or event_type_str

    if not event_type_list:
        event_type_list = [x.strip() for x in event_type_str.split("&")]

    deduped = []
    for item in event_type_list:
        normalized = re.sub(r"\s+", " ", str(item).strip().lower())
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return event_type_str, (deduped or ["unknown"])

def get_all_logs_range(log_dir):
    """Scan logs directory to find the total time range."""
    if not os.path.exists(log_dir):
        return None, None, None, None

    folders = [
        f
        for f in os.listdir(log_dir)
        if os.path.isdir(os.path.join(log_dir, f)) and re.match(r"^\d{8}_\d{6}$", f)
    ]
    if not folders:
        return None, None, None, None

    folders.sort()
    try:
        start_local = datetime.strptime(folders[0], "%Y%m%d_%H%M%S")
        end_local = datetime.strptime(folders[-1], "%Y%m%d_%H%M%S")
        start_utc = start_local - CST_OFFSET
        end_utc = end_local - CST_OFFSET
        return (
            start_utc,
            end_utc,
            folders[0],
            folders[-1],
        )
    except Exception as e:
        print(f"Error parsing log range: {e}")
        return None, None, None, None

def normalize_memory_lines(text):
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]

def normalize_memory_snapshot(value):
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        if stripped[0] in "{[":
            parsed = parse_jsonish(stripped)
            if isinstance(parsed, (dict, list)):
                return parsed
        return stripped
    return str(value)

def pretty_memory_text(snapshot, fallback=""):
    normalized = normalize_memory_snapshot(snapshot)
    if isinstance(normalized, (dict, list)):
        return json.dumps(normalized, ensure_ascii=False, indent=2)
    return str(normalized or fallback or "")

def shorten_line(text, limit=140):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

def summarize_memory_diff(previous, current):
    if previous is None:
        return {"kind": "initial", "added": [], "removed": [], "changed": []}
    previous = normalize_memory_snapshot(previous)
    current = normalize_memory_snapshot(current)
    if isinstance(previous, (dict, list)) or isinstance(current, (dict, list)):
        if json.dumps(previous, ensure_ascii=False, sort_keys=True) == json.dumps(current, ensure_ascii=False, sort_keys=True):
            return {"kind": "unchanged", "added": [], "removed": [], "changed": []}

        if isinstance(previous, dict) and isinstance(current, dict):
            added = [f"{key} added" for key in current.keys() - previous.keys()]
            removed = [f"{key} removed" for key in previous.keys() - current.keys()]
            changed = []
            for key in current.keys() & previous.keys():
                prev_val = previous.get(key)
                curr_val = current.get(key)
                if json.dumps(prev_val, ensure_ascii=False, sort_keys=True) == json.dumps(curr_val, ensure_ascii=False, sort_keys=True):
                    continue
                if isinstance(prev_val, list) and isinstance(curr_val, list):
                    changed.append(f"{key} changed ({len(prev_val)} -> {len(curr_val)} items)")
                elif isinstance(prev_val, dict) and isinstance(curr_val, dict):
                    changed.append(f"{key} changed")
                else:
                    changed.append(f"{key}: {shorten_line(prev_val, 60)} -> {shorten_line(curr_val, 60)}")
            return {
                "kind": "changed",
                "added": [shorten_line(item) for item in added[:5]],
                "removed": [shorten_line(item) for item in removed[:5]],
                "changed": [shorten_line(item) for item in changed[:5]],
            }

        return {
            "kind": "changed",
            "added": [],
            "removed": [],
            "changed": ["memory snapshot replaced"],
        }

    if str(previous or "") == str(current or ""):
        return {"kind": "unchanged", "added": [], "removed": [], "changed": []}

    prev_lines = normalize_memory_lines(previous)
    curr_lines = normalize_memory_lines(current)
    matcher = difflib.SequenceMatcher(a=prev_lines, b=curr_lines)
    added = []
    removed = []
    changed = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added.extend(curr_lines[j1:j2])
        elif tag == "delete":
            removed.extend(prev_lines[i1:i2])
        elif tag == "replace":
            before = prev_lines[i1:i2]
            after = curr_lines[j1:j2]
            pair_count = min(len(before), len(after))
            for idx in range(pair_count):
                changed.append(f"{shorten_line(before[idx], 90)} -> {shorten_line(after[idx], 90)}")
            if len(before) > pair_count:
                removed.extend(before[pair_count:])
            if len(after) > pair_count:
                added.extend(after[pair_count:])

    return {
        "kind": "changed",
        "added": [shorten_line(item) for item in added[:5]],
        "removed": [shorten_line(item) for item in removed[:5]],
        "changed": [shorten_line(item) for item in changed[:5]],
    }

def render_memory_ops(handle, label, ops):
    ops = list(ops or [])
    if not ops:
        return
    handle.write(f"    [Memory Ops - {label}]:\n")
    for item in ops[:8]:
        handle.write(f"      - {item.get('op', '')} {item.get('path', '')}\n")

def render_memory_diff(handle, label, diff_result):
    handle.write(f"    [Memory Diff - {label}]:\n")
    if diff_result["kind"] == "initial":
        handle.write("    - Initial Snapshot\n")
        return
    if diff_result["kind"] == "unchanged":
        handle.write("    - No changes\n")
        return
    if diff_result["added"]:
        handle.write("    - Added:\n")
        for item in diff_result["added"]:
            handle.write(f"      - {item}\n")
    if diff_result["removed"]:
        handle.write("    - Removed:\n")
        for item in diff_result["removed"]:
            handle.write(f"      - {item}\n")
    if diff_result["changed"]:
        handle.write("    - Changed:\n")
        for item in diff_result["changed"]:
            handle.write(f"      - {item}\n")
    if not diff_result["added"] and not diff_result["removed"] and not diff_result["changed"]:
        handle.write("    - No changes\n")

def render_long_context(handle, op: Dict[str, Any], previous_long_view: Any) -> Any:
    stats = op.get("long_gating_stats", {})
    if not isinstance(stats, dict) or not stats.get("has_structured_context"):
        handle.write("    [Long Horizon Context]: legacy log / no structured long context\n")
        return previous_long_view

    long_view = op.get("long_horizon_view") if isinstance(op.get("long_horizon_view"), dict) else {}
    long_reflection = op.get("long_reflection") if isinstance(op.get("long_reflection"), dict) else {}
    daily_review = op.get("daily_review") if isinstance(op.get("daily_review"), dict) else {}
    kept_inputs = op.get("long_inputs_kept") if isinstance(op.get("long_inputs_kept"), dict) else {}
    dropped_inputs = op.get("long_inputs_dropped") if isinstance(op.get("long_inputs_dropped"), dict) else {}
    kept_items = kept_inputs.get("items", []) if isinstance(kept_inputs.get("items", []), list) else []
    dropped_items = dropped_inputs.get("items", []) if isinstance(dropped_inputs.get("items", []), list) else []
    revision = long_view.get("revision_log", {}) if isinstance(long_view.get("revision_log", {}), dict) else {}
    classification_audit = long_view.get("classification_audit", {}) if isinstance(long_view.get("classification_audit", {}), dict) else {}
    long_meta = long_view.get("meta", {}) if isinstance(long_view.get("meta", {}), dict) else {}

    handle.write("    [Long Horizon Context]:\n")
    handle.write(
        "      - regime: "
        f"{long_view.get('regime', 'unknown')} | bias_direction: {long_view.get('bias_direction', 'unknown')} "
        f"| confidence: {long_view.get('confidence', 'unknown')}\n"
    )
    handle.write(
        "      - status: "
        f"{long_meta.get('status', 'unknown')} | counter_long_horizon_bias={op.get('counter_long_horizon_bias', False)} "
        f"| status_at_trade={op.get('long_horizon_status_at_trade', '') or 'n/a'}\n"
    )
    handle.write(f"      - thesis: {shorten_blob(long_view.get('thesis', ''), 220) or 'N/A'}\n")
    handle.write(
        "      - gating: "
        f"raw={stats.get('raw_count', 0)} | kept={stats.get('kept_count', 0)} | dropped={stats.get('dropped_count', 0)}\n"
    )
    handle.write(
        "      - classification: "
        f"{long_view.get('classification_status', 'unknown')} | unclassified={classification_audit.get('unclassified_count', 0)}\n"
    )
    handle.write(
        "      - revision: "
        f"changed={revision.get('changed', False)} | reason={shorten_blob(revision.get('reason', ''), 220) or 'N/A'}\n"
    )

    if previous_long_view is None:
        handle.write("      - delta: initial structured long context\n")
    elif json.dumps(previous_long_view, ensure_ascii=False, sort_keys=True) == json.dumps(long_view, ensure_ascii=False, sort_keys=True):
        handle.write("      - delta: unchanged\n")
    else:
        handle.write(
            "      - delta: changed | reason="
            f"{shorten_blob(revision.get('reason', ''), 220) or 'structured long context changed'}\n"
        )

    if kept_items:
        handle.write("      - kept why:\n")
        for item in kept_items[:3]:
            handle.write(
                f"        - {shorten_blob(item.get('headline_or_question', item.get('source_id', '')), 120)}"
                f" -> {shorten_blob(item.get('why', ''), 180)}\n"
            )
    if dropped_items:
        handle.write("      - dropped why:\n")
        for item in dropped_items[:3]:
            handle.write(
                f"        - {shorten_blob(item.get('headline_or_question', item.get('source_id', '')), 120)}"
                f" -> {shorten_blob(item.get('why', ''), 180)}\n"
            )
    active_hypotheses = long_reflection.get("active_hypotheses", []) if isinstance(long_reflection.get("active_hypotheses", []), list) else []
    invalidated_hypotheses = long_reflection.get("invalidated_hypotheses", []) if isinstance(long_reflection.get("invalidated_hypotheses", []), list) else []
    if active_hypotheses:
        handle.write("      - active hypotheses:\n")
        for item in active_hypotheses[:3]:
            if not isinstance(item, dict):
                continue
            handle.write(
                f"        - {shorten_blob(item.get('title', ''), 100)} -> {shorten_blob(item.get('statement', ''), 160)}\n"
            )
    if invalidated_hypotheses:
        handle.write("      - recently invalidated:\n")
        for item in invalidated_hypotheses[-3:]:
            if not isinstance(item, dict):
                continue
            handle.write(
                f"        - {shorten_blob(item.get('title', ''), 100)} -> {shorten_blob(item.get('invalidation_reason', ''), 160)}\n"
            )
    if daily_review:
        summary = daily_review.get("summary", {}) if isinstance(daily_review.get("summary", {}), dict) else {}
        handle.write(
            "      - daily review: "
            f"{shorten_blob(summary.get('notes', ''), 220) or 'N/A'}\n"
        )
    return long_view

def infer_shortterm_state(shortterm_text):
    normalized = normalize_memory_snapshot(shortterm_text)
    if isinstance(normalized, dict):
        consistency_state = normalized.get("consistency_state", {})
        if isinstance(consistency_state, dict):
            trade_intent = str(consistency_state.get("trade_intent", "")).strip()
            if trade_intent:
                return trade_intent
        day_plan = normalized.get("day_plan", {})
        if isinstance(day_plan, dict):
            day_bias = str(day_plan.get("day_bias", "")).strip()
            if day_bias:
                return day_bias
    shortterm_text = str(shortterm_text or "")
    patterns = [
        r"trade_intent:\s*([^\n]+)",
        r"##\s*状态[:：]\s*([^\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, shortterm_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return "unknown"

def extract_short_snapshot_field(snapshot: Any, dotted_key: str, default: str = "unknown") -> str:
    if not isinstance(snapshot, dict):
        return default
    current = snapshot
    for part in dotted_key.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part)
    if current in (None, ""):
        return default
    return str(current)

def build_decision_records(sorted_ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records = []
    seen = set()
    for op in sorted_ops:
        key = (op.get("folder"), op.get("timestamp"))
        if key in seen:
            continue
        seen.add(key)
        records.append(op)
    return records

def build_decision_text_excerpt(record: Dict[str, Any], limit: int = 320) -> str:
    text = "\n".join(
        [
            str(record.get("execution_txt", "") or ""),
            str(record.get("explanation", "") or ""),
            str(record.get("memory_management_reasoning", "") or ""),
        ]
    ).strip()
    return shorten_blob(text, limit) if text else "N/A"

def summarize_args_preview(args: Any, limit: int = 220) -> str:
    try:
        return shorten_blob(json.dumps(args or {}, ensure_ascii=False), limit)
    except Exception:
        return shorten_blob(str(args or {}), limit)

def summarize_reason_preview(payload: Any, limit: int = 240) -> str:
    if isinstance(payload, dict):
        for key in ["why_rejected", "reason", "error", "msg", "message", "raw"]:
            value = payload.get(key)
            if value not in (None, ""):
                return shorten_blob(value, limit)
        try:
            return shorten_blob(json.dumps(payload, ensure_ascii=False), limit)
        except Exception:
            return shorten_blob(str(payload), limit)
    if isinstance(payload, list):
        try:
            return shorten_blob(json.dumps(payload, ensure_ascii=False), limit)
        except Exception:
            return shorten_blob(str(payload), limit)
    return shorten_blob(str(payload or ""), limit) or "N/A"

def classify_error_category(rule_or_source: str, status: str, tool: str) -> str:
    rule = str(rule_or_source or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    normalized_tool = str(tool or "").strip().lower()
    if rule in {"missing_structured_wait_alarm", "historical_alarm_confused_as_live"}:
        return "guard_text_semantics"
    if rule:
        return "guard_sequence_or_consistency"
    if normalized_status == "rejected":
        return "exchange_rejection"
    if normalized_status in {"error", "failed", "exception"}:
        return "api_or_tool_error"
    if normalized_tool:
        return "api_or_tool_error"
    return "final_validation_error"

def build_repair_action_summary(rule_or_source: str, ops_list: List[Dict[str, Any]]) -> str:
    rule = str(rule_or_source or "").strip().lower()
    if rule == "missing_structured_wait_alarm":
        repaired = [
            op for op in ops_list if op.get("tool") == "set_alarm" and str(op.get("result_status", "")).lower() not in {"rejected", "error", "failed", "exception"}
        ]
        if repaired:
            alarm_ids = []
            for op in repaired[:2]:
                if isinstance(op.get("result"), dict):
                    alarm_id = str(op["result"].get("id", "") or "").strip()
                    if alarm_id:
                        alarm_ids.append(alarm_id)
            suffix = f" ({', '.join(alarm_ids)})" if alarm_ids else ""
            return f"retry 后成功补设 set_alarm{suffix}"
    if rule == "historical_alarm_confused_as_live":
        repaired = [
            op for op in ops_list if op.get("tool") in {"set_alarm", "delete_alarm"} and str(op.get("result_status", "")).lower() not in {"rejected", "error", "failed", "exception"}
        ]
        if repaired:
            return "retry 后已改为真实闹钟维护动作"
    return ""

def collect_error_events(decision_records: List[Dict[str, Any]], sorted_ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ops_by_folder = defaultdict(list)
    for op in sorted_ops:
        ops_by_folder[str(op.get("folder", ""))].append(op)

    events: List[Dict[str, Any]] = []
    seen = set()
    for record in decision_records:
        folder = str(record.get("folder", "") or "")
        record_time = record.get("timestamp")
        ops_list = ops_by_folder.get(folder, [])
        resolved_guard_keys = {
            (
                str(item.get("error_class", item.get("rule_id", "")) or ""),
                int(item.get("attempt_index", 0) or 0),
            )
            for item in record.get("resolved_guard_failures", [])
            if isinstance(item, dict)
        }
        resolved_guard_rules = {key[0] for key in resolved_guard_keys if key[0]}
        decision_excerpt = build_decision_text_excerpt(record)

        for item in record.get("guard_failures", []):
            if not isinstance(item, dict):
                continue
            rule = str(item.get("error_class", item.get("rule_id", "unknown")) or "unknown")
            attempt_index = int(item.get("attempt_index", 0) or 0)
            resolved_on_retry = (rule, attempt_index) in resolved_guard_keys or rule in resolved_guard_rules
            event = {
                "time": record_time,
                "time_cst": (record_time + CST_OFFSET).strftime("%Y-%m-%d %H:%M:%S") if isinstance(record_time, datetime) else "unknown",
                "log_folder": folder,
                "category": classify_error_category(rule, "blocked", ""),
                "rule_or_source": rule,
                "status": "resolved_on_retry" if resolved_on_retry else "blocked",
                "resolved_on_retry": resolved_on_retry,
                "tool": "",
                "args_preview": "",
                "reason": summarize_reason_preview(item.get("why_rejected", "") or item.get("reason", "")),
                "decision_text_excerpt": decision_excerpt,
                "repair_action": build_repair_action_summary(rule, ops_list),
            }
            dedupe_key = (
                event["log_folder"],
                event["category"],
                event["rule_or_source"],
                event["status"],
                event["reason"],
            )
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                events.append(event)

        for err in record.get("final_validation_errors", []) or []:
            reason = summarize_reason_preview(err)
            event = {
                "time": record_time,
                "time_cst": (record_time + CST_OFFSET).strftime("%Y-%m-%d %H:%M:%S") if isinstance(record_time, datetime) else "unknown",
                "log_folder": folder,
                "category": "final_validation_error",
                "rule_or_source": "final_validation_error",
                "status": "final_error",
                "resolved_on_retry": False,
                "tool": "",
                "args_preview": "",
                "reason": reason,
                "decision_text_excerpt": decision_excerpt,
                "repair_action": "",
            }
            dedupe_key = (event["log_folder"], event["category"], event["reason"])
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                events.append(event)

        for item in record.get("validation_events", []) or []:
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag", "") or "").strip()
            reason = summarize_reason_preview(item.get("reason", ""))
            if not tag and reason == "N/A":
                continue
            event = {
                "time": record_time,
                "time_cst": (record_time + CST_OFFSET).strftime("%Y-%m-%d %H:%M:%S") if isinstance(record_time, datetime) else "unknown",
                "log_folder": folder,
                "category": "final_validation_error",
                "rule_or_source": tag or "validation_event",
                "status": "validation_event",
                "resolved_on_retry": False,
                "tool": str(item.get("tool", "") or ""),
                "args_preview": "",
                "reason": reason,
                "decision_text_excerpt": decision_excerpt,
                "repair_action": "",
            }
            dedupe_key = (
                event["log_folder"],
                event["category"],
                event["rule_or_source"],
                event["tool"],
                event["reason"],
            )
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                events.append(event)

        for op in ops_list:
            tool = str(op.get("tool", "") or "")
            if tool in {"NO_TRADE", "ORDER_FILLED"}:
                continue
            status = str(op.get("result_status", "") or "").lower()
            if status not in {"error", "failed", "exception", "rejected"}:
                continue
            result = op.get("result", {})
            event = {
                "time": op.get("timestamp"),
                "time_cst": ((op.get("timestamp") or record_time) + CST_OFFSET).strftime("%Y-%m-%d %H:%M:%S") if isinstance((op.get("timestamp") or record_time), datetime) else "unknown",
                "log_folder": folder,
                "category": classify_error_category("", status, tool),
                "rule_or_source": str(op.get("stage", "") or "tool_call"),
                "status": status,
                "resolved_on_retry": False,
                "tool": tool,
                "args_preview": summarize_args_preview(op.get("args", {})),
                "reason": summarize_reason_preview(result),
                "decision_text_excerpt": decision_excerpt,
                "repair_action": "",
            }
            dedupe_key = (
                event["log_folder"],
                event["category"],
                event["tool"],
                event["status"],
                event["reason"],
            )
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                events.append(event)

    return sorted(events, key=lambda item: (item.get("time") or datetime.min, item.get("log_folder", ""), item.get("category", "")))

def extract_consistency_timeline_entry(record: Dict[str, Any], previous: Optional[Dict[str, str]]) -> Dict[str, str]:
    snapshot = record.get("short_memory_snapshot")
    current = {
        "time": (record.get("timestamp") + CST_OFFSET).strftime("%Y-%m-%d %H:%M:%S") if isinstance(record.get("timestamp"), datetime) else "unknown",
        "log_folder": str(record.get("folder", "") or ""),
        "wakeup_role": extract_short_snapshot_field(snapshot, "consistency_state.wakeup_role"),
        "intraday_mode": extract_short_snapshot_field(snapshot, "consistency_state.intraday_mode"),
        "execution_mode": extract_short_snapshot_field(snapshot, "consistency_state.execution_mode"),
        "trade_intent": extract_short_snapshot_field(snapshot, "consistency_state.trade_intent"),
        "entry_plan_direction": extract_short_snapshot_field(snapshot, "consistency_state.entry_plan_direction"),
        "cooldown_direction": extract_short_snapshot_field(snapshot, "consistency_state.cooldown_direction"),
        "risk_state": extract_short_snapshot_field(snapshot, "risk_state.risk_state"),
    }
    tracked = [
        "wakeup_role",
        "intraday_mode",
        "execution_mode",
        "trade_intent",
        "entry_plan_direction",
        "cooldown_direction",
        "risk_state",
    ]
    if previous is None:
        current["change"] = "initial"
    else:
        changed = [field for field in tracked if current.get(field) != previous.get(field)]
        current["change"] = "unchanged" if not changed else "changed: " + ", ".join(changed)
    return current

def extract_hypothesis_timeline_entry(record: Dict[str, Any], previous: Optional[Dict[str, str]]) -> Dict[str, str]:
    snapshot = record.get("short_memory_snapshot")
    current = {
        "time": (record.get("timestamp") + CST_OFFSET).strftime("%Y-%m-%d %H:%M:%S") if isinstance(record.get("timestamp"), datetime) else "unknown",
        "log_folder": str(record.get("folder", "") or ""),
        "hypothesis_id": extract_short_snapshot_field(snapshot, "active_hypothesis.hypothesis_id", ""),
        "direction": extract_short_snapshot_field(snapshot, "active_hypothesis.direction", "unknown"),
        "status": extract_short_snapshot_field(snapshot, "active_hypothesis.status", "unknown"),
        "expiry": extract_short_snapshot_field(snapshot, "active_hypothesis.expiry", ""),
        "hypothesis_action": str(record.get("hypothesis_action", "") or ""),
    }
    if previous is None:
        current["change"] = "initial"
        return current
    prev_id = previous.get("hypothesis_id", "")
    curr_id = current.get("hypothesis_id", "")
    prev_status = previous.get("status", "")
    curr_status = current.get("status", "")
    prev_expiry = previous.get("expiry", "")
    curr_expiry = current.get("expiry", "")
    if prev_id != curr_id:
        if curr_id and not prev_id:
            change = "new"
        elif prev_id and not curr_id:
            change = "cleared"
        else:
            change = "replaced"
    elif prev_status != curr_status:
        change = curr_status or "status_changed"
    elif prev_expiry != curr_expiry:
        change = "updated"
    else:
        change = "unchanged"
    current["change"] = change
    return current

def summarize_tool_result_status(result):
    parsed = parse_jsonish(result)
    if isinstance(parsed, dict) and parsed:
        if parsed.get("error"):
            return "error"
        status = str(parsed.get("status", "")).strip().lower()
        if status:
            return status
        if parsed.get("orderId"):
            return str(parsed.get("status", "ok") or "ok").strip().lower() or "ok"
    if isinstance(parsed, list):
        return "ok"
    if isinstance(result, str) and result.strip():
        lowered = result.strip().lower()
        if "error" in lowered:
            return "error"
    return "ok"

def summarize_tool_detail(stage, tool_name, result_status):
    stage_label = str(stage or "unknown").upper()
    status_label = str(result_status or "ok").lower()
    return f"{stage_label} | {tool_name} | {status_label}"

def parse_logs(log_dir, start_time, end_time):
    """Scan logs directory for all records within the calculated range."""
    operations = []
    wakeups = []
    llm_calls = []

    if not os.path.exists(log_dir):
        return operations, wakeups, llm_calls

    folders = [
        f
        for f in os.listdir(log_dir)
        if os.path.isdir(os.path.join(log_dir, f)) and re.match(r"^\d{8}_\d{6}$", f)
    ]
    folders.sort()

    for folder in folders:
        try:
            local_log_time = datetime.strptime(folder, "%Y%m%d_%H%M%S")
            log_time_utc = local_log_time - CST_OFFSET
            if not (start_time <= log_time_utc <= end_time):
                continue

            folder_path = os.path.join(log_dir, folder)
            output_path = os.path.join(folder_path, "output.json")
            input_path = os.path.join(folder_path, "input.md")
            if not os.path.exists(output_path):
                continue

            with open(output_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)

            indicators = {}
            batched_events_count = 1
            audit_meta = data.get("audit_meta", {})
            if not isinstance(audit_meta, dict):
                audit_meta = {}
            long_context = load_structured_long_context(folder_path)
            event_type_str = str(audit_meta.get("event_type", "unknown") or "unknown")

            content_md = ""
            if os.path.exists(input_path):
                with open(input_path, "r", encoding="utf-8") as handle:
                    content_md = extract_user_prompt_block(handle.read())

            if content_md:
                match = re.search(
                    r"## 15 minutes chart.*?\| Timestamp \|.*?\| :--- \|.*?\n(?:\| .*? \|\n)*?(\| .*? \|)\n",
                    content_md,
                    re.DOTALL,
                )
                if match:
                    last_row = [x.strip() for x in match.group(1).strip("|").split("|")]
                    if len(last_row) >= 16:
                        indicators = {
                            "rsi": last_row[5],
                            "macd": last_row[6],
                            "histo": last_row[7],
                            "upper": last_row[8],
                            "lower": last_row[9],
                            "k": last_row[10],
                            "d": last_row[11],
                            "j": last_row[12],
                            "cci": last_row[13],
                            "wr": last_row[14],
                            "obv": last_row[15],
                        }

                event_type_str, event_type_list = extract_event_type_and_list(content_md, fallback_type=event_type_str)
                event_markers = re.findall(r"Event \d+ \((.*?)\):", content_md)
                if event_markers:
                    batched_events_count = len(event_markers)
            else:
                event_type_str, event_type_list = extract_event_type_and_list("", fallback_type=event_type_str)

            for event_type in event_type_list:
                wakeups.append({"timestamp": log_time_utc, "type": event_type})

            usage = data.get("usage", {})
            prompt_details = usage.get("prompt_tokens_details", {})
            langsmith_meta = data.get("langsmith", {})
            if not isinstance(langsmith_meta, dict):
                langsmith_meta = {}
            project_name = str(langsmith_meta.get("project", "") or os.getenv("LANGSMITH_PROJECT", "CoinAutomation"))
            trace_id = str(langsmith_meta.get("trace_id", "") or "")
            thread_id = str(langsmith_meta.get("thread_id", "") or "")
            llm_calls.append(
                {
                    "timestamp": log_time_utc,
                    "total_tokens": usage.get("total_tokens", 0),
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cached_tokens": prompt_details.get("cached_tokens", 0),
                    "cache_creation_tokens": prompt_details.get("cache_creation_input_tokens", 0),
                    "event_count": batched_events_count,
                    "total_turns": data.get("total_turns", 1),
                }
            )

            content = data.get("content", {})
            if not isinstance(content, dict):
                content = {"execution_txt": str(content)}
            short_memory_snapshot = content.get("short_memory_snapshot")
            long_memory_snapshot = content.get("long_memory_snapshot")
            short_memory_ops = content.get("short_memory_ops", [])
            long_memory_ops = content.get("long_memory_ops", [])
            common_payload = {
                "timestamp": log_time_utc,
                "folder": folder,
                "event_type": event_type_str,
                "explanation": content.get("explanation", "No explanation available."),
                "execution_txt": content.get("execution_txt", "No execution summary."),
                "decision_basis": content.get("decision_basis", ""),
                "conflict_check": content.get("conflict_check", ""),
                "falsification_point": content.get("falsification_point", ""),
                "next_alarm_reason": content.get("next_alarm_reason", ""),
                "state_change_evidence": content.get("state_change_evidence", ""),
                "action_intent": str(content.get("action_intent", "") or ""),
                "plan_transition": str(content.get("plan_transition", "") or ""),
                "hypothesis_action": str(content.get("hypothesis_action", "") or ""),
                "hypothesis_action_reason": content.get("hypothesis_action_reason", ""),
                "execution_rationale": content.get("execution_rationale", ""),
                "declared_entry_plan_direction": str(content.get("declared_entry_plan_direction", "") or ""),
                "declared_hypothesis_id": str(content.get("declared_hypothesis_id", "") or ""),
                "declared_hypothesis_direction": str(content.get("declared_hypothesis_direction", "") or ""),
                "declared_hypothesis_status": str(content.get("declared_hypothesis_status", "") or ""),
                "declared_hypothesis_expiry": str(content.get("declared_hypothesis_expiry", "") or ""),
                "declared_reversal_checklist": content.get("declared_reversal_checklist", ""),
                "declared_state_change_evidence": content.get("declared_state_change_evidence", ""),
                "tool_intents": content.get("tool_intents", []) if isinstance(content.get("tool_intents", []), list) else [],
                "memory_management_reasoning": content.get("memory_management_reasoning", "N/A"),
                "account_data": data.get("account_data", "N/A"),
                "indicators": indicators,
                "experience": pretty_memory_text(long_memory_snapshot, content.get("experience", "")),
                "shortterm": pretty_memory_text(short_memory_snapshot, content.get("shortterm", "")),
                "long_memory_snapshot": long_memory_snapshot,
                "short_memory_snapshot": short_memory_snapshot,
                "long_memory_ops": long_memory_ops if isinstance(long_memory_ops, list) else [],
                "short_memory_ops": short_memory_ops if isinstance(short_memory_ops, list) else [],
                "long_horizon_view": long_context.get("long_horizon_view"),
                "long_inputs_raw": long_context.get("long_inputs_raw"),
                "long_inputs_kept": long_context.get("long_inputs_kept"),
                "long_inputs_dropped": long_context.get("long_inputs_dropped"),
                "long_reflection": long_context.get("long_reflection"),
                "long_gating_stats": long_context.get("long_gating_stats", {}),
                "daily_review": data.get("daily_review", {}) if isinstance(data.get("daily_review", {}), dict) else None,
                "validation_status": str(data.get("validation_status", "") or ""),
                "reanswer_count": int(data.get("reanswer_count", 0) or 0),
                "triggered_rules": str(data.get("triggered_rules", "") or ""),
                "triggered_rules_first_fail": str(data.get("triggered_rules_first_fail", "") or ""),
                "triggered_rules_all_fail": str(data.get("triggered_rules_all_fail", "") or ""),
                "triggered_rules_final": str(data.get("triggered_rules_final", "") or ""),
                "resolved_rules": str(data.get("resolved_rules", "") or ""),
                "attempts": data.get("attempts", []) if isinstance(data.get("attempts", []), list) else [],
                "guard_failures": data.get("guard_failures", []) if isinstance(data.get("guard_failures", []), list) else [],
                "resolved_guard_failures": data.get("resolved_guard_failures", []) if isinstance(data.get("resolved_guard_failures", []), list) else [],
                "unparsed_fields": data.get("unparsed_fields", []) if isinstance(data.get("unparsed_fields", []), list) else [],
                "counter_long_horizon_bias": bool(audit_meta.get("counter_long_horizon_bias", False)),
                "long_horizon_status_at_trade": str(audit_meta.get("long_horizon_status_at_trade", "") or ""),
                "final_validation_errors": data.get("final_validation_errors", []),
                "consistency_violations": data.get("consistency_violations", []),
                "validation_events": data.get("validation_events", []),
                "audit_meta": audit_meta,
                "stage_flow": list(audit_meta.get("stage_flow", [])) if isinstance(audit_meta.get("stage_flow", []), list) else [],
                "proposal_text": str(audit_meta.get("proposal_text", "")),
                "execute_raw_text": str(audit_meta.get("execute_raw_text", "")),
                "execute_agent_mode": str(audit_meta.get("execute_agent_mode", "")),
                "execute_passes": list(audit_meta.get("execute_passes", [])) if isinstance(audit_meta.get("execute_passes", []), list) else [],
                "approved_decision_contract": audit_meta.get("approved_decision_contract", {}) if isinstance(audit_meta.get("approved_decision_contract", {}), dict) else {},
                "approved_decision_contract_mode": str(audit_meta.get("approved_decision_contract_mode", "") or ""),
                "approved_decision_contract_raw_text": str(audit_meta.get("approved_decision_contract_raw_text", "")),
                "retry_mode_effective": str(audit_meta.get("retry_mode_effective", "") or ""),
                "retry_runtime_refresh": str(audit_meta.get("retry_runtime_refresh", "")),
                "last_guard_feedback": str(audit_meta.get("last_guard_feedback", "") or ""),
                "decision_validation_failures_all": list(audit_meta.get("decision_validation_failures_all", [])) if isinstance(audit_meta.get("decision_validation_failures_all", []), list) else [],
                "deterministic_guard_repairs": list(audit_meta.get("deterministic_guard_repairs", [])) if isinstance(audit_meta.get("deterministic_guard_repairs", []), list) else [],
                "precheck_source": str(audit_meta.get("precheck_source", "")),
                "langsmith_project": project_name,
                "langsmith_thread_id": thread_id,
                "langsmith_trace_id": trace_id,
                "langsmith_source": "output_json_metadata",
            }

            tool_calls = data.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                tool_calls = []
            tool_calls = annotate_tool_calls_with_trace(tool_calls)

            folder_ops = []
            for call in tool_calls:
                tool_name = call.get("tool")
                args = call.get("args", {})
                raw_result = call.get("result", {})
                result = parse_jsonish(raw_result)
                if result == {}:
                    if isinstance(raw_result, list):
                        result = raw_result
                    elif isinstance(raw_result, str) and raw_result.strip():
                        result = {"raw": raw_result}
                side = args.get("side") or (result.get("side") if isinstance(result, dict) else None)
                status = summarize_tool_result_status(raw_result)
                is_bullish = None
                if side:
                    is_bullish = str(side).upper() == "BUY"
                detail = summarize_tool_detail(call.get("stage", "unknown"), tool_name, status)
                folder_ops.append(
                    {
                        **common_payload,
                        "tool": tool_name,
                        "detail": detail,
                        "is_bullish": is_bullish,
                        "stage": str(call.get("stage", "") or "unknown"),
                        "stage_source": str(call.get("stage_source", "") or "local_fallback"),
                        "recorded_at": str(call.get("recorded_at", "") or ""),
                        "args": args,
                        "result": result,
                        "result_status": status,
                    }
                )

            if not folder_ops and (
                common_payload["explanation"] != "No explanation available."
                or common_payload["execution_txt"] != "No execution summary."
            ):
                operations.append(
                    {
                        **common_payload,
                        "tool": "NO_TRADE",
                        "detail": "N/A",
                        "is_bullish": None,
                        "args": {},
                        "result": {},
                    }
                )
            else:
                operations.extend(folder_ops)

            for event_type in event_type_list:
                if event_type.lower() == "order_fill":
                    operations.append(
                        {
                            **common_payload,
                            "tool": "ORDER_FILLED",
                            "detail": "R(FILL)",
                            "is_bullish": None,
                            "stage": "event",
                            "stage_source": "local_fallback",
                            "recorded_at": "",
                            "args": {},
                            "result": {},
                            "result_status": "filled",
                        }
                    )
        except Exception:
            continue

    return operations, wakeups, llm_calls

def plot_backtest_with_stats(df, operations, wakeups, llm_calls, output_html="backtest_report.html"):
    """Create an interactive market+decision visualization from LangChain logs."""
    if df.empty:
        print("Skip chart export: empty K-line dataframe.")
        return

    plot_df = df.copy()
    plot_df["timestamp_cst"] = plot_df["timestamp"] + CST_OFFSET

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.62, 0.18, 0.20],
        specs=[[{"secondary_y": False}], [{"secondary_y": False}], [{"secondary_y": True}]],
    )

    fig.add_trace(
        go.Candlestick(
            x=plot_df["timestamp_cst"],
            open=plot_df["open"],
            high=plot_df["high"],
            low=plot_df["low"],
            close=plot_df["close"],
            name="ETHUSDT",
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(
            x=plot_df["timestamp_cst"],
            y=plot_df["volume"],
            name="Volume",
            marker_color="rgba(110,130,160,0.45)",
        ),
        row=2,
        col=1,
    )

    ts_list = plot_df["timestamp"].tolist()
    close_list = plot_df["close"].tolist()

    def nearest_close(ts):
        if not ts_list:
            return None
        idx = np.searchsorted(ts_list, ts)
        if idx <= 0:
            return float(close_list[0])
        if idx >= len(ts_list):
            return float(close_list[-1])
        left_ts = ts_list[idx - 1]
        right_ts = ts_list[idx]
        if abs((ts - left_ts).total_seconds()) <= abs((right_ts - ts).total_seconds()):
            return float(close_list[idx - 1])
        return float(close_list[idx])

    marker_buckets = {
        "BUY Entry": {"x": [], "y": [], "color": "#2ecc71", "symbol": "triangle-up"},
        "SELL Entry": {"x": [], "y": [], "color": "#e74c3c", "symbol": "triangle-down"},
        "Close": {"x": [], "y": [], "color": "#8e44ad", "symbol": "x"},
        "Fill Event": {"x": [], "y": [], "color": "#f39c12", "symbol": "circle"},
    }

    for op in sorted(operations, key=lambda item: item.get("timestamp")):
        tool = op.get("tool")
        ts = op.get("timestamp")
        if ts is None:
            continue
        y = nearest_close(ts)
        if y is None:
            continue
        x = ts + CST_OFFSET

        if tool in {"close_coin_futures_position", "close_usdt_futures_position"}:
            marker_buckets["Close"]["x"].append(x)
            marker_buckets["Close"]["y"].append(y)
            continue
        if tool == "ORDER_FILLED":
            marker_buckets["Fill Event"]["x"].append(x)
            marker_buckets["Fill Event"]["y"].append(y)
            continue
        if tool not in {"trade_coin_futures", "trade_usdt_futures"}:
            continue

        side = str(op.get("args", {}).get("side", "")).upper()
        reduce_only = str(op.get("args", {}).get("reduce_only", "")).lower() in {"true", "1", "yes"}
        if reduce_only:
            marker_buckets["Close"]["x"].append(x)
            marker_buckets["Close"]["y"].append(y)
        elif side == "BUY":
            marker_buckets["BUY Entry"]["x"].append(x)
            marker_buckets["BUY Entry"]["y"].append(y)
        elif side == "SELL":
            marker_buckets["SELL Entry"]["x"].append(x)
            marker_buckets["SELL Entry"]["y"].append(y)

    for name, payload in marker_buckets.items():
        if not payload["x"]:
            continue
        fig.add_trace(
            go.Scatter(
                x=payload["x"],
                y=payload["y"],
                mode="markers",
                name=name,
                marker=dict(size=10, color=payload["color"], symbol=payload["symbol"]),
            ),
            row=1,
            col=1,
        )

    wakeup_counter = defaultdict(int)
    for wake in wakeups:
        ts = wake.get("timestamp")
        if ts is None:
            continue
        wakeup_counter[ts + CST_OFFSET] += 1
    if wakeup_counter:
        wake_x = sorted(wakeup_counter.keys())
        wake_y = [wakeup_counter[x] for x in wake_x]
        fig.add_trace(
            go.Bar(
                x=wake_x,
                y=wake_y,
                name="Wakeups",
                marker_color="rgba(52,152,219,0.45)",
            ),
            row=3,
            col=1,
            secondary_y=False,
        )

    if llm_calls:
        llm_x = [item["timestamp"] + CST_OFFSET for item in llm_calls if item.get("timestamp") is not None]
        llm_y = [item.get("total_tokens", 0) for item in llm_calls if item.get("timestamp") is not None]
        if llm_x:
            fig.add_trace(
                go.Scatter(
                    x=llm_x,
                    y=llm_y,
                    mode="lines+markers",
                    name="LLM Tokens",
                    line=dict(color="#f1c40f", width=2),
                ),
                row=3,
                col=1,
                secondary_y=True,
            )

    fig.update_layout(
        title=f"Backtest Timeline ({SYMBOL})",
        xaxis_rangeslider_visible=False,
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    fig.update_yaxes(title_text="Wakeups", row=3, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Tokens", row=3, col=1, secondary_y=True)
    fig.update_xaxes(title_text="Time (CST)", row=3, col=1)

    fig.write_html(output_html, include_plotlyjs="cdn")
    print(f"Chart exported successfully to {output_html}.")

def export_to_text(df, operations, output_file="backtest_report.md", start_label=None, end_label=None):
    """Export market/decision logs, structured error records, and state timelines."""
    print(f"Exporting report to {output_file}...")
    audit_counts = defaultdict(int)
    pnl_completeness = {"status": "完整", "reason": "未发现部分匹配警告"}
    fee_completeness = {"status": "未知", "reason": "未发现 fee_breakdown"}

    def classify_decision(window_row, ops_list):
        def extract_short_memory_field(op, dotted_key):
            snapshot = op.get("short_memory_snapshot")
            if isinstance(snapshot, dict):
                current = snapshot
                for part in dotted_key.split("."):
                    if not isinstance(current, dict):
                        current = None
                        break
                    current = current.get(part)
                if current not in (None, ""):
                    return str(current)
            text_short = str(op.get("shortterm", ""))
            legacy_patterns = {
                "day_plan.day_bias": r"- day_bias:\s*([^\n]+)",
                "consistency_state.wakeup_role": r"- wakeup_role:\s*([^\n]+)",
            }
            pattern = legacy_patterns.get(dotted_key)
            if not pattern:
                return ""
            match = re.search(pattern, text_short, re.IGNORECASE)
            return match.group(1).strip() if match else ""

        tags = []
        first_op = ops_list[0] if ops_list else {}
        event_type_text = str(first_op.get("event_type", "") or "").lower()
        text_exec = str(first_op.get("execution_txt", ""))
        text_expl = str(first_op.get("explanation", ""))
        text_memory = str(first_op.get("memory_management_reasoning", ""))
        text_shortterm = str(first_op.get("shortterm", ""))
        text_all = f"{text_exec} {text_expl} {text_shortterm}"
        decision_text = f"{text_exec} {text_expl} {text_memory}"
        tools = [o.get("tool") for o in ops_list]
        audit_meta = first_op.get("audit_meta", {}) if isinstance(first_op.get("audit_meta", {}), dict) else {}
        pending_alarms_text = str(
            audit_meta.get("pending_alarms_snapshot_refreshed", "")
            or audit_meta.get("pending_alarms_snapshot", "")
            or ""
        )
        pending_alarm_authority_known = bool(pending_alarms_text.strip())
        no_live_pending_alarms = pending_alarms_text.strip() == "No pending alarms."
        has_trade = any(
            t in {"close_coin_futures_position", "close_usdt_futures_position"}
            or (t in {"trade_coin_futures", "trade_usdt_futures"} and str(o.get("result_status", "")) != "rejected")
            for t, o in ((item.get("tool"), item) for item in ops_list)
        )
        has_futures_audit = all(t in tools for t in {"get_coin_futures_position", "get_coin_futures_account", "get_coin_futures_max_open_position"})
        violation_text = " ".join(
            f"{item.get('tag', '')} {item.get('reason', '')}"
            for item in first_op.get("consistency_violations", [])
        ).lower()
        day_bias = extract_short_memory_field(first_op, "day_plan.day_bias").strip().lower()
        wakeup_role = extract_short_memory_field(first_op, "consistency_state.wakeup_role").strip().lower()

        opening_attempt = any(op.get("tool") == "trade_coin_futures" for op in ops_list)
        opening_rejected = any(op.get("tool") == "trade_coin_futures" and str(op.get("result_status", "")) == "rejected" for op in ops_list)
        if not any(op.get("tool") for op in ops_list) and re.search(r"执行加仓|已加仓|已平仓|当前持仓|止损单已设置|保护单已就位", text_all, re.IGNORECASE):
            tags.append("execution_fact_mismatch")
        if "daily_long_review" in event_type_text:
            tags.append("daily_review_started")
            status = str(first_op.get("validation_status", "") or "").lower()
            if status == "review_only":
                tags.append("daily_review_completed")
            elif status == "review_failed":
                tags.append("daily_review_failed")
        if ("consistency_break" in violation_text or "execution_mode_blocked" in violation_text or "observe_trade_decoupling_failed" in violation_text) and opening_attempt:
            tags.append("consistency_break")
        if "observe_trade_decoupling_failed" in violation_text:
            tags.append("observe_trade_decoupling_failed")

        has_opening = False
        has_risk_action = False
        for op in ops_list:
            if op.get("tool") in {"close_coin_futures_position", "close_usdt_futures_position"}:
                has_risk_action = True
            if op.get("tool") not in {"trade_coin_futures", "trade_usdt_futures"}:
                continue
            args = op.get("args", {})
            order_type = str(args.get("order_type", "MARKET")).upper()
            reduce_only_raw = args.get("reduce_only", False)
            reduce_only = str(reduce_only_raw).strip().lower() in {"true", "1", "yes"}
            if reduce_only:
                has_risk_action = True
            if order_type in {"MARKET", "LIMIT"} and not reduce_only:
                has_opening = True
                side = str(args.get("side", "")).upper()
                if day_bias == "long" and side == "SELL":
                    tags.append("overtrading_against_day_bias")
                if day_bias == "short" and side == "BUY":
                    tags.append("overtrading_against_day_bias")
        if has_opening and not has_futures_audit:
            tags.append("execution_chain_missing")
            tags.append("futures_precheck_missing")
        if (
            any(op.get("tool") in {"cancel_coin_futures_order", "cancel_usdt_futures_order"} for op in ops_list)
            and any(op.get("tool") in {"trade_coin_futures", "trade_usdt_futures"} and str(op.get("result_status", "")) == "rejected" for op in ops_list)
            and re.search(r"STOP|TAKE_PROFIT|保护单|止损单", text_all, re.IGNORECASE)
        ):
            tags.append("protection_gap_created")
        if re.search(r"做多|看涨|反弹|待涨|long", text_all, re.IGNORECASE) and re.search(
            r"止损|亏损|stopped|loss", text_all, re.IGNORECASE
        ):
            tags.append("bullish_failed")

        if "countertrend_long_blocked" in violation_text:
            tags.append("countertrend_long_blocked")
        if opening_rejected and "countertrend_long_blocked" not in tags and "consistency_break" not in tags:
            tags.append("entry_attempt_rejected")

        short_opportunity_taken = any(
            op.get("tool") == "trade_coin_futures"
            and str(op.get("args", {}).get("side", "")).upper() == "SELL"
            and str(op.get("args", {}).get("reduce_only", False)).strip().lower() not in {"true", "1", "yes"}
            for op in ops_list
        )
        long_opportunity_taken = any(
            op.get("tool") == "trade_coin_futures"
            and str(op.get("args", {}).get("side", "")).upper() == "BUY"
            and str(op.get("args", {}).get("reduce_only", False)).strip().lower() not in {"true", "1", "yes"}
            for op in ops_list
        )
        if short_opportunity_taken:
            tags.append("short_opportunity_taken")
        if long_opportunity_taken:
            tags.append("long_opportunity_taken")

        blocked_short_reason = re.search(
            r"容量阻塞|capacity blocked|max_contracts=0|max_quantity=0|不可开仓|insufficient margin|short blocked|客观执行障碍",
            text_all,
            re.IGNORECASE,
        )
        blocked_long_reason = re.search(
            r"容量阻塞|capacity blocked|max_contracts=0|max_quantity=0|不可开仓|insufficient margin|long blocked|客观执行障碍|做多条件不足|放弃做多|skip long|no long",
            text_all,
            re.IGNORECASE,
        )
        observe_or_wait = bool(re.search(r"空仓|继续等待|继续观察|观望|wait|observe", text_all, re.IGNORECASE))
        bearish_evidence = bool(
            re.search(
                r"强空|strong_bearish|breakdown confirmed|breakdown_confirmed|跌破|失守|lower high|反抽失败|1h macd 转负|趋势转弱",
                text_all,
                re.IGNORECASE,
            )
        )
        bullish_evidence = bool(
            re.search(
                r"强多|strong_bullish|收复|站回|higher low|回踩承接|break reclaim|突破站稳|1h macd 转正|趋势转强",
                text_all,
                re.IGNORECASE,
            )
        )
        if bearish_evidence and observe_or_wait and not blocked_short_reason and not short_opportunity_taken:
            tags.append("tactical_short_missed")
        if bullish_evidence and observe_or_wait and not blocked_long_reason and not long_opportunity_taken:
            tags.append("tactical_long_missed")

        has_set_alarm = any(op.get("tool") == "set_alarm" and str(op.get("result_status", "")).lower() != "rejected" for op in ops_list)
        historical_alarm_live_confusion = bool(
            re.search(
                r"(已有|现有|保留|等待).{0,24}(闹钟|ALARM_\d+)|闹钟覆盖|alarm\s+coverage|现有闹钟会验证|已有 alarm 覆盖",
                decision_text,
                re.IGNORECASE,
            )
        )
        explicit_wait_condition = bool(
            re.search(r"(等待|验证|触发|monitor|wait|until|观察|观望)", decision_text, re.IGNORECASE)
            and re.search(
                r"((价格|price|RSI(?:_1h|_15m)?|MACD(?:_HISTO)?(?:_1h|_15m)?)"
                r".{0,30}(突破|跌破|站回|收复|上破|下破|>|<|>=|<=|转正|转负|收敛|扩大|回落至|升至)"
                r".{0,20}\d)"
                r"|(\bprice\s*(>=|<=|>|<)\s*\d)"
                r"|(\bRSI(?:_1h|_15m)?\s*(>=|<=|>|<)\s*\d)"
                r"|(\bMACD(?:_HISTO)?(?:_1h|_15m)?\s*(>=|<=|>|<)\s*-?\d)",
                decision_text,
                re.IGNORECASE,
            )
        )
        if no_live_pending_alarms and not has_set_alarm and historical_alarm_live_confusion:
            tags.append("historical_alarm_confused_as_live")
        if explicit_wait_condition and not has_set_alarm and "historical_alarm_confused_as_live" not in tags:
            if no_live_pending_alarms:
                tags.append("missing_structured_wait_alarm")
            elif pending_alarm_authority_known:
                live_alarm_ids = re.findall(r"\bALARM_\d+\b", pending_alarms_text)
                if not any(alarm_id in decision_text for alarm_id in live_alarm_ids):
                    tags.append("missing_structured_wait_alarm")

        countertrend_opening = bool(first_op.get("counter_long_horizon_bias", False)) and (short_opportunity_taken or long_opportunity_taken)
        countertrend_probe = bool(re.search(r"试探仓|probe", text_all, re.IGNORECASE))
        countertrend_has_protection = any(
            op.get("tool") == "trade_coin_futures"
            and str(op.get("args", {}).get("order_type", "")).upper().startswith(("STOP", "TAKE_PROFIT"))
            for op in ops_list
        )
        if countertrend_opening and not countertrend_probe and not countertrend_has_protection:
            tags.append("countertrend_risk_too_loose")
        if (
            wakeup_role == "observe"
            and re.search(r"继续持有|继续观察|hold_until|day_invalidation", text_all, re.IGNORECASE)
            and not opening_attempt
            and "tactical_short_missed" not in tags
            and "tactical_long_missed" not in tags
        ):
            tags.append("hold_with_valid_thesis")
        if re.search(r"day_thesis", text_shortterm, re.IGNORECASE) and re.search(r"观望|继续等待|hold", text_all, re.IGNORECASE):
            pass
        elif re.search(r"Bitmine|whale|ETF|Long-term Narrative Tracking", text_all, re.IGNORECASE) and opening_attempt and day_bias == "neutral":
            tags.append("ignored_day_thesis")

        return sorted(set(tags))

    sorted_ops = sorted(operations, key=lambda item: item["timestamp"])
    start_cst = df["timestamp"].iloc[0] + CST_OFFSET
    end_cst = df["timestamp"].iloc[-1] + CST_OFFSET
    generated_at_cst = datetime.now()
    if not start_label:
        start_label = start_cst.strftime("%Y%m%d_%H%M%S")
    if not end_label:
        end_label = end_cst.strftime("%Y%m%d_%H%M%S")

    prev_experience = None
    prev_shortterm = None
    prev_shortterm_state = None
    prev_long_horizon = None
    op_idx = 0
    decision_records = build_decision_records(sorted_ops)
    error_events = collect_error_events(decision_records, sorted_ops)

    with open(output_file, "w", encoding="utf-8") as handle:
        handle.write("=" * 80 + "\n")
        handle.write(f" BACKTEST REPORT: {SYMBOL}\n")
        handle.write(f" Time Range (CST): {start_cst} to {end_cst}\n")
        handle.write(f" Generated at (CST): {generated_at_cst.strftime('%Y-%m-%d %H:%M:%S')}\n")
        handle.write("=" * 80 + "\n\n")
        handle.write("# 行情与决策\n\n")

        for _, row in df.iterrows():
            k_start_utc = row["timestamp"]
            k_end_utc = k_start_utc + timedelta(minutes=15)
            k_start_cst = k_start_utc + timedelta(hours=8)

            while op_idx < len(sorted_ops) and sorted_ops[op_idx]["timestamp"] < k_start_utc:
                op_idx += 1

            current_window_ops = []
            while op_idx < len(sorted_ops) and k_start_utc <= sorted_ops[op_idx]["timestamp"] < k_end_utc:
                current_window_ops.append(sorted_ops[op_idx])
                op_idx += 1

            handle.write(
                f"[{k_start_cst.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"O: {row['open']:.2f} | H: {row['high']:.2f} | "
                f"L: {row['low']:.2f} | C: {row['close']:.2f} | V: {row['volume']:.1f}"
            )
            if current_window_ops:
                ind = current_window_ops[0].get("indicators", {})
                if ind:
                    handle.write(
                        f" | RSI: {ind.get('rsi')} | MACD: {ind.get('macd')}({ind.get('histo')})"
                        f" | BB: {ind.get('lower')}-{ind.get('upper')} | KDJ: {ind.get('k')}/{ind.get('d')}/{ind.get('j')}"
                        f" | CCI: {ind.get('cci')} | WR: {ind.get('wr')} | OBV: {ind.get('obv')}"
                    )
            handle.write("\n")

            from itertools import groupby

            for timestamp_utc, group in groupby(current_window_ops, key=lambda item: item["timestamp"]):
                ops_list = list(group)
                first_op = ops_list[0]
                timestamp_cst = timestamp_utc + CST_OFFSET
                tags = classify_decision(row, ops_list)
                current_long_memory = first_op.get("long_memory_snapshot")
                if current_long_memory in (None, ""):
                    current_long_memory = first_op.get("experience", "")
                current_short_memory = first_op.get("short_memory_snapshot")
                if current_short_memory in (None, ""):
                    current_short_memory = first_op.get("shortterm", "")

                experience_diff = summarize_memory_diff(prev_experience, current_long_memory)
                shortterm_diff = summarize_memory_diff(prev_shortterm, current_short_memory)
                current_shortterm_state = infer_shortterm_state(current_short_memory)

                handle.write(f"    --- LLM DECISION @ {timestamp_cst.strftime('%H:%M:%S')} ---\n")
                handle.write(f"    [Execution]: {first_op.get('execution_txt', 'N/A')}\n")
                handle.write(f"    [Explanation]: {first_op.get('explanation', 'N/A')}\n")
                structured_bits = []
                if first_op.get("action_intent"):
                    structured_bits.append(f"action_intent={first_op.get('action_intent')}")
                if first_op.get("plan_transition"):
                    structured_bits.append(f"plan_transition={first_op.get('plan_transition')}")
                if first_op.get("hypothesis_action"):
                    structured_bits.append(f"hypothesis_action={first_op.get('hypothesis_action')}")
                if first_op.get("declared_entry_plan_direction"):
                    structured_bits.append(f"declared_entry_plan_direction={first_op.get('declared_entry_plan_direction')}")
                if first_op.get("declared_hypothesis_status"):
                    structured_bits.append(f"declared_hypothesis_status={first_op.get('declared_hypothesis_status')}")
                if structured_bits:
                    handle.write(f"    [Structured Decision]: {' | '.join(structured_bits)}\n")
                if first_op.get("tool_intents"):
                    handle.write(f"    [Tool Intents]: {shorten_blob(json.dumps(first_op.get('tool_intents', []), ensure_ascii=False), 260)}\n")
                if first_op.get("execution_rationale"):
                    handle.write(f"    [Execution Rationale]: {first_op.get('execution_rationale', 'N/A')}\n")
                if first_op.get("hypothesis_action_reason"):
                    handle.write(f"    [Hypothesis Action Reason]: {first_op.get('hypothesis_action_reason', 'N/A')}\n")
                if first_op.get("decision_basis"):
                    handle.write(f"    [Decision Basis]: {first_op.get('decision_basis', 'N/A')}\n")
                if first_op.get("conflict_check"):
                    handle.write(f"    [Conflict Check]: {first_op.get('conflict_check', 'N/A')}\n")
                if first_op.get("state_change_evidence"):
                    handle.write(f"    [State Change Evidence]: {first_op.get('state_change_evidence', 'N/A')}\n")
                if first_op.get("falsification_point"):
                    handle.write(f"    [Falsification Point]: {first_op.get('falsification_point', 'N/A')}\n")
                if first_op.get("next_alarm_reason"):
                    handle.write(f"    [Next Alarm Reason]: {first_op.get('next_alarm_reason', 'N/A')}\n")
                stage_flow = first_op.get("stage_flow", [])
                handle.write("    [LangChain Flow]: ")
                if stage_flow:
                    handle.write(" -> ".join(str(item) for item in stage_flow) + "\n")
                else:
                    handle.write("unknown\n")
                handle.write("    [Local Audit Trace]:\n")
                handle.write(f"      - log_folder: {first_op.get('folder', 'unknown')}\n")
                handle.write(f"      - validation_status: {first_op.get('validation_status', 'unknown')}\n")
                handle.write(f"      - reanswer_count: {first_op.get('reanswer_count', 0)}\n")
                handle.write(f"      - triggered_rules_first_fail: {first_op.get('triggered_rules_first_fail', 'none') or 'none'}\n")
                handle.write(f"      - triggered_rules_all_fail: {first_op.get('triggered_rules_all_fail', 'none') or 'none'}\n")
                handle.write(f"      - triggered_rules_final: {first_op.get('triggered_rules_final', 'none') or 'none'}\n")
                handle.write(f"      - resolved_rules: {first_op.get('resolved_rules', 'none') or 'none'}\n")
                if first_op.get("langsmith_trace_id"):
                    handle.write("    [LangSmith Trace (Supplemental)]:\n")
                    handle.write("      - source: output.json metadata only\n")
                    handle.write(f"      - project: {first_op.get('langsmith_project', '') or 'unknown'}\n")
                    handle.write(f"      - thread_id: {first_op.get('langsmith_thread_id', '') or 'unknown'}\n")
                    handle.write(f"      - trace_id: {first_op.get('langsmith_trace_id', '')}\n")
                handle.write("    [Output.json Reconstruction]:\n")
                handle.write(f"      - approved_contract_mode: {first_op.get('approved_decision_contract_mode', 'unknown') or 'unknown'}\n")
                handle.write(f"      - execute_agent_mode: {first_op.get('execute_agent_mode', 'unknown') or 'unknown'}\n")
                handle.write(f"      - retry_mode_effective: {first_op.get('retry_mode_effective', 'none') or 'none'}\n")
                repairs = first_op.get("deterministic_guard_repairs", [])
                handle.write(
                    f"      - deterministic_guard_repairs: {', '.join(str(x) for x in repairs) if repairs else 'none'}\n"
                )
                validation_failures_all = first_op.get("decision_validation_failures_all", [])
                if validation_failures_all:
                    handle.write("    [Detected Validation Issues]:\n")
                    for item in validation_failures_all[:8]:
                        if not isinstance(item, dict):
                            continue
                        handle.write(
                            f"      - error={item.get('error_class', 'unknown')} "
                            f"| why={shorten_blob(item.get('why_rejected', ''), 220)} "
                            f"| fix={shorten_blob(item.get('model_fix_hint', ''), 180)}\n"
                        )
                handle.write(
                    f"    [Approved Contract Draft Raw]: {shorten_blob(first_op.get('approved_decision_contract_raw_text', ''), 500) or 'N/A'}\n"
                )
                handle.write(f"    [Proposal Output]: {shorten_blob(first_op.get('proposal_text', ''), 400) or 'N/A'}\n")
                execute_output = []
                execute_agent_mode = first_op.get("execute_agent_mode", "")
                if execute_agent_mode:
                    execute_output.append(f"mode={execute_agent_mode}")
                if first_op.get("execute_passes"):
                    execute_output.append(
                        "passes="
                        + ", ".join(
                            f"{item.get('stage', 'unknown')}[{','.join(item.get('tool_names', [])) or 'no_tools'}]"
                            for item in first_op.get("execute_passes", [])
                        )
                    )
                execute_output.append(shorten_blob(first_op.get("execute_raw_text", ""), 400) or "N/A")
                handle.write(f"    [Execute Output]: {' | '.join(execute_output)}\n")
                attempts = first_op.get("attempts", [])
                if attempts:
                    handle.write("    [Attempt Timeline]:\n")
                    for attempt in attempts:
                        if not isinstance(attempt, dict):
                            continue
                        attempt_stage = attempt.get("stage", "unknown")
                        attempt_idx = attempt.get("attempt_index", 0)
                        attempt_tools = ",".join(attempt.get("tool_names", [])) or "no_tools"
                        attempt_mode = attempt.get("retry_mode", "") or "normal"
                        failure = attempt.get("decision_validation_failure")
                        if isinstance(failure, dict):
                            failure_txt = failure.get("error_class", "unknown")
                        else:
                            failure_txt = "none"
                        handle.write(
                            f"      - attempt={attempt_idx} stage={attempt_stage} mode={attempt_mode} "
                            f"tools=[{attempt_tools}] decision_guard={failure_txt}\n"
                        )
                        if isinstance(failure, dict):
                            handle.write(
                                f"        why_rejected: {shorten_blob(failure.get('why_rejected', ''), 220)}\n"
                            )
                            handle.write(
                                f"        fix_hint: {shorten_blob(failure.get('model_fix_hint', ''), 180)}\n"
                            )
                execute_passes = first_op.get("execute_passes", [])
                if execute_passes:
                    handle.write("    [Execute Pass Reconstruction]:\n")
                    for item in execute_passes:
                        if not isinstance(item, dict):
                            continue
                        pass_tools = ",".join(item.get("tool_names", [])) or "no_tools"
                        refresh_required = "yes" if item.get("refresh_required") else "no"
                        handle.write(
                            f"      - stage={item.get('stage', 'unknown')} mode={item.get('agent_mode', 'unknown')} "
                            f"tools=[{pass_tools}] refresh_required={refresh_required}\n"
                        )
                        handle.write(
                            f"        raw: {shorten_blob(item.get('raw_text_excerpt', ''), 320) or 'N/A'}\n"
                        )
                guard_failures = first_op.get("guard_failures", [])
                if guard_failures:
                    handle.write("    [Guard Failures]:\n")
                    for item in guard_failures:
                        if not isinstance(item, dict):
                            continue
                        handle.write(
                            f"      - attempt={item.get('attempt_index', 0)} "
                            f"rule={item.get('error_class', item.get('rule_id', 'unknown'))} "
                            f"reason={shorten_blob(item.get('why_rejected', ''), 180)}\n"
                        )
                if first_op.get("last_guard_feedback"):
                    handle.write(
                        f"    [Last Guard Feedback]: {shorten_blob(first_op.get('last_guard_feedback', ''), 500)}\n"
                    )
                if first_op.get("unparsed_fields"):
                    handle.write(
                        f"    [Unparsed Fields]: {', '.join(str(x) for x in first_op.get('unparsed_fields', []))}\n"
                    )
                handle.write(f"    [Account Status]:\n{first_op.get('account_data', 'N/A')}\n")
                handle.write(f"    [Memory Reasoning]: {first_op.get('memory_management_reasoning', 'N/A')}\n")
                prev_long_horizon = render_long_context(handle, first_op, prev_long_horizon)
                render_memory_diff(handle, "Experience", experience_diff)
                render_memory_diff(handle, "Shortterm", shortterm_diff)
                render_memory_ops(handle, "Experience", first_op.get("long_memory_ops", []))
                render_memory_ops(handle, "Shortterm", first_op.get("short_memory_ops", []))

                if tags:
                    handle.write(f"    [Audit Tags]: {', '.join(tags)}\n")
                    for tag in tags:
                        audit_counts[tag] += 1

                if first_op.get("final_validation_errors"):
                    handle.write("    [Final Validation Errors]:\n")
                    for err in first_op["final_validation_errors"]:
                        handle.write(f"      - {err}\n")
                if first_op.get("validation_events"):
                    handle.write("    [Validation Events]:\n")
                    for event in first_op.get("validation_events", []):
                        if isinstance(event, dict):
                            tag = event.get("tag", "unknown")
                            reason = shorten_blob(event.get("reason", ""), 220)
                            tool = event.get("tool", "")
                            turn = event.get("turn", "")
                            suffix = []
                            if turn != "":
                                suffix.append(f"turn={turn}")
                            if tool:
                                suffix.append(f"tool={tool}")
                            meta = f" ({', '.join(suffix)})" if suffix else ""
                            handle.write(f"      - {tag}{meta}: {reason}\n")
                        else:
                            handle.write(f"      - {event}\n")

                handle.write("    [Tool Timeline]:\n")
                has_timeline = False
                for op in ops_list:
                    if op["tool"] == "NO_TRADE":
                        continue
                    has_timeline = True
                    handle.write(
                        f"        >>> stage={op.get('stage', 'unknown')} tool={op.get('tool', 'unknown')} "
                        f"result_status={op.get('result_status', 'unknown')} "
                        f"stage_source={op.get('stage_source', 'local_fallback')}\n"
                    )
                    handle.write(f"            Detail: {op.get('detail', 'UNKNOWN')}\n")
                    handle.write(f"            Args: {op.get('args', {})}\n")
                    tool_explanation = ""
                    if isinstance(op.get("args", {}), dict):
                        tool_explanation = str(op.get("args", {}).get("explanation", "") or "").strip()
                    if tool_explanation:
                        handle.write(f"            Explanation: {tool_explanation}\n")
                    handle.write(f"            Result Status: {op.get('result_status', 'unknown')}\n")
                    if op.get("recorded_at"):
                        handle.write(f"            Recorded At: {op.get('recorded_at')}\n")
                    try:
                        result_preview = shorten_blob(json.dumps(op.get("result", {}), ensure_ascii=False), 260)
                    except Exception:
                        result_preview = shorten_blob(str(op.get("result", {})), 260)
                    if result_preview:
                        handle.write(f"            Result Preview: {result_preview}\n")
                if not has_timeline:
                    handle.write("      - no tool calls recorded in this window\n")
                handle.write("\n")

                prev_experience = current_long_memory
                prev_shortterm = current_short_memory
                prev_shortterm_state = current_shortterm_state

            handle.write("-" * 40 + "\n")

        handle.write("\n# 被挡下 / API 报错记录\n\n")
        guard_events = [item for item in error_events if item.get("category", "").startswith("guard_")]
        resolved_guard_events = [item for item in guard_events if item.get("resolved_on_retry")]
        api_or_tool_events = [item for item in error_events if item.get("category") == "api_or_tool_error"]
        exchange_rejections = [item for item in error_events if item.get("category") == "exchange_rejection"]
        final_validation_events = [item for item in error_events if item.get("category") == "final_validation_error"]
        handle.write(f"- guard_blocked_total: {len(guard_events)}\n")
        handle.write(f"- guard_blocked_resolved_on_retry: {len(resolved_guard_events)}\n")
        handle.write(f"- api_or_tool_errors_total: {len(api_or_tool_events)}\n")
        handle.write(f"- exchange_rejections_total: {len(exchange_rejections)}\n")
        handle.write(f"- final_validation_error_total: {len(final_validation_events)}\n")
        handle.write(f"- pnl_completeness: {pnl_completeness['status']} ({pnl_completeness['reason']})\n")
        handle.write(f"- fee_completeness: {fee_completeness['status']} ({fee_completeness['reason']})\n")
        handle.write("\n")
        if not error_events:
            handle.write("无记录。\n")
        else:
            for event in error_events:
                handle.write(
                    f"- [{event.get('time_cst', 'unknown')}] category={event.get('category', 'unknown')} "
                    f"| source={event.get('rule_or_source', 'unknown')} | status={event.get('status', 'unknown')} "
                    f"| resolved_on_retry={str(event.get('resolved_on_retry', False)).lower()} "
                    f"| log_folder={event.get('log_folder', 'unknown')}\n"
                )
                if event.get("tool"):
                    handle.write(f"  tool: {event.get('tool')}\n")
                if event.get("args_preview"):
                    handle.write(f"  args: {event.get('args_preview')}\n")
                handle.write(f"  reason: {event.get('reason', 'N/A')}\n")
                handle.write(f"  decision_text_excerpt: {event.get('decision_text_excerpt', 'N/A')}\n")
                if event.get("repair_action"):
                    handle.write(f"  repair_action: {event.get('repair_action')}\n")
                handle.write("\n")

        handle.write("# Consistency State Timeline / Hypothesis Timeline\n\n")
        handle.write("## Consistency State Timeline\n")
        prev_state_entry = None
        if not decision_records:
            handle.write("- 无记录\n")
        else:
            for record in decision_records:
                state_entry = extract_consistency_timeline_entry(record, prev_state_entry)
                handle.write(
                    f"- [{state_entry['time']}] wakeup_role={state_entry['wakeup_role']} "
                    f"| intraday_mode={state_entry['intraday_mode']} | execution_mode={state_entry['execution_mode']} "
                    f"| trade_intent={state_entry['trade_intent']} | entry_plan_direction={state_entry['entry_plan_direction']} "
                    f"| cooldown_direction={state_entry['cooldown_direction']} | risk_state={state_entry['risk_state']} "
                    f"| change={state_entry['change']}\n"
                )
                prev_state_entry = state_entry

        handle.write("\n## Hypothesis Timeline\n")
        prev_hypothesis_entry = None
        if not decision_records:
            handle.write("- 无记录\n")
        else:
            for record in decision_records:
                hypothesis_entry = extract_hypothesis_timeline_entry(record, prev_hypothesis_entry)
                handle.write(
                    f"- [{hypothesis_entry['time']}] hypothesis_id={hypothesis_entry['hypothesis_id'] or 'none'} "
                    f"| direction={hypothesis_entry['direction']} | status={hypothesis_entry['status']} "
                    f"| expiry={hypothesis_entry['expiry'] or 'none'} "
                    f"| hypothesis_action={hypothesis_entry['hypothesis_action'] or 'none'} "
                    f"| change={hypothesis_entry['change']}\n"
                )
                prev_hypothesis_entry = hypothesis_entry

    print(f"Report exported successfully to {output_file}.")

if __name__ == "__main__":
    import argparse

    start_utc = None
    end_utc = None
    start_label = None
    end_label = None

    parser = argparse.ArgumentParser(
        description="Generate backtest report from logs with optional CST time range."
    )
    parser.add_argument("start", nargs="?", help="Start time in CST, format: YYYYMMDD_HHMMSS")
    parser.add_argument("end", nargs="?", help="End time in CST, format: YYYYMMDD_HHMMSS")
    args = parser.parse_args()

    if bool(args.start) ^ bool(args.end):
        raise ValueError("Both start and end must be provided together, or provide neither to use full logs range.")

    if args.start and args.end:
        start_label = args.start
        end_label = args.end
        pattern = r"^\d{8}_\d{6}$"
        if not re.match(pattern, start_label) or not re.match(pattern, end_label):
            raise ValueError(
                f"Invalid format. Expected YYYYMMDD_HHMMSS (e.g., 20260414_094755). Got: {start_label}, {end_label}"
            )
        start_local = datetime.strptime(start_label, "%Y%m%d_%H%M%S")
        end_local = datetime.strptime(end_label, "%Y%m%d_%H%M%S")
        if start_local > end_local:
            raise ValueError(f"Order error: Start time ({start_label}) must be before or equal to end time ({end_label})")
        start_utc = start_local - CST_OFFSET
        end_utc = end_local - CST_OFFSET
        print(f"Using manual range: {start_label} to {end_label} (CST)")
    else:
        print("No time range provided. Using full logs range from /home/coinautomation/logs ...")
        start_utc, end_utc, start_label, end_label = get_all_logs_range(LOG_DIR)

    if not start_utc:
        print("No logs found. Cannot generate report.")
    else:
        df = get_klines(SYMBOL, INTERVAL, start_utc, end_utc)
        if df.empty:
            print("No market data found for the given range.")
        else:
            ops, wakeups, llm_calls = parse_logs(LOG_DIR, start_utc, end_utc)
            print("\nAnalysis Summary:")
            print(f"- Time Range: {start_utc + CST_OFFSET} to {end_utc + CST_OFFSET} (CST)")
            print(f"- Total Logs Processed: {len(llm_calls)}")
            print(f"- Total Trading Operations: {len([o for o in ops if o['tool'] != 'NO_TRADE'])}")
            plot_backtest_with_stats(df, ops, wakeups, llm_calls, output_html=os.path.join(BASE_DIR, "backtest_report.html"))
            export_to_text(df, ops, start_label=start_label, end_label=end_label)
