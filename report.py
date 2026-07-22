from env_config import load_project_env
load_project_env()

import os
import json
import re
import hashlib
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, jsonify, request
from binance.client import Client
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

# --- CONFIGURATION ---
SYMBOL = "ETHUSDT"
INTERVAL = Client.KLINE_INTERVAL_15MINUTE
LOG_DIR = "/home/coinautomation/logs"
CACHE_DIR = "/home/coinautomation/.cache"
CACHE_VERSION = "oco_v12"

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

app = Flask(__name__)

# --- CACHE HELPERS ---
def get_cache_path(category, key):
    """Generate a file path for a cache entry."""
    hash_key = hashlib.md5(str(key).encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{category}_{hash_key}.json")

def load_from_file_cache(category, key):
    path = get_cache_path(category, key)
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return None

def save_to_file_cache(category, key, data):
    path = get_cache_path(category, key)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except:
        pass

# --- HELPER FUNCTIONS ---

def get_klines(symbol, interval, start_time, end_time):
    """Fetch K-line data from Binance for a specific range."""
    # To calculate indicators like MA99, we need some historical data before start_time
    # We fetch extra 150 periods before start_time
    if interval == Client.KLINE_INTERVAL_15MINUTE:
        buffer_time = timedelta(minutes=15 * 150)
    elif interval == Client.KLINE_INTERVAL_1HOUR:
        buffer_time = timedelta(hours=150)
    elif interval == Client.KLINE_INTERVAL_4HOUR:
        buffer_time = timedelta(hours=4 * 150)
    else:
        buffer_time = timedelta(days=150)
        
    start_ts = int((start_time - buffer_time).timestamp() * 1000)
    end_ts = int(end_time.timestamp() * 1000)

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

    # Deduplicate on open-time while preserving order.
    deduped = {}
    for row in all_klines:
        deduped[int(row[0])] = row
    klines = [deduped[k] for k in sorted(deduped.keys())]

    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_asset_volume', 'number_of_trades',
        'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = df[col].astype(float)
    
    return df

def add_indicators(df):
    """Add technical indicators to the dataframe (logic from Binance.py)"""
    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    # 1. Moving Averages
    df['ma7'] = close.rolling(7).mean()
    df['ma25'] = close.rolling(25).mean()
    df['ma99'] = close.rolling(99).mean()
    
    # 2. RSI (Wilder's Smoothing)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # rsi6 and rsi14
    df['rsi6'] = 100 - (100 / (1 + gain.ewm(alpha=1/6, adjust=False).mean() / loss.ewm(alpha=1/6, adjust=False).mean().replace(0, np.nan))).fillna(100)
    df['rsi'] = 100 - (100 / (1 + gain.ewm(alpha=1/14, adjust=False).mean() / loss.ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan))).fillna(100)

    # 3. MACD
    df['ema12'] = close.ewm(span=12, adjust=False).mean()
    df['ema26'] = close.ewm(span=26, adjust=False).mean()
    df['macd'] = df['ema12'] - df['ema26']
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['histo'] = df['macd'] - df['signal']

    # 4. Bollinger Bands
    df['mid'] = close.rolling(20).mean()
    std = close.rolling(20).std()
    df['upper'] = df['mid'] + 2 * std
    df['lower'] = df['mid'] - 2 * std

    # 5. KDJ
    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    df['rsv'] = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    df['k'] = df['rsv'].ewm(span=3, adjust=False).mean()
    df['d'] = df['k'].ewm(span=3, adjust=False).mean()
    df['j'] = 3 * df['k'] - 2 * df['d']

    # 6. CCI
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(20).mean()
    mad = (tp - sma_tp).abs().rolling(20).mean()
    df['cci'] = (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))

    # 7. WR
    df['wr'] = (high9 - close) / (high9 - low9).replace(0, np.nan) * (-100)

    # 8. OBV
    df['obv'] = (np.sign(close.diff()) * volume).fillna(0).cumsum()

    return df

def parse_event_types(event_type_str):
    """Parse event type string like 'heartbeat & whale' into normalized labels."""
    raw = (event_type_str or "").strip()
    if not raw:
        return ["unknown"]

    parts = re.split(r"\s*(?:&|,|/|\+| and )\s*", raw, flags=re.IGNORECASE)
    labels = []
    for part in parts:
        label = re.sub(r"\s+", " ", part.strip().lower())
        if label and label not in labels:
            labels.append(label)
    return labels if labels else ["unknown"]

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
            try:
                return json.loads(stripped)
            except Exception:
                return stripped
        return stripped
    return str(value)

def pretty_memory_text(snapshot, fallback=""):
    normalized = normalize_memory_snapshot(snapshot)
    if isinstance(normalized, (dict, list)):
        return json.dumps(normalized, ensure_ascii=False, indent=2)
    return str(normalized or fallback or "")

def _extract_user_prompt_block(input_md: str) -> str:
    if not input_md:
        return ""
    marker = "--- USER PROMPT ---"
    if marker not in input_md:
        return input_md
    return input_md.split(marker, 1)[1]

def _extract_event_type_and_list(content_md: str, fallback_type: str = "unknown"):
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
        event_type_list = parse_event_types(event_type_str)

    deduped = []
    for item in event_type_list:
        normalized = re.sub(r"\s+", " ", str(item).strip().lower())
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return event_type_str, (deduped or ["unknown"])

def _load_context_json(folder_path, filename):
    path = os.path.join(folder_path, "context", filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None

def load_structured_long_context(folder_path):
    raw_inputs = _load_context_json(folder_path, "raw_long_inputs.json")
    kept_inputs = _load_context_json(folder_path, "kept_long_inputs.json")
    dropped_inputs = _load_context_json(folder_path, "dropped_long_inputs.json")
    long_view = _load_context_json(folder_path, "long_horizon_view.json")
    long_reflection = _load_context_json(folder_path, "long_reflection.json")

    raw_count = 0
    if isinstance(raw_inputs, dict):
        raw_count += len(raw_inputs.get("news_items", [])) if isinstance(raw_inputs.get("news_items", []), list) else 0
        raw_count += len(raw_inputs.get("polymarket_items", [])) if isinstance(raw_inputs.get("polymarket_items", []), list) else 0
    kept_count = len(kept_inputs.get("items", [])) if isinstance(kept_inputs, dict) and isinstance(kept_inputs.get("items", []), list) else 0
    dropped_count = len(dropped_inputs.get("items", [])) if isinstance(dropped_inputs, dict) and isinstance(dropped_inputs.get("items", []), list) else 0

    return {
        "long_inputs_raw": raw_inputs if isinstance(raw_inputs, dict) else None,
        "long_inputs_kept": kept_inputs if isinstance(kept_inputs, dict) else None,
        "long_inputs_dropped": dropped_inputs if isinstance(dropped_inputs, dict) else None,
        "long_horizon_view": long_view if isinstance(long_view, dict) else None,
        "long_reflection": long_reflection if isinstance(long_reflection, dict) else None,
        "long_gating_stats": {
            "raw_count": raw_count,
            "kept_count": kept_count,
            "dropped_count": dropped_count,
            "has_structured_context": any(isinstance(item, dict) for item in [raw_inputs, kept_inputs, dropped_inputs, long_view, long_reflection]),
        },
    }

def parse_whale_transfer_amounts(content_md):
    """Extract whale transfer USD amounts from input markdown text."""
    if not content_md:
        return []

    amounts = []
    patterns = [
        r"A total of\s+([0-9][0-9,]*(?:\.[0-9]+)?)\s+USD worth of .*? was moved",
        r"Total transferred value:\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content_md, flags=re.IGNORECASE):
            raw_num = match.group(1).replace(",", "")
            try:
                value = float(raw_num)
                if value > 0:
                    amounts.append(value)
            except:
                continue
    return amounts

def format_usd_short(value):
    """Format USD value to compact text for histogram labels."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"

def build_event_and_whale_stats(operations):
    """Build event frequency and whale transfer value distribution."""
    event_counter = {}
    whale_values = []

    for op in operations:
        event_types = op.get("event_types") or parse_event_types(op.get("event_type", "unknown"))
        for event in event_types:
            event_counter[event] = event_counter.get(event, 0) + 1

        whale_values.extend(op.get("whale_transfer_usd", []))

    event_frequency = [
        {"event_type": event, "count": count}
        for event, count in sorted(event_counter.items(), key=lambda x: (-x[1], x[0]))
    ]

    whale_distribution = []
    if whale_values:
        vmin = min(whale_values)
        vmax = max(whale_values)
        if abs(vmax - vmin) < 1e-9:
            whale_distribution.append({
                "range_label": format_usd_short(vmin),
                "range_start": vmin,
                "range_end": vmax,
                "count": len(whale_values)
            })
        else:
            # Fixed 1M USD bins for finer whale transfer distribution.
            bin_size = 1_000_000.0
            start_edge = np.floor(vmin / bin_size) * bin_size
            end_edge = np.ceil(vmax / bin_size) * bin_size
            if end_edge <= start_edge:
                end_edge = start_edge + bin_size

            edges = np.arange(start_edge, end_edge + bin_size, bin_size)
            hist_counts, hist_edges = np.histogram(whale_values, bins=edges)
            for idx, count in enumerate(hist_counts):
                whale_distribution.append({
                    "range_label": f"{int(hist_edges[idx] / 1_000_000)}M-{int(hist_edges[idx + 1] / 1_000_000)}M",
                    "range_start": float(hist_edges[idx]),
                    "range_end": float(hist_edges[idx + 1]),
                    "count": int(count)
                })

    return event_frequency, whale_distribution, len(whale_values)

def build_news_gated_records(operations):
    """Aggregate long_reflection.news_case_log records across operations."""
    records = []
    seen = set()

    for op in operations:
        reflection = op.get("long_reflection")
        if not isinstance(reflection, dict):
            continue
        case_log = reflection.get("news_case_log", [])
        if not isinstance(case_log, list):
            continue

        for item in case_log:
            if not isinstance(item, dict):
                continue

            record = {
                "id": str(item.get("id", "") or ""),
                "source_type": str(item.get("source_type", "") or "unknown"),
                "source_id": str(item.get("source_id", "") or ""),
                "headline_or_question": str(item.get("headline_or_question", "") or ""),
                "event_time": str(item.get("event_time", "") or ""),
                "recorded_at": str(item.get("recorded_at", "") or ""),
                "gate_decision": str(item.get("gate_decision", "") or "unknown"),
                "why": str(item.get("why", "") or ""),
                "impact_bias": str(item.get("impact_bias", "") or "unknown"),
                "price_context_at_ingestion": item.get("price_context_at_ingestion", {}),
                "linked_hypothesis_ids": item.get("linked_hypothesis_ids", []) if isinstance(item.get("linked_hypothesis_ids", []), list) else [],
                "log_folder": str(op.get("log_folder", "") or ""),
            }

            dedupe_key = (record["id"], record["recorded_at"])
            if not record["id"] and not record["recorded_at"]:
                dedupe_key = (
                    record["headline_or_question"],
                    record["source_id"],
                    record["log_folder"],
                )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            records.append(record)

    def sort_key(item):
        return (
            str(item.get("recorded_at", "") or ""),
            str(item.get("event_time", "") or ""),
            str(item.get("log_folder", "") or ""),
            str(item.get("id", "") or ""),
        )

    return sorted(records, key=sort_key, reverse=True)

def get_window_size_seconds(interval_req: str) -> int:
    if interval_req == "1h":
        return 3600
    if interval_req == "4h":
        return 14400
    if interval_req == "1d":
        return 86400
    return 900

def build_operations_lite(operations, interval_req: str):
    window_size = get_window_size_seconds(interval_req)
    lite_ops = []
    for op in operations:
        ts = int(op.get("timestamp_display", 0) or 0)
        lite_ops.append({
            "log_folder": op.get("log_folder", ""),
            "timestamp_display": ts,
            "window_ts": (ts // window_size) * window_size if ts > 0 else 0,
            "local_time": op.get("local_time", ""),
            "event_type": op.get("event_type", "unknown"),
            "event_types": op.get("event_types", ["unknown"]),
            "trades": op.get("trades", []),
            "protection_orders": op.get("protection_orders", {}),
        })
    return lite_ops

def _safe_float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def build_range_plan_overlays(operations):
    """Build historical range-plan segments that had real exchange execution evidence."""
    overlays = []
    active = None

    def _normalize_levels(raw_levels, bucket: str):
        normalized = []
        if not isinstance(raw_levels, list):
            return normalized
        for item in raw_levels:
            if not isinstance(item, dict):
                continue
            entry = _safe_float_or_none(item.get("entry_price"))
            exit_price = _safe_float_or_none(item.get("exit_price"))
            qty = _safe_float_or_none(item.get("quantity"))
            if entry is not None:
                normalized.append(
                    {
                        "bucket": bucket,
                        "role": "entry",
                        "price": entry,
                        "quantity": qty,
                    }
                )
            if exit_price is not None:
                normalized.append(
                    {
                        "bucket": bucket,
                        "role": "exit",
                        "price": exit_price,
                        "quantity": qty,
                    }
                )
        return normalized

    def _extract_plan_snapshot(result, args):
        if not isinstance(result, dict):
            return {}
        plan = result.get("plan", {})
        if isinstance(plan, dict):
            return plan
        fallback = {}
        if isinstance(args, dict):
            fallback.update(args)
        if isinstance(result, dict):
            fallback.update(result)
        return fallback

    def _has_range_execution_evidence(plan):
        if not isinstance(plan, dict):
            return False

        audit = plan.get("audit", {})
        if isinstance(audit, dict):
            recent_order_ids = audit.get("recent_order_ids", [])
            if isinstance(recent_order_ids, list) and any(str(item or "").strip() for item in recent_order_ids):
                return True

        breakout_watch = plan.get("breakout_watch", {})
        if isinstance(breakout_watch, dict):
            canceled_entry_order_ids = breakout_watch.get("canceled_entry_order_ids", [])
            if isinstance(canceled_entry_order_ids, list) and any(str(item or "").strip() for item in canceled_entry_order_ids):
                return True

        for level_key in ("long_levels", "short_levels"):
            levels = plan.get(level_key, [])
            if not isinstance(levels, list):
                continue
            for level in levels:
                if not isinstance(level, dict):
                    continue
                if str(level.get("entry_order_id", "") or "").strip():
                    return True
                if str(level.get("exit_order_id", "") or "").strip():
                    return True
                filled_qty = _safe_float_or_none(level.get("filled_qty"))
                if filled_qty is not None and abs(filled_qty) > 0:
                    return True
                completed_cycles = _safe_float_or_none(level.get("completed_cycles"))
                if completed_cycles is not None and completed_cycles > 0:
                    return True
                if str(level.get("last_entry_at", "") or "").strip():
                    return True
                if str(level.get("last_exit_at", "") or "").strip():
                    return True
                if str(level.get("entry_status", "") or "").strip().lower() == "filled":
                    return True
                if str(level.get("exit_status", "") or "").strip().lower() in {"pending", "filled"}:
                    return True
        return False

    def _close_active(end_ts: int, reason: str):
        nonlocal active
        if active is None:
            return
        if active.get("start_ts") is not None:
            active["end_ts"] = end_ts
            active["end_reason"] = reason
            overlays.append(active)
        active = None

    def _upsert_active(plan, op, op_ts: int):
        nonlocal active
        if not isinstance(plan, dict):
            return

        lower = _safe_float_or_none(plan.get("lower_breakout"))
        upper = _safe_float_or_none(plan.get("upper_breakout"))
        if lower is None or upper is None or upper <= lower:
            return

        evidence = _has_range_execution_evidence(plan)
        if active is None:
            active = {
                "status": "active",
                "symbol": str(plan.get("symbol", "ETHUSDT") or "ETHUSDT"),
                "mode": str(plan.get("mode", "range_automation") or "range_automation"),
                "execution_style": str(plan.get("execution_style", "") or ""),
                "position_mode": str(plan.get("position_mode", "") or ""),
                "lower_breakout": lower,
                "upper_breakout": upper,
                "long_level_count": len(plan.get("long_levels", [])) if isinstance(plan.get("long_levels", []), list) else 0,
                "short_level_count": len(plan.get("short_levels", [])) if isinstance(plan.get("short_levels", []), list) else 0,
                "levels": (
                    _normalize_levels(plan.get("long_levels"), "long")
                    + _normalize_levels(plan.get("short_levels"), "short")
                ),
                "expires_at": str(plan.get("expires_at", "") or ""),
                "start_ts": op_ts if evidence else None,
                "end_ts": None,
                "start_folder": str(op.get("log_folder", "") or ""),
                "end_reason": "",
            }
            return

        active["status"] = "active"
        active["symbol"] = str(plan.get("symbol", active.get("symbol", "ETHUSDT")) or "ETHUSDT")
        active["mode"] = str(plan.get("mode", active.get("mode", "range_automation")) or "range_automation")
        active["execution_style"] = str(plan.get("execution_style", active.get("execution_style", "")) or "")
        active["position_mode"] = str(plan.get("position_mode", active.get("position_mode", "")) or "")
        active["lower_breakout"] = lower
        active["upper_breakout"] = upper
        active["long_level_count"] = len(plan.get("long_levels", [])) if isinstance(plan.get("long_levels", []), list) else 0
        active["short_level_count"] = len(plan.get("short_levels", [])) if isinstance(plan.get("short_levels", []), list) else 0
        active["levels"] = _normalize_levels(plan.get("long_levels"), "long") + _normalize_levels(plan.get("short_levels"), "short")
        active["expires_at"] = str(plan.get("expires_at", active.get("expires_at", "")) or "")
        if active.get("start_ts") is None and evidence:
            active["start_ts"] = op_ts
            active["start_folder"] = str(op.get("log_folder", "") or "")

    ordered = sorted(
        operations,
        key=lambda op: int(op.get("timestamp_display", 0) or 0),
    )

    for op in ordered:
        op_ts = int(op.get("timestamp_display", 0) or 0)
        if op_ts <= 0:
            continue
        for trade in op.get("range_plan_calls", []):
            tool = str(trade.get("tool", "") or "").strip()
            if tool not in {"set_range_plan", "cancel_range_plan", "get_range_plan"}:
                continue

            args = trade.get("args", {}) if isinstance(trade.get("args", {}), dict) else {}
            result = trade.get("result", {})
            if not isinstance(result, dict):
                continue
            status = str(result.get("status", "") or "").strip().lower()
            plan = _extract_plan_snapshot(result, args)
            plan_status = str(plan.get("status", status) or status).strip().lower()

            if tool == "set_range_plan":
                if status != "success":
                    continue
                _close_active(op_ts, "replaced")
                _upsert_active(plan, op, op_ts)
                continue

            if tool == "get_range_plan":
                if plan_status == "active" or status == "active":
                    _upsert_active(plan, op, op_ts)
                elif active is not None and status in {"inactive", "expired", "stopped", "canceled", "cancelled"}:
                    _close_active(op_ts, status)
                continue

            if tool == "cancel_range_plan" and status in {"success", "skipped"}:
                if active is not None:
                    _upsert_active(plan, op, op_ts)
                _close_active(op_ts, "canceled")

    if active is not None:
        if active.get("start_ts") is not None:
            overlays.append(active)

    return overlays

def get_all_logs_range(log_dir):
    """Scan logs directory to find the total time range."""
    if not os.path.exists(log_dir):
        return None, None
    
    all_entries = os.listdir(log_dir)
    folders = [f for f in all_entries if os.path.isdir(os.path.join(log_dir, f)) and re.match(r'^\d{8}_\d{6}$', f)]
    if not folders:
        return None, None
    
    folders.sort()
    
    try:
        start_local = datetime.strptime(folders[0], "%Y%m%d_%H%M%S")
        end_local = datetime.strptime(folders[-1], "%Y%m%d_%H%M%S")
        
        # 关键：由于系统本身就在 CST 时区，我们需要明确地将其标记为 CST (UTC+8) 
        # 然后转换为 UTC，以避免 naive datetime 在不同系统下的歧义。
        tz_cst = timezone(timedelta(hours=8))
        start_utc = start_local.replace(tzinfo=tz_cst).astimezone(timezone.utc)
        end_utc = end_local.replace(tzinfo=tz_cst).astimezone(timezone.utc)
        
        # 返回的是带时区信息的 datetime 对象
        return start_utc - timedelta(hours=1), end_utc + timedelta(hours=1)
    except Exception as e:
        return None, None

def parse_logs(log_dir, start_time, end_time):
    """Scan logs directory for all records within the calculated range."""
    operations = []
    
    ORDER_TOOLS = {
        "trade_coin_futures": "T",
        "trade_usdt_futures": "T",
        "close_coin_futures_position": "C",
        "close_usdt_futures_position": "C",
        "cancel_all_coin_futures_orders": "CAN",
        "cancel_all_usdt_futures_orders": "CAN",
        "cancel_coin_futures_order": "CAN",
        "cancel_usdt_futures_order": "CAN"
    }

    if not os.path.exists(log_dir):
        return operations

    folders = [f for f in os.listdir(log_dir) if os.path.isdir(os.path.join(log_dir, f)) and re.match(r'^\d{8}_\d{6}$', f)]
    folders.sort()

    for folder in folders:
        try:
            # folder 格式: 20260416_071856 (这是 CST 时间)
            try:
                naive_log_time = datetime.strptime(folder, "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            
            from datetime import timezone
            log_time_display_ts = int(naive_log_time.replace(tzinfo=timezone.utc).timestamp())
            tz_cst = timezone(timedelta(hours=8))
            log_time_utc = naive_log_time.replace(tzinfo=tz_cst).astimezone(timezone.utc)
            
            if start_time <= log_time_utc <= end_time:
                folder_path = os.path.join(log_dir, folder)
                output_path = os.path.join(folder_path, "output.json")
                input_path = os.path.join(folder_path, "input.md")
                
                if not os.path.exists(output_path):
                    continue
                
                with open(output_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                content = data.get("content", {})
                if not isinstance(content, dict):
                    content = {"execution_txt": str(content)}
                short_memory_snapshot = content.get("short_memory_snapshot")
                long_memory_snapshot = content.get("long_memory_snapshot")
                short_memory_ops = content.get("short_memory_ops", [])
                long_memory_ops = content.get("long_memory_ops", [])
                tool_calls = data.get("tool_calls", [])
                if not isinstance(tool_calls, list):
                    tool_calls = []
                audit_meta = data.get("audit_meta", {})
                if not isinstance(audit_meta, dict):
                    audit_meta = {}
                long_context = load_structured_long_context(folder_path)

                indicators = {}
                event_type_str = str(audit_meta.get("event_type", "unknown") or "unknown")
                event_types = ["unknown"]
                whale_transfer_usd = []
                content_md = ""
                if os.path.exists(input_path):
                    with open(input_path, 'r', encoding='utf-8') as f:
                        content_md = _extract_user_prompt_block(f.read())

                if content_md:
                    m = re.search(
                        r"## 15 minutes chart.*?\| Timestamp \|.*?\| :--- \|.*?\n(?:\| .*? \|\n)*?(\| .*? \|)\n",
                        content_md,
                        re.DOTALL,
                    )
                    if m:
                        last_row = [x.strip() for x in m.group(1).strip("|").split("|")]
                        if len(last_row) >= 16:
                            indicators = {
                                "rsi": last_row[5], "macd": last_row[6], "histo": last_row[7],
                                "upper": last_row[8], "lower": last_row[9], "k": last_row[10],
                                "d": last_row[11], "j": last_row[12], "cci": last_row[13],
                                "wr": last_row[14], "obv": last_row[15]
                            }

                    whale_transfer_usd = parse_whale_transfer_amounts(content_md)
                    event_type_str, event_types = _extract_event_type_and_list(content_md, fallback_type=event_type_str)
                else:
                    event_type_str, event_types = _extract_event_type_and_list("", fallback_type=event_type_str)

                folder_op_info = {
                    "log_folder": folder,
                    "timestamp_display": log_time_display_ts,
                    "local_time": naive_log_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "event_type": event_type_str,
                    "event_types": event_types,
                    "whale_transfer_usd": whale_transfer_usd,
                    "explanation": content.get("explanation", "N/A"),
                    "execution_txt": content.get("execution_txt", "N/A"),
                    "memory_reasoning": content.get("memory_management_reasoning", "N/A"),
                    "experience": pretty_memory_text(long_memory_snapshot, content.get("experience", "N/A")),
                    "shortterm": pretty_memory_text(short_memory_snapshot, content.get("shortterm", "N/A")),
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
                    "counter_long_horizon_bias": bool(audit_meta.get("counter_long_horizon_bias", False)),
                    "long_horizon_status_at_trade": str(audit_meta.get("long_horizon_status_at_trade", "") or ""),
                    "indicators": indicators,
                    "trades": [],
                    "range_plan_calls": [],
                    "protection_orders": {
                        "tp": None,
                        "sl": None,
                        "tp_updated": False,
                        "sl_updated": False,
                        "tp_order_id": None,
                        "sl_order_id": None
                    }
                }

                for call in tool_calls:
                    tool_name = call.get("tool")
                    raw_result = call.get("result", "{}")
                        
                    # 尝试解析 result
                    result = raw_result
                    if isinstance(raw_result, str) and raw_result.strip().startswith(('{', '[')):
                        try:
                            result = json.loads(raw_result)
                        except:
                            pass

                    # 1) 从 open orders 快照提取保护单（这是完整快照，可用于清空）
                    if tool_name in {"get_coin_futures_open_orders", "get_usdt_futures_open_orders"}:
                        if isinstance(result, list):
                            tp_val = None
                            sl_val = None
                            has_tp_type = False
                            has_sl_type = False
                            for order in result:
                                otype = str(order.get("type", "")).upper()
                                stop_price = order.get("stopPrice")
                                order_id = order.get("orderId")
                                stop_val = None
                                if stop_price:
                                    try:
                                        stop_val = float(stop_price)
                                    except:
                                        stop_val = None

                                if otype.startswith("TAKE_PROFIT"):
                                    has_tp_type = True
                                    if stop_val and stop_val > 0:
                                        tp_val = stop_val
                                        folder_op_info["protection_orders"]["tp_order_id"] = order_id
                                elif otype.startswith("STOP"):
                                    has_sl_type = True
                                    if stop_val and stop_val > 0:
                                        sl_val = stop_val
                                        folder_op_info["protection_orders"]["sl_order_id"] = order_id

                            # 空列表表示当前无挂单：明确清空 TP/SL
                            if len(result) == 0:
                                folder_op_info["protection_orders"]["tp"] = None
                                folder_op_info["protection_orders"]["sl"] = None
                                folder_op_info["protection_orders"]["tp_updated"] = True
                                folder_op_info["protection_orders"]["sl_updated"] = True
                                folder_op_info["protection_orders"]["tp_order_id"] = None
                                folder_op_info["protection_orders"]["sl_order_id"] = None
                            else:
                                # open_orders 是快照：出现某一类即更新，缺失则代表该类不存在
                                if has_tp_type:
                                    folder_op_info["protection_orders"]["tp"] = tp_val
                                    folder_op_info["protection_orders"]["tp_updated"] = True
                                if has_sl_type:
                                    folder_op_info["protection_orders"]["sl"] = sl_val
                                    folder_op_info["protection_orders"]["sl_updated"] = True

                    # 2) 从 trade_coin_futures 提取当次新挂保护单
                    if tool_name in {"trade_coin_futures", "trade_usdt_futures"}:
                        args = call.get("args", {})
                        otype = str(args.get("order_type", "")).upper()
                        stop_price = args.get("stop_price") or args.get("stopPrice")

                        if (not stop_price) and isinstance(result, dict):
                            stop_price = result.get("stopPrice")
                        if not otype and isinstance(result, dict):
                            otype = str(result.get("type", "")).upper()

                        stop_val = None
                        if stop_price:
                            try:
                                stop_val = float(stop_price)
                            except:
                                stop_val = None

                        if stop_val and stop_val > 0:
                            if otype.startswith("TAKE_PROFIT"):
                                folder_op_info["protection_orders"]["tp"] = stop_val
                                folder_op_info["protection_orders"]["tp_updated"] = True
                                if isinstance(result, dict):
                                    folder_op_info["protection_orders"]["tp_order_id"] = result.get("orderId")
                            elif otype.startswith("STOP"):
                                folder_op_info["protection_orders"]["sl"] = stop_val
                                folder_op_info["protection_orders"]["sl_updated"] = True
                                if isinstance(result, dict):
                                    folder_op_info["protection_orders"]["sl_order_id"] = result.get("orderId")

                    # 3) 取消类操作需要明确清空对应保护线
                    if tool_name in {"cancel_coin_futures_order", "cancel_usdt_futures_order"} and isinstance(result, dict):
                        canceled_type = str(result.get("type", "")).upper()
                        canceled_order_id = result.get("orderId")
                        if canceled_type.startswith("TAKE_PROFIT"):
                            if (
                                folder_op_info["protection_orders"]["tp_order_id"] is None
                                or folder_op_info["protection_orders"]["tp_order_id"] == canceled_order_id
                            ):
                                folder_op_info["protection_orders"]["tp"] = None
                                folder_op_info["protection_orders"]["tp_updated"] = True
                                folder_op_info["protection_orders"]["tp_order_id"] = None
                        elif canceled_type.startswith("STOP"):
                            if (
                                folder_op_info["protection_orders"]["sl_order_id"] is None
                                or folder_op_info["protection_orders"]["sl_order_id"] == canceled_order_id
                            ):
                                folder_op_info["protection_orders"]["sl"] = None
                                folder_op_info["protection_orders"]["sl_updated"] = True
                                folder_op_info["protection_orders"]["sl_order_id"] = None

                    if tool_name in {"cancel_all_coin_futures_orders", "cancel_all_usdt_futures_orders"}:
                        folder_op_info["protection_orders"]["tp"] = None
                        folder_op_info["protection_orders"]["sl"] = None
                        folder_op_info["protection_orders"]["tp_updated"] = True
                        folder_op_info["protection_orders"]["sl_updated"] = True
                        folder_op_info["protection_orders"]["tp_order_id"] = None
                        folder_op_info["protection_orders"]["sl_order_id"] = None

                    if tool_name in ORDER_TOOLS:
                        args = call.get("args", {})
                        # 提取 side
                        side = args.get("side")
                        if not side and isinstance(result, dict):
                            side = result.get("side")
                        
                        # 对于取消挂单等工具，side 可能为空，我们给它一个默认值方便前端识别
                        if not side:
                            side = "NEUTRAL"

                        folder_op_info["trades"].append({
                            "tool": tool_name,
                            "side": str(side).upper(),
                            "args": args,
                            "result": result
                        })

                    if tool_name in {"set_range_plan", "cancel_range_plan", "get_range_plan"}:
                        args = call.get("args", {}) if isinstance(call.get("args", {}), dict) else {}
                        folder_op_info["range_plan_calls"].append({
                            "tool": tool_name,
                            "args": args,
                            "result": result if isinstance(result, dict) else {},
                        })

                # 仅当是纯 heartbeat 且没有任何可审计动作时才跳过
                is_pure_heartbeat = all(str(t).lower() == "heartbeat" for t in event_types)
                has_trade_actions = len(folder_op_info["trades"]) > 0
                has_range_actions = len(folder_op_info["range_plan_calls"]) > 0
                has_whale_signal = len(folder_op_info["whale_transfer_usd"]) > 0
                if not (is_pure_heartbeat and not has_trade_actions and not has_range_actions and not has_whale_signal):
                    operations.append(folder_op_info)

                for event_type in event_types:
                    if str(event_type).lower() == "order_fill":
                        operations.append({
                            "log_folder": folder,
                            "timestamp_display": log_time_display_ts,
                            "local_time": naive_log_time.strftime("%Y-%m-%d %H:%M:%S"),
                            "event_type": "order_fill",
                            "event_types": ["order_fill"],
                            "whale_transfer_usd": whale_transfer_usd,
                            "explanation": content.get("explanation", "N/A"),
                            "execution_txt": content.get("execution_txt", "N/A"),
                            "memory_reasoning": content.get("memory_management_reasoning", "N/A"),
                            "experience": pretty_memory_text(long_memory_snapshot, content.get("experience", "N/A")),
                            "shortterm": pretty_memory_text(short_memory_snapshot, content.get("shortterm", "N/A")),
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
                            "counter_long_horizon_bias": bool(audit_meta.get("counter_long_horizon_bias", False)),
                            "long_horizon_status_at_trade": str(audit_meta.get("long_horizon_status_at_trade", "") or ""),
                            "indicators": indicators,
                            "trades": [{
                                "tool": "ORDER_FILLED",
                                "side": "R",
                                "args": {},
                                "result": {}
                            }],
                            "protection_orders": {
                                "tp": None,
                                "sl": None,
                                "tp_updated": False,
                                "sl_updated": False,
                                "tp_order_id": None,
                                "sl_order_id": None
                            }
                        })

        except Exception as e:
            continue
            
    return operations

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def get_data():
    start_str = request.args.get('start')
    end_str = request.args.get('end')
    interval_req = request.args.get('interval', '1h')
    
    # Map interval string to Binance constants
    interval_map = {
        "15m": Client.KLINE_INTERVAL_15MINUTE,
        "1h": Client.KLINE_INTERVAL_1HOUR,
        "4h": Client.KLINE_INTERVAL_4HOUR,
        "1d": Client.KLINE_INTERVAL_1DAY
    }
    interval_val = interval_map.get(interval_req, Client.KLINE_INTERVAL_15MINUTE)

    if start_str and end_str:
        try:
            start_local = datetime.strptime(start_str, "%Y%m%d_%H%M%S")
            end_local = datetime.strptime(end_str, "%Y%m%d_%H%M%S")
            tz_cst = timezone(timedelta(hours=8))
            start_utc = start_local.replace(tzinfo=tz_cst).astimezone(timezone.utc) - timedelta(hours=1)
            end_utc = end_local.replace(tzinfo=tz_cst).astimezone(timezone.utc) + timedelta(hours=1)
        except:
            start_utc, end_utc = get_all_logs_range(LOG_DIR)
    else:
        start_utc, end_utc = get_all_logs_range(LOG_DIR)

    if not start_utc:
        return jsonify({"error": "No logs found"}), 404

    # Check cache (include interval in cache key)
    cache_key = (CACHE_VERSION, start_utc.isoformat(), end_utc.isoformat(), interval_req)
    cached_data = load_from_file_cache("data", cache_key)
    if cached_data:
        return jsonify(cached_data)

    df = get_klines(SYMBOL, interval_val, start_utc, end_utc)
    df = add_indicators(df)
    
    # Filter back to the requested time range (start_utc to end_utc)
    # This ensures we don't send the buffer data to the frontend
    df = df[df['timestamp'] >= start_utc]
    
    ops_full = parse_logs(LOG_DIR, start_utc, end_utc)
    event_frequency, whale_distribution, whale_sample_count = build_event_and_whale_stats(ops_full)
    news_gated_records = build_news_gated_records(ops_full)
    ops_lite = build_operations_lite(ops_full, interval_req)
    range_plans = build_range_plan_overlays(ops_full)
    range_call_count = 0
    for op in ops_full:
        calls = op.get("range_plan_calls", [])
        if isinstance(calls, list):
            range_call_count += len(calls)
    
    # Format K-lines for Lightweight Charts (Adding 8 hours for CST display)
    klines = []
    for _, row in df.iterrows():
        # Add 8 hours to UTC timestamp to fake CST for the chart
        local_ts = int(row['timestamp'].timestamp()) + (8 * 3600)
        klines.append({
            "time": local_ts,
            "open": row['open'],
            "high": row['high'],
            "low": row['low'],
            "close": row['close'],
            "volume": row['volume'],
            # Indicators
            "ma7": row['ma7'] if not np.isnan(row['ma7']) else None,
            "ma25": row['ma25'] if not np.isnan(row['ma25']) else None,
            "ma99": row['ma99'] if not np.isnan(row['ma99']) else None,
            "rsi6": row['rsi6'] if not np.isnan(row['rsi6']) else None,
            "rsi": row['rsi'] if not np.isnan(row['rsi']) else None,
            "macd": row['macd'] if not np.isnan(row['macd']) else None,
            "signal": row['signal'] if not np.isnan(row['signal']) else None,
            "histo": row['histo'] if not np.isnan(row['histo']) else None,
            "upper": row['upper'] if not np.isnan(row['upper']) else None,
            "lower": row['lower'] if not np.isnan(row['lower']) else None,
            "k": row['k'] if not np.isnan(row['k']) else None,
            "d": row['d'] if not np.isnan(row['d']) else None,
            "j": row['j'] if not np.isnan(row['j']) else None,
            "cci": row['cci'] if not np.isnan(row['cci']) else None,
            "wr": row['wr'] if not np.isnan(row['wr']) else None,
            "obv": row['obv'] if not np.isnan(row['obv']) else None,
        })

    result = {
        "klines": klines,
        "operations": ops_lite,
        "range_plans": range_plans,
        "range_debug": {
            "range_plan_count": len(range_plans),
            "range_call_count": range_call_count,
            "range_plan_samples": range_plans[:5],
        },
        "event_frequency": event_frequency,
        "whale_transfer_distribution": whale_distribution,
        "whale_transfer_samples": whale_sample_count,
        "news_gated_records": news_gated_records,
        "symbol": SYMBOL,
        "range": {
            "start": (start_utc + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"),
            "end": (end_utc + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        }
    }
    
    # Store in cache
    save_to_file_cache("data", cache_key, result)
    
    return jsonify(result)

@app.route('/api/window_details')
def get_window_details():
    interval_req = request.args.get('interval', '1h')
    window_ts_raw = request.args.get('window_ts')
    if not window_ts_raw:
        return jsonify({"error": "missing window_ts"}), 400
    try:
        window_ts = int(window_ts_raw)
    except Exception:
        return jsonify({"error": "invalid window_ts"}), 400

    start_utc, end_utc = get_all_logs_range(LOG_DIR)
    if not start_utc:
        return jsonify({"error": "No logs found"}), 404

    ops_full = parse_logs(LOG_DIR, start_utc, end_utc)
    window_size = get_window_size_seconds(interval_req)
    matched = []
    for op in ops_full:
        ts = int(op.get("timestamp_display", 0) or 0)
        if ts > 0 and (ts // window_size) * window_size == window_ts:
            matched.append(op)

    matched.sort(key=lambda x: int(x.get("timestamp_display", 0) or 0))
    return jsonify({"operations": matched, "window_ts": window_ts, "interval": interval_req})

@app.route('/api/stats')
def get_stats():
    # Reuse some logic from original report.py if needed, 
    # but for now just basic stats
    start_utc, end_utc = get_all_logs_range(LOG_DIR)
    if not start_utc:
        return jsonify({})
    
    # Check cache
    cache_key = (start_utc.isoformat(), end_utc.isoformat())
    cached_stats = load_from_file_cache("stats", cache_key)
    if cached_stats:
        return jsonify(cached_stats)
    
    ops = parse_logs(LOG_DIR, start_utc, end_utc)
    
    trade_count = sum(len(o['trades']) for o in ops)
    activation_count = len(ops)
    
    buy_count = 0
    sell_count = 0
    for o in ops:
        for t in o['trades']:
            if t['side'] == 'BUY': buy_count += 1
            elif t['side'] == 'SELL': sell_count += 1

    result = {
        "activations": activation_count,
        "total_trades": trade_count,
        "buy_trades": buy_count,
        "sell_trades": sell_count
    }
    
    # Store in cache
    save_to_file_cache("stats", cache_key, result)
    
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=1520, debug=False)
