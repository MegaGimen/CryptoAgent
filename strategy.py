import asyncio
import builtins
import hashlib
import importlib.util
import json
import math
import os
import re
import symtable
import time
import traceback
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from env_config import load_project_env
load_project_env()

import httpx
import websockets
from aiohttp import web
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from tools import safe_json_dump, safe_json_read, safe_json_loads

try:
    from langchain.agents import create_agent
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
except ImportError as e:
    raise RuntimeError(
        "LangChain/LangGraph dependencies are required. Install langchain, langchain-openai, and langgraph."
    ) from e

try:
    from langsmith import tracing_context
    from langsmith.run_helpers import get_current_run_tree
    LANGSMITH_SDK_AVAILABLE = True
except ImportError:
    tracing_context = None
    get_current_run_tree = None
    LANGSMITH_SDK_AVAILABLE = False

from execution.main import (
    acknowledge_range_plan_event,
    classify_range_fill_batch,
    get_usdt_futures_account,
    get_range_plan,
    get_usdt_futures_max_open_position,
    get_usdt_futures_open_orders,
    get_usdt_futures_position,
    get_langchain_tools,
    maintain_range_plan,
    read_range_plan_state,
    reset_tool_recorder,
    reset_tool_stage,
    set_tool_recorder,
    set_tool_stage,
)


def load_module_from_path(module_name: str, file_path: str):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ENABLE_WHALE_MONITOR = os.getenv("ENABLE_WHALE_MONITOR", "false").strip().lower() == "true"

WHALE_ALERT_PROTOCOL_PATH = os.path.join(PROJECT_ROOT, "whale", "whalealert", "protocol.py")
POLY_PROTOCOL_PATH = os.path.join(PROJECT_ROOT, "polymarket", "protocol.py")
NEWS_PROTOCOL_PATH = os.path.join(PROJECT_ROOT, "news", "protocol.py")

whalealert_protocol_func = None
whalealert_model_func = None
whalealert_load_data_func = None
if ENABLE_WHALE_MONITOR:
    whale_alert_module = load_module_from_path("whalealert_protocol", WHALE_ALERT_PROTOCOL_PATH)
    whalealert_protocol_func = whale_alert_module.protocol
    whalealert_model_func = whale_alert_module.analyze_and_model_whale
    whalealert_load_data_func = whale_alert_module.load_whale_data

poly_module = load_module_from_path("poly_protocol", POLY_PROTOCOL_PATH)
poly_protocol_func = poly_module.protocol

news_module = load_module_from_path("news_protocol", NEWS_PROTOCOL_PATH)
news_protocol_func = news_module.protocol

LLM_API_KEY = os.getenv("LLMAPIKEY")
LLM_BASE_URL = os.getenv("LLMBASEURL")
LLM_MODEL_ID = os.getenv("LLMMODELID", "gpt-4.1")
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "CoinAutomation")
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY")
LANGSMITH_RUNTIME_TRACING = os.getenv("LANGSMITH_RUNTIME_TRACING", "true").strip().lower() == "true"
DASHSCOPE_ENABLE_THINKING = os.getenv("DASHSCOPE_ENABLE_THINKING", "false").strip().lower() == "true"
VOLCENGINE_ENABLE_THINKING = os.getenv("VOLCENGINE_ENABLE_THINKING", "false").strip().lower() == "true"

BINANCE_REPORT_PATH = os.path.join(PROJECT_ROOT, "Binance", "metrics_report.md")
BINANCE_ACCOUNT_PATH = os.path.join(PROJECT_ROOT, "Binance", "account.md")
BINANCE_ALERTS_PATH = os.path.join(PROJECT_ROOT, "Binance", "alerts.json")
EXPERIENCE_PATH = os.path.join(PROJECT_ROOT, "experience.md")
SHORTMEMORY_PATH = os.path.join(PROJECT_ROOT, "shortmemory.md")
MEM_DIR = os.path.join(PROJECT_ROOT, "mem")
SHORT_MEMORY_JSON_PATH = os.path.join(MEM_DIR, "short.json")
LONG_PIPELINE_DIR = os.path.join(MEM_DIR, "long_pipeline")
LONG_REFLECTION_PATH = os.path.join(MEM_DIR, "long_reflection.json")
LONG_MEMORY_JSON_PATH = os.path.join(MEM_DIR, "long.json")
DAILY_EXECUTION_REFLECTION_PATH = os.path.join(MEM_DIR, "daily_execution_reflection.json")
POST_STOP_REFLECTION_PATH = os.path.join(MEM_DIR, "post_stop_reflection.json")
LONG_RAW_INPUTS_PATH = os.path.join(LONG_PIPELINE_DIR, "raw_long_inputs.json")
LONG_KEPT_INPUTS_PATH = os.path.join(LONG_PIPELINE_DIR, "kept_long_inputs.json")
LONG_DROPPED_INPUTS_PATH = os.path.join(LONG_PIPELINE_DIR, "dropped_long_inputs.json")
LONG_HORIZON_VIEW_PATH = os.path.join(LONG_PIPELINE_DIR, "long_horizon_view.json")
LONG_REVIEW_STATE_PATH = os.path.join(LONG_PIPELINE_DIR, "long_review_state.json")
STRUCTURED_MEMORY_PATH = os.path.join(PROJECT_ROOT, "strategy_state.json")
PROMPT_TEMPLATE_PATH = os.path.join(PROJECT_ROOT, "prompts", "agentprompt.md")
LONG_REVIEW_PROMPT_TEMPLATE_PATH = os.path.join(PROJECT_ROOT, "prompts", "agentprompt_long_review.md")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
FILLS_JSON_PATH = os.path.join(PROJECT_ROOT, "Binance", "fills.json")
TASKS_JSON_PATH = os.path.join(PROJECT_ROOT, "tasks.json")
CLOCK_JSON_PATH = os.path.join(PROJECT_ROOT, "clock.json")
ALERT_HISTORY_PATH = os.path.join(PROJECT_ROOT, "alert_history.json")
NEWS_DATA_JSON_PATH = os.path.join(PROJECT_ROOT, "news", "news_data.json")
POLY_MONITOR_JSON_PATH = os.path.join(PROJECT_ROOT, "polymarket", "monitor.json")
DAILY_LONG_REVIEW_TIME = "08:10"
DAILY_LONG_REVIEW_RETRY_MINUTES = 15
LONG_REVIEW_RAW_RESPONSE_FILENAME = "long_review_raw_response.txt"

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(MEM_DIR, exist_ok=True)
os.makedirs(LONG_PIPELINE_DIR, exist_ok=True)

STARTUP_REQUIRED_FILE_TARGETS: List[Tuple[str, str]] = [
    ("Prompt Template", PROMPT_TEMPLATE_PATH),
    ("Long Review Prompt Template", LONG_REVIEW_PROMPT_TEMPLATE_PATH),
    ("Polymarket Protocol", POLY_PROTOCOL_PATH),
    ("News Protocol", NEWS_PROTOCOL_PATH),
]
if ENABLE_WHALE_MONITOR:
    STARTUP_REQUIRED_FILE_TARGETS.append(("Whale Protocol", WHALE_ALERT_PROTOCOL_PATH))

PROPOSE_TOOL_NAMES = [
    "get_spot_balance",
    "get_range_plan",
    "get_usdt_futures_open_orders",
    "get_usdt_futures_order",
    "calculate_expression",
]

EXECUTE_TOOL_NAMES = [
    "get_spot_balance",
    "transfer_to_usdt_futures",
    "set_usdt_futures_leverage",
    "set_usdt_futures_margin_type",
    "get_range_plan",
    "get_usdt_futures_open_orders",
    "cancel_usdt_futures_order",
    "cancel_all_usdt_futures_orders",
    "trade_usdt_futures",
    "modify_usdt_futures_order",
    "get_usdt_futures_order",
    "close_usdt_futures_position",
    "set_range_plan",
    "cancel_range_plan",
    "calculate_expression",
    "set_alarm",
    "delete_alarm",
]

EXECUTE_RUNTIME_READONLY_TOOL_NAMES = tuple(
    tool_name for tool_name in PROPOSE_TOOL_NAMES if tool_name in EXECUTE_TOOL_NAMES
)

# Retry mode for schema/alarm-semantic guard failures after primary side effects.
# In this mode, retry can only do decision repair and lightweight verification.
EXECUTE_RETRY_SAFE_TOOL_NAMES = [
    "get_spot_balance",
    "get_usdt_futures_position",
    "get_usdt_futures_account",
    "get_usdt_futures_max_open_position",
    "get_range_plan",
    "get_usdt_futures_open_orders",
    "get_usdt_futures_order",
    "calculate_expression",
    "set_alarm",
    "delete_alarm",
]

RANGE_CONTRACT_REPAIR_GUARD_ERRORS = {
    "declared_trigger_window_not_persisted",
    "declared_state_change_not_persisted",
    "declared_hypothesis_expiry_not_persisted",
    "hypothesis_rollover_action_missing",
    "hypothesis_rollover_reason_missing",
    "hypothesis_rollover_id_mismatch",
    "hypothesis_rollover_missing_expiry_patch",
    "hypothesis_rollover_expiry_invalid",
    "hypothesis_rollover_evidence_missing",
    "hypothesis_replace_action_missing",
    "hypothesis_replace_reason_missing",
    "hypothesis_replace_id_missing",
    "hypothesis_replace_patch_missing",
    "sideways_range_decision_missing",
    "sideways_range_start_missing_tool_intent",
    "sideways_range_start_missing_execution",
    "sideways_range_decline_reason_missing",
    "sideways_range_decline_execution_conflict",
    "sideways_range_edge_decline_not_supported",
    "sideways_range_activation_decline_not_supported",
}

RETRY_NON_SIDE_EFFECT_GUARD_ERRORS = {
    "memory_patch_invalid",
    "decision_schema_invalid",
    "missing_structured_wait_alarm",
    "historical_alarm_confused_as_live",
    "live_position_trade_intent_mismatch",
    "live_position_hypothesis_direction_mismatch",
    "declared_trigger_window_not_persisted",
    "declared_state_change_not_persisted",
    "declared_hypothesis_expiry_not_persisted",
    "hypothesis_rollover_action_missing",
    "hypothesis_rollover_reason_missing",
    "hypothesis_rollover_id_mismatch",
    "hypothesis_rollover_missing_expiry_patch",
    "hypothesis_rollover_expiry_invalid",
    "hypothesis_rollover_evidence_missing",
    "hypothesis_replace_action_missing",
    "hypothesis_replace_reason_missing",
    "hypothesis_replace_id_missing",
    "hypothesis_replace_patch_missing",
} | RANGE_CONTRACT_REPAIR_GUARD_ERRORS

HARD_GUARD_RETRY_ERRORS = {
    "memory_patch_invalid",
    "decision_schema_invalid",
    "live_position_trade_intent_mismatch",
    "live_position_hypothesis_direction_mismatch",
} | RANGE_CONTRACT_REPAIR_GUARD_ERRORS

RETRY_SIDE_EFFECT_BLOCKED_TOOLS = {
    "trade_usdt_futures",
    "modify_usdt_futures_order",
    "close_usdt_futures_position",
    "cancel_usdt_futures_order",
    "cancel_all_usdt_futures_orders",
    "set_usdt_futures_leverage",
    "set_usdt_futures_margin_type",
    "transfer_to_usdt_futures",
}

CAPACITY_DIRTY_TOOLS = {
    "set_usdt_futures_leverage",
    "set_usdt_futures_margin_type",
    "transfer_to_usdt_futures",
    "cancel_usdt_futures_order",
    "cancel_all_usdt_futures_orders",
    "close_usdt_futures_position",
    "set_range_plan",
    "cancel_range_plan",
}

VERIFY_REQUIRED_TOOLS = CAPACITY_DIRTY_TOOLS | {
    "trade_usdt_futures",
    "modify_usdt_futures_order",
}

LANGCHAIN_SYSTEM_PROMPT = (
    "You are CoinAutomation's autonomous intraday trading agent for ETH perpetual futures. "
    "Use auditable data, maintain memory discipline, and keep outputs structured."
)

EXECUTE_PRIMARY_RETRY_LIMIT = 1
HARD_GUARD_RETRY_LIMIT = 12
MAX_SAME_GUARD_RETRY_ATTEMPTS = 4
MEMORY_SCHEMA_VERSION = 1

EXECUTE_PRIMARY_STRATEGY_GUARD_RULES = [
    ("guard_precheck_gate", "precheck_gate_violation"),
    ("guard_capacity_refresh_chain", "capacity_refresh_chain"),
    ("guard_opening_sequence_tracking", "opening_sequence_tracking"),
    ("guard_verify_required_tracking", "verify_required_tracking"),
]

EXECUTION_ERROR_GUARD_RULES = [
    ("guard_unsupported_order_type", "unsupported_order_type"),
    ("guard_optional_params_bad_combo", "optional_params_bad_combo"),
    ("guard_invalid_position_side", "invalid_position_side"),
    ("guard_position_side_not_match", "position_side_not_match"),
    ("guard_reduce_only_rejected", "reduce_only_rejected"),
    ("guard_reduce_only_order_type_not_supported", "reduce_only_order_type_not_supported"),
    ("guard_exchange_rejected", "exchange_rejected"),
    ("guard_opening_sequence_already_consumed", "opening_sequence_already_consumed"),
    ("guard_opening_retry_exhausted", "opening_retry_exhausted"),
    ("guard_capacity_refresh_required", "capacity_refresh_required"),
    ("guard_invalid_order_id", "invalid_order_id"),
    ("guard_last_verified_protection_guard", "last_verified_protection_guard"),
    ("guard_live_protection_cancel_all_blocked", "live_protection_cancel_all_blocked"),
    ("guard_invalid_side", "invalid_side"),
    ("guard_protective_position_side_required", "protective_position_side_required"),
    ("guard_close_position_order_type_invalid", "close_position_order_type_invalid"),
    ("guard_close_position_with_quantity", "close_position_with_quantity"),
    ("guard_close_position_with_reduce_only", "close_position_with_reduce_only"),
    ("guard_protective_quantity_missing", "protective_quantity_missing"),
    ("guard_protective_quantity_exceeds_position", "protective_quantity_exceeds_position"),
    ("guard_hedge_mode_reduce_only_not_supported", "hedge_mode_reduce_only_not_supported"),
    ("guard_protective_semantic_mismatch", "protective_semantic_mismatch"),
    ("guard_limit_price_missing", "limit_price_missing"),
    ("guard_trigger_price_missing", "trigger_price_missing"),
    ("guard_stop_price_missing", "stop_price_missing"),
    ("guard_trailing_params_missing", "trailing_params_missing"),
    ("guard_quantity_too_small", "quantity_too_small"),
    ("guard_short_entry_quality_violation", "short_entry_quality_violation"),
    ("guard_range_plan_active_conflict", "range_plan_active_conflict"),
]

WAIT_TRUTHFULNESS_GUARDS = {
    "historical_alarm_confused_as_live",
    "missing_structured_wait_alarm",
    "active_hypothesis_expiry_stale",
    "wait_deadline_stale",
    "breakout_watch_deadline_stale",
}

RUNTIME_ALIGNMENT_GUARDS = {
    "live_position_trade_intent_mismatch",
    "live_position_hypothesis_direction_mismatch",
}

EXECUTION_SHAPE_GUARDS = {
    "precheck_gate_violation",
    "capacity_refresh_chain",
    "opening_sequence_tracking",
    "verify_required_tracking",
    "unsupported_order_type",
    "optional_params_bad_combo",
    "invalid_position_side",
    "position_side_not_match",
    "reduce_only_rejected",
    "reduce_only_order_type_not_supported",
    "exchange_rejected",
    "opening_sequence_already_consumed",
    "opening_retry_exhausted",
    "capacity_refresh_required",
    "invalid_order_id",
    "last_verified_protection_guard",
    "live_protection_cancel_all_blocked",
    "invalid_side",
    "protective_position_side_required",
    "close_position_order_type_invalid",
    "close_position_with_quantity",
    "close_position_with_reduce_only",
    "protective_quantity_missing",
    "protective_quantity_exceeds_position",
    "hedge_mode_reduce_only_not_supported",
    "protective_semantic_mismatch",
    "limit_price_missing",
    "trigger_price_missing",
    "stop_price_missing",
    "trailing_params_missing",
    "quantity_too_small",
    "short_entry_quality_violation",
    "range_plan_active_conflict",
    "retry_side_effect_blocked",
    "duplicate_followup_action",
    "approved_contract_runtime_mismatch",
}

RANGE_DECISION_GUARDS = {
    "sideways_range_decision_missing",
    "sideways_range_start_missing_tool_intent",
    "sideways_range_start_missing_execution",
    "sideways_range_decline_reason_missing",
    "sideways_range_edge_decline_not_supported",
    "sideways_range_activation_decline_not_supported",
    "sideways_range_decline_execution_conflict",
}

RISK_TRUTHFULNESS_GUARDS = {
    "risk_compression_required",
}

CONTRACT_INTEGRITY_GUARDS = {
    "memory_patch_invalid",
    "decision_schema_invalid",
    "plan_transition_missing_evidence",
    "declared_state_change_not_persisted",
    "hypothesis_replace_action_missing",
    "hypothesis_rollover_action_missing",
    "expired_hypothesis_not_rolled",
    "hypothesis_rollover_reason_missing",
    "hypothesis_rollover_id_mismatch",
    "hypothesis_rollover_missing_expiry_patch",
    "hypothesis_rollover_expiry_invalid",
    "hypothesis_rollover_evidence_missing",
    "hypothesis_replace_reason_missing",
    "hypothesis_replace_id_missing",
    "hypothesis_replace_patch_missing",
}

GUARD_BLOCKED_DECISION_REPAIR_PATHS_BY_PURPOSE = {
    "wait_truthfulness": {
        "/day_plan/hold_until",
        "/day_plan/recheck_at",
        "/active_hypothesis/expiry",
    },
    "runtime_alignment": {
        "/consistency_state/trade_intent",
        "/consistency_state/entry_plan_direction",
        "/active_hypothesis/direction",
        "/active_hypothesis/status",
        "/consistency_state/intraday_mode",
        "/risk_state/risk_action_required",
        "/risk_state/risk_action_taken",
    },
}

SHORT_MEMORY_ENUMS = {
    "consistency_state.market_regime": {"strong_bullish", "strong_bearish", "neutral_sideways"},
    "consistency_state.trade_intent": {"wait", "long_bias", "short_bias", "cooldown"},
    "consistency_state.entry_plan_direction": {"long", "short", "both", "flat"},
    "day_plan.day_bias": {"long", "short", "neutral"},
    "consistency_state.intraday_mode": {"observe", "probe", "confirm", "scale_in", "manage", "reduce", "exit"},
    "consistency_state.thesis_strength": {"low", "medium", "high"},
    "consistency_state.execution_mode": {"observe_only", "managed_execution"},
    "consistency_state.wakeup_role": {"observe", "risk_review", "execution_review", "entry_review"},
    "active_hypothesis.status": {"active", "verified", "invalidated", "blocked"},
    "risk_state.risk_state": {"normal", "warn", "critical", "emergency"},
    "risk_state.risk_action_required": {"yes", "no"},
    "risk_state.risk_action_taken": {"reduce", "close", "tighten_stop", "none"},
    "mtf_state.mtf_bias_15m": {"bullish", "bearish", "mixed"},
    "mtf_state.mtf_bias_1h": {"bullish", "bearish", "mixed"},
    "mtf_state.mtf_bias_4h": {"bullish", "bearish", "mixed"},
    "mtf_state.regime_confidence": {"low", "medium", "high"},
    "mtf_state.switch_hysteresis": {"armed", "cooldown", "none"},
}
SHORT_MEMORY_ENUM_ALIASES = {
    "consistency_state.market_regime": {
        "mixed": "neutral_sideways",
        "neutral": "neutral_sideways",
        "sideways": "neutral_sideways",
        "bullish": "strong_bullish",
        "bearish": "strong_bearish",
        "breakout_bullish": "strong_bullish",
        "breakout_bearish": "strong_bearish",
        "potential_breakout_bullish": "strong_bullish",
        "potential_breakdown_bearish": "strong_bearish",
    },
    "consistency_state.trade_intent": {
        "observe": "wait",
        "observe_only": "wait",
        "flat": "wait",
        "close": "wait",
        "exit": "wait",
        "hold_long": "long_bias",
        "hold_short": "short_bias",
        "long": "long_bias",
        "short": "short_bias",
    },
    "day_plan.day_bias": {
        "bullish": "long",
        "bearish": "short",
        "flat": "neutral",
        "sideways": "neutral",
    },
    "active_hypothesis.status": {
        "terminated": "invalidated",
    },
}

# Canonical short-memory JSON patch paths. Keep legacy aliases for one-shot
# compatibility so malformed model output can be normalized before schema checks.
SHORT_MEMORY_PATCH_PATH_ALIASES = {
    "/consistency_state/state_change_evidence": "/risk_state/state_change_evidence",
    "/consistency_state/reversal_checklist": "/risk_state/reversal_checklist",
    "/consistency_state/risk_trigger_evidence": "/risk_state/risk_trigger_evidence",
    "/consistency_state/recheck_at": "/day_plan/recheck_at",
    "/active_hypothesis/hypothesis_status": "/active_hypothesis/status",
    "/active_hypothesis/hypothesis_direction": "/active_hypothesis/direction",
    "/active_hypothesis/hypothesis_expiry": "/active_hypothesis/expiry",
}

ALARM_CONDITION_METRIC_ALIASES = {
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
ALARM_CONDITION_OPERATOR_ALIASES = {
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "gt": ">",
    "lt": "<",
    "gte": ">=",
    "lte": "<=",
}


def _utc_now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


SHORT_PROMPT_NARRATIVE_LIMIT = 3
SHORT_PROMPT_NARRATIVE_LOOKBACK_DAYS = 14
SHORT_MEMORY_NARRATIVE_STORE_LIMIT = 12
SHORT_PROMPT_DECISION_ALERT_LIMIT = 2
SHORT_PROMPT_HISTORICAL_ALERT_LIMIT = 3
SHORT_PROMPT_TACTICAL_LOOKBACK_HOURS = 18
SHORT_MEMORY_TACTICAL_STORE_LOOKBACK_HOURS = 36
SHORT_MEMORY_TACTICAL_STORE_LIMIT = 24
DAILY_REFLECTION_CARRYOVER_PROMPT_LIMIT = 2
DAILY_REFLECTION_BEHAVIOR_PROMPT_LIMIT = 2
DAILY_REFLECTION_WATCH_PROMPT_LIMIT = 2
DAILY_REFLECTION_EPISODE_PROMPT_LIMIT = 2
DAILY_REFLECTION_EPISODE_PROMPT_LOOKBACK_HOURS = 96
DAILY_REFLECTION_EPISODE_STORE_LOOKBACK_HOURS = 168
POST_STOP_RECENT_STORE_LOOKBACK_HOURS = 72


def _slugify_memory_id(text: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")
    return normalized or fallback


def _extract_markdown_section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^#\s+{re.escape(heading)}\s*\n(.*?)(?=^\#\s+|\Z)"
    )
    match = pattern.search(str(text or ""))
    return match.group(1).strip() if match else ""


def _parse_markdown_key_values(section: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for raw_line in str(section or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        payload = line[2:].strip()
        if ":" not in payload:
            continue
        key, value = payload.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _parse_bracket_metadata(summary: str) -> Dict[str, str]:
    metadata: Dict[str, str] = {}
    bracket_match = re.search(r"\[(.*?)\]", summary)
    if not bracket_match:
        return metadata
    for piece in bracket_match.group(1).split("|"):
        if ":" not in piece:
            continue
        key, value = piece.split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def _parse_markdown_list_entries(section: str, *, item_kind: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for idx, raw_line in enumerate(str(section or "").splitlines()):
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        payload = line[2:].strip()
        title_match = re.search(r"\*\*(.*?)\*\*", payload)
        title = title_match.group(1).strip() if title_match else payload.split(":", 1)[0].strip()
        summary = payload
        if ":" in payload:
            summary = payload.split(":", 1)[1].strip()
        meta = _parse_bracket_metadata(summary)
        source_time = meta.get("news time") or meta.get("date") or meta.get("time") or ""
        recorded_at = meta.get("recorded") or meta.get("time") or ""
        source_type = "system"
        if "news time" in meta:
            source_type = "news"
        elif "date" in meta:
            source_type = "polymarket"
        item_id = f"{item_kind}-{idx + 1}-{_slugify_memory_id(title, str(idx + 1))}"
        base_item: Dict[str, Any] = {
            "id": item_id,
            "title": title,
            "summary": summary,
            "recorded_at": recorded_at,
        }
        if item_kind == "narrative":
            base_item.update(
                {
                    "source_type": source_type,
                    "source_time": source_time,
                    "impact_bias": "neutral",
                    "status": "active",
                }
            )
        else:
            base_item.update(
                {
                    "kind": title,
                    "related_hypothesis_id": "",
                    "expires_at": "",
                }
            )
        items.append(base_item)
    return items


def _default_short_memory(*, source: str = "system") -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "updated_at": _utc_now_iso(),
            "source": source,
        },
        "consistency_state": {
            "market_regime": "neutral_sideways",
            "trade_intent": "wait",
            "entry_plan_direction": "flat",
            "intraday_mode": "observe",
            "thesis_strength": "low",
            "thesis_score": 0,
            "entry_trigger": "none",
            "execution_mode": "observe_only",
            "wakeup_role": "observe",
            "cooldown_direction": "none",
            "cooldown_until": "none",
            "cooldown_reason": "none",
        },
        "day_plan": {
            "day_bias": "neutral",
            "day_thesis": "",
            "day_invalidation": "",
            "day_horizon_until": "",
            "hold_until": "",
            "recheck_at": "",
        },
        "active_hypothesis": {
            "hypothesis_id": "",
            "direction": "flat",
            "status": "blocked",
            "expiry": "",
            "wait_count": 0,
            "falsification_point": "",
        },
        "risk_state": {
            "risk_state": "normal",
            "risk_action_required": "no",
            "risk_action_taken": "none",
            "risk_trigger_evidence": "",
            "state_change_evidence": "",
            "reversal_checklist": "",
        },
        "mtf_state": {
            "mtf_bias_15m": "mixed",
            "mtf_bias_1h": "mixed",
            "mtf_bias_4h": "mixed",
            "regime_confidence": "low",
            "switch_hysteresis": "none",
        },
        "narrative_tracking": [],
        "tactical_alerts": [],
    }


def _default_long_memory(*, source: str = "system") -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "updated_at": _utc_now_iso(),
            "source": source,
        },
        "validated_rules": [],
        "invalidated_rules": [],
        "operator_notes": [],
    }


def _migrate_shortmemory_markdown_to_json(markdown_text: str) -> Dict[str, Any]:
    migrated = _default_short_memory(source="migrated_from_markdown")
    consistency = _parse_markdown_key_values(_extract_markdown_section(markdown_text, "Consistency State"))
    narrative_section = _extract_markdown_section(markdown_text, "Long-term Narrative Tracking")
    tactical_section = _extract_markdown_section(markdown_text, "Tactical Alerts")

    for field in [
        "market_regime",
        "trade_intent",
        "entry_plan_direction",
        "intraday_mode",
        "thesis_strength",
        "thesis_score",
        "entry_trigger",
        "execution_mode",
        "wakeup_role",
        "cooldown_direction",
        "cooldown_until",
        "cooldown_reason",
    ]:
        if field in consistency:
            migrated["consistency_state"][field] = consistency[field]

    for field in [
        "day_bias",
        "day_thesis",
        "day_invalidation",
        "day_horizon_until",
        "hold_until",
        "recheck_at",
    ]:
        if field in consistency:
            migrated["day_plan"][field] = consistency[field]

    hypothesis_map = {
        "hypothesis_id": "hypothesis_id",
        "hypothesis_direction": "direction",
        "hypothesis_status": "status",
        "hypothesis_expiry": "expiry",
        "wait_count": "wait_count",
    }
    for old_key, new_key in hypothesis_map.items():
        if old_key in consistency:
            migrated["active_hypothesis"][new_key] = consistency[old_key]

    if "falsification_point" in consistency:
        migrated["active_hypothesis"]["falsification_point"] = consistency["falsification_point"]

    risk_map = {
        "risk_state": "risk_state",
        "risk_action_required": "risk_action_required",
        "risk_action_taken": "risk_action_taken",
        "risk_trigger_evidence": "risk_trigger_evidence",
        "state_change_evidence": "state_change_evidence",
        "reversal_checklist": "reversal_checklist",
    }
    for old_key, new_key in risk_map.items():
        if old_key in consistency:
            migrated["risk_state"][new_key] = consistency[old_key]

    mtf_map = {
        "mtf_bias_15m": "mtf_bias_15m",
        "mtf_bias_1h": "mtf_bias_1h",
        "mtf_bias_4h": "mtf_bias_4h",
        "regime_confidence": "regime_confidence",
        "switch_hysteresis": "switch_hysteresis",
    }
    for old_key, new_key in mtf_map.items():
        if old_key in consistency:
            migrated["mtf_state"][new_key] = consistency[old_key]

    migrated["narrative_tracking"] = _parse_markdown_list_entries(narrative_section, item_kind="narrative")
    migrated["tactical_alerts"] = _parse_markdown_list_entries(tactical_section, item_kind="alert")
    for field in ["thesis_score"]:
        raw_value = migrated["consistency_state"].get(field)
        if str(raw_value).isdigit():
            migrated["consistency_state"][field] = int(str(raw_value))
    # Legacy compatibility only: wait_count may exist in historical markdown, but
    # current prompt/audit flow must not use it as a trigger or forced-action gate.
    raw_wait_count = migrated["active_hypothesis"].get("wait_count")
    if str(raw_wait_count).isdigit():
        migrated["active_hypothesis"]["wait_count"] = int(str(raw_wait_count))
    migrated["meta"]["updated_at"] = _utc_now_iso()
    return migrated


def _migrate_experience_markdown_to_json(markdown_text: str) -> Dict[str, Any]:
    migrated = _default_long_memory(source="migrated_from_markdown")
    raw_text = str(markdown_text or "").strip()
    if not raw_text:
        return migrated

    summary = raw_text
    if "## Raw Decision experience" in raw_text:
        summary = raw_text.split("## Raw Decision experience", 1)[1].strip()

    if summary:
        migrated["validated_rules"].append(
            {
                "id": "legacy-migrated-rule-1",
                "title": "Migrated legacy experience",
                "rule": summary.splitlines()[0].strip()[:160],
                "scope": "legacy_markdown",
                "evidence": summary,
                "falsification": "Legacy markdown migration placeholder; refine on next verified review.",
                "verification_plan": "Review this migrated rule at the next relevant alarm and replace with a narrower validated rule if needed.",
                "status": "active",
                "updated_at": _utc_now_iso(),
            }
        )
        migrated["operator_notes"].append(
            {
                "id": "legacy-markdown-note",
                "title": "Original experience markdown",
                "summary": raw_text,
                "updated_at": _utc_now_iso(),
            }
        )
    return migrated


def _merge_memory_shape(default_value: Any, current_value: Any) -> Any:
    if isinstance(default_value, dict):
        merged = deepcopy(default_value)
        if isinstance(current_value, dict):
            for key, value in current_value.items():
                if key in merged:
                    merged[key] = _merge_memory_shape(merged[key], value)
                else:
                    merged[key] = deepcopy(value)
        return merged
    if isinstance(default_value, list):
        if isinstance(current_value, list):
            return deepcopy(current_value)
        return deepcopy(default_value)
    if current_value is None:
        return deepcopy(default_value)
    return deepcopy(current_value)


def _json_pointer_tokens(path: str) -> List[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"Invalid JSON Pointer path: {path}")
    return [token.replace("~1", "/").replace("~0", "~") for token in path.split("/")[1:]]


def _resolve_json_pointer_parent(document: Any, tokens: List[str]) -> Any:
    current = document
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(f"JSON Pointer path not found: {token}")
            current = current[token]
            continue
        if isinstance(current, list):
            if token == "-":
                raise KeyError("Cannot traverse through '-' in JSON Pointer")
            index = int(token)
            current = current[index]
            continue
        raise KeyError(f"Cannot traverse JSON Pointer token on scalar value: {token}")
    return current


def _apply_single_json_patch(document: Any, operation: Dict[str, Any]) -> Any:
    op = str(operation.get("op", "")).strip().lower()
    path = str(operation.get("path", "")).strip()
    if op not in {"add", "remove", "replace"}:
        raise ValueError(f"Unsupported JSON patch op: {op}")
    tokens = _json_pointer_tokens(path)
    if not tokens:
        raise ValueError("Root-level patch is not supported")
    parent = _resolve_json_pointer_parent(document, tokens)
    last = tokens[-1]

    if isinstance(parent, dict):
        if op == "add":
            parent[last] = operation.get("value")
            return document
        if op == "remove":
            if last not in parent:
                raise KeyError(f"JSON patch remove path missing: {path}")
            parent.pop(last)
            return document
        if last not in parent:
            raise KeyError(f"JSON patch replace path missing: {path}")
        parent[last] = operation.get("value")
        return document

    if not isinstance(parent, list):
        raise TypeError(f"JSON patch parent is not list/dict for path: {path}")

    if last == "-":
        if op != "add":
            raise ValueError("'-' list append syntax is only valid for add")
        parent.append(operation.get("value"))
        return document

    index = int(last)
    if op == "add":
        if index < 0 or index > len(parent):
            raise IndexError(f"JSON patch add index out of range: {path}")
        parent.insert(index, operation.get("value"))
        return document
    if index < 0 or index >= len(parent):
        raise IndexError(f"JSON patch index out of range: {path}")
    if op == "remove":
        parent.pop(index)
        return document
    parent[index] = operation.get("value")
    return document


def _validate_json_patch_ops_shape(ops: Any, *, label: str) -> None:
    if not isinstance(ops, list):
        raise ValueError(f"{label} must be a list of JSON patch operations")
    for idx, item in enumerate(ops):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{idx}] must be an object")
        op = str(item.get("op", "")).strip().lower()
        path = str(item.get("path", "")).strip()
        if op not in {"add", "remove", "replace"}:
            raise ValueError(f"{label}[{idx}] has unsupported op: {op}")
        if not path.startswith("/"):
            raise ValueError(f"{label}[{idx}] path must start with '/': {path}")
        if op in {"add", "replace"} and "value" not in item:
            raise ValueError(f"{label}[{idx}] missing value for op={op}")


def _normalize_json_patch_ops(ops: List[Dict[str, Any]], *, label: str) -> List[Dict[str, Any]]:
    if label != "short_memory_ops":
        return list(ops)
    normalized: List[Dict[str, Any]] = []
    for item in ops:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        patched = dict(item)
        raw_path = str(patched.get("path", "")).strip()
        canonical_path = SHORT_MEMORY_PATCH_PATH_ALIASES.get(raw_path)
        if canonical_path:
            patched["path"] = canonical_path
        normalized.append(patched)
    return normalized


def _validate_memory_ops_shape(ops: Any, *, label: str) -> None:
    _validate_json_patch_ops_shape(ops, label=label)


def _normalize_memory_patch_ops(ops: List[Dict[str, Any]], *, label: str) -> List[Dict[str, Any]]:
    return _normalize_json_patch_ops(ops, label=label)


def _hydrate_memory_document_for_patch_ops(
    document: Any,
    ops: List[Dict[str, Any]],
) -> Any:
    if not isinstance(document, dict):
        return deepcopy(document)

    root_tokens: Set[str] = set()
    for item in ops:
        if not isinstance(item, dict):
            continue
        raw_path = str(item.get("path", "") or "").strip()
        if not raw_path.startswith("/"):
            continue
        try:
            tokens = _json_pointer_tokens(raw_path)
        except Exception:
            continue
        if tokens:
            root_tokens.add(tokens[0])

    if not root_tokens:
        return deepcopy(document)

    short_root_keys = {
        "meta",
        "consistency_state",
        "day_plan",
        "active_hypothesis",
        "risk_state",
        "mtf_state",
        "narrative_tracking",
        "tactical_alerts",
    }
    long_root_keys = {
        "meta",
        "validated_rules",
        "invalidated_rules",
        "operator_notes",
    }
    meta = document.get("meta", {}) if isinstance(document.get("meta", {}), dict) else {}
    source = str(meta.get("source", "") or "patch_hydrated")

    short_signal_keys = short_root_keys - {"meta"}
    long_signal_keys = long_root_keys - {"meta"}
    has_short_signal = bool(root_tokens & short_signal_keys) or any(key in document for key in short_signal_keys)
    has_long_signal = bool(root_tokens & long_signal_keys) or any(key in document for key in long_signal_keys)

    if has_short_signal and not has_long_signal:
        return _merge_memory_shape(_default_short_memory(source=source), document)
    if has_long_signal and not has_short_signal:
        return _merge_memory_shape(_default_long_memory(source=source), document)
    if root_tokens & short_signal_keys:
        return _merge_memory_shape(_default_short_memory(source=source), document)
    if root_tokens & long_signal_keys:
        return _merge_memory_shape(_default_long_memory(source=source), document)
    return deepcopy(document)


def _canonicalize_enum_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _normalize_short_memory_enum_values(memory: Any) -> None:
    if not isinstance(memory, dict):
        return
    for field_path, allowed_values in SHORT_MEMORY_ENUMS.items():
        head, tail = field_path.split(".", 1)
        section = memory.get(head)
        if not isinstance(section, dict):
            continue
        value = section.get(tail, "")
        if value == "":
            continue
        raw_text = str(value).strip()
        if raw_text in allowed_values:
            continue
        canonical_raw = _canonicalize_enum_value(raw_text)
        allowed_by_canonical = {
            _canonicalize_enum_value(item): item for item in allowed_values
        }
        if canonical_raw in allowed_by_canonical:
            section[tail] = allowed_by_canonical[canonical_raw]
            continue
        alias_map = SHORT_MEMORY_ENUM_ALIASES.get(field_path, {})
        alias_value = alias_map.get(canonical_raw)
        if alias_value in allowed_values:
            section[tail] = alias_value


def _sanitize_short_memory_transition(
    previous_memory: Dict[str, Any],
    next_memory: Dict[str, Any],
    ops: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not isinstance(previous_memory, dict) or not isinstance(next_memory, dict):
        return next_memory

    touched_paths = {
        str(item.get("path", "")).strip()
        for item in ops
        if isinstance(item, dict)
    }
    previous_hypothesis = previous_memory.get("active_hypothesis", {})
    next_hypothesis = next_memory.get("active_hypothesis", {})
    previous_consistency = previous_memory.get("consistency_state", {})
    next_consistency = next_memory.get("consistency_state", {})
    next_risk_state = next_memory.get("risk_state", {})

    if not isinstance(previous_hypothesis, dict) or not isinstance(next_hypothesis, dict):
        return next_memory
    if not isinstance(previous_consistency, dict) or not isinstance(next_consistency, dict):
        return next_memory
    if not isinstance(next_risk_state, dict):
        return next_memory

    previous_id = str(previous_hypothesis.get("hypothesis_id", "") or "").strip()
    next_id = str(next_hypothesis.get("hypothesis_id", "") or "").strip()
    previous_direction = str(previous_hypothesis.get("direction", "") or "").strip().lower()
    next_direction = str(next_hypothesis.get("direction", "") or "").strip().lower()
    hypothesis_changed = bool(
        (next_id and next_id != previous_id)
        or (
            previous_direction in {"long", "short"}
            and next_direction in {"long", "short"}
            and previous_direction != next_direction
        )
    )
    if not hypothesis_changed:
        return next_memory

    if "/consistency_state/entry_trigger" not in touched_paths:
        next_consistency["entry_trigger"] = "none"
    if "/risk_state/risk_trigger_evidence" not in touched_paths:
        next_risk_state["risk_trigger_evidence"] = str(
            next_risk_state.get("state_change_evidence", "") or ""
        ).strip()
    if "/active_hypothesis/wait_count" not in touched_paths:
        next_hypothesis["wait_count"] = 0

    cooldown_direction = str(next_consistency.get("cooldown_direction", "") or "").strip().lower()
    cooldown_until = str(next_consistency.get("cooldown_until", "") or "").strip().lower()
    if (
        "/consistency_state/cooldown_reason" not in touched_paths
        and cooldown_direction in {"", "none"}
        and cooldown_until in {"", "none"}
    ):
        next_consistency["cooldown_reason"] = "none"
    return next_memory


def _validate_short_memory_schema(memory: Any) -> None:
    if not isinstance(memory, dict):
        raise ValueError("short memory snapshot must be an object")
    _normalize_short_memory_enum_values(memory)
    required_keys = {
        "meta",
        "consistency_state",
        "day_plan",
        "active_hypothesis",
        "risk_state",
        "mtf_state",
        "narrative_tracking",
        "tactical_alerts",
    }
    missing = required_keys - set(memory.keys())
    if missing:
        raise ValueError(f"short memory missing top-level keys: {sorted(missing)}")

    for field_path, allowed_values in SHORT_MEMORY_ENUMS.items():
        head, tail = field_path.split(".", 1)
        section = memory.get(head, {})
        if not isinstance(section, dict):
            raise ValueError(f"short memory section {head} must be an object")
        value = section.get(tail, "")
        if value == "":
            continue
        if str(value) not in allowed_values:
            raise ValueError(f"short memory invalid enum for {field_path}: {value}")

    if not isinstance(memory.get("narrative_tracking", []), list):
        raise ValueError("short memory narrative_tracking must be a list")
    if not isinstance(memory.get("tactical_alerts", []), list):
        raise ValueError("short memory tactical_alerts must be a list")


def _validate_long_memory_schema(memory: Any) -> None:
    if not isinstance(memory, dict):
        raise ValueError("long memory snapshot must be an object")
    required_keys = {"meta", "validated_rules", "invalidated_rules", "operator_notes"}
    missing = required_keys - set(memory.keys())
    if missing:
        raise ValueError(f"long memory missing top-level keys: {sorted(missing)}")
    if not isinstance(memory.get("validated_rules", []), list):
        raise ValueError("long memory validated_rules must be a list")
    if not isinstance(memory.get("invalidated_rules", []), list):
        raise ValueError("long memory invalidated_rules must be a list")
    if not isinstance(memory.get("operator_notes", []), list):
        raise ValueError("long memory operator_notes must be a list")
    for idx, rule in enumerate(memory.get("validated_rules", [])):
        if not isinstance(rule, dict):
            raise ValueError(f"validated_rules[{idx}] must be an object")
        if not str(rule.get("falsification", "")).strip():
            raise ValueError(f"validated_rules[{idx}] missing falsification")
        if not str(rule.get("verification_plan", "")).strip():
            raise ValueError(f"validated_rules[{idx}] missing verification_plan")


def _hydrate_short_memory_snapshot(memory: Any, *, source: str) -> Dict[str, Any]:
    hydrated = _merge_memory_shape(_default_short_memory(source=source), memory)
    _normalize_short_memory_enum_values(hydrated)
    _validate_short_memory_schema(hydrated)
    return hydrated


def _hydrate_long_memory_snapshot(memory: Any, *, source: str) -> Dict[str, Any]:
    hydrated = _merge_memory_shape(_default_long_memory(source=source), memory)
    _validate_long_memory_schema(hydrated)
    return hydrated


def _format_structured_contract_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _normalize_comparable_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.replace("；", ";").replace("：", ":").replace("，", ",").strip()


def _normalize_checklist_items(value: Any) -> List[str]:
    if value is None:
        return []

    candidates: List[Any]
    if isinstance(value, list):
        candidates = list(value)
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return _normalize_checklist_items(parsed)
        candidates = re.split(r"[;\n；]+", text)

    normalized: List[str] = []
    for item in candidates:
        item_text = str(item or "").strip()
        if not item_text:
            continue
        item_text = re.sub(r"^\s*(?:[-*•]\s*|\d+\s*[.)、]\s*)", "", item_text)
        item_text = item_text.strip("[]\"'“”‘’ ")
        item_text = _normalize_comparable_text(item_text)
        item_text = re.sub(r"\s+", "", item_text)
        if item_text and item_text not in normalized:
            normalized.append(item_text)
    return normalized


def _structured_contract_values_match(dotted_path: str, left: Any, right: Any) -> bool:
    if dotted_path == "risk_state.reversal_checklist":
        return _normalize_checklist_items(left) == _normalize_checklist_items(right)
    return _normalize_comparable_text(left) == _normalize_comparable_text(right)


def _memory_snapshot_to_pretty_json(snapshot: Any, fallback: str) -> str:
    if snapshot is None:
        return fallback
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def assert_json_syntax(file_path: str, description: str = "JSON") -> None:
    if not os.path.exists(file_path):
        return
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return
    safe_json_loads(content, description)


def _normalize_alarm_condition_obj(raw_condition: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw_condition, str):
        raw_text = raw_condition.strip()
        if raw_text.startswith("{"):
            try:
                decoded = json.loads(raw_text)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                raw_condition = decoded
        if isinstance(raw_condition, str):
            raw_condition = {"expr": raw_condition}
    if not isinstance(raw_condition, dict):
        return None
    if "expr" in raw_condition:
        expr = _normalize_alarm_expr_text(raw_condition.get("expr", ""))
        if not expr:
            return None
        tokens, token_error = _tokenize_alarm_condition_expr(expr)
        if token_error:
            return None
        try:
            tree = _parse_alarm_condition_expr(tokens)
        except ValueError:
            return None
        return {"expr": expr, "tree": tree}
    raw_metric = str(raw_condition.get("metric", "")).strip()
    raw_operator = str(raw_condition.get("operator", "")).strip()
    raw_value = raw_condition.get("value")

    metric = ALARM_CONDITION_METRIC_ALIASES.get(raw_metric)
    operator = ALARM_CONDITION_OPERATOR_ALIASES.get(raw_operator)
    if metric is None or operator is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return {
        "metric": metric,
        "operator": operator,
        "value": value,
    }


def _normalize_alarm_expr_text(raw_expr: Any) -> str:
    text = str(raw_expr or "").strip()
    if not text:
        return ""
    text = re.sub(r"\bAND\b", "&", text, flags=re.IGNORECASE)
    text = re.sub(r"\bOR\b", "|", text, flags=re.IGNORECASE)
    return text


def _tokenize_alarm_condition_expr(expr: str) -> tuple[Optional[List[str]], Optional[str]]:
    token_pattern = re.compile(
        r"\s*("
        r"\(|\)|\&|\||>=|<=|>|<|"
        r"[A-Za-z_][A-Za-z0-9_]*|"
        r"-?\d+(?:\.\d+)?"
        r")"
    )
    tokens: List[str] = []
    idx = 0
    text = _normalize_alarm_expr_text(expr)
    while idx < len(text):
        match = token_pattern.match(text, idx)
        if not match:
            return None, f"condition.expr 在位置 {idx} 存在非法字符"
        tokens.append(match.group(1))
        idx = match.end()
    return tokens, None


def _parse_alarm_condition_expr(tokens: List[str]) -> Dict[str, Any]:
    idx = 0

    def parse_atom() -> Dict[str, Any]:
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
        metric = ALARM_CONDITION_METRIC_ALIASES.get(token)
        if metric is None:
            raise ValueError(f"condition.metric 不支持: {token}")
        idx += 1
        if idx >= len(tokens):
            raise ValueError("条件表达式缺少比较符")
        operator = ALARM_CONDITION_OPERATOR_ALIASES.get(tokens[idx])
        if operator is None:
            raise ValueError(f"condition.operator 不支持: {tokens[idx]}")
        idx += 1
        if idx >= len(tokens):
            raise ValueError("条件表达式缺少阈值")
        try:
            value = float(tokens[idx])
        except ValueError as exc:
            raise ValueError("条件表达式阈值必须是数值") from exc
        idx += 1
        return {"metric": metric, "operator": operator, "value": value}

    def parse_and() -> Dict[str, Any]:
        nonlocal idx
        node = parse_atom()
        children = [node]
        while idx < len(tokens) and tokens[idx] == "&":
            idx += 1
            children.append(parse_atom())
        if len(children) == 1:
            return children[0]
        return {"op": "and", "children": children}

    def parse_or() -> Dict[str, Any]:
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


def _format_alarm_numeric(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.4f}".rstrip("0").rstrip(".")


def _format_alarm_condition(condition: Any) -> str:
    normalized = _normalize_alarm_condition_obj(condition)
    if not normalized:
        return ""
    if "expr" in normalized:
        return f"Condition: {normalized['expr']}"
    return (
        f"Condition: {normalized['metric']} "
        f"{normalized['operator']} {_format_alarm_numeric(normalized['value'])}"
    )


def _format_pending_alarm_line(item: Dict[str, Any]) -> str:
    parts = [
        f"ID: {item.get('id')}",
        f"Trigger: {item.get('trigger_time')}",
    ]
    condition_text = _format_alarm_condition(item.get("condition"))
    if condition_text:
        parts.append(condition_text)
    parts.append(f"Prompt: {item.get('prompt')}")
    return "- " + " | ".join(parts)


def _extract_chart_section(report_text: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}\s+chart\s*\n(.*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(str(report_text or ""))
    return match.group(1) if match else ""


def _extract_alarm_metrics_from_report(report_text: str) -> Dict[str, float]:
    metrics: Dict[str, float] = {}
    chart_specs = [
        ("15 minutes", "15m"),
        ("1h", "1h"),
    ]
    for heading, suffix in chart_specs:
        section = _extract_chart_section(report_text, heading)
        if not section:
            continue

        rows = [line for line in section.splitlines() if line.startswith("| 20")]
        if rows:
            parts = [part.strip() for part in rows[-1].split("|")[1:-1]]
            if len(parts) >= 2:
                try:
                    close_value = float(parts[1])
                    if suffix == "15m":
                        metrics["price"] = close_value
                    metrics[f"price_{suffix}"] = close_value
                except ValueError:
                    pass

        rsi_match = re.search(r"\*\*RSI\(14\)\*\*:\s*([-+]?\d+(?:\.\d+)?)", section)
        if rsi_match:
            metrics[f"RSI_{suffix}"] = float(rsi_match.group(1))

        histo_match = re.search(
            r"\*\*MACD\*\*:\s*[-+]?\d+(?:\.\d+)?\s*\(Signal:\s*[-+]?\d+(?:\.\d+)?,\s*Histo:\s*([-+]?\d+(?:\.\d+)?)\)",
            section,
        )
        if histo_match:
            metrics[f"MACD_HISTO_{suffix}"] = float(histo_match.group(1))

    if "RSI_15m" in metrics:
        metrics["RSI"] = metrics["RSI_15m"]
    if "MACD_HISTO_15m" in metrics:
        metrics["MACD_HISTO"] = metrics["MACD_HISTO_15m"]
    return metrics


def _alarm_condition_matches(condition: Dict[str, Any], metrics: Dict[str, float]) -> bool:
    if "expr" in condition:
        tree = condition.get("tree")
        if not isinstance(tree, dict):
            return False
        return _evaluate_alarm_condition_tree(tree, metrics)

    metric_key = str(condition.get("metric", ""))
    metric_value = metrics.get(metric_key)
    if metric_value is None:
        return False
    threshold = float(condition.get("value", 0))
    operator = str(condition.get("operator", ""))
    if operator == ">":
        return metric_value > threshold
    if operator == "<":
        return metric_value < threshold
    if operator == ">=":
        return metric_value >= threshold
    if operator == "<=":
        return metric_value <= threshold
    return False


def _evaluate_alarm_condition_tree(tree: Dict[str, Any], metrics: Dict[str, float]) -> bool:
    if "op" not in tree:
        return _alarm_condition_matches(tree, metrics)
    children = tree.get("children", [])
    if not isinstance(children, list) or not children:
        return False
    if tree["op"] == "and":
        return all(_evaluate_alarm_condition_tree(child, metrics) for child in children if isinstance(child, dict))
    if tree["op"] == "or":
        return any(_evaluate_alarm_condition_tree(child, metrics) for child in children if isinstance(child, dict))
    return False


def _recent_system_alert_matches_condition(condition: Dict[str, Any], window_seconds: int = 20) -> bool:
    history = safe_json_read(ALERT_HISTORY_PATH, "Alert History")
    if not isinstance(history, dict):
        return False
    cutoff = time.time() - window_seconds
    metrics_to_check = _collect_alarm_condition_metrics(condition)
    for alert_type, record in history.items():
        if not isinstance(record, dict):
            continue
        try:
            last_time = float(record.get("time", 0))
        except (TypeError, ValueError):
            continue
        if last_time < cutoff:
            continue
        alert_name = str(alert_type).upper()
        for metric in metrics_to_check:
            if metric.startswith("price") and "PRICE" in alert_name:
                return True
            if metric.startswith("RSI") and "RSI" in alert_name:
                return True
    return False


def _collect_alarm_condition_metrics(condition: Dict[str, Any]) -> List[str]:
    if "expr" not in condition:
        metric = str(condition.get("metric", "")).strip()
        return [metric] if metric else []
    tree = condition.get("tree")
    if not isinstance(tree, dict):
        return []
    collected: List[str] = []

    def walk(node: Dict[str, Any]) -> None:
        if "op" not in node:
            metric = str(node.get("metric", "")).strip()
            if metric and metric not in collected:
                collected.append(metric)
            return
        for child in node.get("children", []):
            if isinstance(child, dict):
                walk(child)

    walk(tree)
    return collected


class DecisionOutput(BaseModel):
    execution_txt: str = Field(default="")
    explanation: str = Field(default="")
    memory_management_reasoning: str = Field(default="")
    decision_basis: str = Field(default="")
    conflict_check: str = Field(default="")
    falsification_point: str = Field(default="")
    next_alarm_reason: str = Field(default="")
    state_change_evidence: str = Field(default="")
    action_intent: str = Field(default="")
    tool_intents: List[Dict[str, Any]] = Field(default_factory=list)
    hypothesis_action: str = Field(default="")
    hypothesis_action_reason: str = Field(default="")
    plan_transition: str = Field(default="")
    execution_rationale: str = Field(default="")
    range_decision: str = Field(default="")
    range_decision_reason_code: str = Field(default="")
    declared_entry_plan_direction: str = Field(default="")
    declared_hypothesis_id: str = Field(default="")
    declared_hypothesis_direction: str = Field(default="")
    declared_hypothesis_status: str = Field(default="")
    declared_hypothesis_expiry: str = Field(default="")
    declared_reversal_checklist: Union[str, List[str]] = Field(default="")
    declared_state_change_evidence: str = Field(default="")
    short_memory_ops: List[Dict[str, Any]] = Field(default_factory=list)
    long_memory_ops: List[Dict[str, Any]] = Field(default_factory=list)
    experience: str = Field(default="")
    shortterm: str = Field(default="")


class LongHorizonPatchOutputModel(BaseModel):
    kept_items: List[Dict[str, Any]] = Field(default_factory=list)
    dropped_items: List[Dict[str, Any]] = Field(default_factory=list)
    long_horizon_view_ops: List[Dict[str, Any]] = Field(default_factory=list)
    long_horizon_view: Dict[str, Any] = Field(default_factory=dict)


DECISION_CONTRACT_PRESERVED_FIELDS: tuple[str, ...] = tuple(
    field_name
    for field_name in DecisionOutput.model_fields.keys()
    if field_name not in {"execution_txt", "explanation"}
)


class DecisionState(TypedDict, total=False):
    messages: List[Dict[str, Any]]
    event_batch: List[Dict[str, Any]]
    event_type: str
    full_market_snapshot: str
    market_snapshot: str
    account_snapshot: str
    news_snapshot: str
    poly_snapshot: str
    experience_snapshot: str
    shortmemory_snapshot: str
    long_reflection_obj: Dict[str, Any]
    long_memory_snapshot_obj: Dict[str, Any]
    short_memory_snapshot_obj: Dict[str, Any]
    pending_alarms_snapshot: str
    decision: Dict[str, Any]
    tool_results: List[Dict[str, Any]]
    transition_state: str
    audit_meta: Dict[str, Any]
    system_prompt: str
    user_prompt: str
    event_details: str
    thread_id: str
    precheck_summary: str
    refreshed_precheck_summary: str
    interaction_log_dir: str


class LongReviewHypothesisModel(BaseModel):
    id: str = Field(default="")
    title: str = Field(default="")
    statement: str = Field(default="")
    formed_at: str = Field(default="")
    basis: List[str] = Field(default_factory=list)
    expected_effect_on_eth: str = Field(default="")
    expected_window: str = Field(default="")
    challenge_conditions: List[str] = Field(default_factory=list)
    linked_case_ids: List[str] = Field(default_factory=list)
    status: str = Field(default="")
    invalidation_reason: str = Field(default="")
    origin: str = Field(default="")


class LongReviewCaseModel(BaseModel):
    id: str = Field(default="")
    source_type: str = Field(default="")
    source_id: str = Field(default="")
    headline_or_question: str = Field(default="")
    event_time: str = Field(default="")
    recorded_at: str = Field(default="")
    gate_decision: str = Field(default="")
    why: str = Field(default="")
    impact_bias: str = Field(default="")
    price_context_at_ingestion: Dict[str, Any] = Field(default_factory=dict)
    linked_hypothesis_ids: List[str] = Field(default_factory=list)


class LongReviewMetaModel(BaseModel):
    schema_version: int = Field(default=1)
    updated_at: str = Field(default="")
    source: str = Field(default="")


class LongReviewReflectionModel(BaseModel):
    meta: LongReviewMetaModel = Field(default_factory=LongReviewMetaModel)
    active_hypotheses: List[LongReviewHypothesisModel] = Field(default_factory=list)
    invalidated_hypotheses: List[LongReviewHypothesisModel] = Field(default_factory=list)
    news_case_log: List[LongReviewCaseModel] = Field(default_factory=list)


class DailyExecutionEpisodeModel(BaseModel):
    episode_id: str = Field(default="")
    recorded_at: str = Field(default="")
    hypothesis_id: str = Field(default="")
    direction: str = Field(default="")
    action: str = Field(default="")
    outcome: str = Field(default="")
    why_entered: str = Field(default="")
    why_failed: str = Field(default="")
    retry_guardrail: str = Field(default="")


class DailyExecutionReflectionModel(BaseModel):
    meta: LongReviewMetaModel = Field(default_factory=LongReviewMetaModel)
    carryover: List[str] = Field(default_factory=list)
    behavior_biases: List[str] = Field(default_factory=list)
    watch_items: List[str] = Field(default_factory=list)
    recent_episodes: List[DailyExecutionEpisodeModel] = Field(default_factory=list)


class LongReviewSummaryModel(BaseModel):
    kept_count: int = Field(default=0)
    invalidated_count: int = Field(default=0)
    new_hypotheses_count: int = Field(default=0)
    notes: str = Field(default="")
    changes: List[str] = Field(default_factory=list)


class LongReviewOutputModel(BaseModel):
    long_reflection: LongReviewReflectionModel = Field(default_factory=LongReviewReflectionModel)
    daily_execution_reflection: DailyExecutionReflectionModel = Field(default_factory=DailyExecutionReflectionModel)
    review_summary: LongReviewSummaryModel = Field(default_factory=LongReviewSummaryModel)


class LongReviewPatchOutputModel(BaseModel):
    long_reflection_ops: List[Dict[str, Any]] = Field(default_factory=list)
    daily_execution_reflection_ops: List[Dict[str, Any]] = Field(default_factory=list)
    review_summary: LongReviewSummaryModel = Field(default_factory=LongReviewSummaryModel)
    long_reflection: LongReviewReflectionModel = Field(default_factory=LongReviewReflectionModel)
    daily_execution_reflection: DailyExecutionReflectionModel = Field(default_factory=DailyExecutionReflectionModel)


def _format_tool_result_for_prompt(name: str, payload: Any) -> str:
    return f"[{name}]\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


def _extract_text_from_agent_output(agent_output: Dict[str, Any]) -> str:
    messages = agent_output.get("messages", [])
    if not messages:
        return ""
    last_message = messages[-1]
    content = getattr(last_message, "content", "")
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
            else:
                chunks.append(str(item))
        return "\n".join([c for c in chunks if c])
    return str(content or "")


def _is_tool_choice_thinking_mode_error(exc: Exception) -> bool:
    msg = str(exc)
    return (
        "tool_choice parameter does not support being set to required or object in thinking mode" in msg
        or ("invalid_parameter_error" in msg and "tool_choice" in msg and "thinking mode" in msg)
    )


def _is_recoverable_llm_api_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.HTTPError, TimeoutError, ConnectionError)):
        return True

    api_error_tokens = (
        "apiconnectionerror",
        "api connection error",
        "ratelimiterror",
        "rate limit",
        "service unavailable",
        "503",
        "504",
        "gateway timeout",
        "timed out",
        "temporarily unavailable",
        "remoteprotocolerror",
        "readtimeout",
        "connecttimeout",
    )

    cursor: Optional[BaseException] = exc
    visited: Set[int] = set()
    while cursor is not None and id(cursor) not in visited:
        visited.add(id(cursor))
        msg = str(cursor).strip().lower()
        if any(token in msg for token in api_error_tokens):
            return True
        cursor = getattr(cursor, "__cause__", None) or getattr(cursor, "__context__", None)
    return False


def _strip_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return text


def _recover_json_dict(raw_text: str, description: str = "JSON") -> Optional[Dict[str, Any]]:
    parsed, _ = _recover_json_dict_with_diagnostics(raw_text, description)
    return parsed


def _recover_json_dict_with_diagnostics(
    raw_text: str,
    description: str = "JSON",
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    text = (raw_text or "").strip()
    diagnostics: Dict[str, Any] = {
        "description": description,
        "raw_length": len(text),
        "attempts": [],
        "recovered_via": "",
    }
    if not text:
        diagnostics["empty_response"] = True
        return None, diagnostics

    candidates: List[Tuple[str, str]] = [("direct", text)]
    unfenced = _strip_markdown_code_fence(text)
    if unfenced and unfenced != text:
        candidates.append(("markdown_unfenced", unfenced))

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(("outer_brace_slice", text[start : end + 1]))

    seen = set()
    last_error: Optional[json.JSONDecodeError] = None

    for method, candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as e:
            last_error = e
            diagnostics["attempts"].append(
                {
                    "method": method,
                    "candidate_length": len(candidate),
                    "status": "json_decode_error",
                    "error": str(e),
                }
            )
            continue
        parsed_type = type(parsed).__name__
        diagnostics["attempts"].append(
            {
                "method": method,
                "candidate_length": len(candidate),
                "status": "parsed",
                "parsed_type": parsed_type,
            }
        )
        if isinstance(parsed, dict):
            diagnostics["recovered_via"] = method
            return parsed, diagnostics
        diagnostics["non_dict_top_level_type"] = parsed_type
        return None, diagnostics

    if last_error is not None:
        print(f"❌ JSON Decode Error ({description}): {str(last_error)}", flush=True)
        print(f"--- FULL {description} START ---", flush=True)
        print(text, flush=True)
        print(f"--- FULL {description} END ---", flush=True)
    return None, diagnostics


class JSONRecoveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw_text: str = "",
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.diagnostics = diagnostics or {}


def _iter_symbol_tables(table: symtable.SymbolTable):
    yield table
    for child in table.get_children():
        yield from _iter_symbol_tables(child)


class StrategyManager:
    def __init__(self):
        self._assert_startup_static_integrity()
        self._assert_startup_json_syntax()
        if not LLM_API_KEY:
            raise RuntimeError("LLMAPIKEY is required.")

        self.langsmith_enabled = bool(LANGSMITH_RUNTIME_TRACING and LANGSMITH_API_KEY and LANGSMITH_SDK_AVAILABLE)
        self.langsmith_degraded_reason: str = ""
        if self.langsmith_enabled:
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGSMITH_API_KEY"] = LANGSMITH_API_KEY
            os.environ["LANGSMITH_PROJECT"] = LANGSMITH_PROJECT
        else:
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            os.environ["LANGSMITH_TRACING"] = "false"
            if not LANGSMITH_RUNTIME_TRACING:
                self.langsmith_degraded_reason = "runtime_tracing_disabled_via_env"
                print("[LangSmith] Runtime tracing disabled by env override.", flush=True)
            elif not LANGSMITH_SDK_AVAILABLE:
                self.langsmith_degraded_reason = "sdk_not_installed"
                print("[LangSmith] SDK not installed; tracing disabled and strategy will continue.", flush=True)
            elif not LANGSMITH_API_KEY:
                self.langsmith_degraded_reason = "missing_api_key"
                print("[LangSmith] LANGSMITH_API_KEY missing; tracing disabled and strategy will continue.", flush=True)

        self.event_queue: asyncio.Queue = asyncio.Queue()
        self.is_processing = False
        self.current_task = None
        self.pending_events: List[Dict[str, Any]] = []
        self.last_range_event_key: str = ""

        self.propose_tools = get_langchain_tools(PROPOSE_TOOL_NAMES)
        self.execute_tools = get_langchain_tools(EXECUTE_TOOL_NAMES)
        self.execute_retry_safe_tools = get_langchain_tools(EXECUTE_RETRY_SAFE_TOOL_NAMES)
        self.long_horizon_model = self._new_chat_model()
        self.long_review_model = self._new_chat_model(temperature=0.2)
        self.execute_system_prompt = (
            "You are the execution stage of CoinAutomation. "
            "Precheck has already been completed in this wakeup. "
            "The fixed precheck trio has already been injected into context. "
            "Do not call get_usdt_futures_position, get_usdt_futures_account, "
            "or get_usdt_futures_max_open_position yourself. "
            "When a live execution refresh conflicts with older prompt snapshots, trust the live refresh. "
            "For protective STOP_MARKET/TAKE_PROFIT_MARKET orders, choose one legal shape only: "
            "full-close protection uses close_position=true with quantity omitted; "
            "partial protection uses quantity with close_position=false. "
            "You can call tools when needed and must return a structured decision."
        )
        self.execute_compat_system_prompt = (
            "You are the execution stage of CoinAutomation. "
            "Precheck has already been completed in this wakeup. "
            "The fixed precheck trio has already been injected into context. "
            "Do not call get_usdt_futures_position, get_usdt_futures_account, "
            "or get_usdt_futures_max_open_position yourself. "
            "When a live execution refresh conflicts with older prompt snapshots, trust the live refresh. "
            "For protective STOP_MARKET/TAKE_PROFIT_MARKET orders, choose one legal shape only: "
            "full-close protection uses close_position=true with quantity omitted; "
            "partial protection uses quantity with close_position=false. "
            "You can call tools when needed. "
            "When you finish, output a single JSON object matching these fields exactly: "
            "execution_txt, explanation, memory_management_reasoning, decision_basis, "
            "conflict_check, falsification_point, next_alarm_reason, state_change_evidence, "
            "action_intent, tool_intents, hypothesis_action, hypothesis_action_reason, "
            "plan_transition, execution_rationale, range_decision, range_decision_reason_code, declared_entry_plan_direction, "
            "declared_hypothesis_id, declared_hypothesis_direction, declared_hypothesis_status, "
            "declared_hypothesis_expiry, declared_reversal_checklist, declared_state_change_evidence, "
            "short_memory_ops, long_memory_ops."
        )

        self.propose_agent = create_agent(
            model=self._new_chat_model(),
            tools=self.propose_tools,
            system_prompt=(
                "You are the proposal stage of CoinAutomation. "
                "You may reason and collect read-only evidence, "
                "but do not schedule alarms, place trades, or mutate state at this stage."
            ),
        )
        self.execute_contract_agent = create_agent(
            model=self._new_chat_model(),
            tools=[],
            response_format=DecisionOutput,
            system_prompt=(
                "You are the structured execution-contract stage of CoinAutomation. "
                "You must not call tools in this stage. "
                "Return the final structured decision contract only."
            ),
        )
        self.execute_contract_agent_compat = create_agent(
            model=self._new_chat_model(),
            tools=[],
            system_prompt=(
                "You are the structured execution-contract stage of CoinAutomation. "
                "You must not call tools in this stage. "
                "When you finish, output a single JSON object matching these fields exactly: "
                "execution_txt, explanation, memory_management_reasoning, decision_basis, "
                "conflict_check, falsification_point, next_alarm_reason, state_change_evidence, "
                "action_intent, tool_intents, hypothesis_action, hypothesis_action_reason, "
                "plan_transition, execution_rationale, range_decision, range_decision_reason_code, declared_entry_plan_direction, "
                "declared_hypothesis_id, declared_hypothesis_direction, declared_hypothesis_status, "
                "declared_hypothesis_expiry, declared_reversal_checklist, declared_state_change_evidence, "
                "short_memory_ops, long_memory_ops."
            ),
        )
        self.execute_agent = create_agent(
            model=self._new_chat_model(),
            tools=self.execute_tools,
            response_format=DecisionOutput,
            system_prompt=self.execute_system_prompt,
        )
        self.execute_agent_compat = create_agent(
            model=self._new_chat_model(),
            tools=self.execute_tools,
            system_prompt=self.execute_compat_system_prompt,
        )
        self.execute_retry_safe_agent = create_agent(
            model=self._new_chat_model(),
            tools=self.execute_retry_safe_tools,
            response_format=DecisionOutput,
            system_prompt=(
                "You are the execution retry repair stage of CoinAutomation. "
                "Primary already produced side effects in this wakeup and retry is for decision/shape repair only. "
                "Do not place new trade/protective/cancel/transfer/leverage actions in retry. "
                "When a live execution refresh conflicts with older prompt snapshots, trust the live refresh. "
                "Only use lightweight verification and alarm tools when necessary."
            ),
        )
        self.execute_retry_safe_agent_compat = create_agent(
            model=self._new_chat_model(),
            tools=self.execute_retry_safe_tools,
            system_prompt=(
                "You are the execution retry repair stage of CoinAutomation. "
                "Primary already produced side effects in this wakeup and retry is for decision/shape repair only. "
                "Do not place new trade/protective/cancel/transfer/leverage actions in retry. "
                "When a live execution refresh conflicts with older prompt snapshots, trust the live refresh. "
                "Only use lightweight verification and alarm tools when necessary. "
                "When you finish, output a single JSON object matching these fields exactly: "
                "execution_txt, explanation, memory_management_reasoning, decision_basis, "
                "conflict_check, falsification_point, next_alarm_reason, state_change_evidence, "
                "action_intent, tool_intents, hypothesis_action, hypothesis_action_reason, "
                "plan_transition, execution_rationale, range_decision, range_decision_reason_code, declared_entry_plan_direction, "
                "declared_hypothesis_id, declared_hypothesis_direction, declared_hypothesis_status, "
                "declared_hypothesis_expiry, declared_reversal_checklist, declared_state_change_evidence, "
                "short_memory_ops, long_memory_ops."
            ),
        )
        self.execute_agent_cache: Dict[Tuple[str, ...], Any] = {
            tuple(EXECUTE_TOOL_NAMES): self.execute_agent,
        }
        self.execute_agent_compat_cache: Dict[Tuple[str, ...], Any] = {
            tuple(EXECUTE_TOOL_NAMES): self.execute_agent_compat,
        }

        self.guard_node_specs: List[Dict[str, str]] = []
        for node_name, rule_id in EXECUTE_PRIMARY_STRATEGY_GUARD_RULES + EXECUTION_ERROR_GUARD_RULES:
            self.guard_node_specs.append({"node": node_name, "rule_id": rule_id})
        self.guard_rule_by_node: Dict[str, str] = {item["node"]: item["rule_id"] for item in self.guard_node_specs}

        workflow = StateGraph(DecisionState)
        workflow.add_node("observe", self._node_observe)
        workflow.add_node("load_binance_account", self._node_load_binance_account)
        workflow.add_node("load_news", self._node_load_news)
        workflow.add_node("load_polymarket", self._node_load_polymarket)
        workflow.add_node("load_memory", self._node_load_memory)
        workflow.add_node("compose_prompt", self._node_compose_prompt)
        workflow.add_node("precheck", self._node_precheck)
        workflow.add_node("propose", self._node_propose)
        workflow.add_node("execute_primary_model", self._node_execute_primary_model)
        workflow.add_node("execute_primary_guards_entry", self._node_execute_primary_guards_entry)
        workflow.add_node("guard", self._node_guard)
        workflow.add_node("guard_reject_router", self._node_guard_reject_router)
        workflow.add_node("execute_primary_retry", self._node_execute_primary_retry)
        workflow.add_node("execute_primary_guards_decision", self._node_execute_primary_guards_decision)
        workflow.add_node("refresh_precheck", self._node_refresh_precheck)
        workflow.add_node("execute_followup", self._node_execute_followup)
        workflow.add_node("verify", self._node_verify)
        workflow.add_node("apply_memory_patches", self._node_apply_memory_patches)
        workflow.add_node("persist_memory", self._node_persist_memory)
        workflow.add_node("manage_or_exit", self._node_manage_or_exit)

        workflow.add_edge(START, "observe")
        workflow.add_edge("observe", "load_binance_account")
        workflow.add_edge("load_binance_account", "load_news")
        workflow.add_edge("load_news", "load_polymarket")
        workflow.add_edge("load_polymarket", "load_memory")
        workflow.add_edge("load_memory", "compose_prompt")
        workflow.add_edge("compose_prompt", "precheck")
        workflow.add_edge("precheck", "propose")
        workflow.add_edge("propose", "execute_primary_model")
        workflow.add_edge("execute_primary_model", "execute_primary_guards_entry")
        workflow.add_edge("execute_primary_guards_entry", "guard")
        workflow.add_conditional_edges(
            "guard",
            self._route_after_guard_node,
            {
                "guard_reject_router": "guard_reject_router",
                "execute_primary_guards_decision": "execute_primary_guards_decision",
            },
        )
        workflow.add_conditional_edges(
            "guard_reject_router",
            self._route_after_guard_reject_router,
            {
                "execute_primary_retry": "execute_primary_retry",
                "verify": "verify",
            },
        )
        workflow.add_edge("execute_primary_retry", "execute_primary_guards_entry")
        workflow.add_conditional_edges(
            "execute_primary_guards_decision",
            self._route_after_execute_primary_guards_decision,
            {
                "refresh_precheck": "refresh_precheck",
                "verify": "verify",
            },
        )
        workflow.add_edge("refresh_precheck", "execute_followup")
        workflow.add_edge("execute_followup", "verify")
        workflow.add_edge("verify", "apply_memory_patches")
        workflow.add_edge("apply_memory_patches", "persist_memory")
        workflow.add_edge("persist_memory", "manage_or_exit")
        workflow.add_edge("manage_or_exit", END)

        self.graph = workflow.compile(checkpointer=MemorySaver())

    def _new_chat_model(self, *, temperature: float = 0.7) -> ChatOpenAI:
        auth_key = LLM_API_KEY
        if auth_key.startswith("Bearer "):
            auth_key = auth_key.replace("Bearer ", "", 1)
        kwargs = {
            "api_key": auth_key,
            "model": LLM_MODEL_ID,
            "temperature": temperature,
        }
        if LLM_BASE_URL:
            kwargs["base_url"] = LLM_BASE_URL
            if "dashscope.aliyuncs.com" in LLM_BASE_URL:
                kwargs["extra_body"] = {"enable_thinking": DASHSCOPE_ENABLE_THINKING}
            elif "volces.com/api/coding" in LLM_BASE_URL and VOLCENGINE_ENABLE_THINKING:
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        return ChatOpenAI(**kwargs)

    def _assert_transition(self, state: DecisionState, expected: str) -> None:
        current = state.get("transition_state")
        if current != expected:
            raise RuntimeError(
                f"Illegal state transition: expected {expected}, got {current}."
            )

    def _read_text_file(self, path: str, fallback: str) -> str:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return fallback

    def _read_json_file(self, path: str, description: str, default: Any) -> Any:
        data = safe_json_read(path, description, use_lock=True)
        if data is None:
            return deepcopy(default)
        return data

    def _infer_memory_source(self, payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            meta = payload.get("meta", {})
            if isinstance(meta, dict):
                source = str(meta.get("source", "") or "").strip()
                if source:
                    return source
        return fallback

    def _assert_startup_json_syntax(self) -> None:
        targets = [
            ("Binance Alerts File", BINANCE_ALERTS_PATH),
            ("Short Memory JSON", SHORT_MEMORY_JSON_PATH),
            ("Long Memory JSON", LONG_MEMORY_JSON_PATH),
            ("Long Reflection JSON", LONG_REFLECTION_PATH),
            ("Daily Execution Reflection", DAILY_EXECUTION_REFLECTION_PATH),
            ("Post Stop Reflection", POST_STOP_REFLECTION_PATH),
            ("Long Review State", LONG_REVIEW_STATE_PATH),
            ("Structured Memory", STRUCTURED_MEMORY_PATH),
            ("Fills File", FILLS_JSON_PATH),
            ("Tasks File", TASKS_JSON_PATH),
            ("Clock JSON", CLOCK_JSON_PATH),
            ("Alert History File", ALERT_HISTORY_PATH),
            ("Structured News Source", NEWS_DATA_JSON_PATH),
            ("Structured Polymarket Source", POLY_MONITOR_JSON_PATH),
        ]
        failures: List[str] = []
        for description, path in targets:
            try:
                assert_json_syntax(path, description)
            except json.JSONDecodeError as exc:
                failures.append(f"{description}: {path} -> {exc}")
        if failures:
            joined = "\n".join(failures)
            raise RuntimeError(
                "Startup JSON syntax check failed. Fix invalid JSON files before starting strategy:\n"
                f"{joined}"
            )

    def _assert_startup_static_integrity(self) -> None:
        failures: List[str] = []
        for description, path in STARTUP_REQUIRED_FILE_TARGETS:
            if not os.path.exists(path):
                failures.append(f"{description}: missing required file -> {path}")

        source_path = os.path.abspath(__file__)
        try:
            with open(source_path, "r", encoding="utf-8") as f:
                source_text = f.read()
        except OSError as exc:
            failures.append(f"Strategy Source: unable to read {source_path} -> {exc}")
            source_text = ""

        if source_text:
            try:
                symbol_root = symtable.symtable(source_text, source_path, "exec")
            except SyntaxError as exc:
                failures.append(f"Strategy Source: syntax error -> {exc}")
            else:
                runtime_globals = set(globals().keys())
                builtin_names = set(dir(builtins))
                ignored_names = {
                    "__annotations__",
                    "__builtins__",
                    "__cached__",
                    "__class__",
                    "__doc__",
                    "__file__",
                    "__loader__",
                    "__name__",
                    "__package__",
                    "__spec__",
                }
                unresolved: List[str] = []
                seen_unresolved: Set[Tuple[str, int, str]] = set()
                for table in _iter_symbol_tables(symbol_root):
                    if table.get_type() == "module":
                        continue
                    for name in sorted(table.get_identifiers()):
                        symbol = table.lookup(name)
                        if not symbol.is_referenced() or not symbol.is_global():
                            continue
                        if name in runtime_globals or name in builtin_names or name in ignored_names:
                            continue
                        scope_name = table.get_name()
                        scope_line = table.get_lineno()
                        key = (scope_name, scope_line, name)
                        if key in seen_unresolved:
                            continue
                        seen_unresolved.add(key)
                        unresolved.append(
                            f"undefined global name '{name}' referenced in scope '{scope_name}' (line {scope_line})"
                        )
                failures.extend(unresolved)

        required_strategy_methods = {
            "_build_long_review_prompt_payloads",
            "_extract_chat_message_text",
            "_render_long_context_json",
            "build_long_review_prompt",
            "_run_daily_long_review_events",
        }
        for method_name in sorted(required_strategy_methods):
            attr = getattr(type(self), method_name, None)
            if not callable(attr):
                failures.append(f"StrategyManager missing required callable: {method_name}")

        if failures:
            joined = "\n".join(failures)
            raise RuntimeError(
                "Startup static integrity check failed. Fix static strategy issues before starting strategy:\n"
                f"{joined}"
            )

    def _parse_timestamp(self, value: Any) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = raw.replace("T", " ")
        for parser in (
            lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
            lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M"),
            lambda text: datetime.strptime(text, "%Y-%m-%d"),
            lambda text: datetime.strptime(text, "%a, %d %b %Y %H:%M:%S %z"),
            lambda text: datetime.fromisoformat(text),
        ):
            try:
                parsed = parser(normalized)
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                return parsed
            except Exception:
                continue
        return None

    def _compact_text(self, value: Any, limit: int = 1200) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _extract_entry_timestamp(self, payload: Any, *keys: str) -> Optional[datetime]:
        if not isinstance(payload, dict):
            return None
        for key in keys:
            parsed = self._parse_timestamp(payload.get(key))
            if parsed is not None:
                return parsed
        return None

    def _status_is_inactive(self, status: Any) -> bool:
        normalized = str(status or "").strip().lower()
        return normalized in {
            "inactive",
            "invalidated",
            "closed",
            "completed",
            "resolved",
            "expired",
            "archived",
            "stopped",
            "done",
        }

    def _looks_like_stale_price_plan_text(self, value: Any) -> bool:
        text = self._compact_text(value, 320).lower()
        if not text:
            return False
        if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text):
            return True
        has_price_like_number = bool(re.search(r"\b\d{4,5}(?:\.\d+)?\b", text))
        has_level_language = bool(
            re.search(
                r"(support|resistance|breakout|reclaim|hold above|hold below|1h close|15m close|"
                r"突破|跌破|涨破|站稳|站回|收盘|支撑|阻力|关键位|上方|下方)",
                text,
            )
        )
        return has_price_like_number and has_level_language

    def _coerce_reflection_note_text(self, value: Any) -> str:
        if isinstance(value, dict):
            for key in ["text", "summary", "note", "item", "title"]:
                text = self._compact_text(value.get(key, ""), 280)
                if text:
                    return text
            return ""
        return self._compact_text(value, 280)

    def _sanitize_reflection_note_list(
        self,
        raw_items: Any,
        *,
        kind: str,
        limit: int,
    ) -> List[str]:
        if not isinstance(raw_items, list):
            return []
        cleaned: List[str] = []
        seen = set()
        for item in raw_items:
            if isinstance(item, dict) and self._status_is_inactive(item.get("status", "")):
                continue
            if isinstance(item, dict):
                expires_at = self._extract_entry_timestamp(item, "expires_at")
                if expires_at is not None and expires_at < datetime.now():
                    continue
            text = self._coerce_reflection_note_text(item)
            if not text:
                continue
            if kind in {"carryover", "watch_items"} and self._looks_like_stale_price_plan_text(text):
                continue
            normalized_key = text.strip().lower()
            if normalized_key in seen:
                continue
            seen.add(normalized_key)
            cleaned.append(text)
            if len(cleaned) >= limit:
                break
        return cleaned

    def _daily_episode_is_relevant(
        self,
        episode: Dict[str, Any],
        *,
        now: datetime,
        lookback_hours: int,
        active_hypothesis_id: str = "",
    ) -> bool:
        if not isinstance(episode, dict):
            return False
        recorded_at = self._extract_entry_timestamp(episode, "recorded_at")
        if recorded_at is not None and recorded_at >= now - timedelta(hours=lookback_hours):
            return True
        hypothesis_id = str(episode.get("hypothesis_id", "") or "").strip()
        return bool(active_hypothesis_id and hypothesis_id and hypothesis_id == active_hypothesis_id)

    def _prune_daily_execution_reflection(
        self,
        reflection: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
        for_prompt: bool = False,
        active_hypothesis_id: str = "",
    ) -> Dict[str, Any]:
        current_time = now or datetime.now()
        payload = deepcopy(reflection if isinstance(reflection, dict) else {})
        payload["carryover"] = self._sanitize_reflection_note_list(
            payload.get("carryover", []),
            kind="carryover",
            limit=DAILY_REFLECTION_CARRYOVER_PROMPT_LIMIT if for_prompt else 4,
        )
        payload["behavior_biases"] = self._sanitize_reflection_note_list(
            payload.get("behavior_biases", []),
            kind="behavior_biases",
            limit=DAILY_REFLECTION_BEHAVIOR_PROMPT_LIMIT if for_prompt else 6,
        )
        payload["watch_items"] = self._sanitize_reflection_note_list(
            payload.get("watch_items", []),
            kind="watch_items",
            limit=DAILY_REFLECTION_WATCH_PROMPT_LIMIT if for_prompt else 4,
        )

        raw_episodes = payload.get("recent_episodes", [])
        cleaned_episodes: List[Dict[str, Any]] = []
        if isinstance(raw_episodes, list):
            lookback_hours = (
                DAILY_REFLECTION_EPISODE_PROMPT_LOOKBACK_HOURS
                if for_prompt
                else DAILY_REFLECTION_EPISODE_STORE_LOOKBACK_HOURS
            )
            episode_limit = DAILY_REFLECTION_EPISODE_PROMPT_LIMIT if for_prompt else 8
            for item in raw_episodes:
                if not isinstance(item, dict):
                    continue
                if not self._daily_episode_is_relevant(
                    item,
                    now=current_time,
                    lookback_hours=lookback_hours,
                    active_hypothesis_id=active_hypothesis_id,
                ):
                    continue
                cleaned_episodes.append(item)
            cleaned_episodes = cleaned_episodes[-episode_limit:]
        payload["recent_episodes"] = cleaned_episodes
        return payload

    def _narrative_is_prompt_relevant(self, item: Dict[str, Any], *, now: datetime) -> bool:
        if not isinstance(item, dict):
            return False
        if self._status_is_inactive(item.get("status", "")):
            return False
        source_type = str(item.get("source_type", "") or "").strip().lower()
        if source_type == "execution":
            return False
        summary = self._compact_text(item.get("summary", ""), 240)
        if not summary:
            return False
        event_time = self._extract_entry_timestamp(item, "source_time", "recorded_at")
        if event_time is None:
            return True
        if event_time >= now:
            return True
        if event_time < now - timedelta(days=SHORT_PROMPT_NARRATIVE_LOOKBACK_DAYS):
            return False
        if source_type == "polymarket" and event_time < now - timedelta(days=1):
            return False
        return True

    def _select_relevant_narratives(
        self,
        raw_items: Any,
        *,
        now: Optional[datetime] = None,
        limit: int = SHORT_PROMPT_NARRATIVE_LIMIT,
    ) -> List[Dict[str, Any]]:
        current_time = now or datetime.now()
        if not isinstance(raw_items, list):
            return []
        ranked: List[Tuple[int, datetime, Dict[str, Any]]] = []
        for item in raw_items:
            if not self._narrative_is_prompt_relevant(item, now=current_time):
                continue
            event_time = self._extract_entry_timestamp(item, "source_time", "recorded_at") or datetime.min
            future_bonus = 1 if event_time >= current_time else 0
            ranked.append((future_bonus, event_time, deepcopy(item)))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [row[2] for row in ranked[:limit]]

    def _compact_narrative_for_prompt(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(item.get("id", "") or ""),
            "title": self._compact_text(item.get("title", ""), 80),
            "summary": self._compact_text(item.get("summary", ""), 180),
            "source_type": str(item.get("source_type", "") or ""),
            "source_time": str(item.get("source_time", "") or ""),
            "impact_bias": str(item.get("impact_bias", "") or ""),
            "status": str(item.get("status", "") or ""),
        }

    def _tactical_alert_is_relevant(
        self,
        alert: Dict[str, Any],
        *,
        now: datetime,
        active_hypothesis_id: str = "",
        storage_mode: bool = False,
    ) -> bool:
        if not isinstance(alert, dict):
            return False
        if self._status_is_inactive(alert.get("status", "")):
            return False
        if self._is_alarm_like_tactical_alert(alert) and self._is_expired_tactical_alert(alert, now=now):
            return False
        related_hypothesis_id = str(alert.get("related_hypothesis_id", "") or "").strip()
        if active_hypothesis_id and related_hypothesis_id and related_hypothesis_id == active_hypothesis_id:
            return True
        expires_at = self._extract_entry_timestamp(alert, "expires_at")
        if expires_at is not None and expires_at >= now:
            return True
        recorded_at = self._extract_entry_timestamp(alert, "recorded_at")
        if recorded_at is None:
            return False
        lookback_hours = SHORT_MEMORY_TACTICAL_STORE_LOOKBACK_HOURS if storage_mode else SHORT_PROMPT_TACTICAL_LOOKBACK_HOURS
        return recorded_at >= now - timedelta(hours=lookback_hours)

    def _select_relevant_tactical_alerts(
        self,
        raw_items: Any,
        *,
        now: Optional[datetime] = None,
        active_hypothesis_id: str = "",
        storage_mode: bool = False,
    ) -> Tuple[List[Dict[str, Any]], int]:
        current_time = now or datetime.now()
        if not isinstance(raw_items, list):
            return [], 0
        selected: List[Tuple[datetime, Dict[str, Any]]] = []
        suppressed_expired_alarm_alerts = 0
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            if self._is_alarm_like_tactical_alert(item) and self._is_expired_tactical_alert(item, now=current_time):
                suppressed_expired_alarm_alerts += 1
                continue
            if not self._tactical_alert_is_relevant(
                item,
                now=current_time,
                active_hypothesis_id=active_hypothesis_id,
                storage_mode=storage_mode,
            ):
                continue
            recorded_at = self._extract_entry_timestamp(item, "recorded_at") or datetime.min
            selected.append((recorded_at, deepcopy(item)))
        selected.sort(key=lambda row: row[0], reverse=True)
        limit = (
            SHORT_MEMORY_TACTICAL_STORE_LIMIT
            if storage_mode
            else SHORT_PROMPT_DECISION_ALERT_LIMIT + SHORT_PROMPT_HISTORICAL_ALERT_LIMIT
        )
        return [row[1] for row in selected[:limit]], suppressed_expired_alarm_alerts

    def _prune_short_memory_noise(
        self,
        snapshot: Dict[str, Any],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        current_time = now or datetime.now()
        payload = deepcopy(snapshot if isinstance(snapshot, dict) else {})
        day_plan = payload.get("day_plan", {})
        if isinstance(day_plan, dict):
            for field_name in ("hold_until", "recheck_at"):
                raw_value = str(day_plan.get(field_name, "") or "").strip()
                parsed = self._parse_memory_datetime(raw_value)
                if raw_value and parsed is not None and parsed <= current_time:
                    day_plan[field_name] = ""
        active_hypothesis = payload.get("active_hypothesis", {})
        active_hypothesis_id = (
            str(active_hypothesis.get("hypothesis_id", "") or "").strip()
            if isinstance(active_hypothesis, dict)
            else ""
        )
        payload["narrative_tracking"] = self._select_relevant_narratives(
            payload.get("narrative_tracking", []),
            now=current_time,
            limit=SHORT_MEMORY_NARRATIVE_STORE_LIMIT,
        )
        payload["tactical_alerts"], _ = self._select_relevant_tactical_alerts(
            payload.get("tactical_alerts", []),
            now=current_time,
            active_hypothesis_id=active_hypothesis_id,
            storage_mode=True,
        )
        return payload

    def _compact_long_review_hypothesis(
        self,
        payload: Any,
        *,
        include_invalidation_reason: bool = False,
    ) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        compacted = {
            "id": str(payload.get("id", "") or ""),
            "title": self._compact_text(payload.get("title", ""), 100),
            "statement": self._compact_text(payload.get("statement", ""), 320),
            "formed_at": str(payload.get("formed_at", "") or ""),
            "basis": [self._compact_text(item, 180) for item in payload.get("basis", []) if str(item or "").strip()][:4],
            "expected_effect_on_eth": str(payload.get("expected_effect_on_eth", "") or ""),
            "expected_window": self._compact_text(payload.get("expected_window", ""), 40),
            "challenge_conditions": [
                self._compact_text(item, 180) for item in payload.get("challenge_conditions", []) if str(item or "").strip()
            ][:4],
            "linked_case_ids": [str(item or "") for item in payload.get("linked_case_ids", []) if str(item or "").strip()][:8],
            "status": str(payload.get("status", "") or ""),
        }
        origin = str(payload.get("origin", "") or "")
        if origin:
            compacted["origin"] = origin
        if include_invalidation_reason:
            invalidation_reason = self._compact_text(payload.get("invalidation_reason", ""), 220)
            if invalidation_reason:
                compacted["invalidation_reason"] = invalidation_reason
        return compacted

    def _compact_long_review_case(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        compacted = {
            "id": str(payload.get("id", "") or ""),
            "source_type": str(payload.get("source_type", "") or ""),
            "source_id": str(payload.get("source_id", "") or ""),
            "headline_or_question": self._compact_text(payload.get("headline_or_question", ""), 180),
            "event_time": str(payload.get("event_time", "") or ""),
            "recorded_at": str(payload.get("recorded_at", "") or ""),
            "gate_decision": str(payload.get("gate_decision", "") or ""),
            "why": self._compact_text(payload.get("why", ""), 200),
            "impact_bias": str(payload.get("impact_bias", "") or ""),
            "linked_hypothesis_ids": [
                str(item or "") for item in payload.get("linked_hypothesis_ids", []) if str(item or "").strip()
            ][:6],
        }
        price_context = payload.get("price_context_at_ingestion", {})
        if isinstance(price_context, dict) and price_context:
            compacted["price_context_at_ingestion"] = {
                "daily_bias": str(price_context.get("daily_bias", "") or ""),
                "h12_bias": str(price_context.get("h12_bias", "") or ""),
            }
        return compacted

    def _compact_long_review_memory(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        compacted: Dict[str, Any] = {
            "meta": payload.get("meta", {}) if isinstance(payload.get("meta", {}), dict) else {},
            "validated_rules": [],
            "invalidated_rules": [],
            "operator_notes": [],
        }
        for rule in payload.get("validated_rules", [])[:12]:
            if not isinstance(rule, dict):
                continue
            compacted["validated_rules"].append(
                {
                    "id": str(rule.get("id", "") or ""),
                    "title": self._compact_text(rule.get("title", ""), 100),
                    "rule": self._compact_text(rule.get("rule", ""), 220),
                    "evidence": self._compact_text(rule.get("evidence", ""), 200),
                    "falsification": self._compact_text(rule.get("falsification", ""), 200),
                    "verification_plan": self._compact_text(rule.get("verification_plan", ""), 180),
                    "status": str(rule.get("status", "") or ""),
                    "updated_at": str(rule.get("updated_at", "") or ""),
                }
            )
        for rule in payload.get("invalidated_rules", [])[-8:]:
            if not isinstance(rule, dict):
                continue
            compacted["invalidated_rules"].append(
                {
                    "id": str(rule.get("id", "") or ""),
                    "title": self._compact_text(rule.get("title", ""), 100),
                    "rule": self._compact_text(rule.get("rule", ""), 200),
                    "why_invalidated": self._compact_text(
                        rule.get("why_invalidated", "") or rule.get("falsification", ""),
                        200,
                    ),
                    "updated_at": str(rule.get("updated_at", "") or ""),
                }
            )
        for note in payload.get("operator_notes", [])[-8:]:
            if not isinstance(note, dict):
                continue
            compacted["operator_notes"].append(
                {
                    "id": str(note.get("id", "") or ""),
                    "title": self._compact_text(note.get("title", ""), 100),
                    "summary": self._compact_text(note.get("summary", ""), 220),
                    "updated_at": str(note.get("updated_at", "") or ""),
                }
            )
        return compacted

    def _compact_long_review_execution_reflection(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        payload = self._prune_daily_execution_reflection(payload, for_prompt=True)
        compacted: Dict[str, Any] = {
            "meta": payload.get("meta", {}) if isinstance(payload.get("meta", {}), dict) else {},
            "closed_position_feedback": self._sanitize_closed_position_feedback(payload.get("closed_position_feedback", {})),
            "carryover": [self._compact_text(item, 200) for item in payload.get("carryover", []) if str(item or "").strip()][:4],
            "behavior_biases": [
                self._compact_text(item, 200) for item in payload.get("behavior_biases", []) if str(item or "").strip()
            ][:6],
            "watch_items": [self._compact_text(item, 180) for item in payload.get("watch_items", []) if str(item or "").strip()][:6],
            "recent_episodes": [],
        }
        for item in payload.get("recent_episodes", [])[-5:]:
            if not isinstance(item, dict):
                continue
            compacted["recent_episodes"].append(
                {
                    "episode_id": str(item.get("episode_id", "") or ""),
                    "recorded_at": str(item.get("recorded_at", "") or ""),
                    "hypothesis_id": str(item.get("hypothesis_id", "") or ""),
                    "direction": str(item.get("direction", "") or ""),
                    "action": self._compact_text(item.get("action", ""), 120),
                    "outcome": self._compact_text(item.get("outcome", ""), 120),
                    "why_entered": self._compact_text(item.get("why_entered", ""), 180),
                    "why_failed": self._compact_text(item.get("why_failed", ""), 180),
                    "retry_guardrail": self._compact_text(item.get("retry_guardrail", ""), 180),
                }
            )
        return compacted

    def _compact_post_stop_reflection_for_long_review(self, payload: Any) -> Dict[str, Any]:
        normalized = self._sanitize_post_stop_reflection(payload, source="long_review_prompt")
        if not isinstance(normalized, dict):
            return {}

        def _compact_entry(item: Any) -> Dict[str, Any]:
            if not isinstance(item, dict):
                return {}
            return {
                "episode_id": str(item.get("episode_id", "") or ""),
                "recorded_at": str(item.get("recorded_at", "") or ""),
                "expires_at": str(item.get("expires_at", "") or ""),
                "source_log_dir": str(item.get("source_log_dir", "") or ""),
                "event_type": str(item.get("event_type", "") or ""),
                "hypothesis_id": str(item.get("hypothesis_id", "") or ""),
                "direction": str(item.get("direction", "") or ""),
                "trigger_context": self._compact_text(item.get("trigger_context", ""), 180),
                "outcome": self._compact_text(item.get("outcome", ""), 140),
                "why_entered": self._compact_text(item.get("why_entered", ""), 180),
                "why_failed": self._compact_text(item.get("why_failed", ""), 180),
                "retry_guardrail": self._compact_text(item.get("retry_guardrail", ""), 180),
            }

        return {
            "meta": normalized.get("meta", {}),
            "active_reflection": _compact_entry(normalized.get("active_reflection", {})),
            "recent_reflections": [
                _compact_entry(item) for item in normalized.get("recent_reflections", [])[-5:] if isinstance(item, dict)
            ],
        }

    def _build_long_review_prompt_payloads(
        self,
        long_reflection: Dict[str, Any],
        long_memory: Dict[str, Any],
        daily_execution_reflection: Dict[str, Any],
        post_stop_reflection: Dict[str, Any],
        recent_execution_history: List[Dict[str, Any]],
        closed_position_feedback: Dict[str, Any],
    ) -> Dict[str, Any]:
        compact_long_reflection = {
            "meta": long_reflection.get("meta", {}) if isinstance(long_reflection.get("meta", {}), dict) else {},
            "active_hypotheses": [
                self._compact_long_review_hypothesis(item) for item in long_reflection.get("active_hypotheses", []) if isinstance(item, dict)
            ][:12],
            "invalidated_hypotheses": [
                self._compact_long_review_hypothesis(item, include_invalidation_reason=True)
                for item in long_reflection.get("invalidated_hypotheses", [])[-12:]
                if isinstance(item, dict)
            ],
            "news_case_log": [
                self._compact_long_review_case(item) for item in long_reflection.get("news_case_log", [])[-20:] if isinstance(item, dict)
            ],
        }
        compact_recent_execution_history = [
            {
                "log_dir": str(item.get("log_dir", "") or ""),
                "recorded_at": str(item.get("recorded_at", "") or ""),
                "event_type": str(item.get("event_type", "") or ""),
                "execution_txt": self._compact_text(item.get("execution_txt", ""), 180),
                "explanation": self._compact_text(item.get("explanation", ""), 180),
                "action_intent": str(item.get("action_intent", "") or ""),
                "plan_transition": str(item.get("plan_transition", "") or ""),
                "hypothesis_action": str(item.get("hypothesis_action", "") or ""),
                "state_change_evidence": self._compact_text(item.get("state_change_evidence", ""), 160),
                "falsification_point": self._compact_text(item.get("falsification_point", ""), 140),
                "validation_status": str(item.get("validation_status", "") or ""),
            }
            for item in recent_execution_history[-24:]
            if isinstance(item, dict)
        ]
        return {
            "long_reflection": compact_long_reflection,
            "long_textbook": self._compact_long_review_memory(long_memory),
            "daily_execution_reflection": self._compact_long_review_execution_reflection(daily_execution_reflection),
            "post_stop_reflection": self._compact_post_stop_reflection_for_long_review(post_stop_reflection),
            "recent_execution_history": compact_recent_execution_history,
            "closed_position_feedback": self._sanitize_closed_position_feedback(closed_position_feedback),
        }

    def _extract_chart_block(self, snapshot: str, chart_name: str) -> str:
        pattern = re.compile(
            rf"(?ms)^##\s+{re.escape(chart_name)} chart\s*\n(.*?)(?=^##\s+|\Z)"
        )
        match = pattern.search(str(snapshot or ""))
        return match.group(1).strip() if match else ""

    def _extract_chart_subsection(self, chart_block: str, subsection_name: str) -> str:
        pattern = re.compile(
            rf"(?ms)^###\s+{re.escape(subsection_name)}\s*\n(.*?)(?=^###\s+|\Z)"
        )
        match = pattern.search(str(chart_block or ""))
        return match.group(1).strip() if match else ""

    def _infer_momentum_bias_from_text(self, text: str) -> str:
        lower = str(text or "").lower()
        if not lower.strip():
            return "mixed"
        bullish_terms = [
            "strengthening_up",
            "weakening_down",
            "oversold_rebounding",
            "mid_rising",
            "expanding_bullish",
            "contracting_bearish",
        ]
        bearish_terms = [
            "strengthening_down",
            "weakening_up",
            "overbought_fading",
            "mid_falling",
            "expanding_bearish",
            "contracting_bullish",
        ]
        bull_score = sum(1 for term in bullish_terms if term in lower)
        bear_score = sum(1 for term in bearish_terms if term in lower)
        if bull_score > bear_score:
            return "bullish"
        if bear_score > bull_score:
            return "bearish"
        return "mixed"

    def _extract_price_context(self, market_snapshot: str) -> Dict[str, Any]:
        daily_block = self._extract_chart_block(market_snapshot, "daily")
        h12_block = self._extract_chart_block(market_snapshot, "12 hours")
        daily_momentum = self._extract_chart_subsection(daily_block, "Momentum Ruler")
        h12_momentum = self._extract_chart_subsection(h12_block, "Momentum Ruler")
        return {
            "daily_summary": self._compact_text(daily_block, 1600),
            "h12_summary": self._compact_text(h12_block, 1600),
            "daily_momentum_summary": self._compact_text(daily_momentum, 600),
            "h12_momentum_summary": self._compact_text(h12_momentum, 600),
            "daily_momentum_bias": self._infer_momentum_bias_from_text(daily_momentum),
            "h12_momentum_bias": self._infer_momentum_bias_from_text(h12_momentum),
            "source": "Binance metrics snapshot",
        }

    def _build_short_term_market_snapshot(self, market_snapshot: str) -> str:
        sections: List[str] = []
        for chart_name in ["4 hours", "1 hour", "15 minutes"]:
            block = self._extract_chart_block(market_snapshot, chart_name)
            if not block:
                continue
            sections.append(f"## {chart_name} chart\n{block}")
        if sections:
            return (
                "# Short-Term Market Snapshot\n\n"
                "Only execution horizons are included here. Daily and 12-hour raw text is intentionally hidden "
                "from the short-term agent and only exposed through long_horizon_view.\n\n"
                + "\n\n".join(sections)
            )
        return "Short-term market snapshot unavailable. Use long_horizon_view for higher-horizon context."

    def _extract_chart_latest_reference(self, market_snapshot: str, chart_name: str) -> Dict[str, Any]:
        block = self._extract_chart_block(market_snapshot, chart_name)
        if not block:
            return {}

        latest_section = self._extract_chart_subsection(block, "Latest Indicators Summary")
        momentum_section = self._extract_chart_subsection(block, "Momentum Ruler")
        latest_close = None
        latest_timestamp = ""
        table_rows: List[List[str]] = []
        for line in str(block or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            fields = [part.strip() for part in stripped.strip("|").split("|")]
            if len(fields) < 8:
                continue
            if fields[0].lower() == "timestamp" or fields[0].startswith(":---"):
                continue
            try:
                close_value = float(fields[1])
            except Exception:
                continue
            latest_close = close_value
            latest_timestamp = fields[0]
            table_rows.append(fields)

        def _extract_float(pattern: str, text: str) -> Optional[float]:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                return None
            try:
                return float(match.group(1))
            except Exception:
                return None

        rsi = _extract_float(r"RSI\(14\)\*\*:\s*([-+]?\d+(?:\.\d+)?)", latest_section)
        macd_histo = _extract_float(r"Histo:\s*([-+]?\d+(?:\.\d+)?)", latest_section)
        upper = mid = lower = None
        band_match = re.search(
            r"Upper\s+([-+]?\d+(?:\.\d+)?)\s*\|\s*Mid\s+([-+]?\d+(?:\.\d+)?)\s*\|\s*Lower\s+([-+]?\d+(?:\.\d+)?)",
            latest_section,
            re.IGNORECASE,
        )
        if band_match:
            try:
                upper = float(band_match.group(1))
                mid = float(band_match.group(2))
                lower = float(band_match.group(3))
            except Exception:
                upper = mid = lower = None

        def _extract_state(label: str) -> str:
            match = re.search(rf"{re.escape(label)}\*\*:\s*`([^`]+)`", momentum_section, re.IGNORECASE)
            return str(match.group(1)).strip() if match else ""

        return {
            "chart_name": chart_name,
            "latest_close": latest_close,
            "latest_timestamp": latest_timestamp,
            "rsi": rsi,
            "macd_histo": macd_histo,
            "bollinger_upper": upper,
            "bollinger_mid": mid,
            "bollinger_lower": lower,
            "price_momentum": _extract_state("price_momentum"),
            "rsi_state": _extract_state("rsi_state"),
            "macd_histo_state": _extract_state("macd_histo_state"),
            "table_rows": table_rows[-12:],
        }

    def _build_recent_range_quality_snapshot(self, chart_ref: Dict[str, Any]) -> Dict[str, Any]:
        rows = chart_ref.get("table_rows", []) if isinstance(chart_ref, dict) else []
        normalized: List[Dict[str, float]] = []
        for fields in rows if isinstance(rows, list) else []:
            if not isinstance(fields, list) or len(fields) < 10:
                continue
            close_value = self._safe_float(fields[1])
            upper_value = self._safe_float(fields[8])
            lower_value = self._safe_float(fields[9])
            if (
                close_value is None
                or upper_value is None
                or lower_value is None
                or close_value <= 0
                or upper_value <= lower_value
            ):
                continue
            width = upper_value - lower_value
            position = max(0.0, min(1.0, (close_value - lower_value) / width))
            normalized.append(
                {
                    "close": close_value,
                    "upper": upper_value,
                    "lower": lower_value,
                    "width": width,
                    "width_pct": (width / close_value) * 100.0,
                    "position": position,
                }
            )

        if len(normalized) < 6:
            return {
                "available": False,
                "reason": "insufficient_recent_band_rows",
            }

        positions = [row["position"] for row in normalized]
        width_pcts = [row["width_pct"] for row in normalized]
        upper_touch_count = sum(1 for pos in positions if pos >= 0.75)
        lower_touch_count = sum(1 for pos in positions if pos <= 0.25)

        def _bucket(pos: float) -> str:
            if pos >= 0.72:
                return "U"
            if pos <= 0.28:
                return "L"
            return "M"

        bucket_seq: List[str] = []
        for pos in positions:
            bucket = _bucket(pos)
            if not bucket_seq or bucket_seq[-1] != bucket:
                bucket_seq.append(bucket)

        midpoint_cross_count = 0
        last_side = ""
        for pos in positions:
            side = "U" if pos >= 0.55 else "L" if pos <= 0.45 else ""
            if side and last_side and side != last_side:
                midpoint_cross_count += 1
            if side:
                last_side = side

        swing_count = 0
        last_extreme = ""
        for bucket in bucket_seq:
            if bucket not in {"U", "L"}:
                continue
            if last_extreme and bucket != last_extreme:
                swing_count += 1
            last_extreme = bucket

        avg_width_pct = sum(width_pcts) / len(width_pcts)
        span = max(positions) - min(positions)
        oscillation_score = min(
            1.0,
            0.35 * min(span / 0.8, 1.0)
            + 0.25 * min(avg_width_pct / 1.2, 1.0)
            + 0.20 * min((upper_touch_count + lower_touch_count) / 6.0, 1.0)
            + 0.20 * min((midpoint_cross_count + swing_count) / 5.0, 1.0),
        )

        latest_position = positions[-1]
        edge_touch_total = upper_touch_count + lower_touch_count
        structural_reversion_present = bool(
            midpoint_cross_count >= 1
            or swing_count >= 1
            or (upper_touch_count >= 2 and lower_touch_count >= 1)
            or (lower_touch_count >= 2 and upper_touch_count >= 1)
        )
        if (
            oscillation_score >= 0.72
            and avg_width_pct >= 0.55
            and (edge_touch_total >= 4 or midpoint_cross_count >= 2 or swing_count >= 1)
        ):
            activation_bias = "strong"
        elif (
            oscillation_score >= 0.58
            and avg_width_pct >= 0.45
            and (edge_touch_total >= 3 or midpoint_cross_count >= 1 or swing_count >= 1)
        ):
            activation_bias = "moderate"
        else:
            activation_bias = "weak"

        if activation_bias == "strong":
            if latest_position >= 0.66 and upper_touch_count >= 2:
                suggested_mode = "short_only"
            elif latest_position <= 0.34 and lower_touch_count >= 2:
                suggested_mode = "long_only"
            elif structural_reversion_present:
                suggested_mode = "symmetric"
            else:
                suggested_mode = "symmetric"
        elif activation_bias == "moderate":
            if latest_position >= 0.7 and upper_touch_count >= 2:
                suggested_mode = "short_only"
            elif latest_position <= 0.3 and lower_touch_count >= 2:
                suggested_mode = "long_only"
            elif structural_reversion_present:
                suggested_mode = "symmetric"
            else:
                suggested_mode = "low_confidence"
        else:
            suggested_mode = "low_confidence"

        return {
            "available": True,
            "recent_bar_count": len(normalized),
            "upper_touch_count": upper_touch_count,
            "lower_touch_count": lower_touch_count,
            "midpoint_cross_count": midpoint_cross_count,
            "swing_count": swing_count,
            "avg_width_pct": round(avg_width_pct, 2),
            "span_pct_of_band": round(span * 100.0, 1),
            "oscillation_score": round(oscillation_score, 2),
            "activation_bias": activation_bias,
            "suggested_range_mode": suggested_mode,
        }

    def _default_long_reflection(self, *, source: str = "system") -> Dict[str, Any]:
        return {
            "meta": {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "updated_at": _utc_now_iso(),
                "source": source,
            },
            "active_hypotheses": [],
            "invalidated_hypotheses": [],
            "news_case_log": [],
        }

    def _default_daily_execution_reflection(self, *, source: str = "system") -> Dict[str, Any]:
        return {
            "meta": {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "updated_at": _utc_now_iso(),
                "source": source,
            },
            "closed_position_feedback": self._default_closed_position_feedback(),
            "carryover": [],
            "behavior_biases": [],
            "watch_items": [],
            "recent_episodes": [],
        }

    def _default_closed_position_feedback(self) -> Dict[str, Any]:
        return {
            "window_days": 0,
            "source_file": "",
            "source_updated_at": "",
            "source_stale": False,
            "source_age_hours": 0.0,
            "range_start": "",
            "range_end": "",
            "position_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": 0.0,
            "realized_pnl_eth": 0.0,
            "net_pnl_after_fee_eth": 0.0,
            "average_return_pct": 0.0,
            "long_count": 0,
            "long_net_pnl_after_fee_eth": 0.0,
            "short_count": 0,
            "short_net_pnl_after_fee_eth": 0.0,
            "top_losses": [],
            "top_wins": [],
        }

    def _default_post_stop_reflection(self, *, source: str = "system") -> Dict[str, Any]:
        return {
            "meta": {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "updated_at": _utc_now_iso(),
                "source": source,
            },
            "active_reflection": {},
            "recent_reflections": [],
        }

    def _default_long_review_state(self) -> Dict[str, Any]:
        return {
            "last_review_date": "",
            "last_triggered_at": "",
            "last_run_status": "",
            "last_run_log_dir": "",
            "last_error": "",
        }

    def _sanitize_long_reflection(self, payload: Any, *, source: str) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}
        normalized = self._default_long_reflection(source=source)
        meta = payload.get("meta", {})
        if isinstance(meta, dict):
            try:
                schema_version = int(meta.get("schema_version", MEMORY_SCHEMA_VERSION) or MEMORY_SCHEMA_VERSION)
            except Exception:
                schema_version = MEMORY_SCHEMA_VERSION
            normalized["meta"].update(
                {
                    "schema_version": schema_version,
                    "updated_at": str(meta.get("updated_at", normalized["meta"]["updated_at"]) or normalized["meta"]["updated_at"]),
                    "source": str(meta.get("source", source) or source),
                }
            )
        for key in ["active_hypotheses", "invalidated_hypotheses", "news_case_log"]:
            value = payload.get(key, [])
            normalized[key] = value if isinstance(value, list) else []
        case_log, case_id_aliases = self._canonicalize_long_case_log(normalized.get("news_case_log", []))
        normalized["news_case_log"] = case_log
        self._canonicalize_long_reflection_linked_case_ids(normalized, case_id_aliases)
        return normalized

    def _canonicalize_long_case_log(self, case_log: Any) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        if not isinstance(case_log, list):
            return [], {}
        deduped: List[Dict[str, Any]] = []
        aliases: Dict[str, str] = {}
        seen_source_keys = set()
        seen_case_ids = set()
        for item in case_log:
            if not isinstance(item, dict):
                continue
            entry = deepcopy(item)
            case_id = str(entry.get("id", "") or "").strip()
            source_type = str(entry.get("source_type", "") or "").strip().lower()
            source_id = str(entry.get("source_id", "") or "").strip()
            source_key = f"{source_type}:{source_id}" if source_type and source_id else ""
            if source_key and source_key in seen_source_keys:
                canonical_id = next(
                    (
                        str(existing.get("id", "") or "").strip()
                        for existing in deduped
                        if isinstance(existing, dict)
                        and str(existing.get("source_type", "") or "").strip().lower() == source_type
                        and str(existing.get("source_id", "") or "").strip() == source_id
                    ),
                    "",
                )
                if case_id and canonical_id and case_id != canonical_id:
                    aliases[case_id] = canonical_id
                continue
            if case_id and case_id in seen_case_ids:
                continue
            deduped.append(entry)
            if source_key:
                seen_source_keys.add(source_key)
            if case_id:
                seen_case_ids.add(case_id)
        return deduped[-400:], aliases

    def _sanitize_daily_execution_reflection(self, payload: Any, *, source: str) -> Dict[str, Any]:
        normalized = self._default_daily_execution_reflection(source=source)
        if not isinstance(payload, dict):
            return normalized

        meta = payload.get("meta", {})
        if isinstance(meta, dict):
            normalized["meta"]["updated_at"] = str(
                meta.get("updated_at", normalized["meta"]["updated_at"]) or normalized["meta"]["updated_at"]
            )

        normalized["closed_position_feedback"] = self._sanitize_closed_position_feedback(
            payload.get("closed_position_feedback", {}),
        )

        normalized["carryover"] = self._sanitize_reflection_note_list(
            payload.get("carryover", []),
            kind="carryover",
            limit=4,
        )
        normalized["behavior_biases"] = self._sanitize_reflection_note_list(
            payload.get("behavior_biases", []),
            kind="behavior_biases",
            limit=6,
        )
        normalized["watch_items"] = self._sanitize_reflection_note_list(
            payload.get("watch_items", []),
            kind="watch_items",
            limit=4,
        )

        raw_episodes = payload.get("recent_episodes", [])
        if isinstance(raw_episodes, list):
            cleaned_episodes: List[Dict[str, Any]] = []
            for item in raw_episodes[-12:]:
                if not isinstance(item, dict):
                    continue
                cleaned_episodes.append(
                    {
                        "episode_id": str(item.get("episode_id", "") or ""),
                        "recorded_at": str(item.get("recorded_at", "") or ""),
                        "hypothesis_id": str(item.get("hypothesis_id", "") or ""),
                        "direction": str(item.get("direction", "") or ""),
                        "action": self._compact_text(item.get("action", ""), 160),
                        "outcome": self._compact_text(item.get("outcome", ""), 160),
                        "why_entered": self._compact_text(item.get("why_entered", ""), 220),
                        "why_failed": self._compact_text(item.get("why_failed", ""), 220),
                        "retry_guardrail": self._compact_text(item.get("retry_guardrail", ""), 220),
                    }
                )
            normalized["recent_episodes"] = cleaned_episodes

        normalized = self._prune_daily_execution_reflection(normalized, now=datetime.now(), for_prompt=False)
        normalized["meta"]["schema_version"] = MEMORY_SCHEMA_VERSION
        normalized["meta"]["updated_at"] = _utc_now_iso()
        normalized["meta"]["source"] = source
        return normalized

    def _sanitize_post_stop_reflection_entry(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        normalized = {
            "episode_id": str(payload.get("episode_id", "") or ""),
            "recorded_at": str(payload.get("recorded_at", "") or ""),
            "expires_at": str(payload.get("expires_at", "") or ""),
            "source_log_dir": str(payload.get("source_log_dir", "") or ""),
            "event_type": str(payload.get("event_type", "") or ""),
            "hypothesis_id": str(payload.get("hypothesis_id", "") or ""),
            "direction": str(payload.get("direction", "") or ""),
            "trigger_context": self._compact_text(payload.get("trigger_context", ""), 220),
            "outcome": self._compact_text(payload.get("outcome", ""), 180),
            "why_entered": self._compact_text(payload.get("why_entered", ""), 220),
            "why_failed": self._compact_text(payload.get("why_failed", ""), 220),
            "retry_guardrail": self._compact_text(payload.get("retry_guardrail", ""), 220),
        }
        if not normalized["episode_id"] or not normalized["recorded_at"]:
            return {}
        return normalized

    def _sanitize_post_stop_reflection(self, payload: Any, *, source: str) -> Dict[str, Any]:
        normalized = self._default_post_stop_reflection(source=source)
        if not isinstance(payload, dict):
            return normalized

        meta = payload.get("meta", {})
        if isinstance(meta, dict):
            normalized["meta"]["updated_at"] = str(
                meta.get("updated_at", normalized["meta"]["updated_at"]) or normalized["meta"]["updated_at"]
            )

        active = self._sanitize_post_stop_reflection_entry(payload.get("active_reflection", {}))
        if active:
            normalized["active_reflection"] = active

        raw_recent = payload.get("recent_reflections", [])
        if isinstance(raw_recent, list):
            cleaned_recent: List[Dict[str, Any]] = []
            seen_episode_ids = set()
            for item in raw_recent[-12:]:
                cleaned = self._sanitize_post_stop_reflection_entry(item)
                episode_id = str(cleaned.get("episode_id", "") or "")
                if not cleaned or not episode_id or episode_id in seen_episode_ids:
                    continue
                cleaned_recent.append(cleaned)
                seen_episode_ids.add(episode_id)
            normalized["recent_reflections"] = cleaned_recent

        if normalized["active_reflection"]:
            active_id = str(normalized["active_reflection"].get("episode_id", "") or "")
            if active_id and active_id not in {
                str(item.get("episode_id", "") or "") for item in normalized["recent_reflections"]
            }:
                normalized["recent_reflections"] = (normalized["recent_reflections"] + [normalized["active_reflection"]])[-12:]

        current_time = datetime.now()
        active_reflection = normalized.get("active_reflection", {})
        if isinstance(active_reflection, dict) and active_reflection and not self._post_stop_reflection_is_live(
            active_reflection,
            now=current_time,
        ):
            normalized["active_reflection"] = {}

        pruned_recent: List[Dict[str, Any]] = []
        for item in normalized.get("recent_reflections", []):
            if not isinstance(item, dict):
                continue
            recorded_at = self._extract_entry_timestamp(item, "recorded_at")
            if recorded_at is None or recorded_at >= current_time - timedelta(hours=POST_STOP_RECENT_STORE_LOOKBACK_HOURS):
                pruned_recent.append(item)
        normalized["recent_reflections"] = pruned_recent[-8:]

        normalized["meta"]["schema_version"] = MEMORY_SCHEMA_VERSION
        normalized["meta"]["updated_at"] = _utc_now_iso()
        normalized["meta"]["source"] = source
        return normalized

    def _canonicalize_long_reflection_linked_case_ids(
        self,
        reflection: Dict[str, Any],
        case_id_aliases: Dict[str, str],
    ) -> None:
        if not isinstance(reflection, dict):
            return
        valid_case_ids = {
            str(item.get("id", "") or "").strip()
            for item in reflection.get("news_case_log", [])
            if isinstance(item, dict) and str(item.get("id", "") or "").strip()
        }
        for bucket in ["active_hypotheses", "invalidated_hypotheses"]:
            hypotheses = reflection.get(bucket, [])
            if not isinstance(hypotheses, list):
                continue
            for hypothesis in hypotheses:
                if not isinstance(hypothesis, dict):
                    continue
                linked_case_ids = hypothesis.get("linked_case_ids", [])
                if not isinstance(linked_case_ids, list):
                    hypothesis["linked_case_ids"] = []
                    continue
                normalized_ids: List[str] = []
                seen_ids = set()
                for raw_case_id in linked_case_ids:
                    case_id = str(raw_case_id or "").strip()
                    if not case_id:
                        continue
                    case_id = case_id_aliases.get(case_id, case_id)
                    if case_id not in valid_case_ids or case_id in seen_ids:
                        continue
                    normalized_ids.append(case_id)
                    seen_ids.add(case_id)
                hypothesis["linked_case_ids"] = normalized_ids

    def _bridge_long_memory_to_reflection(
        self,
        reflection: Dict[str, Any],
        long_memory: Dict[str, Any],
    ) -> Dict[str, Any]:
        next_reflection = deepcopy(reflection if isinstance(reflection, dict) else self._default_long_reflection(source="bridge"))
        validated_rules = long_memory.get("validated_rules", []) if isinstance(long_memory, dict) else []
        if not isinstance(validated_rules, list):
            validated_rules = []
        active = next_reflection.get("active_hypotheses", [])
        if not isinstance(active, list):
            active = []
        invalidated = next_reflection.get("invalidated_hypotheses", [])
        if not isinstance(invalidated, list):
            invalidated = []
        existing_ids = {str(item.get("id", "")) for item in active if isinstance(item, dict)}
        existing_ids.update({str(item.get("id", "")) for item in invalidated if isinstance(item, dict)})
        added = False
        for idx, rule in enumerate(validated_rules):
            if not isinstance(rule, dict):
                continue
            basis = []
            for field in ["evidence", "verification_plan", "falsification"]:
                value = str(rule.get(field, "") or "").strip()
                if value:
                    basis.append(value)
            rule_id = str(rule.get("id", "") or f"bridge-rule-{idx + 1}")
            bridge_id = f"bridge-{_slugify_memory_id(rule_id, str(idx + 1))}"
            if bridge_id in existing_ids:
                continue
            statement = str(rule.get("rule", "") or "").strip()
            if not statement:
                continue
            active.append(
                {
                    "id": bridge_id,
                    "title": str(rule.get("title", "") or "Bridged long-memory rule"),
                    "statement": statement,
                    "formed_at": str(rule.get("updated_at", "") or _utc_now_iso()),
                    "basis": basis or ["Bridged from mem/long.json validated_rules."],
                    "expected_effect_on_eth": "mixed",
                    "expected_window": "24h-7d",
                    "challenge_conditions": [
                        str(rule.get("falsification", "") or "Higher-horizon evidence disproves this bridged rule.")
                    ],
                    "linked_case_ids": [],
                    "status": "active",
                    "origin": "long_json_bridge",
                }
            )
            existing_ids.add(bridge_id)
            added = True
        next_reflection["active_hypotheses"] = active
        if added:
            next_reflection.setdefault("meta", {})
            next_reflection["meta"]["updated_at"] = _utc_now_iso()
            next_reflection["meta"]["source"] = "long_json_bridge"
        return next_reflection

    def _sanitize_closed_position_feedback(self, payload: Any) -> Dict[str, Any]:
        normalized = self._default_closed_position_feedback()
        if not isinstance(payload, dict):
            return normalized

        int_fields = {
            "window_days",
            "position_count",
            "win_count",
            "loss_count",
            "long_count",
            "short_count",
        }
        float_fields = {
            "win_rate_pct",
            "realized_pnl_eth",
            "net_pnl_after_fee_eth",
            "average_return_pct",
            "long_net_pnl_after_fee_eth",
            "short_net_pnl_after_fee_eth",
            "source_age_hours",
        }
        text_fields = {
            "source_file",
            "source_updated_at",
            "range_start",
            "range_end",
        }

        for key in int_fields:
            try:
                normalized[key] = int(payload.get(key, normalized[key]) or 0)
            except Exception:
                pass

        for key in float_fields:
            try:
                normalized[key] = round(float(payload.get(key, normalized[key]) or 0.0), 8)
            except Exception:
                pass

        for key in text_fields:
            normalized[key] = self._compact_text(str(payload.get(key, "") or ""), 80)

        normalized["source_stale"] = bool(payload.get("source_stale", normalized["source_stale"]))

        for list_key in ["top_losses", "top_wins"]:
            raw_items = payload.get(list_key, [])
            if not isinstance(raw_items, list):
                continue
            cleaned_items: List[Dict[str, Any]] = []
            for item in raw_items[:3]:
                if not isinstance(item, dict):
                    continue
                try:
                    net_pnl = round(float(item.get("net_pnl_after_fee_eth", 0.0) or 0.0), 8)
                except Exception:
                    net_pnl = 0.0
                try:
                    return_rate = round(float(item.get("return_rate_pct_estimate", 0.0) or 0.0), 4)
                except Exception:
                    return_rate = 0.0
                cleaned_items.append(
                    {
                        "open_time_bj": self._compact_text(str(item.get("open_time_bj", "") or ""), 32),
                        "close_time_bj": self._compact_text(str(item.get("close_time_bj", "") or ""), 32),
                        "direction": self._compact_text(str(item.get("direction", "") or ""), 12),
                        "net_pnl_after_fee_eth": net_pnl,
                        "return_rate_pct_estimate": return_rate,
                    }
                )
            normalized[list_key] = cleaned_items
        return normalized

    def _load_or_bootstrap_long_reflection(self) -> Dict[str, Any]:
        existing = self._read_json_file(LONG_REFLECTION_PATH, "Long Reflection JSON", default=None)
        if isinstance(existing, dict):
            return self._sanitize_long_reflection(existing, source="file")
        return self._default_long_reflection(source="bootstrap")

    def _persist_long_reflection(self, reflection: Dict[str, Any]) -> None:
        safe_json_dump(self._sanitize_long_reflection(reflection, source="persist"), LONG_REFLECTION_PATH, use_lock=True)

    def _sanitize_long_reflection(self, reflection: Dict[str, Any], source: str = "unknown") -> Dict[str, Any]:
        if not isinstance(reflection, dict):
            return self._default_long_reflection(source=source)
        sanitized = deepcopy(reflection)
        sanitized.setdefault("meta", {})
        sanitized["meta"]["updated_at"] = _utc_now_iso()
        sanitized["meta"]["source"] = source
        sanitized.setdefault("active_hypotheses", [])
        sanitized.setdefault("invalidated_hypotheses", [])
        sanitized.setdefault("news_case_log", [])
        return sanitized

    def _default_long_reflection(self, source: str = "bootstrap") -> Dict[str, Any]:
        return {
            "meta": {
                "created_at": _utc_now_iso(),
                "updated_at": _utc_now_iso(),
                "source": source,
            },
            "active_hypotheses": [],
            "invalidated_hypotheses": [],
            "news_case_log": [],
        }

    def _load_or_bootstrap_daily_execution_reflection(self) -> Dict[str, Any]:
        existing = self._read_json_file(DAILY_EXECUTION_REFLECTION_PATH, "Daily Execution Reflection", default=None)
        if isinstance(existing, dict):
            normalized = self._sanitize_daily_execution_reflection(existing, source="file")
            safe_json_dump(normalized, DAILY_EXECUTION_REFLECTION_PATH, use_lock=True)
            return normalized
        normalized = self._default_daily_execution_reflection(source="bootstrap")
        safe_json_dump(normalized, DAILY_EXECUTION_REFLECTION_PATH, use_lock=True)
        return normalized

    def _persist_daily_execution_reflection(self, reflection: Dict[str, Any]) -> None:
        safe_json_dump(
            self._sanitize_daily_execution_reflection(reflection, source="daily_review_agent"),
            DAILY_EXECUTION_REFLECTION_PATH,
            use_lock=True,
        )

    def _load_or_bootstrap_post_stop_reflection(self) -> Dict[str, Any]:
        existing = self._read_json_file(POST_STOP_REFLECTION_PATH, "Post Stop Reflection", default=None)
        if isinstance(existing, dict):
            normalized = self._sanitize_post_stop_reflection(existing, source="file")
            safe_json_dump(normalized, POST_STOP_REFLECTION_PATH, use_lock=True)
            return normalized
        normalized = self._default_post_stop_reflection(source="bootstrap")
        safe_json_dump(normalized, POST_STOP_REFLECTION_PATH, use_lock=True)
        return normalized

    def _persist_post_stop_reflection(self, reflection: Dict[str, Any]) -> None:
        safe_json_dump(
            self._sanitize_post_stop_reflection(reflection, source="stop_reflection_manager"),
            POST_STOP_REFLECTION_PATH,
            use_lock=True,
        )

    def _load_long_review_state(self) -> Dict[str, Any]:
        state = self._read_json_file(
            LONG_REVIEW_STATE_PATH,
            "Long Review State",
            default=self._default_long_review_state(),
        )
        if isinstance(state, dict):
            return state
        return self._default_long_review_state()

    def _save_long_review_state(self, state: Dict[str, Any]) -> None:
        merged = self._default_long_review_state()
        if isinstance(state, dict):
            merged.update(state)
        safe_json_dump(merged, LONG_REVIEW_STATE_PATH, use_lock=True)

    def _compose_event_context(self, state: DecisionState) -> tuple:
        event_batch = state.get("event_batch", [])
        if not event_batch:
            return "unknown", "No event details."
        combined_type = " & ".join(sorted(set([str(evt.get("type", "unknown")) for evt in event_batch])))
        combined_content = "\n\n---\n\n".join(
            [f"Event {idx + 1} ({evt.get('type', 'unknown')}):\n{evt.get('content', '')}" for idx, evt in enumerate(event_batch)]
        )
        return combined_type, combined_content

    def _format_pending_alarms(self) -> str:
        pending_alarms = "No pending alarms."
        alarms = safe_json_read(CLOCK_JSON_PATH, "Clock JSON Context")
        if alarms and isinstance(alarms, list):
            lines = []
            for item in alarms:
                if item.get("status", "pending") == "pending":
                    lines.append(_format_pending_alarm_line(item))
            if lines:
                pending_alarms = "\n".join(lines)
        return pending_alarms

    def _build_stage_tool_delta_summary(self, state: DecisionState, stage_names: List[str], limit: int = 20) -> str:
        calls = state.get("tool_results", [])
        if not isinstance(calls, list) or not calls:
            return "No tool calls recorded in this wakeup yet."

        stage_set = {str(name) for name in stage_names if str(name).strip()}
        picked = [call for call in calls if str(call.get("stage", "")) in stage_set]
        if not picked:
            stage_label = ", ".join(sorted(stage_set)) or "target stage"
            return f"No tool calls recorded for {stage_label}."

        if limit > 0 and len(picked) > limit:
            picked = picked[-limit:]

        lines: List[str] = []
        for call in picked:
            call_id = str(call.get("id", "") or "n/a")
            tool = str(call.get("tool", "") or "unknown_tool")
            result = call.get("result")
            outcome = "success"
            extra = ""

            if isinstance(result, dict):
                status = str(result.get("status", "")).strip()
                if status:
                    outcome = status
                elif "message" in result and result.get("message"):
                    outcome = "message_only"

                if tool == "set_alarm":
                    alarm_id = str(result.get("id", "")).strip()
                    trigger_time = str(result.get("trigger_time", "")).strip()
                    if alarm_id and trigger_time:
                        extra = f" ({alarm_id} @ {trigger_time})"
                    elif alarm_id:
                        extra = f" ({alarm_id})"
                elif tool == "cancel_coin_futures_order":
                    order_id = result.get("orderId")
                    if order_id is None and isinstance(call.get("args"), dict):
                        order_id = call["args"].get("order_id")
                    if order_id is not None:
                        extra = f" (order_id={order_id})"

                error_class = str(result.get("error_class", "")).strip()
                if error_class:
                    extra = f"{extra} [{error_class}]".strip()

            lines.append(f"- {call_id} {tool} -> {outcome}{extra}")

        return "\n".join(lines)

    def _build_retry_runtime_refresh_section(self, state: DecisionState) -> str:
        token = self._with_tool_stage("execute_primary_retry_refresh")
        try:
            open_orders = get_usdt_futures_open_orders("ETHUSDT")
            range_plan = get_range_plan("ETHUSDT")
        finally:
            reset_tool_stage(token)
        pending_alarms_snapshot = self._format_pending_alarms()
        primary_delta = self._build_stage_tool_delta_summary(
            state,
            stage_names=["execute_primary_model", "execute_primary_retry"],
        )
        deadline_audit = self._build_retry_deadline_audit_section(range_plan_result=range_plan)
        return "\n\n".join(
            [
                _format_tool_result_for_prompt("get_usdt_futures_open_orders", open_orders),
                _format_tool_result_for_prompt("get_range_plan", range_plan),
                deadline_audit,
                "[PENDING ALARMS REFRESHED]\n" + pending_alarms_snapshot,
                "[PRIMARY EXECUTION DELTA]\n" + primary_delta,
            ]
        )

    def _build_execute_runtime_refresh_section(self, *, stage_name: str) -> str:
        now = datetime.now()
        pending_alarms_snapshot = self._format_pending_alarms()
        current_short = self._load_or_migrate_short_memory()
        consistency_state = (
            current_short.get("consistency_state", {})
            if isinstance(current_short.get("consistency_state", {}), dict)
            else {}
        )
        day_plan = current_short.get("day_plan", {}) if isinstance(current_short.get("day_plan", {}), dict) else {}
        active_hypothesis = (
            current_short.get("active_hypothesis", {})
            if isinstance(current_short.get("active_hypothesis", {}), dict)
            else {}
        )
        risk_state = current_short.get("risk_state", {}) if isinstance(current_short.get("risk_state", {}), dict) else {}
        live_alarm_ids = self._extract_alarm_ids_from_text(pending_alarms_snapshot)

        lines = [
            "[LIVE EXECUTION CONTEXT REFRESH]",
            f"- refreshed_now: {now.strftime('%Y-%m-%d %H:%M:%S')}",
            "- Trust this refresh over older prompt snapshots when they disagree.",
            (
                "- short_memory_anchor: "
                f"market_regime={str(consistency_state.get('market_regime', '') or 'none')} | "
                f"trade_intent={str(consistency_state.get('trade_intent', '') or 'none')} | "
                f"entry_plan_direction={str(consistency_state.get('entry_plan_direction', '') or 'none')} | "
                f"intraday_mode={str(consistency_state.get('intraday_mode', '') or 'none')} | "
                f"hypothesis_id={str(active_hypothesis.get('hypothesis_id', '') or 'none')} | "
                f"hypothesis_direction={str(active_hypothesis.get('direction', '') or 'none')} | "
                f"hypothesis_status={str(active_hypothesis.get('status', '') or 'none')}"
            ),
            (
                "- live_deadlines: "
                f"hypothesis_expiry={self._format_deadline_drift(active_hypothesis.get('expiry'), now=now)} | "
                f"hold_until={self._format_deadline_drift(day_plan.get('hold_until'), now=now)} | "
                f"recheck_at={self._format_deadline_drift(day_plan.get('recheck_at'), now=now)}"
            ),
            f"- live_state_change_evidence: {self._compact_text(risk_state.get('state_change_evidence', ''), 220) or 'none'}",
        ]
        if self._pending_alarms_is_empty(pending_alarms_snapshot):
            lines.append("- live_pending_alarms: none")
        else:
            lines.append(
                "- live_pending_alarms: "
                + (", ".join(live_alarm_ids) if live_alarm_ids else "present_but_unparsed")
            )
        if stage_name == "execute_primary_retry":
            lines.append(self._build_retry_deadline_audit_section())
        return "\n".join(lines)

    def _load_or_migrate_short_memory(self) -> Dict[str, Any]:
        current = self._read_json_file(SHORT_MEMORY_JSON_PATH, "Short Memory JSON", default=None)
        if isinstance(current, dict):
            hydrated = _hydrate_short_memory_snapshot(
                current,
                source=self._infer_memory_source(current, "file"),
            )
            pruned = self._prune_short_memory_noise(hydrated, now=datetime.now())
            if json.dumps(pruned, ensure_ascii=False, sort_keys=True) != json.dumps(current, ensure_ascii=False, sort_keys=True):
                safe_json_dump(pruned, SHORT_MEMORY_JSON_PATH, use_lock=True)
            return pruned

        legacy_text = self._read_text_file(SHORTMEMORY_PATH, "")
        migrated = _hydrate_short_memory_snapshot(
            _migrate_shortmemory_markdown_to_json(legacy_text),
            source="migrated_from_markdown",
        )
        pruned = self._prune_short_memory_noise(migrated, now=datetime.now())
        safe_json_dump(pruned, SHORT_MEMORY_JSON_PATH, use_lock=True)
        return pruned

    def _load_or_migrate_long_memory(self) -> Dict[str, Any]:
        current = self._read_json_file(LONG_MEMORY_JSON_PATH, "Long Memory JSON", default=None)
        if isinstance(current, dict):
            hydrated = _hydrate_long_memory_snapshot(
                current,
                source=self._infer_memory_source(current, "file"),
            )
            if json.dumps(hydrated, ensure_ascii=False, sort_keys=True) != json.dumps(current, ensure_ascii=False, sort_keys=True):
                safe_json_dump(hydrated, LONG_MEMORY_JSON_PATH, use_lock=True)
            return hydrated
        hydrated = _hydrate_long_memory_snapshot({}, source="system")
        safe_json_dump(hydrated, LONG_MEMORY_JSON_PATH, use_lock=True)
        return hydrated

    def _persist_long_memory(self, memory: Dict[str, Any]) -> None:
        safe_json_dump(memory, LONG_MEMORY_JSON_PATH, use_lock=True)

    def _render_memory_snapshot_for_prompt(self, snapshot: Dict[str, Any], fallback: str, label: str = "") -> str:
        rendered = _memory_snapshot_to_pretty_json(snapshot, fallback)
        if rendered != fallback:
            prefix = ""
            if label:
                prefix = f"IMPORTANT: When emitting JSON patch ops for {label}, ALWAYS verify paths against this exact structure.\n"
            else:
                prefix = "IMPORTANT: When emitting JSON patch ops, ALWAYS verify paths against this exact structure.\n"
            return prefix + rendered
        return rendered

    def _render_long_context_json(self, payload: Any, fallback: str = "{}") -> str:
        if payload is None:
            return fallback
        if isinstance(payload, (dict, list)) and len(payload) == 0:
            return fallback
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception:
            return fallback

    def _extract_chat_message_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: List[str] = []
            for item in content:
                text = ""
                if isinstance(item, str):
                    text = item
                elif isinstance(item, dict):
                    raw_text = item.get("text", "")
                    if isinstance(raw_text, dict):
                        text = str(raw_text.get("value", "") or raw_text.get("text", "") or "")
                    elif raw_text:
                        text = str(raw_text)
                    elif item.get("type") in {"text", "output_text"}:
                        text = str(item.get("value", "") or "")
                else:
                    maybe_text = getattr(item, "text", None)
                    if maybe_text:
                        text = str(maybe_text)
                    else:
                        maybe_content = getattr(item, "content", None)
                        if isinstance(maybe_content, str):
                            text = maybe_content
                text = str(text or "").strip()
                if text:
                    chunks.append(text)
            return "\n".join(chunks).strip()
        return str(content or "")

    def _pending_alarms_is_empty(self, pending_alarms: str) -> bool:
        text = str(pending_alarms or "").strip()
        return not text or text == "No pending alarms."

    def _extract_alarm_ids_from_text(self, text: str) -> List[str]:
        return sorted(set(re.findall(r"\bALARM_\d+\b", str(text or ""))))

    def _is_alarm_like_tactical_alert(self, alert: Dict[str, Any]) -> bool:
        kind = str(alert.get("kind", "") or "").strip().lower()
        summary = str(alert.get("summary", "") or "").strip().lower()
        return "alarm" in kind or "闹钟" in kind or "alarm_" in summary or "闹钟" in summary

    def _is_expired_tactical_alert(self, alert: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
        expires_at = self._parse_timestamp(alert.get("expires_at"))
        if expires_at is None:
            return False
        current_time = now or datetime.now()
        return expires_at < current_time

    def _format_tactical_alert_for_prompt(self, alert: Dict[str, Any], *, now: Optional[datetime] = None) -> str:
        current_time = now or datetime.now()
        recorded_at = str(alert.get("recorded_at", "") or "unknown_time")
        kind = str(alert.get("kind", "") or "Alert")
        related_hypothesis_id = str(alert.get("related_hypothesis_id", "") or "").strip()
        summary = self._compact_text(str(alert.get("summary", "") or ""), 280)
        labels = ["historical only"]
        if self._is_alarm_like_tactical_alert(alert):
            labels.append("alarm-history")
        if self._is_expired_tactical_alert(alert, now=current_time):
            labels.append("expired historical event")
        suffix = f" | hypothesis={related_hypothesis_id}" if related_hypothesis_id else ""
        return f"- [{recorded_at}] {kind} ({', '.join(labels)}){suffix}: {summary}"

    def _render_short_memory_projection_for_prompt(self, snapshot: Dict[str, Any], pending_alarms: str) -> str:
        current_time = datetime.now()
        normalized_snapshot = self._prune_short_memory_noise(snapshot if isinstance(snapshot, dict) else {}, now=current_time)
        active_hypothesis = normalized_snapshot.get("active_hypothesis", {})
        active_hypothesis_id = (
            str(active_hypothesis.get("hypothesis_id", "") or "").strip()
            if isinstance(active_hypothesis, dict)
            else ""
        )
        daily_reflection = self._prune_daily_execution_reflection(
            self._load_or_bootstrap_daily_execution_reflection(),
            now=current_time,
            for_prompt=True,
            active_hypothesis_id=active_hypothesis_id,
        )
        post_stop_reflection = self._load_or_bootstrap_post_stop_reflection()
        narrative_tracking = [
            self._compact_narrative_for_prompt(item)
            for item in self._select_relevant_narratives(
                normalized_snapshot.get("narrative_tracking", []),
                now=current_time,
                limit=SHORT_PROMPT_NARRATIVE_LIMIT,
            )
        ]
        live_snapshot: Dict[str, Any] = {
            "meta": normalized_snapshot.get("meta", {}),
            "consistency_state": normalized_snapshot.get("consistency_state", {}),
            "day_plan": normalized_snapshot.get("day_plan", {}),
            "active_hypothesis": normalized_snapshot.get("active_hypothesis", {}),
            "risk_state": normalized_snapshot.get("risk_state", {}),
            "mtf_state": normalized_snapshot.get("mtf_state", {}),
            "narrative_tracking": narrative_tracking,
        }
        lines: List[str] = [
            "### Current Live State",
            "- `Pending Alarms` below are the only live alarm authority for this wakeup.",
            "- Never infer a live pending alarm from historical tactical alerts.",
        ]
        if self._pending_alarms_is_empty(pending_alarms):
            lines.append("- There are no live pending alarms right now.")
        else:
            live_alarm_ids = self._extract_alarm_ids_from_text(pending_alarms)
            if live_alarm_ids:
                lines.append(f"- Live pending alarm ids: {', '.join(live_alarm_ids)}")
        lines.extend(
            [
                "",
                "Current structured short memory JSON:",
                "IMPORTANT: When emitting JSON patch ops for short_memory_ops, ALWAYS verify paths against this exact structure.",
                json.dumps(live_snapshot, ensure_ascii=False, indent=2),
                "",
                "### Immediate Post-Stop Reflection (Highest Priority Subject Memory)",
            ]
        )

        active_stop_reflection = post_stop_reflection.get("active_reflection", {})
        if (
            isinstance(active_stop_reflection, dict)
            and active_stop_reflection
            and self._post_stop_reflection_is_live(active_stop_reflection, now=current_time)
        ):
            lines.append(self._format_post_stop_reflection_for_prompt(active_stop_reflection))
            lines.append(
                "- If you want same-direction reentry, explicitly explain what new evidence appeared since this stop and why this is not just a replay."
            )
        else:
            lines.append("- No active post-stop reflection is currently live.")

        lines.extend(
            [
                "",
                "### Decision Continuity Carryover (Compressed Daily Reflection)",
            ]
        )

        carryover = daily_reflection.get("carryover", [])
        if isinstance(carryover, list) and carryover:
            for item in carryover[:DAILY_REFLECTION_CARRYOVER_PROMPT_LIMIT]:
                lines.append(f"- {self._compact_text(item, 280)}")
        else:
            lines.append("- No compressed daily carryover yet.")

        behavior_biases = daily_reflection.get("behavior_biases", [])
        if isinstance(behavior_biases, list) and behavior_biases:
            lines.append("- Recent behavior biases to watch:")
            for item in behavior_biases[:DAILY_REFLECTION_BEHAVIOR_PROMPT_LIMIT]:
                lines.append(f"  - {self._compact_text(item, 240)}")

        watch_items = daily_reflection.get("watch_items", [])
        if isinstance(watch_items, list) and watch_items:
            lines.append("- Current carryover watch items:")
            for item in watch_items[:DAILY_REFLECTION_WATCH_PROMPT_LIMIT]:
                lines.append(f"  - {self._compact_text(item, 240)}")

        lines.extend(
            [
                "",
                "### Recent Closed-Position PnL Feedback (Objective Review Context)",
            ]
        )

        closed_position_feedback = self._sanitize_closed_position_feedback(
            daily_reflection.get("closed_position_feedback", {}),
        )
        if int(closed_position_feedback.get("position_count", 0) or 0) <= 0:
            closed_position_feedback = self._build_recent_closed_position_feedback([21, 3])
        if int(closed_position_feedback.get("position_count", 0) or 0) > 0:
            lines.append(
                "- Window: "
                f"{closed_position_feedback.get('window_days', 0)}d"
                f" | positions={closed_position_feedback.get('position_count', 0)}"
                f" | win_rate={closed_position_feedback.get('win_rate_pct', 0.0)}%"
                f" | realized={closed_position_feedback.get('realized_pnl_eth', 0.0)} ETH"
                f" | net={closed_position_feedback.get('net_pnl_after_fee_eth', 0.0)} ETH"
            )
            lines.append(
                "- Side split: "
                f"long={closed_position_feedback.get('long_count', 0)} / net={closed_position_feedback.get('long_net_pnl_after_fee_eth', 0.0)} ETH"
                f" ; short={closed_position_feedback.get('short_count', 0)} / net={closed_position_feedback.get('short_net_pnl_after_fee_eth', 0.0)} ETH"
            )
            top_losses = closed_position_feedback.get("top_losses", [])
            if isinstance(top_losses, list) and top_losses:
                lines.append("- Biggest recent losing samples:")
                for item in top_losses[:2]:
                    if not isinstance(item, dict):
                        continue
                    lines.append(
                        "  - "
                        f"{item.get('direction', '')} "
                        f"{item.get('open_time_bj', '')} -> {item.get('close_time_bj', '')} "
                        f"net={item.get('net_pnl_after_fee_eth', 0.0)} ETH "
                        f"roi={item.get('return_rate_pct_estimate', 0.0)}%"
                    )
            lines.append(
                "- Use this as setup-quality feedback. It is not permission to mechanically flip bias, "
                "but it is evidence about what has recently lost money."
            )
        else:
            lines.append("- No recent closed-position PnL reflection is available yet.")

        lines.extend(
            [
                "",
                "### Recent Decision Episodes (Use These To Preserve Subject Continuity)",
            ]
        )

        recent_episodes = daily_reflection.get("recent_episodes", [])
        if isinstance(recent_episodes, list) and recent_episodes:
            for episode in recent_episodes[-DAILY_REFLECTION_EPISODE_PROMPT_LIMIT:]:
                if isinstance(episode, dict):
                    lines.append(self._format_decision_episode_for_prompt(episode))
        else:
            lines.append("- No compressed decision episodes available yet.")

        lines.extend(
            [
                "",
                "### Recent Intraday Decision Chain (Fresh Breadcrumbs, Historical Only)",
            ]
        )

        tactical_alerts = normalized_snapshot.get("tactical_alerts", [])
        if not isinstance(tactical_alerts, list):
            tactical_alerts = []

        visible_alerts, suppressed_expired_alarm_alerts = self._select_relevant_tactical_alerts(
            tactical_alerts,
            now=current_time,
            active_hypothesis_id=active_hypothesis_id,
            storage_mode=False,
        )

        if visible_alerts:
            decision_like_alerts = [alert for alert in visible_alerts if self._is_decision_like_tactical_alert(alert)]
            historical_only_alerts = [alert for alert in visible_alerts if not self._is_decision_like_tactical_alert(alert)]
            if decision_like_alerts:
                for alert in decision_like_alerts[:SHORT_PROMPT_DECISION_ALERT_LIMIT]:
                    lines.append(self._format_tactical_alert_for_prompt(alert, now=current_time))
            else:
                lines.append("- No fresh intraday decision breadcrumbs are exposed in this wakeup.")
            lines.extend(
                [
                    "",
                    "### Recent Historical Tactical Alerts (Historical Only / Do Not Treat As Live Tasks)",
                ]
            )
            if historical_only_alerts:
                for alert in historical_only_alerts[:SHORT_PROMPT_HISTORICAL_ALERT_LIMIT]:
                    lines.append(self._format_tactical_alert_for_prompt(alert, now=current_time))
            else:
                lines.append("- No additional historical tactical alerts cleared the relevance filter.")
        else:
            lines.append("- No fresh intraday decision breadcrumbs are exposed in this wakeup.")
            lines.extend(
                [
                    "",
                    "### Recent Historical Tactical Alerts (Historical Only / Do Not Treat As Live Tasks)",
                    "- No recent historical tactical alerts are exposed in this wakeup.",
                ]
            )

        if suppressed_expired_alarm_alerts:
            lines.append(
                f"- Suppressed {suppressed_expired_alarm_alerts} expired alarm-related historical alerts from prompt projection."
            )
        return "\n".join(lines)

    def _decision_alarm_semantics_text(self, decision: Dict[str, Any]) -> str:
        parts = [
            str(decision.get("execution_txt", "") or ""),
            str(decision.get("explanation", "") or ""),
            str(decision.get("memory_management_reasoning", "") or ""),
            str(decision.get("conflict_check", "") or ""),
            str(decision.get("falsification_point", "") or ""),
            str(decision.get("next_alarm_reason", "") or ""),
            str(decision.get("state_change_evidence", "") or ""),
        ]
        return "\n".join(part for part in parts if part.strip())

    def _overlay_runtime_alarm_semantics_fields(
        self,
        *,
        merged_decision: Dict[str, Any],
        runtime_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(merged_decision, dict):
            return {}
        if not isinstance(runtime_decision, dict) or not runtime_decision:
            return dict(merged_decision)

        adjusted = dict(merged_decision)
        for field_name in (
            "execution_txt",
            "explanation",
            "memory_management_reasoning",
            "conflict_check",
            "falsification_point",
            "next_alarm_reason",
        ):
            value = str(runtime_decision.get(field_name, "") or "").strip()
            if value:
                adjusted[field_name] = value
        return adjusted

    def _decision_full_semantics_text(self, decision: Dict[str, Any]) -> str:
        parts = [
            self._decision_alarm_semantics_text(decision),
            str(decision.get("decision_basis", "") or ""),
            str(decision.get("execution_rationale", "") or ""),
        ]
        return "\n".join(part for part in parts if part.strip())

    def _decision_references_existing_alarm(self, decision_text: str) -> bool:
        patterns = [
            r"(已有|现有|保留|继续等待|等待).{0,24}(闹钟|ALARM_\d+)",
            r"(existing|current|keep|retain).{0,24}(alarm|ALARM_\d+)",
            r"(闹钟覆盖|alarm\s+coverage|现有闹钟会验证|已有 alarm 覆盖)",
        ]
        return any(re.search(pattern, decision_text, re.IGNORECASE) for pattern in patterns)

    def _decision_has_explicit_wait_condition(self, decision_text: str) -> bool:
        waitish = re.search(r"(等待|验证|触发|monitor|wait|until|观察|观望)", decision_text, re.IGNORECASE)
        thresholdish = re.search(
            r"((价格|price|RSI(?:_1h|_15m)?|MACD(?:_HISTO)?(?:_1h|_15m)?)"
            r".{0,30}(突破|跌破|站回|收复|上破|下破|>|<|>=|<=|转正|转负|收敛|扩大|回落至|升至)"
            r".{0,20}\d)"
            r"|(\bprice\s*(>=|<=|>|<)\s*\d)"
            r"|(\bRSI(?:_1h|_15m)?\s*(>=|<=|>|<)\s*\d)"
            r"|(\bMACD(?:_HISTO)?(?:_1h|_15m)?\s*(>=|<=|>|<)\s*-?\d)",
            decision_text,
            re.IGNORECASE,
        )
        return bool(waitish and thresholdish)

    def _has_successful_set_alarm_call(self, calls: List[Dict[str, Any]]) -> bool:
        for call in calls:
            if str(call.get("tool", "")) != "set_alarm":
                continue
            if self._tool_call_succeeded(call):
                return True
        return False

    def _validate_decision_alarm_semantics(
        self,
        decision: Dict[str, Any],
        *,
        pending_alarms: str,
        calls: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        decision_text = self._decision_alarm_semantics_text(decision)
        if not decision_text.strip():
            return None

        live_alarm_ids = self._extract_alarm_ids_from_text(pending_alarms)
        has_live_alarm_reference = any(alarm_id in decision_text for alarm_id in live_alarm_ids)
        has_successful_set_alarm = self._has_successful_set_alarm_call(calls)
        no_live_pending_alarms = self._pending_alarms_is_empty(pending_alarms)

        if no_live_pending_alarms and not has_successful_set_alarm and self._decision_references_existing_alarm(decision_text):
            return {
                "error_class": "historical_alarm_confused_as_live",
                "why_rejected": (
                    "Pending Alarms is empty, but the decision text still refers to an existing/current alarm as if it were live. "
                    "Do not treat historical tactical_alerts alarm text as a live schedule."
                ),
                "model_fix_hint": (
                    "If there is no live pending alarm, either set a new set_alarm(condition=...) now or rewrite the text to "
                    "state clearly that no live alarm exists."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "historical_alarm_confused_as_live",
                "related_tool_call": "",
            }

        if self._decision_has_explicit_wait_condition(decision_text) and not has_successful_set_alarm and not has_live_alarm_reference:
            return {
                "error_class": "missing_structured_wait_alarm",
                "why_rejected": (
                    "The decision text contains an explicit future wait condition, but this wakeup neither created a new alarm "
                    "nor cited a live pending alarm id from Pending Alarms."
                ),
                "model_fix_hint": (
                    "If you want to wait for a concrete price/indicator trigger, call set_alarm with condition when possible, "
                    "or explicitly cite a real live pending alarm id from Pending Alarms."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "missing_structured_wait_alarm",
                "related_tool_call": "",
            }
        return None

    def _append_unique_validation_failure(
        self,
        failures: List[Dict[str, Any]],
        failure: Optional[Dict[str, Any]],
    ) -> None:
        if not isinstance(failure, dict):
            return
        error_class = str(failure.get("error_class", "") or "").strip()
        why_rejected = str(failure.get("why_rejected", "") or "").strip()
        for existing in failures:
            if not isinstance(existing, dict):
                continue
            if (
                str(existing.get("error_class", "") or "").strip() == error_class
                and str(existing.get("why_rejected", "") or "").strip() == why_rejected
            ):
                return
        failures.append(dict(failure))

    def _collect_attempt_validation_failures(
        self,
        *,
        stage_name: str,
        state: Dict[str, Any],
        audit_meta: Dict[str, Any],
        decision: Dict[str, Any],
        new_calls: List[Dict[str, Any]],
        pending_alarms_snapshot: str,
        refresh_reasons: List[str],
        primary_failure: Optional[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        failures: List[Dict[str, Any]] = []
        self._append_unique_validation_failure(failures, primary_failure)
        self._append_unique_validation_failure(
            failures,
            self._validate_execute_contract_runtime_alignment(
                approved_contract=dict(audit_meta.get("approved_decision_contract", {})),
                new_calls=new_calls,
            ),
        )
        self._append_unique_validation_failure(
            failures,
            self._validate_retry_no_side_effect_actions(
                stage_name=stage_name,
                audit_meta=audit_meta,
                calls=new_calls,
            ),
        )
        if stage_name == "execute_followup":
            self._append_unique_validation_failure(
                failures,
                self._validate_followup_action_dedup(
                    state,
                    start_idx=0,
                    new_calls=new_calls,
                ),
            )
        self._append_unique_validation_failure(
            failures,
            self._validate_hard_risk_compression_requirement(
                audit_meta,
                decision,
                new_calls,
            ),
        )
        self._append_unique_validation_failure(
            failures,
            self._validate_sideways_range_decision_gate(
                state=state,
                stage_name=stage_name,
                audit_meta=audit_meta,
                decision=decision,
                new_calls=new_calls,
            ),
        )
        should_validate_alarm_semantics = not (
            stage_name in {"execute_primary_model", "execute_primary_retry"} and refresh_reasons
        )
        if should_validate_alarm_semantics:
            self._append_unique_validation_failure(
                failures,
                self._validate_decision_alarm_semantics(
                    decision,
                    pending_alarms=pending_alarms_snapshot,
                    calls=new_calls,
                ),
            )
        return failures

    def _extract_patched_short_memory_value(
        self,
        decision: Dict[str, Any],
        path: str,
    ) -> Optional[Any]:
        ops = decision.get("short_memory_ops", [])
        if not isinstance(ops, list):
            return None
        for op in reversed(ops):
            if not isinstance(op, dict):
                continue
            if str(op.get("path", "") or "").strip() != path:
                continue
            if str(op.get("op", "") or "").strip().lower() not in {"add", "replace"}:
                continue
            return op.get("value")
        return None

    def _resolve_market_regime_for_validation(
        self,
        decision: Dict[str, Any],
        state: Dict[str, Any],
    ) -> str:
        patched = self._extract_patched_short_memory_value(decision, "/consistency_state/market_regime")
        if patched is not None:
            return str(patched or "").strip()
        short_snapshot = state.get("short_memory_snapshot_obj", {})
        if isinstance(short_snapshot, dict):
            consistency_state = short_snapshot.get("consistency_state", {})
            if isinstance(consistency_state, dict):
                return str(consistency_state.get("market_regime", "") or "").strip()
        return ""

    def _build_sideways_range_eligibility(
        self,
        precheck_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        position_payload = precheck_payload.get("position")
        open_orders_payload = precheck_payload.get("open_orders")
        range_plan = read_range_plan_state()

        live_position = self._extract_position_snapshot_from_payload(position_payload)
        open_orders = self._iter_order_rows(open_orders_payload)

        is_hedge_mode = False
        if isinstance(position_payload, dict):
            is_hedge_mode = bool(position_payload.get("is_hedge_mode", False))
        position_mode = "hedge" if is_hedge_mode else "one_way"

        range_plan_active = False
        range_plan_symbol = ""
        if isinstance(range_plan, dict):
            range_plan_active = str(range_plan.get("status", "") or "").strip().lower() == "active"
            range_plan_symbol = str(range_plan.get("symbol", "") or "").strip().upper()

        blockers: List[str] = []
        if live_position is not None:
            blockers.append("symbol_not_flat")
        if open_orders:
            blockers.append("open_orders_present")
        if range_plan_active:
            blockers.append("active_range_plan_exists")

        return {
            "position_mode": position_mode,
            "is_hedge_mode": is_hedge_mode,
            "is_flat": live_position is None,
            "has_open_orders": bool(open_orders),
            "open_order_count": len(open_orders),
            "range_plan_active": range_plan_active,
            "range_plan_symbol": range_plan_symbol,
            "eligible": not blockers,
            "blockers": blockers,
        }

    def _render_sideways_range_eligibility_audit(self, precheck_payload: Dict[str, Any]) -> str:
        eligibility = precheck_payload.get("sideways_range_eligibility")
        if not isinstance(eligibility, dict):
            eligibility = self._build_sideways_range_eligibility(precheck_payload)
        blockers = ", ".join(eligibility["blockers"]) if eligibility["blockers"] else "none"
        lines = [
            "[SIDEWAYS RANGE ELIGIBILITY AUDIT]",
            f"- position_mode: {eligibility['position_mode']}",
            f"- hedge_mode: {'yes' if eligibility['is_hedge_mode'] else 'no'}",
            f"- flat_symbol: {'yes' if eligibility['is_flat'] else 'no'}",
            f"- clean_open_orders: {'yes' if not eligibility['has_open_orders'] else 'no'}",
            f"- active_range_plan: {'yes' if eligibility['range_plan_active'] else 'no'}",
            f"- active_range_plan_symbol: {eligibility['range_plan_symbol'] or 'none'}",
            f"- eligible_to_start_range_plan_now: {'yes' if eligibility['eligible'] else 'no'}",
            f"- blockers: {blockers}",
            "- This block is capability context, not a mandatory action. It tells you whether range automation is currently possible.",
            "- Range automation is supported in both hedge and one_way position modes. In one_way mode the execution layer uses BOTH and manages one net position at a time.",
            "- If market_regime=neutral_sideways and eligible_to_start_range_plan_now=yes, you may choose between pure observe/wait and set_range_plan.",
            "- If your current plan is only to wait for breakout while price remains inside a stable band, set_range_plan is the optional path for monetizing the waiting window.",
            "- If eligible_to_start_range_plan_now=no, the blocker(s) above explain why range automation is currently unavailable.",
        ]
        return "\n".join(lines)

    def _build_sideways_range_proposal_priority(
        self,
        state: DecisionState,
        precheck_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        eligibility = precheck_payload.get("sideways_range_eligibility")
        if not isinstance(eligibility, dict):
            eligibility = self._build_sideways_range_eligibility(precheck_payload)
        snapshot = precheck_payload.get("sideways_range_snapshot")
        if not isinstance(snapshot, dict):
            snapshot = self._build_sideways_range_opportunity_snapshot(state, precheck_payload)

        short_snapshot = state.get("short_memory_snapshot_obj", {})
        if not isinstance(short_snapshot, dict):
            short_snapshot = {}
        consistency_state = short_snapshot.get("consistency_state", {})
        if not isinstance(consistency_state, dict):
            consistency_state = {}
        active_hypothesis = short_snapshot.get("active_hypothesis", {})
        if not isinstance(active_hypothesis, dict):
            active_hypothesis = {}

        memory_trade_intent = str(consistency_state.get("trade_intent", "") or "").strip()
        memory_entry_direction = str(consistency_state.get("entry_plan_direction", "") or "").strip()
        memory_execution_mode = str(consistency_state.get("execution_mode", "") or "").strip()
        memory_hypothesis_id = str(active_hypothesis.get("hypothesis_id", "") or "").strip()
        memory_hypothesis_status = str(active_hypothesis.get("status", "") or "").strip()

        proposal_priority = "normal"
        reason = "no_strong_runtime_sideways_override"
        if (
            eligibility.get("eligible")
            and snapshot.get("available")
            and str(snapshot.get("preferred_range_action", "") or "") == "start_preferred"
            and not eligibility.get("range_plan_active")
        ):
            proposal_priority = "high"
            reason = "runtime_sideways_harvesting_preferred"

        stale_wait_anchor = memory_execution_mode == "observe_only" or memory_trade_intent in {
            "wait",
            "long_bias",
            "short_bias",
            "cooldown",
        }

        return {
            "available": proposal_priority == "high",
            "proposal_priority": proposal_priority,
            "reason": reason,
            "stale_wait_anchor_present": stale_wait_anchor,
            "memory_trade_intent": memory_trade_intent,
            "memory_entry_direction": memory_entry_direction,
            "memory_execution_mode": memory_execution_mode,
            "memory_hypothesis_id": memory_hypothesis_id,
            "memory_hypothesis_status": memory_hypothesis_status,
        }

    def _render_sideways_range_proposal_priority(self, precheck_payload: Dict[str, Any]) -> str:
        priority = precheck_payload.get("sideways_range_priority")
        if not isinstance(priority, dict):
            return ""
        if not priority.get("available"):
            return ""
        return "\n".join(
            [
                "[SIDEWAYS RANGE PROPOSAL PRIORITY]",
                f"- proposal_priority: {priority.get('proposal_priority', 'normal')}",
                f"- reason: {priority.get('reason', 'unknown')}",
                (
                    "- prior_live_memory: "
                    f"trade_intent={priority.get('memory_trade_intent', 'none') or 'none'} | "
                    f"entry_plan_direction={priority.get('memory_entry_direction', 'none') or 'none'} | "
                    f"execution_mode={priority.get('memory_execution_mode', 'none') or 'none'} | "
                    f"hypothesis={priority.get('memory_hypothesis_id', 'none') or 'none'} | "
                    f"status={priority.get('memory_hypothesis_status', 'none') or 'none'}"
                ),
                f"- stale_wait_anchor_present: {'yes' if priority.get('stale_wait_anchor_present') else 'no'}",
                "- This wakeup contains a high-quality runtime sideways opportunity. Treat range harvesting as the first proposal candidate before defaulting to another observe-only extension.",
                "- `mem/short.json` is continuity context, not a veto. Do not let stale wait/observe or blocked directional memory suppress a strong start_preferred range opportunity by inertia.",
                "- If you still prefer pure waiting, explain the stronger current-market reason in the proposal itself; habit, stale commitment, or generic breakout-watch language is not enough.",
            ]
        )

    def _build_sideways_range_opportunity_snapshot(
        self,
        state: DecisionState,
        precheck_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        market_snapshot = str(state.get("market_snapshot", "") or "")
        short_snapshot = state.get("short_memory_snapshot_obj", {})
        if not isinstance(short_snapshot, dict):
            short_snapshot = {}

        chart_15m = self._extract_chart_latest_reference(market_snapshot, "15 minutes")
        chart_1h = self._extract_chart_latest_reference(market_snapshot, "1 hour")
        quality = self._build_recent_range_quality_snapshot(chart_15m)
        lower = self._safe_float(chart_15m.get("bollinger_lower"))
        upper = self._safe_float(chart_15m.get("bollinger_upper"))
        mid = self._safe_float(chart_15m.get("bollinger_mid"))
        reference_price = self._safe_float(chart_15m.get("latest_close"))
        if reference_price is None:
            live_position = self._extract_position_snapshot_from_payload(precheck_payload.get("position"))
            if isinstance(live_position, dict):
                reference_price = self._safe_float(live_position.get("mark_price"))

        if lower is None or upper is None or mid is None or reference_price is None or upper <= lower:
            return {
                "available": False,
                "reason": "15m_range_reference_unavailable",
            }

        width = upper - lower
        width_pct = (width / reference_price) * 100.0 if reference_price > 0 else 0.0
        relative_position = (reference_price - lower) / width if width > 0 else 0.5
        relative_position = max(0.0, min(1.0, relative_position))
        if relative_position < 0.34:
            location = "lower_third"
        elif relative_position > 0.66:
            location = "upper_third"
        else:
            location = "middle_band"

        max_open_payload = precheck_payload.get("max_open", {})
        max_quantity = self._safe_float(max_open_payload.get("max_quantity")) if isinstance(max_open_payload, dict) else None
        step_size = self._safe_float(max_open_payload.get("step_size")) if isinstance(max_open_payload, dict) else None
        suggested_quantity = None
        if max_quantity and max_quantity > 0:
            raw_qty = max_quantity * 0.08
            if step_size and step_size > 0:
                raw_qty = math.floor((raw_qty + 1e-12) / step_size) * step_size
            suggested_quantity = round(max(raw_qty, 0.0), 8)
            if suggested_quantity <= 0 and step_size and step_size > 0 and max_quantity >= step_size:
                suggested_quantity = round(step_size, 8)

        quality_score = self._safe_float(quality.get("oscillation_score")) or 0.0
        suggested_buffer_pct = 0.18
        if quality_score >= 0.75:
            suggested_buffer_pct = 0.24
        elif quality_score >= 0.62:
            suggested_buffer_pct = 0.21
        elif quality_score < 0.45:
            suggested_buffer_pct = 0.14

        working_lower = round(lower, 2)
        working_upper = round(upper, 2)
        working_mid = round(mid, 2)
        hard_lower = round(lower - width * suggested_buffer_pct, 2)
        hard_upper = round(upper + width * suggested_buffer_pct, 2)
        q1 = round(lower + width * 0.25, 2)
        q2 = round(mid, 2)
        q3 = round(lower + width * 0.75, 2)
        upper_inner = round(lower + width * 0.88, 2)
        lower_inner = round(lower + width * 0.12, 2)
        expires_at = (datetime.now() + timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")

        consistency_state = short_snapshot.get("consistency_state", {})
        if not isinstance(consistency_state, dict):
            consistency_state = {}
        active_hypothesis = short_snapshot.get("active_hypothesis", {})
        if not isinstance(active_hypothesis, dict):
            active_hypothesis = {}

        long_levels: List[Dict[str, Any]] = []
        short_levels: List[Dict[str, Any]] = []
        suggested_mode = str(quality.get("suggested_range_mode", "low_confidence") or "low_confidence")
        activation_bias = str(quality.get("activation_bias", "weak") or "weak")
        if suggested_quantity and suggested_quantity > 0:
            if suggested_mode in {"symmetric", "low_confidence"}:
                long_levels.append(
                    {
                        "entry_price": q1,
                        "exit_price": q2,
                        "quantity": suggested_quantity,
                    }
                )
                short_levels.append(
                    {
                        "entry_price": q3,
                        "exit_price": q2,
                        "quantity": suggested_quantity,
                    }
                )
            elif suggested_mode == "short_only":
                short_levels.append(
                    {
                        "entry_price": q3,
                        "exit_price": q2,
                        "quantity": suggested_quantity,
                    }
                )
                short_levels.append(
                    {
                        "entry_price": upper_inner,
                        "exit_price": q2,
                        "quantity": suggested_quantity,
                    }
                )
            elif suggested_mode == "long_only":
                long_levels.append(
                    {
                        "entry_price": q1,
                        "exit_price": q2,
                        "quantity": suggested_quantity,
                    }
                )
                long_levels.append(
                    {
                        "entry_price": lower_inner,
                        "exit_price": q2,
                        "quantity": suggested_quantity,
                    }
                )

        current_hypothesis_expiry = str(active_hypothesis.get("expiry", "") or "").strip()
        current_hypothesis_expiry_dt = self._parse_memory_datetime(current_hypothesis_expiry)
        candidate_expires_dt = self._parse_memory_datetime(expires_at)
        now = datetime.now()
        recommended_entry_direction = "both"
        if suggested_mode == "short_only":
            recommended_entry_direction = "short"
        elif suggested_mode == "long_only":
            recommended_entry_direction = "long"

        if current_hypothesis_expiry_dt and current_hypothesis_expiry_dt > now:
            if candidate_expires_dt and current_hypothesis_expiry_dt >= candidate_expires_dt:
                recommended_hypothesis_action = "keep"
                expiry_note = (
                    "Current hypothesis expiry already covers the proposed range window. "
                    "If you start range, keep the same hypothesis metadata and let the range plan expire earlier."
                )
            else:
                recommended_hypothesis_action = "rollover"
                expiry_note = (
                    "If you keep the same hypothesis_id while extending it for a longer range window, "
                    "rollover is legal only when the new hypothesis expiry is strictly later than the current one."
                )
        elif current_hypothesis_expiry_dt and current_hypothesis_expiry_dt <= now:
            recommended_hypothesis_action = "rollover"
            expiry_note = (
                "Current hypothesis already expired. If the same thesis is still alive, rollover must write a fresh future expiry later than the old one."
            )
        elif str(active_hypothesis.get("hypothesis_id", "") or "").strip():
            recommended_hypothesis_action = "keep"
            expiry_note = (
                "If the current hypothesis is still structurally valid, you may keep it unchanged and start range as the monetization layer."
            )
        else:
            recommended_hypothesis_action = "keep"
            expiry_note = (
                "No active hypothesis expiry constraint was detected. You may start range without inventing a new hypothesis lifecycle change."
            )

        preferred_range_action = "neutral"
        if activation_bias == "strong" and suggested_mode in {"symmetric", "short_only", "long_only"}:
            preferred_range_action = "start_preferred"
        elif activation_bias in {"strong", "moderate"} and (long_levels or short_levels):
            preferred_range_action = "start_viable"

        return {
            "available": True,
            "reference_price": round(reference_price, 2),
            "range_width": round(width, 2),
            "range_width_pct": round(width_pct, 2),
            "price_location": location,
            "position_in_band_pct": round(relative_position * 100.0, 1),
            "lower_breakout": hard_lower,
            "upper_breakout": hard_upper,
            "midpoint": working_mid,
            "distance_to_lower": round(reference_price - lower, 2),
            "distance_to_upper": round(upper - reference_price, 2),
            "hard_bounds": {
                "lower_breakout": hard_lower,
                "upper_breakout": hard_upper,
            },
            "working_band": {
                "lower": working_lower,
                "mid": working_mid,
                "upper": working_upper,
            },
            "quartile_levels": {
                "q1": q1,
                "q2": q2,
                "q3": q3,
            },
            "suggested_buffer_pct": round(suggested_buffer_pct * 100.0, 1),
            "long_levels": long_levels,
            "short_levels": short_levels,
            "expires_at": expires_at,
            "max_quantity": round(max_quantity, 8) if max_quantity is not None else None,
            "active_trade_intent": str(consistency_state.get("trade_intent", "") or ""),
            "active_entry_plan_direction": str(consistency_state.get("entry_plan_direction", "") or ""),
            "active_hypothesis_id": str(active_hypothesis.get("hypothesis_id", "") or ""),
            "active_hypothesis_direction": str(active_hypothesis.get("direction", "") or ""),
            "active_hypothesis_status": str(active_hypothesis.get("status", "") or ""),
            "rsi_15m": self._safe_float(chart_15m.get("rsi")),
            "macd_histo_15m": self._safe_float(chart_15m.get("macd_histo")),
            "rsi_1h": self._safe_float(chart_1h.get("rsi")),
            "macd_histo_1h": self._safe_float(chart_1h.get("macd_histo")),
            "price_momentum_15m": str(chart_15m.get("price_momentum", "") or ""),
            "macd_state_15m": str(chart_15m.get("macd_histo_state", "") or ""),
            "range_quality": quality,
            "range_activation_bias": activation_bias,
            "suggested_range_mode": suggested_mode,
            "preferred_range_action": preferred_range_action,
            "range_start_contract": {
                "recommended_action_intent": "manage_orders",
                "recommended_execution_mode": "managed_execution",
                "recommended_intraday_mode": "manage",
                "recommended_entry_plan_direction": recommended_entry_direction,
                "recommended_hypothesis_action": recommended_hypothesis_action,
                "current_hypothesis_expiry": current_hypothesis_expiry or "none",
                "candidate_range_expires_at": expires_at,
                "expiry_handling_note": expiry_note,
            },
        }

    def _render_sideways_range_opportunity_snapshot(
        self,
        state: DecisionState,
        precheck_payload: Dict[str, Any],
    ) -> str:
        snapshot = precheck_payload.get("sideways_range_snapshot")
        if not isinstance(snapshot, dict):
            snapshot = self._build_sideways_range_opportunity_snapshot(state, precheck_payload)
        lines = ["[SIDEWAYS RANGE OPPORTUNITY SNAPSHOT]"]
        if not snapshot.get("available"):
            lines.append(f"- status: unavailable ({snapshot.get('reason', 'unknown')})")
            lines.append("- No structured 15m band scaffold could be derived from the current market snapshot.")
            return "\n".join(lines)

        quality = snapshot.get("range_quality", {})
        if not isinstance(quality, dict):
            quality = {}
        hard_bounds = snapshot.get("hard_bounds", {})
        if not isinstance(hard_bounds, dict):
            hard_bounds = {}
        working_band = snapshot.get("working_band", {})
        if not isinstance(working_band, dict):
            working_band = {}
        quartile_levels = snapshot.get("quartile_levels", {})
        if not isinstance(quartile_levels, dict):
            quartile_levels = {}
        range_start_contract = snapshot.get("range_start_contract", {})
        if not isinstance(range_start_contract, dict):
            range_start_contract = {}
        lines.extend(
            [
                f"- reference_price: {snapshot['reference_price']}",
                f"- hard_bounds_for_breakout_invalidation: lower={hard_bounds.get('lower_breakout', snapshot['lower_breakout'])} | upper={hard_bounds.get('upper_breakout', snapshot['upper_breakout'])} | suggested_buffer_pct={snapshot.get('suggested_buffer_pct', 'unknown')}%",
                f"- working_band_for_order_placement: lower={working_band.get('lower', 'unknown')} | mid={working_band.get('mid', snapshot['midpoint'])} | upper={working_band.get('upper', 'unknown')}",
                f"- quartile_levels: q1={quartile_levels.get('q1', 'unknown')} | q2={quartile_levels.get('q2', 'unknown')} | q3={quartile_levels.get('q3', 'unknown')}",
                f"- band_width: {snapshot['range_width']} ({snapshot['range_width_pct']}% of price)",
                f"- price_location_inside_band: {snapshot['price_location']} ({snapshot['position_in_band_pct']}%)",
                f"- distance_to_lower: {snapshot['distance_to_lower']}",
                f"- distance_to_upper: {snapshot['distance_to_upper']}",
                f"- 15m_signal_context: RSI={snapshot['rsi_15m']} | MACD_HISTO={snapshot['macd_histo_15m']} | price_momentum={snapshot['price_momentum_15m'] or 'unknown'} | macd_state={snapshot['macd_state_15m'] or 'unknown'}",
                f"- 1h_signal_context: RSI={snapshot['rsi_1h']} | MACD_HISTO={snapshot['macd_histo_1h']}",
                f"- recent_range_quality: oscillation_score={quality.get('oscillation_score', 'unknown')} | activation_bias={snapshot.get('range_activation_bias', 'unknown')} | suggested_range_mode={snapshot.get('suggested_range_mode', 'unknown')} | preferred_range_action={snapshot.get('preferred_range_action', 'unknown')} | recent_bars={quality.get('recent_bar_count', 'unknown')} | upper_touches={quality.get('upper_touch_count', 'unknown')} | lower_touches={quality.get('lower_touch_count', 'unknown')} | midpoint_crosses={quality.get('midpoint_cross_count', 'unknown')} | swings={quality.get('swing_count', 'unknown')} | avg_width_pct={quality.get('avg_width_pct', 'unknown')}",
                f"- active_directional_memory: trade_intent={snapshot['active_trade_intent'] or 'none'} | entry_plan_direction={snapshot['active_entry_plan_direction'] or 'none'} | hypothesis={snapshot['active_hypothesis_id'] or 'none'} | status={snapshot['active_hypothesis_status'] or 'none'}",
                f"- candidate_breakout_bounds_for_set_range_plan: lower_breakout={snapshot['lower_breakout']} | upper_breakout={snapshot['upper_breakout']}",
                f"- candidate_long_levels: {json.dumps(snapshot['long_levels'], ensure_ascii=False)}",
                f"- candidate_short_levels: {json.dumps(snapshot['short_levels'], ensure_ascii=False)}",
                f"- candidate_expires_at: {snapshot['expires_at']}",
                f"- max_open_reference: {snapshot['max_quantity'] if snapshot['max_quantity'] is not None else 'unknown'}",
                f"- range_start_contract_hint: action_intent={range_start_contract.get('recommended_action_intent', 'unknown')} | execution_mode={range_start_contract.get('recommended_execution_mode', 'unknown')} | intraday_mode={range_start_contract.get('recommended_intraday_mode', 'unknown')} | entry_plan_direction={range_start_contract.get('recommended_entry_plan_direction', 'unknown')} | hypothesis_action={range_start_contract.get('recommended_hypothesis_action', 'unknown')}",
                f"- hypothesis_expiry_contract: current={range_start_contract.get('current_hypothesis_expiry', 'unknown')} | candidate_range_expires_at={range_start_contract.get('candidate_range_expires_at', 'unknown')}",
                f"- hypothesis_expiry_handling_note: {range_start_contract.get('expiry_handling_note', 'unknown')}",
                "- This block is an execution scaffold, not a forced action. It gives you explicit bounds and level shapes if you decide the waiting window is better monetized as sideways harvesting.",
                "- Use recent_range_quality as the primary judge of whether the recent window is behaving like a tradable oscillating box. Near-edge location alone is not a sufficient reason to decline range when oscillation quality is strong.",
                "- If preferred_range_action=start_preferred, the system is telling you this window looks more like a monetizable sideways box than a pure sit-and-wait regime.",
                "- If you choose range_decision=start, follow the range_start_contract_hint literally: action_intent should usually be manage_orders, execution_mode should be managed_execution, and intraday_mode should be manage (never managed_execution).",
                "- If candidate_range_expires_at is earlier than the current hypothesis expiry, do not shrink the hypothesis just to match the range plan. Keep the hypothesis unchanged and let the range plan expire first.",
                "- If you declare a new reversal checklist or hypothesis expiry while starting range, you must persist those exact fields in short_memory_ops; otherwise the guard will reject the wakeup as a contract error rather than a market disagreement.",
                "- Read the band in two layers: hard_bounds define where the sideways thesis is invalidated; working_band and quartile_levels define where the passive orders should actually live.",
                "- If breakout risk is non-trivial but box quality is still good, widen hard_bounds first and keep the execution levels inside the working_band. Do not abandon range automatically just because the live price is near an edge.",
                "- If suggested_range_mode=short_only or long_only, you may activate a one-sided range plan from the edge instead of waiting for price to drift back toward the middle.",
                "- A blocked directional hypothesis is not itself a live entry trigger. If you keep waiting, do so by choice, not because the symbol is implicitly reserved from optional range harvesting.",
            ]
        )
        return "\n".join(lines)

    def _load_memory_snapshots(self) -> Dict[str, Any]:
        short_snapshot = self._load_or_migrate_short_memory()
        long_snapshot = self._load_or_migrate_long_memory()
        return {
            "short_obj": short_snapshot,
            "long_obj": long_snapshot,
            "short_text": self._render_memory_snapshot_for_prompt(short_snapshot, "No active short-term memory.", "short_memory_ops"),
            "long_text": self._render_memory_snapshot_for_prompt(long_snapshot, "No historical experience yet.", "long_memory_ops"),
        }

    def _collect_recent_execution_history(self, days: int = 3, limit: int = 48) -> List[Dict[str, Any]]:
        cutoff = datetime.now() - timedelta(days=days)
        history: List[Dict[str, Any]] = []
        if not os.path.isdir(LOGS_DIR):
            return history

        folder_names = sorted(
            [
                name
                for name in os.listdir(LOGS_DIR)
                if os.path.isdir(os.path.join(LOGS_DIR, name)) and re.match(r"^\d{8}_\d{6}$", name)
            ]
        )
        for folder_name in folder_names:
            try:
                folder_dt = datetime.strptime(folder_name, "%Y%m%d_%H%M%S")
            except Exception:
                continue
            if folder_dt < cutoff:
                continue
            output_path = os.path.join(LOGS_DIR, folder_name, "output.json")
            if not os.path.exists(output_path):
                continue
            payload = safe_json_read(output_path, f"Execution History {folder_name}", use_lock=False)
            if not isinstance(payload, dict) or not payload:
                continue
            content = payload.get("content", {})
            audit_meta = payload.get("audit_meta", {})
            if not isinstance(content, dict):
                content = {}
            if not isinstance(audit_meta, dict):
                audit_meta = {}
            history.append(
                {
                    "log_dir": folder_name,
                    "recorded_at": folder_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "event_type": str(audit_meta.get("event_type", "") or ""),
                    "execution_txt": self._compact_text(content.get("execution_txt", ""), 240),
                    "explanation": self._compact_text(content.get("explanation", ""), 240),
                    "action_intent": str(content.get("action_intent", "") or ""),
                    "plan_transition": str(content.get("plan_transition", "") or ""),
                    "hypothesis_action": str(content.get("hypothesis_action", "") or ""),
                    "state_change_evidence": self._compact_text(content.get("state_change_evidence", ""), 200),
                    "falsification_point": self._compact_text(content.get("falsification_point", ""), 180),
                    "validation_status": str(payload.get("validation_status", "") or ""),
                }
            )
        return history[-limit:]

    def _is_decision_like_tactical_alert(self, alert: Dict[str, Any]) -> bool:
        kind = str(alert.get("kind", "") or "").lower()
        summary = str(alert.get("summary", "") or "").lower()
        if self._is_alarm_like_tactical_alert(alert):
            return False
        patterns = [
            "long @",
            "short @",
            "stopped out",
            "invalidated",
            "已成交",
            "已平",
            "止损",
            "平仓",
            "hypothesis",
        ]
        return any(token in kind or token in summary for token in patterns)

    def _post_stop_reflection_is_live(self, reflection: Dict[str, Any], *, now: Optional[datetime] = None) -> bool:
        if not isinstance(reflection, dict) or not reflection:
            return False
        current_time = now or datetime.now()
        expires_at = self._parse_timestamp(reflection.get("expires_at"))
        if expires_at is None:
            recorded_at = self._parse_timestamp(reflection.get("recorded_at"))
            if recorded_at is None:
                return False
            expires_at = recorded_at + timedelta(hours=12)
        return current_time <= expires_at

    def _format_post_stop_reflection_for_prompt(self, reflection: Dict[str, Any]) -> str:
        recorded_at = str(reflection.get("recorded_at", "") or "unknown_time")
        direction = str(reflection.get("direction", "") or "flat")
        hypothesis_id = str(reflection.get("hypothesis_id", "") or "").strip()
        trigger_context = self._compact_text(reflection.get("trigger_context", ""), 160)
        outcome = self._compact_text(reflection.get("outcome", ""), 140)
        why_entered = self._compact_text(reflection.get("why_entered", ""), 180)
        why_failed = self._compact_text(reflection.get("why_failed", ""), 180)
        retry_guardrail = self._compact_text(reflection.get("retry_guardrail", ""), 180)
        suffix = f" | hypothesis={hypothesis_id}" if hypothesis_id else ""
        parts = [f"- [{recorded_at}] {direction}{suffix}: {outcome or 'recent protective stop / stop-out reflection'}"]
        if trigger_context:
            parts.append(f"  trigger context: {trigger_context}")
        if why_entered:
            parts.append(f"  thesis before stop: {why_entered}")
        if why_failed:
            parts.append(f"  why it failed: {why_failed}")
        if retry_guardrail:
            parts.append(f"  retry requires: {retry_guardrail}")
        return "\n".join(parts)

    def _format_decision_episode_for_prompt(self, episode: Dict[str, Any]) -> str:
        recorded_at = str(episode.get("recorded_at", "") or "unknown_time")
        direction = str(episode.get("direction", "") or "flat")
        action = self._compact_text(episode.get("action", ""), 120)
        outcome = self._compact_text(episode.get("outcome", ""), 120)
        hypothesis_id = str(episode.get("hypothesis_id", "") or "").strip()
        why_entered = self._compact_text(episode.get("why_entered", ""), 160)
        why_failed = self._compact_text(episode.get("why_failed", ""), 160)
        retry_guardrail = self._compact_text(episode.get("retry_guardrail", ""), 160)
        suffix = f" | hypothesis={hypothesis_id}" if hypothesis_id else ""
        parts = [f"- [{recorded_at}] {direction}{suffix}: {action} -> {outcome}"]
        if why_entered:
            parts.append(f"  entered because: {why_entered}")
        if why_failed:
            parts.append(f"  failed because: {why_failed}")
        if retry_guardrail:
            parts.append(f"  retry only if: {retry_guardrail}")
        return "\n".join(parts)

    def _build_recent_closed_position_feedback(self, preferred_windows: Optional[List[int]] = None) -> Dict[str, Any]:
        candidate_windows = list(preferred_windows or [21, 3])
        for window_days in candidate_windows:
            artifact_path = os.path.join(PROJECT_ROOT, f"positions_last_{window_days}_0_days.json")
            feedback = self._build_closed_position_feedback_from_file(artifact_path, window_days=window_days)
            if feedback.get("position_count", 0) > 0:
                return feedback
        return self._default_closed_position_feedback()

    def _build_closed_position_feedback_from_file(self, file_path: str, *, window_days: int) -> Dict[str, Any]:
        normalized = self._default_closed_position_feedback()
        normalized["window_days"] = int(window_days or 0)
        if not os.path.exists(file_path):
            return normalized

        payload = safe_json_read(file_path, f"Closed Position Feedback {os.path.basename(file_path)}", use_lock=False)
        if not isinstance(payload, list):
            return normalized

        rows = [item for item in payload if isinstance(item, dict)]
        if not rows:
            return normalized

        def _safe_float(value: Any) -> float:
            try:
                return float(value or 0.0)
            except Exception:
                return 0.0

        def _direction_bucket(item: Dict[str, Any]) -> str:
            raw_direction = str(item.get("direction", "") or item.get("side", "") or "").strip().upper()
            if raw_direction in {"LONG", "BUY"}:
                return "long"
            if raw_direction in {"SHORT", "SELL"}:
                return "short"
            return ""

        realized_total = sum(_safe_float(item.get("realized_pnl_eth")) for item in rows)
        net_total = sum(_safe_float(item.get("net_pnl_after_fee_eth")) for item in rows)
        return_values = [
            _safe_float(item.get("return_rate_pct_estimate"))
            for item in rows
            if item.get("return_rate_pct_estimate") not in (None, "")
        ]
        win_rows = [item for item in rows if _safe_float(item.get("net_pnl_after_fee_eth")) > 0]
        loss_rows = [item for item in rows if _safe_float(item.get("net_pnl_after_fee_eth")) < 0]
        long_rows = [item for item in rows if _direction_bucket(item) == "long"]
        short_rows = [item for item in rows if _direction_bucket(item) == "short"]

        def _sample(item: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "open_time_bj": str(item.get("open_time_bj", "") or ""),
                "close_time_bj": str(item.get("all_close_time_bj", "") or item.get("close_time_bj", "") or ""),
                "direction": str(item.get("direction", "") or ""),
                "net_pnl_after_fee_eth": round(_safe_float(item.get("net_pnl_after_fee_eth")), 8),
                "return_rate_pct_estimate": round(_safe_float(item.get("return_rate_pct_estimate")), 4),
            }

        range_start = min(
            [str(item.get("open_time_bj", "") or "") for item in rows if str(item.get("open_time_bj", "") or "").strip()],
            default="",
        )
        range_end = max(
            [
                str(item.get("all_close_time_bj", "") or item.get("close_time_bj", "") or "")
                for item in rows
                if str(item.get("all_close_time_bj", "") or item.get("close_time_bj", "") or "").strip()
            ],
            default="",
        )
        source_updated_at_dt = datetime.fromtimestamp(os.path.getmtime(file_path))
        source_age_hours = max((datetime.now() - source_updated_at_dt).total_seconds() / 3600.0, 0.0)

        normalized.update(
            {
                "source_file": os.path.basename(file_path),
                "source_updated_at": source_updated_at_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "source_stale": source_age_hours > 24.0,
                "source_age_hours": round(source_age_hours, 2),
                "range_start": range_start,
                "range_end": range_end,
                "position_count": len(rows),
                "win_count": len(win_rows),
                "loss_count": len(loss_rows),
                "win_rate_pct": round((len(win_rows) / len(rows)) * 100, 2),
                "realized_pnl_eth": round(realized_total, 8),
                "net_pnl_after_fee_eth": round(net_total, 8),
                "average_return_pct": round(sum(return_values) / len(return_values), 4) if return_values else 0.0,
                "long_count": len(long_rows),
                "long_net_pnl_after_fee_eth": round(
                    sum(_safe_float(item.get("net_pnl_after_fee_eth")) for item in long_rows),
                    8,
                ),
                "short_count": len(short_rows),
                "short_net_pnl_after_fee_eth": round(
                    sum(_safe_float(item.get("net_pnl_after_fee_eth")) for item in short_rows),
                    8,
                ),
                "top_losses": [_sample(item) for item in sorted(rows, key=lambda item: _safe_float(item.get("net_pnl_after_fee_eth")))[:3]],
                "top_wins": [
                    _sample(item)
                    for item in sorted(rows, key=lambda item: _safe_float(item.get("net_pnl_after_fee_eth")), reverse=True)[:3]
                    if _safe_float(item.get("net_pnl_after_fee_eth")) > 0
                ],
            }
        )
        return self._sanitize_closed_position_feedback(normalized)

    def _persist_emergency_turn_log(
        self,
        *,
        interaction_log_dir: str,
        combined_type: str,
        combined_content: str,
        exc: BaseException,
        state: Optional[DecisionState] = None,
        recorder: Optional[List[Dict[str, Any]]] = None,
        stage: str,
    ) -> None:
        try:
            os.makedirs(interaction_log_dir, exist_ok=True)
            input_path = os.path.join(interaction_log_dir, "input.md")
            output_path = os.path.join(interaction_log_dir, "output.json")

            state_obj = state if isinstance(state, dict) else {}
            system_prompt = str(state_obj.get("system_prompt", "") or "")
            user_prompt = str(state_obj.get("user_prompt", "") or "")
            if not user_prompt:
                user_prompt = (
                    f"Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Event Type: {combined_type}\n\nEvent Content:\n{combined_content}"
                )
            if not os.path.exists(input_path):
                with open(input_path, "w", encoding="utf-8") as f:
                    f.write(f"--- SYSTEM PROMPT ---\n{system_prompt}\n\n--- USER PROMPT ---\n{user_prompt}")

            if os.path.exists(output_path):
                return

            traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            fatal_decision = DecisionOutput(
                execution_txt="日志目录已创建，但本轮在最终日志落盘前异常中断，未执行新的交易动作。",
                explanation=f"Fatal turn interruption ({stage}): {type(exc).__name__}: {exc}",
                memory_management_reasoning="保留现有结构化记忆；本轮仅补写紧急错误日志供审计。",
                decision_basis="fatal_turn_interruption",
                conflict_check="unexpected_turn_abort",
                falsification_point="检查 traceback 与同时间段进程生命周期，确认是未捕获异常还是进程中断。",
                next_alarm_reason="修复异常后等待下一次正常唤醒。",
                state_change_evidence="none",
                experience="",
                shortterm="",
            ).model_dump()
            log_data = {
                "content": fatal_decision,
                "usage": {},
                "tool_calls": recorder if isinstance(recorder, list) else [],
                "attempts": [],
                "unparsed_fields": [],
                "consistency_violations": [],
                "final_validation_errors": [f"fatal_turn_interruption: {type(exc).__name__}: {exc}"],
                "validation_events": [{"tag": "fatal_turn_interruption", "reason": str(exc)}],
                "validation_status": "fatal_turn_interruption",
                "reanswer_count": 0,
                "triggered_rules": "fatal_turn_interruption",
                "triggered_rules_first_fail": "fatal_turn_interruption",
                "triggered_rules_all_fail": "fatal_turn_interruption",
                "triggered_rules_final": "fatal_turn_interruption",
                "resolved_rules": "none",
                "rejected_tool_calls": [],
                "guard_pipeline": [],
                "guard_checks_all": [],
                "guard_failures": [],
                "resolved_guard_failures": [],
                "guard_retry": {},
                "total_turns": 1,
                "all_turns": [],
                "timestamp": datetime.now().isoformat(),
                "account_data": str(state_obj.get("account_snapshot", "")),
                "audit_meta": {
                    "event_type": combined_type,
                    "event_details": combined_content,
                    "fatal_stage": stage,
                    "traceback": traceback_text,
                },
            }
            safe_json_dump(log_data, output_path, use_lock=False)
            if state_obj:
                self.write_turn_context_snapshots(interaction_log_dir, state_obj)
        except Exception as persist_exc:
            print(
                f"[EMERGENCY_LOG_WRITE_FAILED] {type(persist_exc).__name__}: {persist_exc}",
                flush=True,
            )

    def _persist_prompt_input_snapshot(self, interaction_log_dir: str, system_prompt: str, user_prompt: str) -> None:
        try:
            if not interaction_log_dir:
                return
            os.makedirs(interaction_log_dir, exist_ok=True)
            with open(os.path.join(interaction_log_dir, "input.md"), "w", encoding="utf-8") as f:
                f.write(f"--- SYSTEM PROMPT ---\n{system_prompt}\n\n--- USER PROMPT ---\n{user_prompt}")
        except Exception as persist_exc:
            print(
                f"[PROMPT_SNAPSHOT_WRITE_FAILED] {type(persist_exc).__name__}: {persist_exc}",
                flush=True,
            )

    def build_long_review_prompt(
        self,
        long_reflection: Dict[str, Any],
        long_memory: Dict[str, Any],
        daily_execution_reflection: Dict[str, Any],
        post_stop_reflection: Dict[str, Any],
        recent_execution_history: List[Dict[str, Any]],
        closed_position_feedback: Dict[str, Any],
    ) -> tuple:
        prompt_payloads = self._build_long_review_prompt_payloads(
            long_reflection,
            long_memory,
            daily_execution_reflection,
            post_stop_reflection,
            recent_execution_history,
            closed_position_feedback,
        )
        replacements = {
            "{{current_time}}": _utc_now_iso(),
            "{{long_reflection}}": self._render_long_context_json(prompt_payloads.get("long_reflection", {})),
            "{{long_textbook}}": self._render_long_context_json(prompt_payloads.get("long_textbook", {}), "{}"),
            "{{daily_execution_reflection}}": self._render_long_context_json(prompt_payloads.get("daily_execution_reflection", {}), "{}"),
            "{{post_stop_reflection}}": self._render_long_context_json(prompt_payloads.get("post_stop_reflection", {}), "{}"),
            "{{recent_execution_history}}": self._render_long_context_json(prompt_payloads.get("recent_execution_history", []), "[]"),
            "{{closed_position_feedback}}": self._render_long_context_json(prompt_payloads.get("closed_position_feedback", {}), "{}"),
        }
        if os.path.exists(LONG_REVIEW_PROMPT_TEMPLATE_PATH):
            try:
                with open(LONG_REVIEW_PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                    template = f.read()
                return self._render_prompt_template(template, replacements)
            except Exception as e:
                print(f"Error building long review prompt from template: {e}", flush=True)

        system_prompt = "You are CoinAutomation's daily long-horizon review agent."
        user_prompt = (
            f"Current Time: {_utc_now_iso()}\n\n"
            f"Long Reflection:\n{json.dumps(prompt_payloads.get('long_reflection', {}), ensure_ascii=False, indent=2)}\n\n"
            f"Long Textbook:\n{json.dumps(prompt_payloads.get('long_textbook', {}), ensure_ascii=False, indent=2)}\n\n"
            f"Daily Execution Reflection:\n{json.dumps(prompt_payloads.get('daily_execution_reflection', {}), ensure_ascii=False, indent=2)}\n\n"
            f"Post Stop Reflection:\n{json.dumps(prompt_payloads.get('post_stop_reflection', {}), ensure_ascii=False, indent=2)}\n\n"
            f"Recent Execution History:\n{json.dumps(prompt_payloads.get('recent_execution_history', []), ensure_ascii=False, indent=2)}\n\n"
            f"Recent Closed Position PnL Reflection:\n{json.dumps(prompt_payloads.get('closed_position_feedback', {}), ensure_ascii=False, indent=2)}\n"
        )
        return system_prompt, user_prompt

    def _invoke_long_review_agent(
        self,
        long_reflection: Dict[str, Any],
        long_memory: Dict[str, Any],
        daily_execution_reflection: Dict[str, Any],
        post_stop_reflection: Dict[str, Any],
        recent_execution_history: List[Dict[str, Any]],
        closed_position_feedback: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        system_prompt, user_prompt = self.build_long_review_prompt(
            long_reflection,
            long_memory,
            daily_execution_reflection,
            post_stop_reflection,
            recent_execution_history,
            closed_position_feedback,
        )
        response = self.long_review_model.invoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        raw_text = self._extract_chat_message_text(getattr(response, "content", ""))
        recovered, recovery = _recover_json_dict_with_diagnostics(raw_text, "Long Review Agent JSON")
        diagnostics = {
            "response_length": len(raw_text or ""),
            "recovery": recovery,
            "raw_response_text": raw_text,
        }
        if isinstance(recovered, dict):
            try:
                validated = LongReviewPatchOutputModel.model_validate(recovered)
            except Exception as exc:
                diagnostics["schema_validation_error"] = str(exc)
                raise JSONRecoveryError(
                    "Long review agent returned JSON but failed schema validation.",
                    raw_text=raw_text,
                    diagnostics=diagnostics,
                ) from exc
            long_reflection_ops = _normalize_json_patch_ops(
                list(validated.long_reflection_ops or []),
                label="long_reflection_ops",
            )
            daily_execution_reflection_ops = _normalize_json_patch_ops(
                list(validated.daily_execution_reflection_ops or []),
                label="daily_execution_reflection_ops",
            )
            _validate_json_patch_ops_shape(long_reflection_ops, label="long_reflection_ops")
            _validate_json_patch_ops_shape(
                daily_execution_reflection_ops,
                label="daily_execution_reflection_ops",
            )
            payload = validated.model_dump(exclude_unset=True)
            payload["long_reflection_ops"] = long_reflection_ops
            payload["daily_execution_reflection_ops"] = daily_execution_reflection_ops
            diagnostics["validated_with_model"] = True
            return payload, diagnostics
        raise JSONRecoveryError(
            "Long review agent did not return valid JSON.",
            raw_text=raw_text,
            diagnostics=diagnostics,
        )

    def _normalize_long_review_payload(
        self,
        current_reflection: Dict[str, Any],
        current_daily_execution_reflection: Dict[str, Any],
        agent_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        raw_long_reflection_ops = list(agent_payload.get("long_reflection_ops", []) or [])
        long_reflection_ops = _normalize_json_patch_ops(
            raw_long_reflection_ops,
            label="long_reflection_ops",
        )
        _validate_json_patch_ops_shape(long_reflection_ops, label="long_reflection_ops")
        if long_reflection_ops:
            patched_reflection = self._apply_json_patch_ops(current_reflection, long_reflection_ops)
        else:
            legacy_reflection = agent_payload.get("long_reflection", {})
            if isinstance(legacy_reflection, dict) and legacy_reflection:
                patched_reflection = legacy_reflection
            else:
                patched_reflection = deepcopy(current_reflection)
        next_reflection = self._sanitize_long_reflection(patched_reflection, source="long_review_agent")
        next_reflection["news_case_log"] = list(next_reflection.get("news_case_log", []))[-400:]
        next_reflection["meta"]["updated_at"] = _utc_now_iso()
        next_reflection["meta"]["source"] = "long_review_agent"

        review_summary = agent_payload.get("review_summary", {})
        if not isinstance(review_summary, dict):
            review_summary = {}
        active_count = len(next_reflection.get("active_hypotheses", [])) if isinstance(next_reflection.get("active_hypotheses", []), list) else 0
        invalidated_count = len(next_reflection.get("invalidated_hypotheses", [])) if isinstance(next_reflection.get("invalidated_hypotheses", []), list) else 0
        normalized_summary = {
            "reviewed_at": _utc_now_iso(),
            "kept_count": int(review_summary.get("kept_count", active_count) or active_count),
            "invalidated_count": int(review_summary.get("invalidated_count", invalidated_count) or invalidated_count),
            "new_hypotheses_count": int(review_summary.get("new_hypotheses_count", 0) or 0),
            "notes": str(review_summary.get("notes", "") or "").strip() or "Daily long review completed.",
            "changes": review_summary.get("changes", []) if isinstance(review_summary.get("changes", []), list) else [],
        }
        raw_daily_execution_reflection_ops = list(agent_payload.get("daily_execution_reflection_ops", []) or [])
        daily_execution_reflection_ops = _normalize_json_patch_ops(
            raw_daily_execution_reflection_ops,
            label="daily_execution_reflection_ops",
        )
        _validate_json_patch_ops_shape(
            daily_execution_reflection_ops,
            label="daily_execution_reflection_ops",
        )
        if daily_execution_reflection_ops:
            patched_daily_execution_reflection = self._apply_json_patch_ops(
                current_daily_execution_reflection,
                daily_execution_reflection_ops,
            )
        else:
            legacy_daily_execution_reflection = agent_payload.get("daily_execution_reflection", {})
            if isinstance(legacy_daily_execution_reflection, dict) and legacy_daily_execution_reflection:
                patched_daily_execution_reflection = legacy_daily_execution_reflection
            else:
                patched_daily_execution_reflection = deepcopy(current_daily_execution_reflection)
        next_daily_execution_reflection = self._sanitize_daily_execution_reflection(
            patched_daily_execution_reflection,
            source="daily_review_agent",
        )
        next_daily_execution_reflection["closed_position_feedback"] = self._sanitize_closed_position_feedback(
            current_daily_execution_reflection.get("closed_position_feedback", {}),
        )
        next_daily_execution_reflection["recent_episodes"] = list(next_daily_execution_reflection.get("recent_episodes", []))[-12:]
        next_daily_execution_reflection["meta"]["updated_at"] = _utc_now_iso()
        next_daily_execution_reflection["meta"]["source"] = "daily_review_agent"
        return {
            "long_reflection": next_reflection,
            "daily_execution_reflection": next_daily_execution_reflection,
            "review_summary": normalized_summary,
        }

    async def _run_daily_long_review_events(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        interaction_log_dir = os.path.join(LOGS_DIR, timestamp)
        os.makedirs(interaction_log_dir, exist_ok=True)
        combined_type = " & ".join(sorted(set([str(e.get("type", "daily_long_review")) for e in events])))
        combined_content = "\n\n---\n\n".join(
            [f"Event {idx + 1} ({evt.get('type', 'daily_long_review')}):\n{evt.get('content', '')}" for idx, evt in enumerate(events)]
        )

        long_reflection = self._load_or_bootstrap_long_reflection()
        long_memory = self._load_or_migrate_long_memory()
        daily_execution_reflection = self._load_or_bootstrap_daily_execution_reflection()
        daily_execution_reflection["closed_position_feedback"] = self._build_recent_closed_position_feedback([21, 3])
        post_stop_reflection = self._load_or_bootstrap_post_stop_reflection()
        recent_execution_history = self._collect_recent_execution_history(days=3, limit=48)
        closed_position_feedback = self._sanitize_closed_position_feedback(
            daily_execution_reflection.get("closed_position_feedback", {}),
        )
        state_snapshot: DecisionState = {
            "event_batch": events,
            "event_type": combined_type,
            "event_details": combined_content,
            "long_reflection_obj": long_reflection,
        }

        system_prompt = ""
        user_prompt = ""
        long_review_state = self._load_long_review_state()
        review_agent_diagnostics: Dict[str, Any] = {}
        review_raw_response = ""
        try:
            system_prompt, user_prompt = self.build_long_review_prompt(
                long_reflection,
                long_memory,
                daily_execution_reflection,
                post_stop_reflection,
                recent_execution_history,
                closed_position_feedback,
            )
            agent_payload, review_agent_diagnostics = self._invoke_long_review_agent(
                long_reflection,
                long_memory,
                daily_execution_reflection,
                post_stop_reflection,
                recent_execution_history,
                closed_position_feedback,
            )
            review_raw_response = str(review_agent_diagnostics.pop("raw_response_text", "") or "")
            normalized = self._normalize_long_review_payload(
                long_reflection,
                daily_execution_reflection,
                agent_payload,
            )
            updated_reflection = normalized["long_reflection"]
            updated_daily_execution_reflection = normalized["daily_execution_reflection"]
            self._persist_long_reflection(updated_reflection)
            self._persist_daily_execution_reflection(updated_daily_execution_reflection)
            state_snapshot["long_reflection_obj"] = updated_reflection
            summary = normalized["review_summary"]
            long_review_state.update(
                {
                    "last_review_date": datetime.now().strftime("%Y-%m-%d"),
                    "last_triggered_at": _utc_now_iso(),
                    "last_run_status": "updated",
                    "last_run_log_dir": interaction_log_dir,
                    "last_error": "",
                }
            )
            self._save_long_review_state(long_review_state)
            log_data = {
                "content": {
                    "execution_txt": "Daily long review completed; no trading action taken.",
                    "explanation": summary.get("notes", ""),
                    "memory_management_reasoning": "Updated mem/long_reflection.json and mem/daily_execution_reflection.json.",
                    "decision_basis": "daily_long_review",
                    "conflict_check": "review_only_no_trade",
                    "falsification_point": "",
                    "next_alarm_reason": "",
                    "state_change_evidence": "daily_long_review_completed",
                    "short_memory_ops": [],
                    "long_memory_ops": [],
                    "experience": "",
                    "shortterm": "",
                },
                "daily_review": {
                    "summary": summary,
                    "active_hypotheses": updated_reflection.get("active_hypotheses", []),
                    "invalidated_hypotheses": updated_reflection.get("invalidated_hypotheses", [])[-20:],
                    "daily_execution_reflection": updated_daily_execution_reflection,
                },
                "usage": {},
                "tool_calls": [],
                "attempts": [],
                "unparsed_fields": [],
                "consistency_violations": [],
                "final_validation_errors": [],
                "validation_events": [],
                "validation_status": "review_only",
                "reanswer_count": 0,
                "triggered_rules": "none",
                "triggered_rules_first_fail": "none",
                "triggered_rules_all_fail": "none",
                "triggered_rules_final": "none",
                "resolved_rules": "none",
                "rejected_tool_calls": [],
                "guard_pipeline": [],
                "guard_checks_all": [],
                "guard_failures": [],
                "resolved_guard_failures": [],
                "guard_retry": {},
                "total_turns": 1,
                "all_turns": [],
                "timestamp": datetime.now().isoformat(),
                "account_data": "",
                "audit_meta": {
                    "event_type": combined_type,
                    "event_details": combined_content,
                    "daily_review_completed_at": _utc_now_iso(),
                },
            }
        except Exception as e:
            if isinstance(e, JSONRecoveryError):
                review_raw_response = str(getattr(e, "raw_text", "") or "")
                review_agent_diagnostics = deepcopy(getattr(e, "diagnostics", {}) or {})
            long_review_state.update(
                {
                    "last_triggered_at": _utc_now_iso(),
                    "last_run_status": f"error:{type(e).__name__}",
                    "last_run_log_dir": interaction_log_dir,
                    "last_error": str(e),
                }
            )
            self._save_long_review_state(long_review_state)
            log_data = {
                "content": {
                    "execution_txt": "Daily long review failed; no trading action taken.",
                    "explanation": f"{type(e).__name__}: {e}",
                    "memory_management_reasoning": "Kept previous long_reflection and daily_execution_reflection snapshots.",
                    "decision_basis": "daily_long_review",
                    "conflict_check": "review_failed",
                    "falsification_point": "",
                    "next_alarm_reason": "",
                    "state_change_evidence": "daily_long_review_failed",
                    "short_memory_ops": [],
                    "long_memory_ops": [],
                    "experience": "",
                    "shortterm": "",
                },
                "daily_review": {
                    "summary": {
                        "reviewed_at": _utc_now_iso(),
                        "kept_count": 0,
                        "invalidated_count": 0,
                        "new_hypotheses_count": 0,
                        "notes": f"Daily long review failed: {type(e).__name__}: {e}",
                        "changes": [],
                    }
                },
                "usage": {},
                "tool_calls": [],
                "attempts": [],
                "unparsed_fields": [],
                "consistency_violations": [],
                "final_validation_errors": [f"daily_long_review_error: {type(e).__name__}: {e}"],
                "validation_events": [{"tag": "daily_long_review_error", "reason": str(e)}],
                "validation_status": "review_failed",
                "reanswer_count": 0,
                "triggered_rules": "daily_long_review_error",
                "triggered_rules_first_fail": "daily_long_review_error",
                "triggered_rules_all_fail": "daily_long_review_error",
                "triggered_rules_final": "daily_long_review_error",
                "resolved_rules": "none",
                "rejected_tool_calls": [],
                "guard_pipeline": [],
                "guard_checks_all": [],
                "guard_failures": [],
                "resolved_guard_failures": [],
                "guard_retry": {},
                "total_turns": 1,
                "all_turns": [],
                "timestamp": datetime.now().isoformat(),
                "account_data": "",
                "audit_meta": {
                    "event_type": combined_type,
                    "event_details": combined_content,
                    "daily_review_completed_at": _utc_now_iso(),
                },
            }

        if review_agent_diagnostics:
            diagnostics = deepcopy(review_agent_diagnostics)
            diagnostics["raw_response_file"] = LONG_REVIEW_RAW_RESPONSE_FILENAME if review_raw_response else ""
            log_data["daily_review_diagnostics"] = diagnostics
        if review_raw_response:
            with open(os.path.join(interaction_log_dir, LONG_REVIEW_RAW_RESPONSE_FILENAME), "w", encoding="utf-8") as f:
                f.write(review_raw_response)

        with open(os.path.join(interaction_log_dir, "input.md"), "w", encoding="utf-8") as f:
            f.write(f"--- SYSTEM PROMPT ---\n{system_prompt}\n\n--- USER PROMPT ---\n{user_prompt}")
        safe_json_dump(log_data, os.path.join(interaction_log_dir, "output.json"), use_lock=False)
        self.write_turn_context_snapshots(interaction_log_dir, state_snapshot)

    async def daily_review_monitor(self):
        while True:
            try:
                now = datetime.now()
                review_state = self._load_long_review_state()
                last_review_date = str(review_state.get("last_review_date", "") or "")
                last_run_status = str(review_state.get("last_run_status", "") or "")
                last_triggered_at = self._parse_timestamp(review_state.get("last_triggered_at"))
                target_hour, target_minute = [int(piece) for piece in DAILY_LONG_REVIEW_TIME.split(":", 1)]
                target_dt = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                retry_window = timedelta(minutes=DAILY_LONG_REVIEW_RETRY_MINUTES)
                same_day_trigger = bool(last_triggered_at and last_triggered_at.date() == now.date())
                if now >= target_dt and last_review_date != now.strftime("%Y-%m-%d"):
                    should_schedule = False
                    if not same_day_trigger:
                        should_schedule = True
                    elif last_run_status == "updated":
                        should_schedule = False
                    elif last_run_status in {"scheduled"} or last_run_status.startswith("error:"):
                        should_schedule = bool(last_triggered_at and now - last_triggered_at >= retry_window)
                    else:
                        should_schedule = True

                    if not should_schedule:
                        await asyncio.sleep(30)
                        continue

                    review_state.update(
                        {
                            "last_triggered_at": _utc_now_iso(),
                            "last_run_status": "scheduled",
                            "last_error": "",
                        }
                    )
                    self._save_long_review_state(review_state)
                    await self.add_event(
                        "daily_long_review",
                        "Run the daily long-horizon review. Reassess active hypotheses against the last 7 days of news cases and high-horizon outcomes.",
                    )
            except Exception as e:
                print(f"Daily review monitor error: {e}", flush=True)
            await asyncio.sleep(30)

    def _validate_runtime_directional_memory_alignment(
        self,
        *,
        audit_meta: Dict[str, Any],
        next_short: Dict[str, Any],
        decision: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        precheck_payload = self._select_runtime_precheck_payload(audit_meta)
        if not isinstance(precheck_payload, dict) or not precheck_payload:
            return None

        live_position = self._extract_position_snapshot_from_payload(precheck_payload.get("position"))
        if not isinstance(live_position, dict):
            return None

        live_direction = str(live_position.get("direction", "") or "").strip().lower()
        if live_direction not in {"long", "short"}:
            return None
        if self._decision_targets_live_position_close(
            audit_meta=audit_meta,
            live_direction=live_direction,
            decision=decision,
        ):
            return None

        consistency_state = next_short.get("consistency_state", {})
        if not isinstance(consistency_state, dict):
            consistency_state = {}
        active_hypothesis = next_short.get("active_hypothesis", {})
        if not isinstance(active_hypothesis, dict):
            active_hypothesis = {}

        expected_trade_intent = f"{live_direction}_bias"
        persisted_trade_intent = str(consistency_state.get("trade_intent", "") or "").strip()
        if persisted_trade_intent != expected_trade_intent:
            return {
                "error_class": "live_position_trade_intent_mismatch",
                "why_rejected": (
                    f"Runtime precheck shows a live {live_direction} position, but "
                    f"consistency_state.trade_intent persisted as {persisted_trade_intent or '<empty>'}."
                ),
                "model_fix_hint": (
                    f"Patch /consistency_state/trade_intent to {expected_trade_intent} whenever a live "
                    f"{live_direction} position already exists, including range-plan fills."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "live_position_trade_intent_mismatch",
                "related_tool_call": "",
            }

        hypothesis_direction = str(active_hypothesis.get("direction", "") or "").strip()
        hypothesis_status = str(active_hypothesis.get("status", "") or "").strip()
        if hypothesis_direction and hypothesis_status in {"active", "blocked"} and hypothesis_direction != live_direction:
            return {
                "error_class": "live_position_hypothesis_direction_mismatch",
                "why_rejected": (
                    f"Runtime precheck shows a live {live_direction} position, but active_hypothesis.direction "
                    f"persisted as {hypothesis_direction} while status={hypothesis_status}."
                ),
                "model_fix_hint": (
                    f"Patch /active_hypothesis/direction to {live_direction} (or terminate/replace the old "
                    "hypothesis) when a live position is already carrying the opposite side."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "live_position_hypothesis_direction_mismatch",
                "related_tool_call": "",
            }

        return None

    def _decision_targets_live_position_close(
        self,
        *,
        audit_meta: Dict[str, Any],
        live_direction: str,
        decision: Optional[Dict[str, Any]] = None,
    ) -> bool:
        approved_contract = decision if isinstance(decision, dict) and decision else audit_meta.get("approved_decision_contract", {})
        if not isinstance(approved_contract, dict):
            return False

        action_intent = str(approved_contract.get("action_intent", "") or "").strip().lower()
        if action_intent == "close_position":
            return True

        for item in list(approved_contract.get("tool_intents", []) or []):
            if not isinstance(item, dict):
                continue
            if str(item.get("tool", "") or "").strip() != "close_usdt_futures_position":
                continue
            parameters = item.get("parameters", {})
            if not isinstance(parameters, dict):
                return True
            position_side = str(parameters.get("position_side", "") or "").strip().lower()
            if not position_side or position_side == live_direction:
                return True
        return False

    def _decision_is_wait_centric(
        self,
        *,
        action_intent: str,
        declared_tools: Set[str],
        decision_text: str,
    ) -> bool:
        normalized_intent = str(action_intent or "").strip().lower()
        if normalized_intent == "schedule_wait":
            return True
        non_wait_tools = {
            tool_name
            for tool_name in declared_tools
            if tool_name and tool_name not in {"set_alarm", "delete_alarm"}
        }
        if non_wait_tools:
            return False
        if normalized_intent in {"close_position", "open_position", "manage_orders", "account_config", "cleanup"}:
            return False
        return bool(
            "set_alarm" in declared_tools
            or self._decision_has_explicit_wait_condition(decision_text)
        )

    def _build_runtime_alignment_memory_ops(
        self,
        *,
        audit_meta: Dict[str, Any],
        current_short: Dict[str, Any],
        decision: Optional[Dict[str, Any]] = None,
        allow_planned_close_skip: bool = True,
        promote_live_position_status: bool = False,
        prefer_runtime_truth: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        payload = (
            self._select_runtime_truth_payload(audit_meta)
            if prefer_runtime_truth
            else self._select_runtime_precheck_payload(audit_meta)
        )
        if not isinstance(payload, dict) or not payload:
            return [], []

        live_position = self._extract_position_snapshot_from_payload(payload.get("position"))
        if not isinstance(live_position, dict):
            return [], []

        live_direction = str(live_position.get("direction", "") or "").strip().lower()
        if live_direction not in {"long", "short"}:
            return [], []
        if allow_planned_close_skip and self._decision_targets_live_position_close(
            audit_meta=audit_meta,
            live_direction=live_direction,
            decision=decision,
        ):
            return [], []

        raw_short_ops = list(decision.get("short_memory_ops", []) or []) if isinstance(decision, dict) else []
        try:
            short_ops = _normalize_memory_patch_ops(raw_short_ops, label="short_memory_ops")
            next_short = self._apply_memory_ops(current_short, short_ops) if short_ops else deepcopy(current_short)
            next_short = _sanitize_short_memory_transition(current_short, next_short, short_ops)
        except Exception:
            return [], []

        consistency_state = next_short.get("consistency_state", {}) if isinstance(next_short, dict) else {}
        active_hypothesis = next_short.get("active_hypothesis", {}) if isinstance(next_short, dict) else {}
        if not isinstance(consistency_state, dict):
            consistency_state = {}
        if not isinstance(active_hypothesis, dict):
            active_hypothesis = {}

        repair_notes: List[str] = []
        expected_trade_intent = f"{live_direction}_bias"
        persisted_trade_intent = str(consistency_state.get("trade_intent", "") or "").strip()
        if persisted_trade_intent != expected_trade_intent:
            short_ops = self._upsert_memory_patch_op(
                short_ops,
                path="/consistency_state/trade_intent",
                value=expected_trade_intent,
            )
            repair_notes.append(f"trade_intent->{expected_trade_intent}")

        persisted_entry_direction = str(consistency_state.get("entry_plan_direction", "") or "").strip().lower()
        if persisted_entry_direction != live_direction:
            short_ops = self._upsert_memory_patch_op(
                short_ops,
                path="/consistency_state/entry_plan_direction",
                value=live_direction,
            )
            repair_notes.append(f"entry_plan_direction->{live_direction}")

        hypothesis_direction = str(active_hypothesis.get("direction", "") or "").strip().lower()
        if hypothesis_direction != live_direction:
            short_ops = self._upsert_memory_patch_op(
                short_ops,
                path="/active_hypothesis/direction",
                value=live_direction,
            )
            repair_notes.append(f"active_hypothesis.direction->{live_direction}")

        hypothesis_status = str(active_hypothesis.get("status", "") or "").strip()
        if promote_live_position_status:
            close_targeted = self._decision_targets_live_position_close(
                audit_meta=audit_meta,
                live_direction=live_direction,
                decision=decision,
            )
            target_status = "blocked" if close_targeted else "active"
            if hypothesis_status != target_status:
                short_ops = self._upsert_memory_patch_op(
                    short_ops,
                    path="/active_hypothesis/status",
                    value=target_status,
                )
                repair_notes.append(f"active_hypothesis.status->{target_status}")

            intraday_mode = str(consistency_state.get("intraday_mode", "") or "").strip().lower()
            target_intraday_mode = "reduce" if close_targeted else "manage"
            if intraday_mode != target_intraday_mode:
                short_ops = self._upsert_memory_patch_op(
                    short_ops,
                    path="/consistency_state/intraday_mode",
                    value=target_intraday_mode,
                )
                repair_notes.append(f"intraday_mode->{target_intraday_mode}")

            risk_action_required = str(
                ((next_short.get("risk_state", {}) if isinstance(next_short, dict) else {}) or {}).get("risk_action_required", "")
                or ""
            ).strip().lower()
            target_risk_required = "yes" if close_targeted else "no"
            if risk_action_required != target_risk_required:
                short_ops = self._upsert_memory_patch_op(
                    short_ops,
                    path="/risk_state/risk_action_required",
                    value=target_risk_required,
                )
                repair_notes.append(f"risk_action_required->{target_risk_required}")

            risk_action_taken = str(
                ((next_short.get("risk_state", {}) if isinstance(next_short, dict) else {}) or {}).get("risk_action_taken", "")
                or ""
            ).strip().lower()
            if risk_action_taken != "none":
                short_ops = self._upsert_memory_patch_op(
                    short_ops,
                    path="/risk_state/risk_action_taken",
                    value="none",
                )
                repair_notes.append("risk_action_taken->none")

        return short_ops, repair_notes

    def _validate_memory_patch_decision(
        self,
        decision: Dict[str, Any],
        *,
        audit_meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            short_ops = list(decision.get("short_memory_ops", []) or [])
            long_ops = list(decision.get("long_memory_ops", []) or [])
            short_ops = _normalize_memory_patch_ops(short_ops, label="short_memory_ops")
            long_ops = _normalize_memory_patch_ops(long_ops, label="long_memory_ops")
            _validate_memory_ops_shape(short_ops, label="short_memory_ops")
            _validate_memory_ops_shape(long_ops, label="long_memory_ops")
            current_short = self._load_or_migrate_short_memory()
            current_long = self._load_or_migrate_long_memory()
            next_short = self._apply_memory_ops(current_short, short_ops) if short_ops else deepcopy(current_short)
            next_short = _sanitize_short_memory_transition(current_short, next_short, short_ops)
            next_long = self._apply_memory_ops(current_long, long_ops) if long_ops else deepcopy(current_long)
            self._reconcile_decision_contract_with_memory(
                decision=decision,
                current_short=current_short,
                next_short=next_short,
                short_ops=short_ops,
            )
            structured_validation_failure = self._validate_structured_state_contract(
                decision=decision,
                current_short=current_short,
                next_short=next_short,
                short_ops=short_ops,
            )
            if structured_validation_failure is not None:
                return structured_validation_failure
            runtime_alignment_failure = self._validate_runtime_directional_memory_alignment(
                audit_meta=audit_meta or {},
                next_short=next_short,
                decision=decision,
            )
            if runtime_alignment_failure is not None:
                return runtime_alignment_failure
            _validate_short_memory_schema(next_short)
            _validate_long_memory_schema(next_long)
        except Exception as exc:
            return {
                "error_class": "memory_patch_invalid",
                "why_rejected": str(exc),
                "model_fix_hint": (
                    "Return only valid JSON Patch ops (add/remove/replace) in short_memory_ops/long_memory_ops. "
                    "If path ends with '/-', op MUST be add (array append only)."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "memory_patch_invalid",
                "related_tool_call": "",
            }
        return None

    def _validate_decision_schema_fields(self, decision: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = set(DecisionOutput.model_fields.keys())
        unknown = sorted([str(key) for key in decision.keys() if str(key) not in allowed])
        if unknown:
            return {
                "error_class": "decision_schema_invalid",
                "why_rejected": f"Decision payload contains unexpected fields: {', '.join(unknown[:8])}",
                "model_fix_hint": (
                    "Return only DecisionOutput fields. Do not emit malformed keys "
                    "(for example typos like state_change_evidence>)."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "decision_schema_invalid",
                "related_tool_call": "",
            }

        enum_specs = {
            "action_intent": {"observe", "schedule_wait", "open_position", "close_position", "manage_orders", "account_config", "cleanup"},
            "hypothesis_action": {"keep", "rollover", "replace", "terminate"},
            "plan_transition": {"unchanged", "promoted", "demoted", "reframed"},
            "range_decision": {"", "start", "decline"},
            "range_decision_reason_code": {
                "",
                "directional_setup_near_trigger",
                "price_too_close_to_band_edge",
                "band_too_narrow",
                "event_risk_near",
                "range_levels_low_confidence",
            },
            "declared_entry_plan_direction": {"", "long", "short", "both", "flat"},
            "declared_hypothesis_direction": {"", "long", "short", "flat"},
            "declared_hypothesis_status": {"", "active", "verified", "invalidated", "blocked"},
        }
        for field_name, allowed_values in enum_specs.items():
            raw_value = decision.get(field_name, "")
            if raw_value is None:
                raw_value = ""
            value = str(raw_value).strip()
            if value not in allowed_values:
                return {
                    "error_class": "decision_schema_invalid",
                    "why_rejected": f"{field_name} must be one of {sorted(allowed_values)}; got {value or '<empty>'}.",
                    "model_fix_hint": f"Return a valid structured enum for {field_name}.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "decision_schema_invalid",
                    "related_tool_call": "",
                }

        tool_intents = decision.get("tool_intents", [])
        if not isinstance(tool_intents, list):
            return {
                "error_class": "decision_schema_invalid",
                "why_rejected": "tool_intents must be a list.",
                "model_fix_hint": "Return tool_intents as a list of objects like {\"tool\": \"set_alarm\", \"purpose\": \"verify trigger\"}.",
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "decision_schema_invalid",
                "related_tool_call": "",
            }
        for idx, item in enumerate(tool_intents):
            if not isinstance(item, dict):
                return {
                    "error_class": "decision_schema_invalid",
                    "why_rejected": f"tool_intents[{idx}] must be an object.",
                    "model_fix_hint": "Each tool_intents item must be an object containing at least tool and purpose.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "decision_schema_invalid",
                    "related_tool_call": "",
                }
        return None

    def _decision_contract_value_is_empty(self, raw_value: Any) -> bool:
        if raw_value is None:
            return True
        if isinstance(raw_value, list):
            return len(_normalize_checklist_items(raw_value)) == 0
        return not str(raw_value).strip()

    def _structured_state_raw_value(self, payload: Dict[str, Any], dotted_path: str) -> Any:
        current: Any = payload
        for token in dotted_path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(token)
        return current

    def _structured_state_value(self, payload: Dict[str, Any], dotted_path: str) -> str:
        return _format_structured_contract_value(self._structured_state_raw_value(payload, dotted_path))

    def _structured_state_set_value(self, payload: Dict[str, Any], dotted_path: str, value: Any) -> None:
        if not isinstance(payload, dict):
            return
        current: Dict[str, Any] = payload
        tokens = dotted_path.split(".")
        for token in tokens[:-1]:
            child = current.get(token)
            if not isinstance(child, dict):
                child = {}
                current[token] = child
            current = child
        current[tokens[-1]] = deepcopy(value)

    def _json_pointer_touched(self, touched_paths: Set[str], path: str) -> bool:
        return path in touched_paths

    def _parse_memory_datetime(self, raw_value: Any) -> Optional[datetime]:
        value = str(raw_value or "").strip()
        if not value or value.lower() == "none":
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _format_deadline_drift(self, raw_value: Any, *, now: datetime) -> str:
        value = str(raw_value or "").strip()
        if not value or value.lower() == "none":
            return "none"
        parsed = self._parse_memory_datetime(value)
        if parsed is None:
            return f"{value} (unparsed)"
        drift_seconds = int(round((parsed - now).total_seconds()))
        if drift_seconds == 0:
            return f"{value} (now)"
        if drift_seconds > 0:
            return f"{value} (in {drift_seconds}s)"
        return f"{value} ({abs(drift_seconds)}s ago)"

    def _read_live_breakout_watch_state(self, *, refresh_if_stale: bool = False) -> Dict[str, Any]:
        plan = read_range_plan_state()
        if not isinstance(plan, dict):
            return {}
        if str(plan.get("status", "") or "").strip().lower() != "active":
            return {}
        breakout_watch = plan.get("breakout_watch", {})
        if not isinstance(breakout_watch, dict):
            return {}
        deadline_raw = str(breakout_watch.get("confirm_deadline_at", "") or "").strip()
        deadline_dt = self._parse_memory_datetime(deadline_raw)
        if (
            refresh_if_stale
            and str(breakout_watch.get("status", "") or "").strip().lower() == "pending"
            and isinstance(deadline_dt, datetime)
            and deadline_dt <= datetime.now()
        ):
            try:
                maintain_range_plan()
            except Exception:
                pass
            plan = read_range_plan_state()
            if not isinstance(plan, dict):
                return {}
            if str(plan.get("status", "") or "").strip().lower() != "active":
                return {}
            breakout_watch = plan.get("breakout_watch", {})
            if not isinstance(breakout_watch, dict):
                return {}
            deadline_raw = str(breakout_watch.get("confirm_deadline_at", "") or "").strip()
            deadline_dt = self._parse_memory_datetime(deadline_raw)
        return {
            "symbol": str(plan.get("symbol", "") or "").strip().upper(),
            "plan_status": str(plan.get("status", "") or "").strip().lower(),
            "watch_status": str(breakout_watch.get("status", "") or "").strip().lower(),
            "watch_side": str(breakout_watch.get("side", "") or "").strip().lower(),
            "confirm_deadline_at": deadline_raw,
            "confirm_deadline_dt": deadline_dt,
        }

    def _build_retry_deadline_audit_section(
        self,
        *,
        range_plan_result: Optional[Dict[str, Any]] = None,
    ) -> str:
        now = datetime.now()
        current_short = self._load_or_migrate_short_memory()
        active_hypothesis = current_short.get("active_hypothesis", {}) if isinstance(current_short.get("active_hypothesis", {}), dict) else {}
        day_plan = current_short.get("day_plan", {}) if isinstance(current_short.get("day_plan", {}), dict) else {}

        lines = [
            "[RETRY DEADLINE AUDIT]",
            f"- refreshed_now: {now.strftime('%Y-%m-%d %H:%M:%S')}",
            (
                "- current_short_memory: "
                f"hypothesis_expiry={self._format_deadline_drift(active_hypothesis.get('expiry'), now=now)} | "
                f"hold_until={self._format_deadline_drift(day_plan.get('hold_until'), now=now)} | "
                f"recheck_at={self._format_deadline_drift(day_plan.get('recheck_at'), now=now)}"
            ),
        ]

        plan_payload = range_plan_result if isinstance(range_plan_result, dict) else {}
        plan = plan_payload.get("plan") if isinstance(plan_payload.get("plan"), dict) else {}
        if not plan and isinstance(plan_payload, dict):
            plan = plan_payload
        if isinstance(plan, dict):
            breakout_watch = plan.get("breakout_watch", {})
            if isinstance(breakout_watch, dict):
                confirm_deadline_raw = str(breakout_watch.get("confirm_deadline_at", "") or "").strip()
                confirm_deadline_dt = self._parse_memory_datetime(confirm_deadline_raw)
                status = str(breakout_watch.get("status", "") or "").strip().lower()
                side = str(breakout_watch.get("side", "") or "").strip().lower()
                lines.append(
                    "- live_range_breakout_watch: "
                    f"status={status or 'none'} | side={side or 'none'} | "
                    f"confirm_deadline_at={self._format_deadline_drift(confirm_deadline_raw, now=now)}"
                )
                if status == "pending" and confirm_deadline_dt is not None and confirm_deadline_dt <= now:
                    lines.append(
                        "- drift_warning: The original breakout_watch confirmation deadline is already in the past. "
                        "Do not keep quoting it as a future wait target; refresh the outcome or create a genuinely future recheck window."
                    )
        return "\n".join(lines)

    def _upsert_memory_patch_op(
        self,
        ops: List[Dict[str, Any]],
        *,
        path: str,
        value: Any,
    ) -> List[Dict[str, Any]]:
        updated: List[Dict[str, Any]] = []
        replaced = False
        for item in ops:
            if not isinstance(item, dict):
                continue
            item_path = str(item.get("path", "")).strip()
            if item_path != path:
                updated.append(dict(item))
                continue
            patched = dict(item)
            patched["op"] = "replace"
            patched["path"] = path
            patched["value"] = value
            updated.append(patched)
            replaced = True
        if not replaced:
            updated.append({"op": "replace", "path": path, "value": value})
        return updated

    def _apply_deterministic_runtime_alignment_repairs(
        self,
        decision: Dict[str, Any],
        *,
        audit_meta: Optional[Dict[str, Any]] = None,
        allow_planned_close_skip: bool = True,
        promote_live_position_status: bool = False,
        prefer_runtime_truth: bool = False,
    ) -> Dict[str, Any]:
        if not isinstance(decision, dict):
            return decision

        audit_payload = audit_meta if isinstance(audit_meta, dict) else {}
        current_short = self._load_or_migrate_short_memory()
        short_ops, repair_notes = self._build_runtime_alignment_memory_ops(
            audit_meta=audit_payload,
            current_short=current_short,
            decision=decision,
            allow_planned_close_skip=allow_planned_close_skip,
            promote_live_position_status=promote_live_position_status,
            prefer_runtime_truth=prefer_runtime_truth,
        )

        if not repair_notes:
            return decision

        decision["short_memory_ops"] = short_ops
        try:
            repaired_next_short = self._apply_memory_ops(current_short, short_ops) if short_ops else deepcopy(current_short)
            repaired_next_short = _sanitize_short_memory_transition(current_short, repaired_next_short, short_ops)
        except Exception:
            return decision
        self._reconcile_decision_contract_with_memory(
            decision=decision,
            current_short=current_short,
            next_short=repaired_next_short,
            short_ops=short_ops,
        )
        if isinstance(audit_payload, dict):
            existing_notes = list(audit_payload.get("deterministic_guard_repairs", []))
            for note in repair_notes:
                if note not in existing_notes:
                    existing_notes.append(note)
            audit_payload["deterministic_guard_repairs"] = existing_notes
        return decision

    def _prepare_guard_blocked_memory_updates(
        self,
        decision: Dict[str, Any],
        audit_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        current_short = self._load_or_migrate_short_memory()
        current_long = self._load_or_migrate_long_memory()
        decision_copy = deepcopy(decision) if isinstance(decision, dict) else {}
        terminal_failure = dict(audit_meta.get("guard_failure", {}))
        terminal_error_class = str(terminal_failure.get("error_class", "") or "").strip()
        allowed_decision_paths = self._guard_blocked_decision_repair_paths(terminal_error_class)

        try:
            decision_short_ops = _normalize_memory_patch_ops(
                list(decision_copy.get("short_memory_ops", []) or []),
                label="short_memory_ops",
            )
        except Exception:
            decision_short_ops = []

        filtered_short_ops = [
            dict(item)
            for item in decision_short_ops
            if isinstance(item, dict)
            and str(item.get("path", "")).strip() in allowed_decision_paths
        ]
        runtime_truth_ops, _runtime_truth_notes = self._build_runtime_alignment_memory_ops(
            audit_meta=audit_meta,
            current_short=current_short,
            decision=decision_copy,
            allow_planned_close_skip=False,
            promote_live_position_status=True,
            prefer_runtime_truth=True,
        )
        merged_short_ops = list(filtered_short_ops)
        for item in runtime_truth_ops:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            value = item.get("value")
            merged_short_ops = self._upsert_memory_patch_op(merged_short_ops, path=path, value=value)

        if not merged_short_ops:
            return {
                "short": current_short,
                "long": current_long,
                "short_ops": [],
                "long_ops": [],
                "reason": "guards_blocked_no_repairable_memory_ops",
            }

        try:
            next_short = self._apply_memory_ops(current_short, merged_short_ops)
            next_short = _sanitize_short_memory_transition(current_short, next_short, merged_short_ops)
            next_short.setdefault("meta", {})
            next_short["meta"]["schema_version"] = MEMORY_SCHEMA_VERSION
            next_short["meta"]["updated_at"] = _utc_now_iso()
            next_short["meta"]["source"] = "guard_blocked_repair_only"

            _validate_short_memory_schema(next_short)
            _validate_long_memory_schema(current_long)
        except Exception:
            return {
                "short": current_short,
                "long": current_long,
                "short_ops": [],
                "long_ops": [],
                "reason": "guards_blocked_repair_failed",
            }

        return {
            "short": next_short,
            "long": current_long,
            "short_ops": merged_short_ops,
            "long_ops": [],
            "reason": "guards_blocked_repair_only",
        }

    def _reconcile_decision_contract_with_memory(
        self,
        *,
        decision: Dict[str, Any],
        current_short: Dict[str, Any],
        next_short: Dict[str, Any],
        short_ops: List[Dict[str, Any]],
    ) -> None:
        self._reconcile_action_intent_with_tool_intents(decision)
        touched_paths = {
            str(item.get("path", "")).strip()
            for item in short_ops
            if isinstance(item, dict)
        }

        def persist_short_value(patch_path: str, dotted_path: str, value: Any) -> None:
            nonlocal touched_paths
            short_ops[:] = self._upsert_memory_patch_op(short_ops, path=patch_path, value=value)
            touched_paths = {
                str(item.get("path", "")).strip()
                for item in short_ops
                if isinstance(item, dict)
            }
            self._structured_state_set_value(next_short, dotted_path, value)

        declared_checks = [
            ("declared_entry_plan_direction", "consistency_state.entry_plan_direction", "/consistency_state/entry_plan_direction"),
            ("declared_hypothesis_id", "active_hypothesis.hypothesis_id", "/active_hypothesis/hypothesis_id"),
            ("declared_hypothesis_direction", "active_hypothesis.direction", "/active_hypothesis/direction"),
            ("declared_hypothesis_status", "active_hypothesis.status", "/active_hypothesis/status"),
            ("declared_hypothesis_expiry", "active_hypothesis.expiry", "/active_hypothesis/expiry"),
            ("declared_reversal_checklist", "risk_state.reversal_checklist", "/risk_state/reversal_checklist"),
            ("declared_state_change_evidence", "risk_state.state_change_evidence", "/risk_state/state_change_evidence"),
        ]
        for declared_field, dotted_path, patch_path in declared_checks:
            raw_declared = decision.get(declared_field, "")
            if self._decision_contract_value_is_empty(raw_declared):
                continue
            previous_value = self._structured_state_raw_value(current_short, dotted_path)
            if (
                not _structured_contract_values_match(dotted_path, previous_value, raw_declared)
                and not self._json_pointer_touched(touched_paths, patch_path)
            ):
                persist_short_value(patch_path, dotted_path, raw_declared)
            next_value = self._structured_state_raw_value(next_short, dotted_path)
            if dotted_path == "risk_state.reversal_checklist" and isinstance(next_value, list):
                decision[declared_field] = deepcopy(next_value)
            else:
                decision[declared_field] = _format_structured_contract_value(next_value)

        plan_transition = str(decision.get("plan_transition", "") or "").strip()
        evidence_text = str(
            decision.get("declared_state_change_evidence", "")
            or self._structured_state_value(next_short, "risk_state.state_change_evidence")
            or decision.get("state_change_evidence", "")
            or ""
        ).strip()
        if plan_transition in {"promoted", "demoted", "reframed"} and evidence_text:
            if decision.get("declared_state_change_evidence", "") != evidence_text:
                decision["declared_state_change_evidence"] = evidence_text
            if not self._json_pointer_touched(touched_paths, "/risk_state/state_change_evidence"):
                persist_short_value(
                    "/risk_state/state_change_evidence",
                    "risk_state.state_change_evidence",
                    evidence_text,
                )

        previous_hypothesis = current_short.get("active_hypothesis", {}) if isinstance(current_short.get("active_hypothesis", {}), dict) else {}
        next_hypothesis = next_short.get("active_hypothesis", {}) if isinstance(next_short.get("active_hypothesis", {}), dict) else {}
        previous_id = str(previous_hypothesis.get("hypothesis_id", "") or "").strip()
        next_id = str(next_hypothesis.get("hypothesis_id", "") or "").strip()
        previous_expiry = str(previous_hypothesis.get("expiry", "") or "").strip()
        next_expiry = str(next_hypothesis.get("expiry", "") or "").strip()
        next_status = str(next_hypothesis.get("status", "") or "").strip()
        action = str(decision.get("hypothesis_action", "") or "").strip()
        reason = str(decision.get("hypothesis_action_reason", "") or "").strip()

        if previous_id and next_id and previous_id != next_id and action != "replace":
            decision["hypothesis_action"] = "replace"
            if not reason:
                decision["hypothesis_action_reason"] = "The short-memory snapshot switched to a new hypothesis_id in this wakeup."
            return

        if (
            previous_id
            and next_id == previous_id
            and next_status in {"verified", "invalidated"}
            and action != "terminate"
        ):
            decision["hypothesis_action"] = "terminate"
            if not reason:
                decision["hypothesis_action_reason"] = "The existing hypothesis reached a terminal state in this wakeup."
            if next_expiry:
                persist_short_value("/active_hypothesis/expiry", "active_hypothesis.expiry", "")
            return

        if (
            previous_id
            and next_id == previous_id
            and previous_expiry
            and next_expiry
            and previous_expiry == next_expiry
            and action == "rollover"
        ):
            decision["hypothesis_action"] = "keep"
            if not reason:
                decision["hypothesis_action_reason"] = (
                    "The same hypothesis remains active with unchanged expiry; no rollover is needed."
                )
            return

        if (
            previous_id
            and next_id == previous_id
            and previous_expiry
            and next_expiry
            and previous_expiry != next_expiry
            and next_status in {"active", "blocked"}
            and action != "rollover"
        ):
            decision["hypothesis_action"] = "rollover"
            if not reason:
                decision["hypothesis_action_reason"] = "The same hypothesis remains active with a refreshed expiry in this wakeup."

        plan_transition = str(decision.get("plan_transition", "") or "").strip()
        evidence_text = str(
            decision.get("declared_state_change_evidence", "")
            or self._structured_state_value(next_short, "risk_state.state_change_evidence")
            or decision.get("state_change_evidence", "")
            or ""
        ).strip()
        if plan_transition in {"promoted", "demoted", "reframed"} and evidence_text:
            if decision.get("declared_state_change_evidence", "") != evidence_text:
                decision["declared_state_change_evidence"] = evidence_text
            if not self._json_pointer_touched(touched_paths, "/risk_state/state_change_evidence"):
                persist_short_value(
                    "/risk_state/state_change_evidence",
                    "risk_state.state_change_evidence",
                    evidence_text,
                )

    def _reconcile_action_intent_with_tool_intents(self, decision: Dict[str, Any]) -> None:
        tool_intents = decision.get("tool_intents", [])
        if not isinstance(tool_intents, list) or not tool_intents:
            return

        tool_names = {
            str(item.get("tool", "") or "").strip()
            for item in tool_intents
            if isinstance(item, dict) and str(item.get("tool", "") or "").strip()
        }
        if not tool_names:
            return

        inferred_action = ""
        alarm_only_tools = {"set_alarm", "delete_alarm"}
        account_config_tools = {
            "transfer_to_usdt_futures",
            "set_usdt_futures_leverage",
            "set_usdt_futures_margin_type",
        }
        close_flow_tools = {
            "close_usdt_futures_position",
            "cancel_range_plan",
            "set_alarm",
            "delete_alarm",
            "cancel_usdt_futures_order",
            "cancel_all_usdt_futures_orders",
        }
        manage_order_tools = {
            "set_range_plan",
            "cancel_range_plan",
            "modify_usdt_futures_order",
            "cancel_usdt_futures_order",
            "cancel_all_usdt_futures_orders",
            "set_alarm",
            "delete_alarm",
        }

        if tool_names.issubset(alarm_only_tools):
            inferred_action = "schedule_wait"
        elif tool_names & account_config_tools:
            inferred_action = "account_config"
        elif "close_usdt_futures_position" in tool_names and tool_names.issubset(close_flow_tools):
            inferred_action = "close_position"
        elif tool_names & manage_order_tools:
            inferred_action = "manage_orders"

        current_action = str(decision.get("action_intent", "") or "").strip()
        if not inferred_action or current_action == inferred_action:
            return
        if current_action in {"", "observe", "schedule_wait"}:
            decision["action_intent"] = inferred_action

    def _validate_declared_snapshot_field(
        self,
        *,
        decision: Dict[str, Any],
        current_short: Dict[str, Any],
        next_short: Dict[str, Any],
        touched_paths: Set[str],
        declared_field: str,
        dotted_path: str,
        patch_path: str,
        error_class: str,
        model_fix_hint: str,
    ) -> Optional[Dict[str, Any]]:
        declared_value = decision.get(declared_field, "")
        if self._decision_contract_value_is_empty(declared_value):
            return None
        next_value = self._structured_state_raw_value(next_short, dotted_path)
        if not _structured_contract_values_match(dotted_path, next_value, declared_value):
            return {
                "error_class": error_class,
                "why_rejected": (
                    f"{declared_field}={_format_structured_contract_value(declared_value)} was declared, "
                    f"but {dotted_path} persisted as {_format_structured_contract_value(next_value) or '<empty>'}."
                ),
                "model_fix_hint": model_fix_hint,
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": error_class,
                "related_tool_call": "",
            }
        previous_value = self._structured_state_raw_value(current_short, dotted_path)
        if (
            not _structured_contract_values_match(dotted_path, previous_value, declared_value)
            and not self._json_pointer_touched(touched_paths, patch_path)
        ):
            return {
                "error_class": error_class,
                "why_rejected": (
                    f"{declared_field} changed from {_format_structured_contract_value(previous_value) or '<empty>'} "
                    f"to {_format_structured_contract_value(declared_value)}, but short_memory_ops did not patch {patch_path}."
                ),
                "model_fix_hint": model_fix_hint,
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": error_class,
                "related_tool_call": "",
            }
        return None

    def _validate_wait_deadline_freshness(
        self,
        *,
        decision: Dict[str, Any],
        next_short: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        now = datetime.now()
        action_intent = str(decision.get("action_intent", "") or "").strip()
        decision_text = self._decision_alarm_semantics_text(decision)
        tool_intents = decision.get("tool_intents", [])
        declared_tools = {
            str(item.get("tool", "") or "").strip()
            for item in tool_intents
            if isinstance(item, dict) and str(item.get("tool", "") or "").strip()
        }
        wait_mode = self._decision_is_wait_centric(
            action_intent=action_intent,
            declared_tools=declared_tools,
            decision_text=decision_text,
        )

        active_hypothesis = next_short.get("active_hypothesis", {}) if isinstance(next_short.get("active_hypothesis", {}), dict) else {}
        next_status = str(active_hypothesis.get("status", "") or "").strip()
        next_expiry_raw = str(active_hypothesis.get("expiry", "") or "").strip()
        next_expiry = self._parse_memory_datetime(next_expiry_raw)
        if next_status in {"active", "blocked"} and next_expiry_raw and next_expiry is not None and next_expiry <= now:
            return {
                "error_class": "active_hypothesis_expiry_stale",
                "why_rejected": (
                    "The final short-memory snapshot keeps an active/blocked hypothesis with a non-future expiry. "
                    f"status={next_status}; expiry={next_expiry_raw}; now={now.strftime('%Y-%m-%d %H:%M:%S')}."
                ),
                "model_fix_hint": (
                    "If the old validation window already expired during this wakeup, do not preserve that stale expiry. "
                    "Either conclude/terminate/replace the old watch based on refreshed live facts, or write a genuinely future expiry later than now."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "active_hypothesis_expiry_stale",
                "related_tool_call": "",
            }

        if not wait_mode:
            return None

        day_plan = next_short.get("day_plan", {}) if isinstance(next_short.get("day_plan", {}), dict) else {}
        stale_wait_fields: List[str] = []
        for label, raw_value in (
            ("day_plan.hold_until", day_plan.get("hold_until")),
            ("day_plan.recheck_at", day_plan.get("recheck_at")),
        ):
            raw_text = str(raw_value or "").strip()
            parsed = self._parse_memory_datetime(raw_text)
            if raw_text and parsed is not None and parsed <= now:
                stale_wait_fields.append(f"{label}={raw_text}")
        if stale_wait_fields:
            return {
                "error_class": "wait_deadline_stale",
                "why_rejected": (
                    "The decision is still in wait/recheck mode, but one or more structured wait deadlines are already in the past at execution time. "
                    f"{'; '.join(stale_wait_fields)}; now={now.strftime('%Y-%m-%d %H:%M:%S')}."
                ),
                "model_fix_hint": (
                    "Do not carry over expired hold_until/recheck_at values into a new wait decision. "
                    "Refresh the live state and either conclude the old watch now or write a genuinely future recheck window."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "wait_deadline_stale",
                "related_tool_call": "",
            }

        live_breakout_watch = self._read_live_breakout_watch_state(refresh_if_stale=True)
        breakout_deadline = live_breakout_watch.get("confirm_deadline_dt")
        if (
            live_breakout_watch.get("watch_status") == "pending"
            and isinstance(breakout_deadline, datetime)
            and breakout_deadline <= now
        ):
            return {
                "error_class": "breakout_watch_deadline_stale",
                "why_rejected": (
                    "A live range-plan breakout_watch is still being referenced by a wait decision, but its original confirmation deadline is already past. "
                    f"symbol={live_breakout_watch.get('symbol', 'unknown') or 'unknown'}; "
                    f"confirm_deadline_at={live_breakout_watch.get('confirm_deadline_at', '') or '<empty>'}; "
                    f"now={now.strftime('%Y-%m-%d %H:%M:%S')}."
                ),
                "model_fix_hint": (
                    "The original breakout_watch confirmation window already elapsed during this wakeup. "
                    "Refresh live range-plan facts and either conclude the breakout result now or create a new future recheck window; "
                    "do not keep quoting the old confirm_deadline/hold_until/recheck_at."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "breakout_watch_deadline_stale",
                "related_tool_call": "",
            }
        return None

    def _validate_structured_state_contract(
        self,
        *,
        decision: Dict[str, Any],
        current_short: Dict[str, Any],
        next_short: Dict[str, Any],
        short_ops: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        touched_paths = {
            str(item.get("path", "")).strip()
            for item in short_ops
            if isinstance(item, dict)
        }
        previous_hypothesis = current_short.get("active_hypothesis", {}) if isinstance(current_short.get("active_hypothesis", {}), dict) else {}
        next_hypothesis = next_short.get("active_hypothesis", {}) if isinstance(next_short.get("active_hypothesis", {}), dict) else {}
        next_hypothesis_status = str(next_hypothesis.get("status", "") or "").strip()
        hypothesis_action = str(decision.get("hypothesis_action", "") or "").strip()

        declared_checks = [
            (
                "declared_entry_plan_direction",
                "consistency_state.entry_plan_direction",
                "/consistency_state/entry_plan_direction",
                "declared_plan_not_persisted",
                "If you declare a new entry plan direction, patch /consistency_state/entry_plan_direction and ensure the final short-memory snapshot matches it.",
            ),
            (
                "declared_hypothesis_id",
                "active_hypothesis.hypothesis_id",
                "/active_hypothesis/hypothesis_id",
                "declared_hypothesis_not_persisted",
                "If you declare a new hypothesis id, patch /active_hypothesis/hypothesis_id and keep the final snapshot aligned.",
            ),
            (
                "declared_hypothesis_direction",
                "active_hypothesis.direction",
                "/active_hypothesis/direction",
                "declared_hypothesis_not_persisted",
                "If you declare a hypothesis direction, patch /active_hypothesis/direction and keep the final snapshot aligned.",
            ),
            (
                "declared_hypothesis_status",
                "active_hypothesis.status",
                "/active_hypothesis/status",
                "declared_hypothesis_not_persisted",
                "If you declare a hypothesis status, patch /active_hypothesis/status and keep the final snapshot aligned.",
            ),
            (
                "declared_hypothesis_expiry",
                "active_hypothesis.expiry",
                "/active_hypothesis/expiry",
                "declared_hypothesis_expiry_not_persisted",
                "If you declare a hypothesis expiry, patch /active_hypothesis/expiry and keep the final snapshot aligned.",
            ),
            (
                "declared_reversal_checklist",
                "risk_state.reversal_checklist",
                "/risk_state/reversal_checklist",
                "declared_trigger_window_not_persisted",
                "If you declare a trigger/reversal window, patch /risk_state/reversal_checklist and keep the final snapshot aligned.",
            ),
            (
                "declared_state_change_evidence",
                "risk_state.state_change_evidence",
                "/risk_state/state_change_evidence",
                "declared_state_change_not_persisted",
                "If you declare state-change evidence, patch /risk_state/state_change_evidence and keep the final snapshot aligned.",
            ),
        ]
        for declared_field, dotted_path, patch_path, error_class, model_fix_hint in declared_checks:
            if (
                declared_field == "declared_hypothesis_expiry"
                and (hypothesis_action == "terminate" or next_hypothesis_status in {"verified", "invalidated"})
            ):
                continue
            failure = self._validate_declared_snapshot_field(
                decision=decision,
                current_short=current_short,
                next_short=next_short,
                touched_paths=touched_paths,
                declared_field=declared_field,
                dotted_path=dotted_path,
                patch_path=patch_path,
                error_class=error_class,
                model_fix_hint=model_fix_hint,
            )
            if failure is not None:
                return failure

        plan_transition = str(decision.get("plan_transition", "") or "").strip()
        hypothesis_action_reason = str(decision.get("hypothesis_action_reason", "") or "").strip()
        evidence_text = str(
            decision.get("declared_state_change_evidence", "")
            or self._structured_state_value(next_short, "risk_state.state_change_evidence")
            or decision.get("state_change_evidence", "")
            or ""
        ).strip()

        if plan_transition in {"promoted", "demoted", "reframed"}:
            if not evidence_text:
                return {
                    "error_class": "plan_transition_missing_evidence",
                    "why_rejected": f"plan_transition={plan_transition} requires structured state-change evidence.",
                    "model_fix_hint": "Write the changed evidence into declared_state_change_evidence and patch /risk_state/state_change_evidence in short_memory_ops.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "plan_transition_missing_evidence",
                    "related_tool_call": "",
                }
            if not self._json_pointer_touched(touched_paths, "/risk_state/state_change_evidence"):
                return {
                    "error_class": "declared_state_change_not_persisted",
                    "why_rejected": f"plan_transition={plan_transition} was declared, but short_memory_ops did not patch /risk_state/state_change_evidence.",
                    "model_fix_hint": "When plan_transition changes, persist the new evidence to /risk_state/state_change_evidence.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "declared_state_change_not_persisted",
                    "related_tool_call": "",
                }

        previous_id = str(previous_hypothesis.get("hypothesis_id", "") or "").strip()
        next_id = str(next_hypothesis.get("hypothesis_id", "") or "").strip()
        previous_expiry = self._parse_memory_datetime(previous_hypothesis.get("expiry"))
        next_expiry = self._parse_memory_datetime(next_hypothesis.get("expiry"))
        now = datetime.now()
        continuing_same_hypothesis = bool(previous_id and previous_id == next_id)
        hypothesis_replaced = bool(previous_id and next_id and previous_id != next_id)
        terminal_same_hypothesis = bool(
            continuing_same_hypothesis
            and (
                hypothesis_action == "terminate"
                or next_hypothesis_status in {"verified", "invalidated"}
            )
        )
        expiry_changed_same_hypothesis = bool(
            continuing_same_hypothesis
            and str(previous_hypothesis.get("expiry", "") or "").strip() != str(next_hypothesis.get("expiry", "") or "").strip()
        )
        expired_active_hypothesis = bool(
            continuing_same_hypothesis
            and previous_expiry is not None
            and previous_expiry < now
            and str(next_hypothesis.get("status", "") or "").strip() in {"active", "blocked"}
        )

        if hypothesis_replaced and hypothesis_action != "replace":
            return {
                "error_class": "hypothesis_replace_action_missing",
                "why_rejected": "The hypothesis_id changed, but hypothesis_action was not set to replace.",
                "model_fix_hint": "When switching to a new hypothesis_id, declare hypothesis_action=replace and explain why the old hypothesis was superseded.",
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "hypothesis_replace_action_missing",
                "related_tool_call": "",
            }

        if expiry_changed_same_hypothesis and not terminal_same_hypothesis and hypothesis_action != "rollover":
            return {
                "error_class": "hypothesis_rollover_action_missing",
                "why_rejected": "The same hypothesis_id received a new expiry, but hypothesis_action was not set to rollover.",
                "model_fix_hint": "When extending the same hypothesis forward in time, declare hypothesis_action=rollover and provide a reason.",
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "hypothesis_rollover_action_missing",
                "related_tool_call": "",
            }

        if expired_active_hypothesis and not terminal_same_hypothesis and hypothesis_action != "rollover":
            return {
                "error_class": "expired_hypothesis_not_rolled",
                "why_rejected": "The active hypothesis already expired, but the decision neither rolled it forward nor replaced/terminated it.",
                "model_fix_hint": "After expiry, either set hypothesis_action=rollover with a new expiry and reason, replace the hypothesis with a new id, or terminate it.",
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "expired_hypothesis_not_rolled",
                "related_tool_call": "",
            }

        if hypothesis_action == "rollover":
            if not hypothesis_action_reason:
                return {
                    "error_class": "hypothesis_rollover_reason_missing",
                    "why_rejected": "hypothesis_action=rollover requires hypothesis_action_reason.",
                    "model_fix_hint": "Explain why the same hypothesis is still valid and what new evidence justifies extending it.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_rollover_reason_missing",
                    "related_tool_call": "",
                }
            if not continuing_same_hypothesis:
                return {
                    "error_class": "hypothesis_rollover_id_mismatch",
                    "why_rejected": "hypothesis_action=rollover can only be used when continuing the same hypothesis_id.",
                    "model_fix_hint": "Use replace for a new hypothesis_id, or keep the same id when extending the current hypothesis.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_rollover_id_mismatch",
                    "related_tool_call": "",
                }
            if not self._json_pointer_touched(touched_paths, "/active_hypothesis/expiry"):
                return {
                    "error_class": "hypothesis_rollover_missing_expiry_patch",
                    "why_rejected": "hypothesis_action=rollover was declared, but short_memory_ops did not patch /active_hypothesis/expiry.",
                    "model_fix_hint": "Rollovers must write a fresh expiry into /active_hypothesis/expiry.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_rollover_missing_expiry_patch",
                    "related_tool_call": "",
                }
            if next_expiry is None or (previous_expiry is not None and next_expiry <= previous_expiry):
                previous_expiry_text = str(previous_hypothesis.get("expiry", "") or "").strip() or "<empty>"
                next_expiry_text = str(next_hypothesis.get("expiry", "") or "").strip() or "<empty>"
                now_text = now.strftime("%Y-%m-%d %H:%M:%S")
                return {
                    "error_class": "hypothesis_rollover_expiry_invalid",
                    "why_rejected": (
                        "hypothesis_action=rollover requires a fresh future expiry later than the previous expiry. "
                        f"previous_expiry={previous_expiry_text}; patched_expiry={next_expiry_text}; now={now_text}."
                    ),
                    "model_fix_hint": (
                        "继续同一个 hypothesis_id 时，必须在 short_memory_ops 中 replace /active_hypothesis/expiry "
                        "为晚于 previous_expiry 且晚于 now 的 YYYY-MM-DD HH:MM:SS，并让 declared_hypothesis_expiry 完全等于该值。"
                    ),
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_rollover_expiry_invalid",
                    "related_tool_call": "",
                }
            if not evidence_text and not hypothesis_action_reason:
                return {
                    "error_class": "hypothesis_rollover_evidence_missing",
                    "why_rejected": "hypothesis_action=rollover requires state_change_evidence or hypothesis_action_reason.",
                    "model_fix_hint": "Persist new auditable evidence in /risk_state/state_change_evidence when continuing the same hypothesis.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_rollover_evidence_missing",
                    "related_tool_call": "",
                }

        if hypothesis_action == "replace":
            if not hypothesis_action_reason:
                return {
                    "error_class": "hypothesis_replace_reason_missing",
                    "why_rejected": "hypothesis_action=replace requires hypothesis_action_reason.",
                    "model_fix_hint": "Explain what invalidated or superseded the prior hypothesis before replacing it.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_replace_reason_missing",
                    "related_tool_call": "",
                }
            if not next_id or next_id == previous_id:
                return {
                    "error_class": "hypothesis_replace_id_missing",
                    "why_rejected": "hypothesis_action=replace requires a new non-empty hypothesis_id.",
                    "model_fix_hint": "Patch /active_hypothesis/hypothesis_id to a fresh id when replacing the old hypothesis.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_replace_id_missing",
                    "related_tool_call": "",
                }
            if not self._json_pointer_touched(touched_paths, "/active_hypothesis/hypothesis_id"):
                return {
                    "error_class": "hypothesis_replace_patch_missing",
                    "why_rejected": "hypothesis_action=replace was declared, but short_memory_ops did not patch /active_hypothesis/hypothesis_id.",
                    "model_fix_hint": "When replacing a hypothesis, persist the new id and associated fields through short_memory_ops.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "hypothesis_replace_patch_missing",
                    "related_tool_call": "",
                }

        wait_deadline_failure = self._validate_wait_deadline_freshness(
            decision=decision,
            next_short=next_short,
        )
        if wait_deadline_failure is not None:
            return wait_deadline_failure

        return None

    def _apply_json_patch_ops(self, base_document: Dict[str, Any], ops: List[Dict[str, Any]]) -> Dict[str, Any]:
        updated = deepcopy(base_document)
        for op in ops:
            updated = _apply_single_json_patch(updated, op)
        return updated

    def _apply_memory_ops(self, base_memory: Dict[str, Any], ops: List[Dict[str, Any]]) -> Dict[str, Any]:
        hydrated_base = _hydrate_memory_document_for_patch_ops(base_memory, ops)
        return self._apply_json_patch_ops(hydrated_base, ops)

    def _approved_execute_runtime_tool_names(
        self,
        approved_contract: Dict[str, Any],
    ) -> List[str]:
        allowed = set(EXECUTE_RUNTIME_READONLY_TOOL_NAMES)
        for item in list(approved_contract.get("tool_intents", []) or []):
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("tool", "") or "").strip()
            if tool_name in EXECUTE_TOOL_NAMES:
                allowed.add(tool_name)
        return [tool_name for tool_name in EXECUTE_TOOL_NAMES if tool_name in allowed]

    def _get_execute_agent_pair_for_tool_names(
        self,
        tool_names: List[str],
    ) -> Tuple[Any, Any]:
        cache_key = tuple(tool_names)
        execute_agent = self.execute_agent_cache.get(cache_key)
        execute_agent_compat = self.execute_agent_compat_cache.get(cache_key)
        if execute_agent is not None and execute_agent_compat is not None:
            return execute_agent, execute_agent_compat

        tools = get_langchain_tools(tool_names)
        prompt_suffix = ""
        if cache_key != tuple(EXECUTE_TOOL_NAMES):
            prompt_suffix = (
                " Only the approved runtime tool subset for this wakeup is available. "
                "Do not improvise undeclared side-effect tools; revise the contract first if execution requires them."
            )
        execute_agent = create_agent(
            model=self._new_chat_model(),
            tools=tools,
            response_format=DecisionOutput,
            system_prompt=self.execute_system_prompt + prompt_suffix,
        )
        execute_agent_compat = create_agent(
            model=self._new_chat_model(),
            tools=tools,
            system_prompt=self.execute_compat_system_prompt + prompt_suffix,
        )
        self.execute_agent_cache[cache_key] = execute_agent
        self.execute_agent_compat_cache[cache_key] = execute_agent_compat
        return execute_agent, execute_agent_compat

    def _prepare_updated_memories(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        current_short = self._load_or_migrate_short_memory()
        current_long = self._load_or_migrate_long_memory()
        short_ops = list(decision.get("short_memory_ops", []) or [])
        long_ops = list(decision.get("long_memory_ops", []) or [])
        short_ops = _normalize_memory_patch_ops(short_ops, label="short_memory_ops")
        long_ops = _normalize_memory_patch_ops(long_ops, label="long_memory_ops")

        try:
            next_short = self._apply_memory_ops(current_short, short_ops) if short_ops else deepcopy(current_short)
            next_short = _sanitize_short_memory_transition(current_short, next_short, short_ops)
            next_long = self._apply_memory_ops(current_long, long_ops) if long_ops else deepcopy(current_long)

            next_short.setdefault("meta", {})
            next_short["meta"]["schema_version"] = MEMORY_SCHEMA_VERSION
            next_short["meta"]["updated_at"] = _utc_now_iso()
            next_short["meta"]["source"] = "strategy_patch_apply"

            next_long.setdefault("meta", {})
            next_long["meta"]["schema_version"] = MEMORY_SCHEMA_VERSION
            next_long["meta"]["updated_at"] = _utc_now_iso()
            next_long["meta"]["source"] = "strategy_patch_apply"

            _validate_short_memory_schema(next_short)
            _validate_long_memory_schema(next_long)
        except Exception as e:
            # Fallback if patch application unexpectedly fails despite earlier validation
            return {
                "short": current_short,
                "long": current_long,
                "short_ops": [],
                "long_ops": [],
                "reason": f"fatal_patch_apply_error: {str(e)}",
            }

        return {
            "short": next_short,
            "long": next_long,
            "short_ops": short_ops,
            "long_ops": long_ops,
        }

    def _append_stage_flow(self, audit_meta: Dict[str, Any], stage_name: str) -> Dict[str, Any]:
        flow = list(audit_meta.get("stage_flow", []))
        flow.append(stage_name)
        audit_meta["stage_flow"] = flow
        return audit_meta

    def _with_tool_stage(self, stage_name: str):
        return set_tool_stage(stage_name)

    def _tool_call_succeeded(self, call: Dict[str, Any]) -> bool:
        result = call.get("result")
        if isinstance(result, dict):
            status = str(result.get("status", "")).strip().lower()
            if status in {"error", "rejected", "failed"}:
                return False
        return True

    def _tool_call_has_effect(self, call: Dict[str, Any]) -> bool:
        result = call.get("result")
        if not isinstance(result, dict):
            return True
        status = str(result.get("status", "")).strip().lower()
        if status in {"error", "rejected", "failed", "skipped"}:
            return False
        return True

    def _safe_float(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _iter_position_rows(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            if isinstance(payload.get("positions"), list):
                return [item for item in payload.get("positions", []) if isinstance(item, dict)]
            return [payload]
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def _iter_order_rows(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            if isinstance(payload.get("orders"), list):
                return [item for item in payload.get("orders", []) if isinstance(item, dict)]
            return [payload]
        return []

    def _extract_position_snapshot_from_payload(self, payload: Any) -> Optional[Dict[str, Any]]:
        positions = self._iter_position_rows(payload)
        if not positions:
            return None
        for row in positions:
            amount = self._safe_float(
                row.get("position_amount", row.get("positionAmt", row.get("position_amt")))
            )
            if amount is None or abs(amount) <= 0:
                continue
            position_side = str(
                row.get("position_side", row.get("positionSide", "BOTH")) or "BOTH"
            ).upper()
            if position_side == "LONG":
                direction = "long"
            elif position_side == "SHORT":
                direction = "short"
            else:
                direction = "long" if amount > 0 else "short"
            return {
                "direction": direction,
                "contracts": abs(amount),
            }
        return None

    def _extract_live_position_snapshot(self, audit_meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        precheck_payload = {}
        refreshed = audit_meta.get("refreshed_precheck")
        initial = audit_meta.get("precheck")
        if isinstance(refreshed, dict):
            precheck_payload = refreshed
        elif isinstance(initial, dict):
            precheck_payload = initial
        if not isinstance(precheck_payload, dict):
            return None

        positions = self._iter_position_rows(precheck_payload.get("position"))
        if not positions:
            return None

        for row in positions:
            amount = self._safe_float(
                row.get("position_amount", row.get("positionAmt", row.get("position_amt")))
            )
            if amount is None or abs(amount) <= 0:
                continue

            position_side = str(
                row.get("position_side", row.get("positionSide", "BOTH")) or "BOTH"
            ).upper()
            if position_side == "LONG":
                direction = "long"
            elif position_side == "SHORT":
                direction = "short"
            else:
                direction = "long" if amount > 0 else "short"

            mark_price = self._safe_float(row.get("mark_price", row.get("markPrice")))
            if mark_price is None or mark_price <= 0:
                continue

            return {
                "direction": direction,
                "contracts": abs(amount),
                "mark_price": mark_price,
                "precheck": precheck_payload,
            }
        return None

    def _extract_order_fill_events(self, event_details: str) -> List[Dict[str, Any]]:
        pattern = re.compile(
            r"order\s+(?P<order_id>\d+).*?You\s+(?P<side>BOUGHT|SOLD)\s+"
            r"(?P<contracts>\d+(?:\.\d+)?)\s+contracts\s+on\s+(?P<symbol>[A-Z0-9_]+)\s+"
            r"at an average price of\s+(?P<price>\d+(?:\.\d+)?)",
            re.IGNORECASE | re.DOTALL,
        )
        fills: List[Dict[str, Any]] = []
        for match in pattern.finditer(str(event_details or "")):
            fills.append(
                {
                    "order_id": str(match.group("order_id")),
                    "side": str(match.group("side")).upper(),
                    "contracts": self._safe_float(match.group("contracts")) or 0.0,
                    "symbol": str(match.group("symbol")).upper(),
                    "price": self._safe_float(match.group("price")) or 0.0,
                }
            )
        return fills

    def _iter_open_order_rows(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            rows = payload.get("orders")
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    def _infer_fill_attribution(
        self,
        fill: Dict[str, Any],
        precheck_payload: Dict[str, Any],
        prior_short_memory: Dict[str, Any],
    ) -> Dict[str, Any]:
        fill_side = str(fill.get("side", "") or "").upper()
        opening_direction = "long" if fill_side == "BUY" else "short"
        closing_direction = "short" if fill_side == "BUY" else "long"
        opening_semantic = "open_long" if fill_side == "BUY" else "open_short"
        closing_semantic = "close_short" if fill_side == "BUY" else "close_long"

        pre_position = self._extract_position_snapshot_from_payload(precheck_payload.get("position"))
        open_orders = self._iter_open_order_rows(precheck_payload.get("open_orders"))
        prior_hypothesis = prior_short_memory.get("active_hypothesis", {}) if isinstance(prior_short_memory, dict) else {}
        prior_direction = str(prior_hypothesis.get("direction", "") or "").strip().lower()

        close_score = 0
        open_score = 0
        reasons: List[str] = []

        if isinstance(pre_position, dict):
            pre_direction = str(pre_position.get("direction", "") or "").strip().lower()
            if pre_direction == closing_direction:
                close_score += 3
                reasons.append(f"precheck showed a live {closing_direction} before this fill")
            elif pre_direction == opening_direction:
                open_score += 2
                reasons.append(f"precheck already showed a live {opening_direction} before this fill")
        else:
            reasons.append("precheck did not confirm any live position before attribution")

        if prior_direction == closing_direction:
            close_score += 2
            reasons.append(f"prior active hypothesis direction was {closing_direction}")
        elif prior_direction == opening_direction:
            open_score += 1
            reasons.append(f"prior active hypothesis direction was {opening_direction}")

        wanted_protective_side = "BUY" if closing_direction == "short" else "SELL"
        wanted_protective_position_side = closing_direction.upper()
        for row in open_orders:
            side = str(row.get("side", "") or "").upper()
            position_side = str(row.get("positionSide", row.get("position_side", "")) or "").upper()
            order_type = str(row.get("type", row.get("order_type", "")) or "").upper()
            if side == wanted_protective_side and position_side == wanted_protective_position_side and (
                "STOP" in order_type or "TAKE_PROFIT" in order_type
            ):
                close_score += 2
                reasons.append(
                    f"there was still a protective {side}+{position_side} order on the opposite-side trade chain"
                )
                break

        if close_score >= open_score + 2 and close_score >= 2:
            semantic = closing_semantic
            confidence = "high" if close_score >= 4 else "medium"
        elif open_score > close_score:
            semantic = opening_semantic
            confidence = "medium"
        else:
            semantic = "ambiguous"
            confidence = "low"

        return {
            "order_id": str(fill.get("order_id", "") or ""),
            "fill_side": fill_side,
            "contracts": float(fill.get("contracts", 0) or 0),
            "price": float(fill.get("price", 0) or 0),
            "semantic": semantic,
            "confidence": confidence,
            "reasons": reasons[:4],
        }

    def _build_fill_attribution_context(
        self,
        event_details: str,
        precheck_payload: Dict[str, Any],
        prior_short_memory: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(precheck_payload, dict):
            return {"items": [], "text": ""}
        fills = self._extract_order_fill_events(event_details)
        if not fills:
            return {"items": [], "text": ""}

        items = [
            self._infer_fill_attribution(fill, precheck_payload, prior_short_memory)
            for fill in fills
        ]
        lines = [
            "Use this attribution before describing any fill as a new position. "
            "A BUY fill after a short chain often means close_short, not open_long."
        ]
        for item in items:
            reasons = "; ".join(item.get("reasons", []) or []) or "insufficient corroborating evidence"
            lines.append(
                "- "
                f"Fill {item['order_id']}: {item['fill_side']} {item['contracts']:.4f} @ {item['price']:.2f} "
                f"-> likely {item['semantic']} (confidence={item['confidence']}). Reasons: {reasons}."
            )
            if item["semantic"] in {"close_short", "close_long"}:
                lines.append(
                    "  Do not describe this as a new opposite-side position unless post-fill verification later confirms it."
                )
        return {
            "items": items,
            "text": "\n".join(lines),
        }

    def _text_indicates_stopout(self, text: Any) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        patterns = [
            "stopped out",
            "stop-out",
            "stop out",
            "protective exit",
            "forced protective exit",
            "triggered stop",
            "stop_market",
            "stopmarket",
            "被止损",
            "触发止损",
            "止损离场",
            "保护性离场",
            "扫损",
        ]
        return any(token in normalized for token in patterns)

    def _build_post_stop_retry_guardrail(
        self,
        decision: Dict[str, Any],
        event_details: str,
    ) -> str:
        state_change_evidence = self._compact_text(decision.get("state_change_evidence", ""), 180)
        next_alarm_reason = self._compact_text(decision.get("next_alarm_reason", ""), 180)
        guardrail_parts: List[str] = []
        if state_change_evidence:
            guardrail_parts.append(f"New evidence required: {state_change_evidence}")
        if next_alarm_reason:
            guardrail_parts.append(f"Recheck plan: {next_alarm_reason}")
        if not guardrail_parts and event_details:
            guardrail_parts.append(
                "Do not treat this stop as a fresh setup reset. Require explicit new structure or momentum evidence before retry."
            )
        return " | ".join(guardrail_parts)[:220]

    def _build_post_stop_reflection_from_turn(
        self,
        state: Dict[str, Any],
        decision: Dict[str, Any],
        audit_meta: Dict[str, Any],
        *,
        log_dir: str,
    ) -> Optional[Dict[str, Any]]:
        event_type = str(audit_meta.get("event_type", state.get("event_type", "")) or "")
        event_details = self._compact_text(audit_meta.get("event_details", state.get("event_details", "")), 220)
        execution_txt = self._compact_text(decision.get("execution_txt", ""), 180)
        explanation = self._compact_text(decision.get("explanation", ""), 220)
        falsification_point = self._compact_text(decision.get("falsification_point", ""), 180)
        direct_stop_statement = self._text_indicates_stopout(
            " | ".join([piece for piece in [execution_txt, explanation, falsification_point] if piece])
        )
        event_stop_statement = self._text_indicates_stopout(event_details)

        precheck_payload = {}
        refreshed = audit_meta.get("refreshed_precheck")
        initial = audit_meta.get("precheck")
        if isinstance(refreshed, dict):
            precheck_payload = refreshed
        elif isinstance(initial, dict):
            precheck_payload = initial
        verify_payload = audit_meta.get("verify", {}) if isinstance(audit_meta.get("verify", {}), dict) else {}

        pre_position = self._extract_position_snapshot_from_payload(precheck_payload.get("position"))
        verified_position = self._extract_position_snapshot_from_payload(verify_payload.get("position"))
        flattened_position = bool(pre_position and not verified_position)
        stop_event_type = "order_fill" in event_type.lower()
        if not direct_stop_statement and not (event_stop_statement and (stop_event_type or flattened_position)):
            return None

        prior_short = state.get("short_memory_snapshot_obj", {})
        if not isinstance(prior_short, dict):
            prior_short = {}
        active_hypothesis = prior_short.get("active_hypothesis", {})
        if not isinstance(active_hypothesis, dict):
            active_hypothesis = {}
        risk_state = prior_short.get("risk_state", {})
        if not isinstance(risk_state, dict):
            risk_state = {}
        day_plan = prior_short.get("day_plan", {})
        if not isinstance(day_plan, dict):
            day_plan = {}

        direction = str((pre_position or {}).get("direction", "") or active_hypothesis.get("direction", "") or "").lower()
        if direction not in {"long", "short"}:
            direction = "flat"

        why_entered = (
            self._compact_text(risk_state.get("state_change_evidence", ""), 220)
            or self._compact_text(day_plan.get("day_thesis", ""), 220)
            or self._compact_text(decision.get("decision_basis", ""), 180)
        )
        why_failed = falsification_point or event_details or explanation or execution_txt
        retry_guardrail = self._build_post_stop_retry_guardrail(decision, event_details)
        recorded_at = _utc_now_iso()
        expires_at = (datetime.now() + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "episode_id": f"stop-{log_dir}",
            "recorded_at": recorded_at,
            "expires_at": expires_at,
            "source_log_dir": log_dir,
            "event_type": event_type,
            "hypothesis_id": str(active_hypothesis.get("hypothesis_id", "") or ""),
            "direction": direction,
            "trigger_context": event_details,
            "outcome": execution_txt or "Protective stop / forced exit entered the decision loop.",
            "why_entered": why_entered,
            "why_failed": why_failed,
            "retry_guardrail": retry_guardrail,
        }

    def _update_post_stop_reflection_from_turn(
        self,
        state: Dict[str, Any],
        decision: Dict[str, Any],
        audit_meta: Dict[str, Any],
        *,
        log_dir: str,
    ) -> None:
        reflection = self._load_or_bootstrap_post_stop_reflection()
        detected = self._build_post_stop_reflection_from_turn(state, decision, audit_meta, log_dir=log_dir)
        if not detected:
            active = reflection.get("active_reflection", {})
            if isinstance(active, dict) and active and not self._post_stop_reflection_is_live(active):
                reflection["active_reflection"] = {}
                self._persist_post_stop_reflection(reflection)
            return

        detected_id = str(detected.get("episode_id", "") or "")
        deduped_recent = [
            item
            for item in list(reflection.get("recent_reflections", []))
            if isinstance(item, dict) and str(item.get("episode_id", "") or "") != detected_id
        ]
        deduped_recent.append(detected)
        reflection["active_reflection"] = detected
        reflection["recent_reflections"] = deduped_recent[-12:]
        self._persist_post_stop_reflection(reflection)

    def _find_protective_stop_distance(
        self, direction: str, mark_price: float, open_orders_payload: Any
    ) -> Optional[float]:
        if mark_price <= 0:
            return None
        rows = self._iter_order_rows(open_orders_payload)
        if not rows:
            return None

        wanted_side = "SELL" if direction == "long" else "BUY"
        wanted_position_side = "LONG" if direction == "long" else "SHORT"
        distances: List[float] = []

        for row in rows:
            status = str(row.get("status", "") or "").upper()
            if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "FILLED"}:
                continue
            order_type = str(row.get("type", row.get("orderType", "")) or "").upper()
            if "STOP" not in order_type or "TAKE_PROFIT" in order_type:
                continue

            side = str(row.get("side", "") or "").upper()
            if side != wanted_side:
                continue

            position_side = str(row.get("positionSide", row.get("position_side", "BOTH")) or "BOTH").upper()
            if position_side not in {wanted_position_side, "BOTH", ""}:
                continue

            stop_price = self._safe_float(row.get("stopPrice", row.get("stop_price")))
            if stop_price is None or stop_price <= 0:
                continue

            distance = abs(mark_price - stop_price) / mark_price
            distances.append(distance)

        if not distances:
            return None
        return min(distances)

    def _has_risk_compression_action(self, calls: List[Dict[str, Any]], direction: str) -> bool:
        for call in calls:
            if not self._tool_call_has_effect(call):
                continue
            tool_name = str(call.get("tool", ""))
            if tool_name in {"close_coin_futures_position", "close_usdt_futures_position"}:
                return True
            if tool_name in {"modify_coin_futures_order", "modify_usdt_futures_order"}:
                return True
            if tool_name not in {"trade_coin_futures", "trade_usdt_futures"}:
                continue

            args = call.get("args", {})
            if not isinstance(args, dict):
                continue
            if bool(args.get("reduce_only")) or bool(args.get("close_position")):
                return True

            order_type = str(args.get("order_type", "") or "").upper()
            if "STOP" in order_type and "TAKE_PROFIT" not in order_type:
                side = str(args.get("side", "") or "").upper()
                if direction == "long" and side == "SELL":
                    return True
                if direction == "short" and side == "BUY":
                    return True
        return False

    def _has_primary_side_effect_actions(self, calls: List[Dict[str, Any]]) -> bool:
        for call in calls:
            if str(call.get("stage", "")) not in {"execute_primary_model", "execute_primary_retry"}:
                continue
            if not self._tool_call_has_effect(call):
                continue
            tool_name = str(call.get("tool", ""))
            if tool_name in RETRY_SIDE_EFFECT_BLOCKED_TOOLS:
                return True
        return False

    def _should_limit_retry_to_non_side_effects(
        self,
        *,
        failure: Dict[str, Any],
        calls: List[Dict[str, Any]],
    ) -> bool:
        _ = failure
        # Once primary has already committed any non-alarm side effect in this
        # wakeup, retry must become decision-repair only. Otherwise the retry
        # can duplicate orders/protection and drift away from verified state.
        return self._has_primary_side_effect_actions(calls)

    def _should_prioritize_contract_repair(
        self,
        *,
        failure: Dict[str, Any],
    ) -> bool:
        error_class = str(failure.get("error_class", "") or "").strip()
        return error_class in RETRY_NON_SIDE_EFFECT_GUARD_ERRORS

    def _validate_retry_no_side_effect_actions(
        self,
        *,
        stage_name: str,
        audit_meta: Dict[str, Any],
        calls: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if stage_name != "execute_primary_retry":
            return None
        if str(audit_meta.get("retry_mode", "")) != "non_side_effect_repair":
            return None
        for call in calls:
            tool_name = str(call.get("tool", ""))
            if tool_name not in RETRY_SIDE_EFFECT_BLOCKED_TOOLS:
                continue
            if not self._tool_call_has_effect(call):
                continue
            return {
                "error_class": "retry_side_effect_blocked",
                "why_rejected": (
                    f"Retry mode is decision-repair only, but retry executed side-effect tool: {tool_name}."
                ),
                "model_fix_hint": (
                    "In this retry, only fix decision schema/alarm semantics and run lightweight verification. "
                    "Do not execute new trade/protective/cancel/transfer/leverage actions."
                ),
                "retryable": False,
                "defer_until_next_turn": True,
                "source_rule": "retry_side_effect_blocked",
                "related_tool_call": str(call.get("id", "")),
            }
        return None

    def _normalize_alarm_condition_signature(self, raw_condition: Any) -> str:
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

    def _canonical_followup_action_signature(self, call: Dict[str, Any]) -> Optional[tuple]:
        tool_name = str(call.get("tool", ""))
        args = call.get("args", {})
        if not isinstance(args, dict):
            args = {}

        if tool_name == "set_alarm":
            return (
                "set_alarm",
                str(args.get("value", "")).strip(),
                str(args.get("unit", "")).strip().lower(),
                str(args.get("prompt", "")).strip(),
                self._normalize_alarm_condition_signature(args.get("condition")),
            )
        if tool_name in {"cancel_coin_futures_order", "cancel_usdt_futures_order"}:
            return (
                "cancel_coin_futures_order",
                str(args.get("symbol", "")).strip(),
                str(args.get("order_id", "")).strip(),
            )
        if tool_name == "delete_alarm":
            return (
                "delete_alarm",
                str(args.get("alarm_id", "")).strip(),
            )
        return None

    def _validate_followup_action_dedup(
        self,
        state: DecisionState,
        *,
        start_idx: int,
        new_calls: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        prior_calls = list(state.get("tool_results", []))[:start_idx]
        prior_signatures = set()
        for call in prior_calls:
            stage_name = str(call.get("stage", ""))
            if stage_name not in {"execute_primary_model", "execute_primary_retry"}:
                continue
            if not self._tool_call_has_effect(call):
                continue
            sig = self._canonical_followup_action_signature(call)
            if sig:
                prior_signatures.add(sig)

        for call in new_calls:
            sig = self._canonical_followup_action_signature(call)
            if not sig:
                continue
            if sig not in prior_signatures:
                continue
            return {
                "error_class": "duplicate_followup_action",
                "why_rejected": (
                    "execute_followup repeated an action that was already completed in execute_primary."
                ),
                "model_fix_hint": (
                    "Do not repeat successful set_alarm/delete_alarm/cancel actions from execute_primary. "
                    "Use refreshed runtime state and only perform remaining actions."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "duplicate_followup_action",
                "related_tool_call": str(call.get("id", "")),
            }
        return None

    def _collect_retry_duplicate_action_warnings(
        self,
        state: DecisionState,
        *,
        start_idx: int,
        new_calls: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        prior_calls = list(state.get("tool_results", []))[:start_idx]
        prior_by_signature: Dict[tuple, Dict[str, Any]] = {}
        for call in prior_calls:
            stage_name = str(call.get("stage", ""))
            if stage_name not in {"execute_primary_model", "execute_primary_retry"}:
                continue
            if not self._tool_call_has_effect(call):
                continue
            sig = self._canonical_followup_action_signature(call)
            if not sig:
                continue
            if sig not in prior_by_signature:
                prior_by_signature[sig] = call

        warnings: List[Dict[str, Any]] = []
        for call in new_calls:
            sig = self._canonical_followup_action_signature(call)
            if not sig:
                continue
            prior = prior_by_signature.get(sig)
            if not prior:
                continue
            warnings.append(
                {
                    "tag": "retry_duplicate_action_warning",
                    "reason": (
                        "execute_primary_retry repeated an action equivalent to a previously completed primary action."
                    ),
                    "tool_call_id": str(call.get("id", "")),
                    "tool": str(call.get("tool", "")),
                    "stage": str(call.get("stage", "")),
                    "duplicate_of_call_id": str(prior.get("id", "")),
                    "duplicate_of_stage": str(prior.get("stage", "")),
                }
            )
        return warnings

    def _preview_next_short_memory(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        current_short = self._load_or_migrate_short_memory()
        short_ops = list(decision.get("short_memory_ops", []) or [])
        short_ops = _normalize_memory_patch_ops(short_ops, label="short_memory_ops")
        if not short_ops:
            return current_short
        return self._apply_memory_ops(current_short, short_ops)

    def _select_runtime_precheck_payload(self, audit_meta: Dict[str, Any]) -> Dict[str, Any]:
        for key in ("refreshed_precheck", "precheck"):
            payload = audit_meta.get(key)
            if isinstance(payload, dict) and payload:
                return payload
        return {}

    def _select_runtime_truth_payload(self, audit_meta: Dict[str, Any]) -> Dict[str, Any]:
        verify_payload = audit_meta.get("verify")
        if isinstance(verify_payload, dict) and not bool(verify_payload.get("skipped")):
            payload = {
                "position": verify_payload.get("position"),
                "account": verify_payload.get("account"),
                "open_orders": verify_payload.get("open_orders"),
            }
            if any(payload.values()):
                return payload
        return self._select_runtime_precheck_payload(audit_meta)

    def _guard_semantic_purpose(self, error_class: str) -> str:
        rule = str(error_class or "").strip()
        if rule in WAIT_TRUTHFULNESS_GUARDS:
            return "wait_truthfulness"
        if rule in RUNTIME_ALIGNMENT_GUARDS:
            return "runtime_alignment"
        if rule in EXECUTION_SHAPE_GUARDS:
            return "execution_shape"
        if rule in RANGE_DECISION_GUARDS:
            return "range_decision"
        if rule in RISK_TRUTHFULNESS_GUARDS:
            return "risk_truthfulness"
        if rule in CONTRACT_INTEGRITY_GUARDS:
            return "contract_integrity"
        return "contract_integrity"

    def _guard_blocked_decision_repair_paths(self, error_class: str) -> Set[str]:
        purpose = self._guard_semantic_purpose(error_class)
        return set(GUARD_BLOCKED_DECISION_REPAIR_PATHS_BY_PURPOSE.get(purpose, set()))

    def _build_sideways_range_gate_context(
        self,
        *,
        state: DecisionState,
        audit_meta: Dict[str, Any],
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        next_short = self._preview_next_short_memory(decision)
        consistency_state = next_short.get("consistency_state", {}) if isinstance(next_short, dict) else {}
        if not isinstance(consistency_state, dict):
            consistency_state = {}
        execution_mode = str(consistency_state.get("execution_mode", "") or "").strip()
        market_regime = str(consistency_state.get("market_regime", "") or "").strip()
        precheck_payload = self._select_runtime_precheck_payload(audit_meta)
        eligibility = self._build_sideways_range_eligibility(precheck_payload) if precheck_payload else {}
        snapshot = self._build_sideways_range_opportunity_snapshot(state, precheck_payload) if precheck_payload else {}
        gate_enabled = bool(
            execution_mode == "observe_only"
            and market_regime == "neutral_sideways"
            and bool(eligibility.get("eligible"))
            and not bool(eligibility.get("range_plan_active"))
            and bool(snapshot.get("available"))
        )
        return {
            "gate_enabled": gate_enabled,
            "execution_mode": execution_mode,
            "market_regime": market_regime,
            "eligibility": eligibility,
            "snapshot": snapshot,
        }

    def _validate_sideways_range_decision_gate(
        self,
        *,
        state: DecisionState,
        stage_name: str,
        audit_meta: Dict[str, Any],
        decision: Dict[str, Any],
        new_calls: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if stage_name not in {"execute_primary_model", "execute_primary_retry"}:
            return None

        context = self._build_sideways_range_gate_context(
            state=state,
            audit_meta=audit_meta,
            decision=decision,
        )
        if not context.get("gate_enabled"):
            return None

        range_decision = str(decision.get("range_decision", "") or "").strip()
        range_reason = str(decision.get("range_decision_reason_code", "") or "").strip()
        declared_tools = {
            str(item.get("tool", "")).strip()
            for item in list(decision.get("tool_intents", []) or [])
            if isinstance(item, dict) and str(item.get("tool", "")).strip()
        }
        all_range_calls = [
            call for call in list(state.get("tool_results", []))
            if str(call.get("tool", "")).strip() == "set_range_plan"
        ]

        if range_decision not in {"start", "decline"}:
            return {
                "error_class": "sideways_range_decision_missing",
                "why_rejected": (
                    "observe_only + neutral_sideways + eligible range context requires an explicit "
                    "range_decision=start or decline."
                ),
                "model_fix_hint": (
                    "Return range_decision=start if you want to launch set_range_plan, or "
                    "range_decision=decline with a valid range_decision_reason_code if you intentionally keep waiting."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "sideways_range_decision_missing",
                "related_tool_call": "",
            }

        if range_decision == "start":
            if "set_range_plan" not in declared_tools:
                return {
                    "error_class": "sideways_range_start_missing_tool_intent",
                    "why_rejected": "range_decision=start was declared, but tool_intents did not include set_range_plan.",
                    "model_fix_hint": "When range_decision=start, declare set_range_plan inside tool_intents with its purpose.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "sideways_range_start_missing_tool_intent",
                    "related_tool_call": "",
                }
            if not all_range_calls:
                return {
                    "error_class": "sideways_range_start_missing_execution",
                    "why_rejected": "range_decision=start was declared, but execute stage did not call set_range_plan.",
                    "model_fix_hint": "If you choose range_decision=start, execute set_range_plan in the same wakeup.",
                    "retryable": True,
                    "defer_until_next_turn": False,
                    "source_rule": "sideways_range_start_missing_execution",
                    "related_tool_call": "",
                }
            return None

        if range_reason not in {
            "directional_setup_near_trigger",
            "price_too_close_to_band_edge",
            "band_too_narrow",
            "event_risk_near",
            "range_levels_low_confidence",
        }:
            return {
                "error_class": "sideways_range_decline_reason_missing",
                "why_rejected": (
                    "range_decision=decline requires a structured range_decision_reason_code."
                ),
                "model_fix_hint": (
                    "Use one of: directional_setup_near_trigger / price_too_close_to_band_edge / "
                    "band_too_narrow / event_risk_near / range_levels_low_confidence."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "sideways_range_decline_reason_missing",
                "related_tool_call": "",
            }
        snapshot = context.get("snapshot", {}) if isinstance(context.get("snapshot", {}), dict) else {}
        quality = snapshot.get("range_quality", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(quality, dict):
            quality = {}
        oscillation_score = self._safe_float(quality.get("oscillation_score"))
        suggested_mode = str(snapshot.get("suggested_range_mode", "") or "")
        activation_bias = str(snapshot.get("range_activation_bias", "") or "")
        if (
            range_reason == "price_too_close_to_band_edge"
            and oscillation_score is not None
            and activation_bias == "strong"
            and suggested_mode in {"symmetric", "short_only", "long_only"}
        ):
            return {
                "error_class": "sideways_range_edge_decline_not_supported",
                "why_rejected": (
                    "Recent range quality is already strong, so near-edge location alone is not a sufficient reason "
                    "to decline range activation in this wakeup."
                ),
                "model_fix_hint": (
                    "If recent_range_quality is strong, either start the suggested range mode "
                    "(including one-sided short_only/long_only when projected) or decline with a stronger reason such as "
                    "event_risk_near / directional_setup_near_trigger / range_levels_low_confidence."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "sideways_range_edge_decline_not_supported",
                "related_tool_call": "",
            }
        if (
            range_reason == "range_levels_low_confidence"
            and activation_bias == "strong"
            and suggested_mode in {"symmetric", "short_only", "long_only"}
        ):
            return {
                "error_class": "sideways_range_activation_decline_not_supported",
                "why_rejected": (
                    "The runtime box quality is strong enough that 'range_levels_low_confidence' is not a sufficient "
                    "reason to abandon sideways harvesting in this wakeup."
                ),
                "model_fix_hint": (
                    "If the snapshot shows activation_bias=strong with an executable suggested_range_mode, "
                    "either start the suggested range shape or decline with a stronger reason such as "
                    "event_risk_near / directional_setup_near_trigger / band_too_narrow."
                ),
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "sideways_range_activation_decline_not_supported",
                "related_tool_call": "",
            }
        if "set_range_plan" in declared_tools or all_range_calls:
            return {
                "error_class": "sideways_range_decline_execution_conflict",
                "why_rejected": (
                    "range_decision=decline conflicts with a declared or executed set_range_plan action."
                ),
                "model_fix_hint": "If you decline range, remove set_range_plan from tool_intents and do not execute it in this wakeup.",
                "retryable": True,
                "defer_until_next_turn": False,
                "source_rule": "sideways_range_decline_execution_conflict",
                "related_tool_call": str(all_range_calls[0].get("id", "")) if all_range_calls else "",
            }
        return None

    def _decision_risk_review_text(self, decision: Dict[str, Any]) -> str:
        parts = [
            str(decision.get("execution_txt", "") or ""),
            str(decision.get("explanation", "") or ""),
            str(decision.get("memory_management_reasoning", "") or ""),
            str(decision.get("decision_basis", "") or ""),
            str(decision.get("conflict_check", "") or ""),
            str(decision.get("falsification_point", "") or ""),
            str(decision.get("next_alarm_reason", "") or ""),
            str(decision.get("state_change_evidence", "") or ""),
            str(decision.get("shortterm", "") or ""),
        ]
        return "\n".join(part for part in parts if part.strip())

    def _append_risk_review_warning(
        self,
        audit_meta: Dict[str, Any],
        *,
        warning_class: str,
        message: str,
    ) -> Dict[str, Any]:
        warnings = list(audit_meta.get("risk_review_warnings", []))
        warnings.append(
            {
                "warning_class": warning_class,
                "message": message,
            }
        )
        audit_meta["risk_review_warnings"] = warnings
        return audit_meta

    def _validate_hard_risk_compression_requirement(
        self,
        audit_meta: Dict[str, Any],
        decision: Dict[str, Any],
        calls: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        snapshot = self._extract_live_position_snapshot(audit_meta)
        if not snapshot:
            return None

        direction = str(snapshot.get("direction", ""))
        mark_price = self._safe_float(snapshot.get("mark_price"))
        if direction not in {"long", "short"} or mark_price is None or mark_price <= 0:
            return None

        precheck_payload = snapshot.get("precheck", {})
        open_orders_payload = {}
        if isinstance(precheck_payload, dict):
            open_orders_payload = precheck_payload.get("open_orders")

        stop_distance = self._find_protective_stop_distance(direction, mark_price, open_orders_payload)
        near_stop = stop_distance is not None and stop_distance <= 0.0035

        next_short = self._preview_next_short_memory(decision)
        risk_state = ""
        risk_action_required = ""
        if isinstance(next_short, dict):
            risk_state_obj = next_short.get("risk_state", {})
            if isinstance(risk_state_obj, dict):
                risk_state = str(risk_state_obj.get("risk_state", "") or "").strip().lower()
                risk_action_required = str(risk_state_obj.get("risk_action_required", "") or "").strip().lower()

        hard_required = risk_state in {"critical", "emergency"} or (
            near_stop and risk_action_required == "yes"
        )

        if not hard_required:
            if near_stop:
                stop_distance_pct = round((stop_distance or 0.0) * 100, 4)
                warning_bits = [f"Open position is near protective stop (distance={stop_distance_pct}%)."]
                if risk_action_required == "yes":
                    warning_bits.append(
                        "Decision still marks risk_action_required=yes, so review whether that flag should remain set."
                    )
                warning_bits.append(
                    "Near stop alone is now a review warning: reassess structure, 15m/1h levels, and momentum before tightening."
                )
                self._append_risk_review_warning(
                    audit_meta,
                    warning_class="near_stop_review",
                    message=" ".join(warning_bits),
                )
            return None

        if self._has_risk_compression_action(calls, direction):
            return None

        stop_distance_pct = round((stop_distance or 0.0) * 100, 4)
        trigger_reasons: List[str] = []
        if risk_state in {"critical", "emergency"}:
            trigger_reasons.append(f"risk_state={risk_state}")
        if near_stop and risk_action_required == "yes":
            trigger_reasons.append(f"near_stop+risk_action_required(distance={stop_distance_pct}%)")
        reason_text = ", ".join(trigger_reasons) if trigger_reasons else "hard risk compression condition"
        return {
            "error_class": "risk_compression_required",
            "why_rejected": (
                f"Open position hit hard risk compression conditions ({reason_text}), "
                "but this batch did not execute reduce/close/tighten-stop action."
            ),
            "model_fix_hint": (
                "Hard-compress risk only from structured risk evidence: risk_state=critical/emergency, "
                "or near_stop with risk_action_required=yes. In those cases, do not only wait or set alarms. "
                "Execute one risk-compression action now: tighten_stop / reduce / close."
            ),
            "retryable": True,
            "defer_until_next_turn": False,
            "source_rule": "risk_compression_required",
            "related_tool_call": "",
        }

    def _is_successful_opening_trade_call(self, call: Dict[str, Any]) -> bool:
        if str(call.get("tool", "")) not in {"trade_coin_futures", "trade_usdt_futures"}:
            return False
        args = call.get("args", {})
        if str(args.get("order_type", "")).upper() not in {"MARKET", "LIMIT"}:
            return False
        return self._tool_call_succeeded(call)

    def _append_rejected_tool_calls(self, audit_meta: Dict[str, Any], calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        rejected = list(audit_meta.get("rejected_tool_calls", []))
        rejected.extend(self._collect_effective_rejected_tool_calls(calls))
        audit_meta["rejected_tool_calls"] = rejected
        return audit_meta

    def _tool_call_repair_family_signature(self, call: Dict[str, Any]) -> Optional[tuple]:
        tool_name = str(call.get("tool", "")).strip()
        args = call.get("args", {})
        if not isinstance(args, dict):
            args = {}

        if tool_name in {"trade_usdt_futures", "trade_coin_futures"}:
            return (
                "trade_futures",
                str(args.get("symbol", "")).strip().upper(),
                str(args.get("side", "")).strip().upper(),
                str(args.get("position_side", "")).strip().upper(),
                str(args.get("order_type", "")).strip().upper(),
                str(args.get("time_in_force", "")).strip().upper(),
                self._safe_float(args.get("price")),
                self._safe_float(args.get("stop_price")),
                self._safe_float(args.get("activation_price")),
                self._safe_float(args.get("callback_rate")),
            )
        if tool_name in {"set_alarm", "delete_alarm", "cancel_coin_futures_order", "cancel_usdt_futures_order"}:
            return self._canonical_followup_action_signature(call)
        return None

    def _is_rejected_call_superseded(self, call: Dict[str, Any], later_calls: List[Dict[str, Any]]) -> bool:
        signature = self._tool_call_repair_family_signature(call)
        if not signature:
            return False
        stage_name = str(call.get("stage", "")).strip()
        for later in later_calls:
            if str(later.get("stage", "")).strip() != stage_name:
                continue
            if not self._tool_call_succeeded(later):
                continue
            if self._tool_call_repair_family_signature(later) == signature:
                return True
        return False

    def _collect_effective_rejected_tool_calls(self, calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rejected: List[Dict[str, Any]] = []
        for idx, call in enumerate(calls):
            result = call.get("result")
            if not isinstance(result, dict):
                continue
            status = str(result.get("status", "")).strip().lower()
            if status not in {"error", "rejected"}:
                continue
            if self._is_rejected_call_superseded(call, calls[idx + 1 :]):
                continue
            rejected.append(
                {
                    "tool_call_id": str(call.get("id", "")),
                    "tool": str(call.get("tool", "")),
                    "stage": str(call.get("stage", "")),
                    "error_class": str(result.get("error_class", status or "error")),
                    "source_rule": str(result.get("guard_rule_id", result.get("error_class", status or "error"))),
                    "why_rejected": str(result.get("why_rejected", result.get("message", ""))),
                    "model_fix_hint": str(result.get("model_fix_hint", "")),
                    "retryable": bool(result.get("retryable", False)),
                    "defer_until_next_turn": bool(result.get("defer_until_next_turn", False)),
                }
            )
        return rejected

    def _append_opening_sequence_state(self, audit_meta: Dict[str, Any], calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        existing_order_ids = list(audit_meta.get("opening_sequence_order_ids", []))
        consumed = bool(audit_meta.get("opening_sequence_consumed", False))
        for call in calls:
            if not self._is_successful_opening_trade_call(call):
                continue
            consumed = True
            result = call.get("result")
            if isinstance(result, dict) and result.get("orderId") is not None:
                existing_order_ids.append(str(result.get("orderId")))
        audit_meta["opening_sequence_consumed"] = consumed
        audit_meta["opening_sequence_order_ids"] = sorted(set(existing_order_ids))
        return audit_meta

    def _build_runtime_guard_summary(self, audit_meta: Dict[str, Any]) -> Dict[str, Any]:
        rejected_calls = list(audit_meta.get("rejected_tool_calls", []))
        guard_retry = dict(audit_meta.get("guard_retry", {}))
        guard_pipeline = list(audit_meta.get("guard_pipeline", []))
        guard_failures = [
            item for item in guard_pipeline if isinstance(item, dict) and str(item.get("status", "")) == "fail"
        ]
        triggered_rules = sorted(
            {
                str(item.get("error_class", "")).strip()
                for item in rejected_calls
                if str(item.get("error_class", "")).strip()
            }
        )
        guard_failure = audit_meta.get("guard_failure")
        if isinstance(guard_failure, dict):
            failure_rule = str(guard_failure.get("error_class", "")).strip()
            if failure_rule:
                triggered_rules.append(failure_rule)
                triggered_rules = sorted(set(triggered_rules))
        validation_status = str(audit_meta.get("validation_status", "guards_passed"))
        reanswer_count = int(guard_retry.get("attempt_index", 0) or 0)
        first_fail_rules = sorted(
            {
                str(item.get("error_class", "")).strip()
                for item in guard_failures
                if int(item.get("attempt_index", 0) or 0) == 0 and str(item.get("error_class", "")).strip()
            }
        )
        all_fail_rules = sorted(
            {str(item.get("error_class", "")).strip() for item in guard_failures if str(item.get("error_class", "")).strip()}
        )
        final_fail_rule = "none"
        if validation_status == "guards_blocked_wait_next_turn":
            final_fail_rule = str(guard_retry.get("stopped_reason", "guard_blocked") or "guard_blocked")
        resolved_fail_rules = list(all_fail_rules)
        if validation_status == "guards_blocked_wait_next_turn" and final_fail_rule in resolved_fail_rules:
            resolved_fail_rules = [item for item in resolved_fail_rules if item != final_fail_rule]
        final_validation_errors = "disabled"
        if validation_status == "guards_blocked_wait_next_turn":
            final_validation_errors = "guard_blocked"
        return {
            "validation_status": validation_status,
            "reanswer_count": str(reanswer_count),
            "triggered_rules": final_fail_rule if final_fail_rule != "none" else "none",
            "triggered_rules_first_fail": ", ".join(first_fail_rules) if first_fail_rules else "none",
            "triggered_rules_all_fail": ", ".join(all_fail_rules) if all_fail_rules else "none",
            "triggered_rules_final": final_fail_rule,
            "resolved_rules": ", ".join(resolved_fail_rules) if resolved_fail_rules else "none",
            "rejected_tool_calls": str(len(rejected_calls)) if rejected_calls else "none",
            "final_validation_errors": final_validation_errors,
            "guard_failures": guard_failures,
            "resolved_guard_failures": [
                item for item in guard_failures if str(item.get("error_class", "") or "") in resolved_fail_rules
            ],
        }

    def _has_successful_side_effect_calls(self, calls: List[Dict[str, Any]]) -> bool:
        readonly_tools = {
            "get_spot_balance",
            "get_range_plan",
            "get_usdt_futures_position",
            "get_usdt_futures_account",
            "get_usdt_futures_max_open_position",
            "get_usdt_futures_open_orders",
            "get_usdt_futures_order",
            "calculate_expression",
        }
        for call in calls:
            tool_name = str(call.get("tool", "")).strip()
            if tool_name in readonly_tools:
                continue
            if self._tool_call_has_effect(call):
                return True
        return False

    def _infer_trade_direction_from_call(self, call: Dict[str, Any]) -> str:
        if str(call.get("tool", "")) not in {"trade_coin_futures", "trade_usdt_futures"}:
            return ""
        args = call.get("args", {})
        order_type = str(args.get("order_type", "") or "").upper()
        if order_type not in {"MARKET", "LIMIT"}:
            return ""
        if bool(args.get("reduce_only")) or bool(args.get("close_position")):
            return ""
        side = str(args.get("side", "") or "").upper()
        position_side = str(args.get("position_side", "") or "").upper()
        if position_side == "LONG":
            return "long"
        if position_side == "SHORT":
            return "short"
        if side == "BUY":
            return "long"
        if side == "SELL":
            return "short"
        return ""

    def _compute_counter_long_horizon_bias(
        self,
        preferred_direction: str,
        tool_calls: List[Dict[str, Any]],
    ) -> bool:
        preferred = str(preferred_direction or "").strip().lower()
        if preferred not in {"long", "short"}:
            return False
        for call in tool_calls:
            if not self._tool_call_succeeded(call):
                continue
            trade_direction = self._infer_trade_direction_from_call(call)
            if trade_direction and trade_direction != preferred:
                return True
        return False

    def _needs_capacity_refresh(self, calls: List[Dict[str, Any]]) -> List[str]:
        reasons = []
        for call in calls:
            tool_name = str(call.get("tool", ""))
            if tool_name not in CAPACITY_DIRTY_TOOLS:
                continue
            if self._tool_call_succeeded(call):
                reasons.append(tool_name)
        return sorted(set(reasons))

    def _mark_verify_required(self, audit_meta: Dict[str, Any], calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        if audit_meta.get("verify_required"):
            return audit_meta
        for call in calls:
            tool_name = str(call.get("tool", ""))
            if tool_name in VERIFY_REQUIRED_TOOLS and self._tool_call_succeeded(call):
                audit_meta["verify_required"] = True
                break
        return audit_meta

    def _append_execute_pass(
        self,
        audit_meta: Dict[str, Any],
        stage_name: str,
        calls: List[Dict[str, Any]],
        agent_mode: str,
        raw_text: str,
        refresh_reasons: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        execute_passes = list(audit_meta.get("execute_passes", []))
        execute_passes.append(
            {
                "stage": stage_name,
                "agent_mode": agent_mode,
                "tool_count": len(calls),
                "tool_names": [str(call.get("tool", "")) for call in calls],
                "refresh_required": bool(refresh_reasons),
                "refresh_reason": list(refresh_reasons or []),
                "raw_text_excerpt": raw_text[:500],
                "completed_at": datetime.now().isoformat(),
            }
        )
        audit_meta["execute_passes"] = execute_passes
        return audit_meta

    def _append_guard_pipeline_event(
        self,
        audit_meta: Dict[str, Any],
        *,
        node: str,
        subguard: str,
        status: str,
        rule_id: str,
        error_class: str = "",
        why_rejected: str = "",
        model_fix_hint: str = "",
        retryable: Optional[bool] = None,
        defer_until_next_turn: Optional[bool] = None,
        tool_ref: str = "",
    ) -> Dict[str, Any]:
        pipeline = list(audit_meta.get("guard_pipeline", []))
        retry_meta = dict(audit_meta.get("guard_retry", {}))
        attempt_index = int(retry_meta.get("attempt_index", 0) or 0)
        event = {
            "node": node,
            "subguard": subguard,
            "status": status,
            "rule_id": rule_id,
            "error_class": error_class or rule_id,
            "why_rejected": why_rejected,
            "model_fix_hint": model_fix_hint,
            "retryable": bool(retryable) if retryable is not None else None,
            "defer_until_next_turn": bool(defer_until_next_turn) if defer_until_next_turn is not None else None,
            "tool_ref": tool_ref,
            "attempt_index": attempt_index,
            "ts": datetime.now().isoformat(),
        }
        pipeline.append(event)
        audit_meta["guard_pipeline"] = pipeline
        return audit_meta

    def _guard_natural_language_guide(self, failure: Dict[str, Any]) -> str:
        error_class = str(failure.get("error_class", failure.get("source_rule", "")) or "").strip()
        why_rejected = str(failure.get("why_rejected", "") or "").strip()
        model_fix_hint = str(failure.get("model_fix_hint", "") or "").strip()
        related_tool_call = str(failure.get("related_tool_call", "") or "").strip()

        specific_guides = {
            "approved_contract_runtime_mismatch": (
                "纠错指南：先回到 STRUCTURED CONTRACT DRAFT，而不是继续补工具。"
                "如果你确实需要这个副作用工具，必须在 draft 的 tool_intents 中声明同名 tool 和 purpose，"
                "并把 action_intent 改成对应的 open_position / close_position / manage_orders / cleanup / account_config / schedule_wait。"
                "如果 draft 原本只是 observe 或 schedule_wait，就不要在 runtime 阶段调用交易、撤单、改单、杠杆、划转或 range 类副作用工具。"
                "已经提交过的副作用不要在 retry 中重复提交；下一次只修正合同与记忆口径。"
            ),
            "hypothesis_rollover_expiry_invalid": (
                "纠错指南：你声明了 hypothesis_action=rollover，表示沿用同一个 hypothesis_id 向后延长。"
                "此时 short_memory_ops 必须 replace /active_hypothesis/expiry，且新值必须是 YYYY-MM-DD HH:MM:SS 格式、晚于旧 expiry 的未来时间。"
                "同时 declared_hypothesis_expiry 必须与 patch 后的 /active_hypothesis/expiry 完全一致。"
                "如果没有新证据支持续期，改用 terminate 或 replace，而不是 rollover。"
            ),
            "hypothesis_rollover_missing_expiry_patch": (
                "纠错指南：rollover 不是只在正文里说继续观察；必须在 short_memory_ops 中写入 "
                "{\"op\":\"replace\",\"path\":\"/active_hypothesis/expiry\",\"value\":\"未来时间\"}，"
                "并同步 declared_hypothesis_expiry。"
            ),
            "hypothesis_rollover_action_missing": (
                "纠错指南：你改变了同一个 hypothesis_id 的 expiry，却没有把 hypothesis_action 写成 rollover。"
                "要么把 hypothesis_action 改为 rollover 并说明续期原因，要么撤销 expiry 修改。"
            ),
            "declared_state_change_not_persisted": (
                "纠错指南：声明字段不会自动落盘。把 declared_state_change_evidence 的完全相同文本写入 "
                "short_memory_ops 的 /risk_state/state_change_evidence。"
            ),
            "declared_hypothesis_not_persisted": (
                "纠错指南：declared_hypothesis_* 必须和 patch 后的 active_hypothesis 完全一致。"
                "请补齐 /active_hypothesis/hypothesis_id、/direction、/status 中对应路径的 replace patch。"
            ),
            "declared_hypothesis_expiry_not_persisted": (
                "纠错指南：declared_hypothesis_expiry 必须通过 short_memory_ops 写到 /active_hypothesis/expiry，"
                "且两边字符串完全一致。"
            ),
            "decision_schema_invalid": (
                "纠错指南：只输出 DecisionOutput schema 中允许的字段；枚举值必须逐字匹配提示词列出的候选值，"
                "不要新增自定义字段或拼错字段名。"
            ),
            "memory_patch_invalid": (
                "纠错指南：记忆更新只能使用标准 JSON Patch。op 只能是 add/remove/replace；"
                "数组追加路径以 /- 结尾时 op 必须是 add；不要输出 append、update 或整份文档覆盖。"
            ),
            "missing_structured_wait_alarm": (
                "纠错指南：如果正文写了具体未来价格、RSI、MACD、回踩、确认或截止时间，就必须本轮调用 set_alarm，"
                "最好带 condition；否则必须引用 Pending Alarms 中真实存在的 alarm_id。"
            ),
            "active_hypothesis_expiry_stale": (
                "纠错指南：active/blocked hypothesis 的 expiry 不能在当前执行时刻之前。"
                "如果旧验证窗口已经过期，就不要把旧 expiry 原样保留到最终 snapshot；"
                "要么基于刷新后的事实结束/替换旧假设，要么写一个真正晚于 now 的新 expiry。"
            ),
            "wait_deadline_stale": (
                "纠错指南：既然本轮仍然是 wait/recheck，就不能把已经过期的 hold_until 或 recheck_at 写回记忆。"
                "先刷新当前事实，再决定是现在给出结论，还是写一个真正未来的复核时间。"
            ),
            "breakout_watch_deadline_stale": (
                "纠错指南：如果 breakout_watch 的 confirm_deadline 已经在 retry 时刻之前，就不要继续把旧 deadline 当成未来目标。"
                "请基于刷新后的 range-plan 状态先判断旧观察窗是否已经到期，再决定是直接下结论还是设置新的未来复核窗口。"
            ),
            "retry_side_effect_blocked": (
                "纠错指南：当前 retry 是合同/记忆修复模式，不允许再调用会改变账户、订单、仓位、range plan 或闹钟状态的副作用工具。"
                "只修正 JSON 字段、declared_* 和 memory patch；必要时只做轻量查询。"
            ),
        }
        guide = specific_guides.get(
            error_class,
            (
                "纠错指南：阅读 why_rejected 后只修正被点名的合同、参数或顺序问题。"
                "不要盲目重复同一个工具调用；如果错误来自交易所或风险守卫且 retryable=false，"
                "应停止本轮执行，转为 blocked/wait，并写清下一轮需要验证的条件。"
            ),
        )
        details = []
        if why_rejected:
            details.append(f"拒绝原因：{why_rejected}")
        if model_fix_hint:
            details.append(f"修正提示：{model_fix_hint}")
        if related_tool_call:
            details.append(f"关联工具调用：{related_tool_call}")
        if details:
            return guide + "\n" + "\n".join(details)
        return guide

    def _extract_attempt_rejected_calls(self, audit_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
        attempt = dict(audit_meta.get("current_guard_attempt", {}))
        return list(attempt.get("rejected_tool_calls", []))

    def _set_guard_failure(
        self,
        audit_meta: Dict[str, Any],
        *,
        node_name: str,
        rule_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        audit_meta["guard_failure"] = dict(payload)
        audit_meta["guard_failure"]["natural_language_guide"] = self._guard_natural_language_guide(audit_meta["guard_failure"])
        audit_meta["last_guard_failed_node"] = node_name
        audit_meta["last_guard_failed_rule"] = rule_id
        audit_meta = self._append_guard_pipeline_event(
            audit_meta,
            node="guard",
            subguard=node_name,
            status="fail",
            rule_id=rule_id,
            error_class=str(payload.get("error_class", "")),
            why_rejected=str(payload.get("why_rejected", "")),
            model_fix_hint=str(payload.get("model_fix_hint", "")),
            retryable=bool(payload.get("retryable", False)),
            defer_until_next_turn=bool(payload.get("defer_until_next_turn", False)),
            tool_ref=str(payload.get("related_tool_call", "")),
        )
        return audit_meta

    def _make_guard_reject_payload(
        self,
        *,
        node_name: str,
        rule_id: str,
        matched_call: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if matched_call:
            payload = {
                "error_class": str(matched_call.get("error_class", rule_id)),
                "why_rejected": str(matched_call.get("why_rejected", "")),
                "model_fix_hint": str(matched_call.get("model_fix_hint", "")),
                "retryable": bool(matched_call.get("retryable", False)),
                "defer_until_next_turn": bool(matched_call.get("defer_until_next_turn", False)),
                "source_rule": str(matched_call.get("source_rule", matched_call.get("error_class", rule_id))),
                "related_tool_call": str(matched_call.get("tool_call_id", "")),
                "node": "guard",
                "subguard": node_name,
            }
            payload["natural_language_guide"] = self._guard_natural_language_guide(payload)
            return payload
        return {
            "error_class": rule_id,
            "why_rejected": "Strategy guard rejected the current execute_primary batch.",
            "model_fix_hint": "Use the guard feedback to adjust plan/actions in the next execute attempt.",
            "retryable": False,
            "defer_until_next_turn": True,
            "source_rule": rule_id,
            "related_tool_call": "",
            "node": "guard",
            "subguard": node_name,
        }

    def _evaluate_strategy_guard(
        self,
        *,
        rule_id: str,
        node_name: str,
        audit_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        decision_failure = audit_meta.get("decision_validation_failure")
        if isinstance(decision_failure, dict):
            payload = dict(decision_failure)
            payload.setdefault("node", "guard")
            payload.setdefault("subguard", node_name)
            payload.setdefault("retryable", True)
            payload.setdefault("defer_until_next_turn", False)
            return {"status": "fail", "payload": payload}

        if rule_id == "precheck_gate_violation":
            if not audit_meta.get("precheck"):
                payload = {
                    "error_class": "precheck_gate_violation",
                    "why_rejected": "Precheck gate violation: execute stage requires completed precheck trio in the same wakeup.",
                    "model_fix_hint": "Do not execute trade/management actions before precheck trio is available.",
                    "retryable": False,
                    "defer_until_next_turn": True,
                    "source_rule": rule_id,
                    "related_tool_call": "",
                    "node": node_name,
                }
                return {"status": "fail", "payload": payload}
            return {"status": "pass"}

        if rule_id == "capacity_refresh_chain":
            if audit_meta.get("pending_capacity_refresh"):
                return {"status": "pass"}
            return {"status": "skip"}

        if rule_id == "opening_sequence_tracking":
            if audit_meta.get("opening_sequence_consumed"):
                return {"status": "pass"}
            return {"status": "skip"}

        if rule_id == "verify_required_tracking":
            if audit_meta.get("verify_required"):
                return {"status": "pass"}
            return {"status": "skip"}

        return {"status": "skip"}

    def _evaluate_error_class_guard(
        self,
        *,
        rule_id: str,
        node_name: str,
        audit_meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        rejected_calls = self._extract_attempt_rejected_calls(audit_meta)
        if not rejected_calls:
            return {"status": "skip"}

        for item in rejected_calls:
            if str(item.get("error_class", "")).strip() == rule_id:
                payload = self._make_guard_reject_payload(
                    node_name=node_name,
                    rule_id=rule_id,
                    matched_call=item,
                )
                return {"status": "fail", "payload": payload}
        return {"status": "pass"}

    def _node_guard(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "guard")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "guard")
        audit_meta.pop("guard_failure", None)

        strategy_rule_ids = {item[1] for item in EXECUTE_PRIMARY_STRATEGY_GUARD_RULES}
        for spec in self.guard_node_specs:
            node_name = spec["node"]
            rule_id = spec["rule_id"]
            if rule_id in strategy_rule_ids:
                outcome = self._evaluate_strategy_guard(
                    rule_id=rule_id,
                    node_name=node_name,
                    audit_meta=audit_meta,
                )
            else:
                outcome = self._evaluate_error_class_guard(
                    rule_id=rule_id,
                    node_name=node_name,
                    audit_meta=audit_meta,
                )

            status = str(outcome.get("status", "skip"))
            payload = outcome.get("payload")
            if status == "fail" and isinstance(payload, dict):
                audit_meta = self._set_guard_failure(
                    audit_meta,
                    node_name=node_name,
                    rule_id=rule_id,
                    payload=payload,
                )
                return {
                    "transition_state": "guard_reject_router",
                    "audit_meta": audit_meta,
                }

            audit_meta = self._append_guard_pipeline_event(
                audit_meta,
                node="guard",
                subguard=node_name,
                status="pass" if status == "pass" else "skip",
                rule_id=rule_id,
                error_class=rule_id,
            )

        return {
            "transition_state": "execute_primary_guards_decision",
            "audit_meta": audit_meta,
        }

    def _build_guard_feedback_text(self, audit_meta: Dict[str, Any]) -> str:
        failure = dict(audit_meta.get("guard_failure", {}))
        if not failure:
            return ""
        rejected_calls = self._extract_attempt_rejected_calls(audit_meta)
        parts = [
            "[GUARD FEEDBACK]",
            f"error_class: {failure.get('error_class', '')}",
            f"source_rule: {failure.get('source_rule', '')}",
            f"why_rejected: {failure.get('why_rejected', '')}",
            f"model_fix_hint: {failure.get('model_fix_hint', '')}",
            f"natural_language_guide: {failure.get('natural_language_guide', self._guard_natural_language_guide(failure))}",
            f"retryable: {failure.get('retryable', False)}",
            f"defer_until_next_turn: {failure.get('defer_until_next_turn', False)}",
        ]
        additional_failures = []
        primary_error_class = str(failure.get("error_class", "") or "").strip()
        primary_why = str(failure.get("why_rejected", "") or "").strip()
        for item in list(audit_meta.get("decision_validation_failures_all", [])):
            if not isinstance(item, dict):
                continue
            error_class = str(item.get("error_class", "") or "").strip()
            why_rejected = str(item.get("why_rejected", "") or "").strip()
            if error_class == primary_error_class and why_rejected == primary_why:
                continue
            additional_failures.append(item)
        if additional_failures:
            parts.append("additional_detected_issues:")
            for item in additional_failures[:6]:
                parts.append(
                    "- "
                    f"error={item.get('error_class', '')} "
                    f"why={item.get('why_rejected', '')} "
                    f"fix={item.get('model_fix_hint', '')}"
                )
            parts.append(
                "repair_rule: In the next retry, fix the primary failure and every additional_detected_issues item in the same draft before stopping."
            )
        if rejected_calls:
            parts.append("rejected_tool_calls:")
            for item in rejected_calls[:8]:
                parts.append(
                    "- "
                    f"id={item.get('tool_call_id', '')} tool={item.get('tool', '')} "
                    f"error={item.get('error_class', '')} why={item.get('why_rejected', '')} "
                    f"fix={item.get('model_fix_hint', '')}"
                )
        return "\n".join(parts)

    def _run_precheck(
        self,
        state: DecisionState,
        stage_name: str,
        audit_key: str,
        next_transition: str,
        prompt_header: str,
        precheck_source: str,
    ) -> Dict[str, Any]:
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, stage_name)
        token = self._with_tool_stage(stage_name)
        try:
            symbol = "ETHUSDT"
            pos = get_usdt_futures_position(symbol)
            acct = get_usdt_futures_account()
            max_open = get_usdt_futures_max_open_position(symbol)
            open_orders = get_usdt_futures_open_orders(symbol)
        finally:
            reset_tool_stage(token)

        audit_meta[audit_key] = {
            "symbol": symbol,
            "position": pos,
            "account": acct,
            "max_open": max_open,
            "open_orders": open_orders,
            "completed_at": datetime.now().isoformat(),
        }
        audit_meta[audit_key]["sideways_range_eligibility"] = self._build_sideways_range_eligibility(audit_meta[audit_key])
        audit_meta[audit_key]["sideways_range_snapshot"] = self._build_sideways_range_opportunity_snapshot(state, audit_meta[audit_key])
        audit_meta[audit_key]["sideways_range_priority"] = self._build_sideways_range_proposal_priority(state, audit_meta[audit_key])
        audit_meta["precheck_source"] = precheck_source
        if precheck_source == "refreshed":
            audit_meta["pending_capacity_refresh"] = False

        precheck_sections: List[str] = [
            _format_tool_result_for_prompt("get_usdt_futures_position", pos),
            _format_tool_result_for_prompt("get_usdt_futures_account", acct),
            _format_tool_result_for_prompt("get_usdt_futures_max_open_position", max_open),
        ]
        fill_attribution = self._build_fill_attribution_context(
            str(state.get("event_details", audit_meta.get("event_details", ""))),
            audit_meta[audit_key],
            state.get("short_memory_snapshot_obj", {}),
        )
        if fill_attribution.get("text"):
            audit_meta["fill_attribution"] = fill_attribution.get("items", [])
            precheck_sections.append("[FILL ATTRIBUTION]\n" + str(fill_attribution["text"]))
        if precheck_source == "initial":
            precheck_sections.append(_format_tool_result_for_prompt("get_usdt_futures_open_orders", open_orders))
        precheck_sections.append(self._render_sideways_range_eligibility_audit(audit_meta[audit_key]))
        sideways_priority_note = self._render_sideways_range_proposal_priority(audit_meta[audit_key])
        if sideways_priority_note:
            precheck_sections.append(sideways_priority_note)
        precheck_sections.append(self._render_sideways_range_opportunity_snapshot(state, audit_meta[audit_key]))
        followup_runtime_refresh = ""
        if precheck_source == "refreshed":
            pending_alarms_snapshot = self._format_pending_alarms()
            audit_meta["pending_alarms_snapshot_refreshed"] = pending_alarms_snapshot
            audit_meta["pending_alarms_snapshot"] = pending_alarms_snapshot
            primary_delta = self._build_stage_tool_delta_summary(
                state,
                stage_names=["execute_primary_model", "execute_primary_retry"],
            )
            followup_runtime_refresh = "\n\n".join(
                [
                    _format_tool_result_for_prompt("get_usdt_futures_open_orders", open_orders),
                    "[PENDING ALARMS REFRESHED]\n" + pending_alarms_snapshot,
                    "[PRIMARY EXECUTION DELTA]\n" + primary_delta,
                ]
            )
            precheck_sections.append("[POST-PRIMARY RUNTIME REFRESH]\n" + followup_runtime_refresh)

        precheck_summary = "\n\n".join(precheck_sections)

        updates: Dict[str, Any] = {
            "transition_state": next_transition,
            "audit_meta": audit_meta,
        }
        if precheck_source == "initial":
            updates["precheck_summary"] = precheck_summary
        else:
            updates["refreshed_precheck_summary"] = precheck_summary
            updates["pending_alarms_snapshot"] = str(audit_meta.get("pending_alarms_snapshot_refreshed", state.get("pending_alarms_snapshot", "No pending alarms.")))
        updates["user_prompt"] = state.get("user_prompt", "") + f"\n\n[{prompt_header}]\n" + precheck_summary
        return updates

    def _node_observe(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "observe")
        combined_type, combined_content = self._compose_event_context(state)
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "observe")
        audit_meta["observe_at"] = datetime.now().isoformat()
        audit_meta["event_type"] = combined_type
        audit_meta["event_details"] = combined_content
        return {
            "transition_state": "load_binance_account",
            "event_type": combined_type,
            "event_details": combined_content,
            "audit_meta": audit_meta,
        }

    def _node_load_binance_account(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "load_binance_account")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "load_binance_account")
        full_market_snapshot = self._read_text_file(BINANCE_REPORT_PATH, "No Binance data available.")
        market_snapshot = self._build_short_term_market_snapshot(full_market_snapshot)
        account_snapshot = self._read_text_file(BINANCE_ACCOUNT_PATH, "No Account data available.")
        audit_meta["context_binance_account_at"] = datetime.now().isoformat()
        return {
            "transition_state": "load_news",
            "full_market_snapshot": full_market_snapshot,
            "market_snapshot": market_snapshot,
            "account_snapshot": account_snapshot,
            "audit_meta": audit_meta,
        }

    def _node_load_news(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "load_news")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "load_news")
        try:
            news_snapshot, news_ids = news_protocol_func(10)
        except Exception as e:
            news_snapshot = f"News source unavailable: {type(e).__name__}: {e}"
            news_ids = []
        audit_meta["context_news_at"] = datetime.now().isoformat()
        audit_meta["news_ids"] = news_ids
        return {
            "transition_state": "load_polymarket",
            "news_snapshot": news_snapshot,
            "audit_meta": audit_meta,
        }

    def _node_load_polymarket(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "load_polymarket")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "load_polymarket")
        try:
            poly_snapshot, _ = poly_protocol_func(20)
        except Exception as e:
            poly_snapshot = f"Polymarket source unavailable: {type(e).__name__}: {e}"
        audit_meta["context_polymarket_at"] = datetime.now().isoformat()
        return {
            "transition_state": "load_memory",
            "poly_snapshot": poly_snapshot,
            "audit_meta": audit_meta,
        }

    def _node_load_memory(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "load_memory")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "load_memory")
        memory_snapshots = self._load_memory_snapshots()
        pending_alarms_snapshot = self._format_pending_alarms()
        shortmemory_prompt_snapshot = self._render_short_memory_projection_for_prompt(
            memory_snapshots["short_obj"],
            pending_alarms_snapshot,
        )
        audit_meta["context_memory_at"] = datetime.now().isoformat()
        audit_meta["pending_alarms_snapshot"] = pending_alarms_snapshot
        return {
            "transition_state": "compose_prompt",
            "experience_snapshot": memory_snapshots["long_text"],
            "shortmemory_snapshot": shortmemory_prompt_snapshot,
            "long_memory_snapshot_obj": memory_snapshots["long_obj"],
            "short_memory_snapshot_obj": memory_snapshots["short_obj"],
            "pending_alarms_snapshot": pending_alarms_snapshot,
            "audit_meta": audit_meta,
        }

    def _node_compose_prompt(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "compose_prompt")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "compose_prompt")
        event_type = str(state.get("event_type", audit_meta.get("event_type", "unknown")))
        event_details = str(state.get("event_details", audit_meta.get("event_details", "")))
        system_prompt, user_prompt = self.build_prompt(
            event_type,
            event_details,
            str(state.get("market_snapshot", "No Binance data available.")),
            str(state.get("account_snapshot", "No Account data available.")),
            str(state.get("news_snapshot", "No gated news available.")),
            str(state.get("poly_snapshot", "No gated Polymarket events available.")),
            str(state.get("shortmemory_snapshot", "No active short-term memory.")),
            str(state.get("pending_alarms_snapshot", "No pending alarms.")),
            self._load_range_plan_snapshot_text(),
            str(state.get("experience_snapshot", "No historical experience yet.")),
        )
        self._persist_prompt_input_snapshot(
            str(state.get("interaction_log_dir", "") or ""),
            system_prompt,
            user_prompt,
        )
        audit_meta["prompt_compose_at"] = datetime.now().isoformat()
        return {
            "transition_state": "precheck",
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "event_details": event_details,
            "audit_meta": audit_meta,
        }

    def _node_precheck(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "precheck")
        return self._run_precheck(
            state,
            stage_name="precheck",
            audit_key="precheck",
            next_transition="propose",
            prompt_header="PRECHECK RESULTS",
            precheck_source="initial",
        )

    def _node_propose(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "propose")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "propose")
        precheck_payload = audit_meta.get("precheck", {})
        if not isinstance(precheck_payload, dict):
            precheck_payload = {}
        sideways_priority = precheck_payload.get("sideways_range_priority", {})
        if not isinstance(sideways_priority, dict):
            sideways_priority = {}
        stage_instruction = (
            "You are in propose stage. Build a plan and collect any missing read-only evidence. "
            "Do not schedule alarms, place trades, or mutate account/order state."
        )
        if sideways_priority.get("available"):
            stage_instruction += (
                " Runtime precheck says this wakeup is a high-priority sideways harvesting candidate "
                "(preferred_range_action=start_preferred with clean eligibility). "
                "Before extending any observe-only path, explicitly compare pure waiting versus range harvesting. "
                "Treat prior wait/observe memory as continuity context, not as a default veto over this runtime opportunity."
            )

        payload = {
            "messages": [
                {"role": "system", "content": state.get("system_prompt", "")},
                {
                    "role": "user",
                    "content": state.get("user_prompt", "")
                    + "\n\n[STAGE]\n"
                    + stage_instruction,
                },
            ]
        }
        token = self._with_tool_stage("propose")
        try:
            result = self.propose_agent.invoke(payload)
        finally:
            reset_tool_stage(token)
        proposal_text = _extract_text_from_agent_output(result)

        audit_meta["proposal_text"] = proposal_text

        return {
            "transition_state": "execute_primary_model",
            "audit_meta": audit_meta,
        }

    def _hydrate_decision_output(self, decision: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
        hydrated = dict(decision or {})
        raw_decision_keys = sorted([str(key) for key in hydrated.keys()])
        for key in DecisionOutput.model_fields.keys():
            if key.endswith("_ops") or key == "tool_intents":
                hydrated.setdefault(key, [])
            else:
                hydrated.setdefault(key, "")
        known_decision_keys = set(DecisionOutput.model_fields.keys())
        unknown_decision_fields = sorted([key for key in raw_decision_keys if key not in known_decision_keys])
        return hydrated, unknown_decision_fields

    def _invoke_execute_contract_stage(
        self,
        *,
        state: DecisionState,
        stage_name: str,
        user_content: str,
    ) -> Dict[str, Any]:
        audit_meta = dict(state.get("audit_meta", {}))
        payload = {
            "messages": [
                {"role": "system", "content": state.get("system_prompt", "")},
                {
                    "role": "user",
                    "content": user_content
                    + "\n\n[STRUCTURED CONTRACT DRAFT]\n"
                    + "Draft the final structured decision contract first. "
                    + "This draft stage has no tool access, so do not claim completed fills, orders, or alarm ids that require execution-stage confirmation.",
                },
            ]
        }
        used_compat_fallback = False
        token = self._with_tool_stage(f"{stage_name}_contract")
        try:
            try:
                result = self.execute_contract_agent.invoke(payload)
            except Exception as e:
                if not _is_tool_choice_thinking_mode_error(e):
                    raise
                used_compat_fallback = True
                result = self.execute_contract_agent_compat.invoke(payload)
        finally:
            reset_tool_stage(token)

        structured = result.get("structured_response")
        if isinstance(structured, DecisionOutput):
            decision = structured.model_dump()
        elif isinstance(structured, dict):
            decision = structured
        else:
            raw_text = _extract_text_from_agent_output(result)
            recovered = _recover_json_dict(raw_text, f"{stage_name} contract JSON")
            if isinstance(recovered, dict):
                decision = recovered
            else:
                decision = DecisionOutput(
                    execution_txt=raw_text,
                    explanation="",
                    memory_management_reasoning="",
                    decision_basis="",
                    conflict_check="",
                    falsification_point="",
                    next_alarm_reason="",
                    state_change_evidence="",
                    short_memory_ops=[],
                    long_memory_ops=[],
                    experience="",
                    shortterm="",
                ).model_dump()
        raw_text = _extract_text_from_agent_output(result)
        decision, unknown_decision_fields = self._hydrate_decision_output(decision)
        decision = self._apply_deterministic_runtime_alignment_repairs(
            decision,
            audit_meta=audit_meta,
        )
        validation_failure = self._validate_decision_schema_fields(decision)
        if validation_failure is None:
            validation_failure = self._validate_memory_patch_decision(
                decision,
                audit_meta=audit_meta,
            )
        return {
            "decision": decision,
            "raw_text": raw_text,
            "unknown_decision_fields": unknown_decision_fields,
            "validation_failure": validation_failure,
            "agent_mode": "compat_fallback" if used_compat_fallback else "structured",
            "captured_at": datetime.now().isoformat(),
        }

    def _merge_execute_decision_with_contract(
        self,
        *,
        approved_contract: Dict[str, Any],
        runtime_decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        merged = deepcopy(approved_contract)
        runtime_execution_txt = str(runtime_decision.get("execution_txt", "") or "").strip()
        runtime_explanation = str(runtime_decision.get("explanation", "") or "").strip()
        if runtime_execution_txt:
            merged["execution_txt"] = runtime_execution_txt
        if runtime_explanation:
            merged["explanation"] = runtime_explanation
        for field_name in DECISION_CONTRACT_PRESERVED_FIELDS:
            merged[field_name] = deepcopy(approved_contract.get(field_name))
        return merged

    def _validate_execute_contract_runtime_alignment(
        self,
        *,
        approved_contract: Dict[str, Any],
        new_calls: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not approved_contract:
            return None
        action_intent = str(approved_contract.get("action_intent", "") or "").strip()
        declared_effect_tools = {
            str(item.get("tool", "")).strip()
            for item in list(approved_contract.get("tool_intents", []) or [])
            if isinstance(item, dict) and str(item.get("tool", "")).strip()
        }
        actual_effect_tools = {
            str(call.get("tool", "")).strip()
            for call in new_calls
            if self._tool_call_has_effect(call)
            and str(call.get("tool", "")).strip() in RETRY_SIDE_EFFECT_BLOCKED_TOOLS
        }
        if action_intent == "observe" and actual_effect_tools:
            return {
                "error_class": "approved_contract_runtime_mismatch",
                "why_rejected": (
                    f"Approved contract declared action_intent=observe, but execute stage ran side-effect tools: "
                    f"{', '.join(sorted(actual_effect_tools))}."
                ),
                "model_fix_hint": (
                    "批准合同是 observe 时，runtime 阶段不得调用副作用工具。"
                    "如果确实需要执行，请在无工具的 contract draft 里先把 action_intent 改为正确动作，"
                    "并在 tool_intents 声明每个副作用工具；否则删除这些工具调用。"
                ),
                "retryable": False,
                "defer_until_next_turn": True,
                "source_rule": "approved_contract_runtime_mismatch",
                "related_tool_call": "",
            }
        if action_intent == "schedule_wait":
            non_wait_tools = sorted(actual_effect_tools - {"set_alarm", "delete_alarm"})
            if non_wait_tools:
                return {
                    "error_class": "approved_contract_runtime_mismatch",
                    "why_rejected": (
                        "Approved contract declared schedule_wait, but execute stage ran non-alarm side-effect tools: "
                        + ", ".join(non_wait_tools)
                    ),
                    "model_fix_hint": (
                        "action_intent=schedule_wait 只允许 set_alarm/delete_alarm。"
                        "如果要交易、撤单、改单、平仓或启动/取消 range plan，先在 contract draft 中改 action_intent，"
                        "并把对应工具加入 tool_intents；否则不要调用这些工具。"
                    ),
                    "retryable": False,
                    "defer_until_next_turn": True,
                    "source_rule": "approved_contract_runtime_mismatch",
                    "related_tool_call": "",
                }
        if declared_effect_tools:
            undeclared_effect_tools = sorted(actual_effect_tools - declared_effect_tools)
            if undeclared_effect_tools:
                declared_text = ", ".join(sorted(declared_effect_tools)) or "none"
                actual_text = ", ".join(sorted(actual_effect_tools)) or "none"
                return {
                    "error_class": "approved_contract_runtime_mismatch",
                    "why_rejected": (
                        "Execute stage produced side-effect tools outside approved tool_intents: "
                        + ", ".join(undeclared_effect_tools)
                        + f". approved_tool_intents={declared_text}; actual_side_effect_tools={actual_text}."
                    ),
                    "model_fix_hint": (
                        "副作用工具必须先在无工具 contract draft 的 tool_intents 中逐项批准。"
                        "修复方式二选一：1) 如果这些工具确实必要，把 tool_intents 增加同名 tool 和 purpose，"
                        "并同步 action_intent；2) 如果不是必要动作，删除 runtime 阶段这些未批准工具调用。"
                        "不要在 retry 中重复已提交的副作用，只修正合同与记忆。"
                    ),
                    "retryable": False,
                    "defer_until_next_turn": True,
                    "source_rule": "approved_contract_runtime_mismatch",
                    "related_tool_call": "",
                }
        return None

    def _invoke_execute_stage(
        self,
        state: DecisionState,
        stage_name: str,
        stage_instruction: str,
        proposal_context: str,
        next_transition_on_complete: str,
        guard_feedback: str = "",
    ) -> Dict[str, Any]:
        audit_meta = dict(state.get("audit_meta", {}))

        tool_results = state.get("tool_results", [])
        user_content = (
            state.get("user_prompt", "")
            + "\n\n[STAGE]\n"
            + stage_instruction
            + "\n\n"
            + "[PROPOSAL CONTEXT]\n"
            + proposal_context
        )
        if stage_name in {"execute_primary_model", "execute_primary_retry"}:
            refreshed_pending_alarms = self._format_pending_alarms()
            state["pending_alarms_snapshot"] = refreshed_pending_alarms
            audit_meta["pending_alarms_snapshot"] = refreshed_pending_alarms
            user_content += "\n\n" + self._build_execute_runtime_refresh_section(stage_name=stage_name)
        if guard_feedback:
            user_content += "\n\n" + guard_feedback
        if stage_name == "execute_primary_retry":
            retry_runtime_refresh = self._build_retry_runtime_refresh_section(state)
            user_content += "\n\n[RETRY RUNTIME REFRESH]\n" + retry_runtime_refresh
            audit_meta["retry_runtime_refresh"] = retry_runtime_refresh
        payload = {
            "messages": [
                {"role": "system", "content": state.get("system_prompt", "")},
                {
                    "role": "user",
                    "content": user_content,
                },
            ]
        }
        start_idx = len(tool_results)
        contract_draft = self._invoke_execute_contract_stage(
            state=state,
            stage_name=stage_name,
            user_content=user_content,
        )
        audit_meta["approved_decision_contract"] = deepcopy(contract_draft.get("decision", {}))
        audit_meta["approved_decision_contract_mode"] = str(contract_draft.get("agent_mode", ""))
        audit_meta["approved_decision_contract_raw_text"] = str(contract_draft.get("raw_text", ""))
        audit_meta["approved_decision_contract_captured_at"] = str(contract_draft.get("captured_at", ""))

        used_compat_fallback = False
        execute_raw_text = str(contract_draft.get("raw_text", ""))
        execute_agent_mode = "contract_only"
        decision = dict(contract_draft.get("decision", {}))
        runtime_decision: Dict[str, Any] = {}
        unknown_decision_fields = list(contract_draft.get("unknown_decision_fields", []))
        decision_validation_failure = contract_draft.get("validation_failure")
        retry_mode = ""

        if decision_validation_failure is None:
            approved_contract = dict(contract_draft.get("decision", {}))
            approved_contract_json = json.dumps(approved_contract, ensure_ascii=False, indent=2)
            runtime_user_content = (
                user_content
                + "\n\n[APPROVED DECISION CONTRACT]\n"
                + approved_contract_json
                + "\n\n"
                + "You must preserve every structured field from the approved decision contract exactly. "
                + "Only execution_txt and explanation may add factual execution results from this stage. "
                + "Do not silently change enums, hypothesis metadata, declared_* values, or memory patch content."
            )
            payload = {
                "messages": [
                    {"role": "system", "content": state.get("system_prompt", "")},
                    {
                        "role": "user",
                        "content": runtime_user_content,
                    },
                ]
            }
            execute_agent = self.execute_agent
            execute_agent_compat = self.execute_agent_compat
            if stage_name == "execute_primary_retry":
                retry_mode = str(audit_meta.get("retry_mode", "") or "")
                if retry_mode == "non_side_effect_repair":
                    execute_agent = self.execute_retry_safe_agent
                    execute_agent_compat = self.execute_retry_safe_agent_compat
                if retry_mode:
                    audit_meta["retry_mode_effective"] = retry_mode
            if retry_mode != "non_side_effect_repair":
                approved_runtime_tool_names = self._approved_execute_runtime_tool_names(approved_contract)
                execute_agent, execute_agent_compat = self._get_execute_agent_pair_for_tool_names(
                    approved_runtime_tool_names
                )
                audit_meta["approved_runtime_tool_names"] = list(approved_runtime_tool_names)
            if not retry_mode:
                audit_meta.pop("retry_mode_effective", None)
            token = self._with_tool_stage(stage_name)
            try:
                try:
                    result = execute_agent.invoke(payload)
                except Exception as e:
                    if not _is_tool_choice_thinking_mode_error(e):
                        raise
                    used_compat_fallback = True
                    audit_meta["execute_fallback_reason"] = str(e)
                    print(
                        "⚠️ execute_agent structured response incompatible with provider thinking mode; "
                        "falling back to compat execute agent.",
                        flush=True,
                    )
                    result = execute_agent_compat.invoke(payload)
            finally:
                reset_tool_stage(token)

            structured = result.get("structured_response")
            if isinstance(structured, DecisionOutput):
                runtime_decision = structured.model_dump()
            elif isinstance(structured, dict):
                runtime_decision = structured
            else:
                raw_text = _extract_text_from_agent_output(result)
                recovered = _recover_json_dict(raw_text, "Execute Decision JSON")
                if isinstance(recovered, dict):
                    runtime_decision = recovered
                else:
                    runtime_decision = DecisionOutput(
                        execution_txt=raw_text,
                        explanation="",
                        memory_management_reasoning="",
                        decision_basis="",
                        conflict_check="",
                        falsification_point="",
                        next_alarm_reason="",
                        state_change_evidence="",
                        short_memory_ops=[],
                        long_memory_ops=[],
                        experience="",
                        shortterm="",
                    ).model_dump()

            runtime_decision, runtime_unknown_decision_fields = self._hydrate_decision_output(runtime_decision)
            unknown_decision_fields = runtime_unknown_decision_fields
            decision = self._merge_execute_decision_with_contract(
                approved_contract=approved_contract,
                runtime_decision=runtime_decision,
            )
            execute_raw_text = _extract_text_from_agent_output(result)
            execute_agent_mode = "compat_fallback" if used_compat_fallback else "structured"
        else:
            audit_meta.pop("retry_mode_effective", None)

        audit_meta["decision_unparsed_fields"] = unknown_decision_fields
        new_calls = list(tool_results[start_idx:])
        retry_duplicate_warnings: List[Dict[str, Any]] = []
        if stage_name == "execute_primary_retry":
            retry_duplicate_warnings = self._collect_retry_duplicate_action_warnings(
                state,
                start_idx=start_idx,
                new_calls=new_calls,
            )
            if retry_duplicate_warnings:
                existing_retry_warnings = list(audit_meta.get("retry_warnings", []))
                existing_retry_warnings.extend(retry_duplicate_warnings)
                audit_meta["retry_warnings"] = existing_retry_warnings
        pending_alarms_snapshot = str(state.get("pending_alarms_snapshot", "No pending alarms."))
        refresh_reasons = self._needs_capacity_refresh(new_calls) if stage_name in {"execute_primary_model", "execute_primary_retry"} else []
        if decision_validation_failure is None:
            decision_validation_failure = self._validate_execute_contract_runtime_alignment(
                approved_contract=dict(contract_draft.get("decision", {})),
                new_calls=new_calls,
            )
        if decision_validation_failure is None:
            decision_validation_failure = self._validate_retry_no_side_effect_actions(
                stage_name=stage_name,
                audit_meta=audit_meta,
                calls=new_calls,
            )
        if decision_validation_failure is None and stage_name == "execute_followup":
            decision_validation_failure = self._validate_followup_action_dedup(
                state,
                start_idx=start_idx,
                new_calls=new_calls,
            )
        if decision_validation_failure is None and stage_name in {"execute_primary_model", "execute_primary_retry", "execute_followup"}:
            decision_validation_failure = self._validate_hard_risk_compression_requirement(
                audit_meta,
                decision,
                new_calls,
            )
        if decision_validation_failure is None:
            decision_validation_failure = self._validate_sideways_range_decision_gate(
                state=state,
                stage_name=stage_name,
                audit_meta=audit_meta,
                decision=decision,
                new_calls=new_calls,
            )
        should_validate_alarm_semantics = not (
            stage_name in {"execute_primary_model", "execute_primary_retry"} and refresh_reasons
        )
        if decision_validation_failure is None and should_validate_alarm_semantics:
            alarm_semantics_decision = self._overlay_runtime_alarm_semantics_fields(
                merged_decision=decision,
                runtime_decision=runtime_decision,
            )
            decision_validation_failure = self._validate_decision_alarm_semantics(
                alarm_semantics_decision,
                pending_alarms=pending_alarms_snapshot,
                calls=new_calls,
            )
        audit_meta["decision_validation_failures_all"] = self._collect_attempt_validation_failures(
            stage_name=stage_name,
            state=state,
            audit_meta=audit_meta,
            decision=self._overlay_runtime_alarm_semantics_fields(
                merged_decision=decision,
                runtime_decision=runtime_decision,
            ),
            new_calls=new_calls,
            pending_alarms_snapshot=pending_alarms_snapshot,
            refresh_reasons=refresh_reasons,
            primary_failure=decision_validation_failure if isinstance(decision_validation_failure, dict) else None,
        )
        audit_meta["decision_validation_failure"] = decision_validation_failure
        rejected_in_attempt = self._collect_effective_rejected_tool_calls(new_calls)

        audit_meta["execute_raw_text"] = execute_raw_text
        audit_meta["execute_agent_mode"] = execute_agent_mode
        if stage_name in {"execute_primary_model", "execute_primary_retry"}:
            audit_meta["pending_capacity_refresh"] = bool(refresh_reasons)
            audit_meta["capacity_refresh_reason"] = list(refresh_reasons)
        else:
            audit_meta["pending_capacity_refresh"] = False
        guard_retry = dict(audit_meta.get("guard_retry", {}))
        attempt_index = int(guard_retry.get("attempt_index", 0) or 0)
        audit_meta["current_guard_attempt"] = {
            "attempt_index": attempt_index,
            "stage": stage_name,
            "tool_count": len(new_calls),
            "tool_call_ids": [str(call.get("id", "")) for call in new_calls],
            "rejected_tool_calls": rejected_in_attempt,
            "captured_at": datetime.now().isoformat(),
        }
        audit_meta["last_guard_feedback"] = guard_feedback or ""
        audit_meta = self._mark_verify_required(audit_meta, new_calls)
        audit_meta = self._append_rejected_tool_calls(audit_meta, new_calls)
        audit_meta = self._append_opening_sequence_state(audit_meta, new_calls)
        audit_meta = self._append_execute_pass(
            audit_meta,
            stage_name=stage_name,
            calls=new_calls,
            agent_mode=execute_agent_mode,
            raw_text=execute_raw_text,
            refresh_reasons=refresh_reasons,
        )
        attempt_records = list(audit_meta.get("attempt_records", []))
        attempt_records.append(
            {
                "attempt_index": attempt_index,
                "stage": stage_name,
                "mode": execute_agent_mode,
                "retry_mode": str(audit_meta.get("retry_mode_effective", "") or ""),
                "decision_validation_failure": dict(decision_validation_failure) if isinstance(decision_validation_failure, dict) else None,
                "decision_unparsed_fields": list(unknown_decision_fields),
                "tool_count": len(new_calls),
                "tool_names": [str(call.get("tool", "")) for call in new_calls],
                "tool_call_ids": [str(call.get("id", "")) for call in new_calls],
                "retry_duplicate_warnings": list(retry_duplicate_warnings),
                "completed_at": datetime.now().isoformat(),
            }
        )
        audit_meta["attempt_records"] = attempt_records

        return {
            "transition_state": next_transition_on_complete,
            "decision": decision,
            "audit_meta": audit_meta,
        }

    def _node_execute_primary_model(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "execute_primary_model")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "execute_primary_model")
        guard_retry = dict(audit_meta.get("guard_retry", {}))
        guard_retry.setdefault("attempt_index", 0)
        guard_retry.setdefault("attempt_limit", EXECUTE_PRIMARY_RETRY_LIMIT)
        guard_retry.setdefault("stopped_reason", "")
        guard_retry.setdefault("next_action", "")
        audit_meta["guard_retry"] = guard_retry
        audit_meta["validation_status"] = "guards_passed"
        state = dict(state)
        state["audit_meta"] = audit_meta
        proposal_text = str(audit_meta.get("proposal_text", ""))
        return self._invoke_execute_stage(
            state,
            stage_name="execute_primary_model",
            stage_instruction=(
                "You are in execute_primary. "
                "The fixed precheck trio has already been completed and injected into this wakeup. "
                "Do not call get_usdt_futures_position, get_usdt_futures_account, or get_usdt_futures_max_open_position. "
                "If live execution refresh conflicts with older prompt snapshots, trust the refresh. "
                "All alarm creation/deletion must happen in execute stages, not in propose. "
                "For protective STOP_MARKET/TAKE_PROFIT_MARKET orders, choose one legal shape only: "
                "full-close protection uses close_position=true with quantity omitted; "
                "partial protection uses quantity with close_position=false. "
                "If you perform any capacity-changing action "
                "(transfer_to_usdt_futures / set_usdt_futures_leverage / set_usdt_futures_margin_type / "
                "cancel_usdt_futures_order / cancel_all_usdt_futures_orders / close_usdt_futures_position), "
                "stop after that action batch. The system will refresh precheck before any capacity-dependent follow-up. "
                "Use tools as needed and return structured output."
            ),
            proposal_context=proposal_text,
            next_transition_on_complete="execute_primary_guards_entry",
        )

    def _node_execute_primary_guards_entry(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "execute_primary_guards_entry")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "execute_primary_guards_entry")
        return {
            "transition_state": "guard",
            "audit_meta": audit_meta,
        }

    def _node_guard_reject_router(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "guard_reject_router")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "guard_reject_router")
        failure = dict(audit_meta.get("guard_failure", {}))
        tool_results = list(state.get("tool_results", []))
        guard_retry = dict(audit_meta.get("guard_retry", {}))
        attempt_index = int(guard_retry.get("attempt_index", 0) or 0)
        attempt_limit = int(guard_retry.get("attempt_limit", EXECUTE_PRIMARY_RETRY_LIMIT) or EXECUTE_PRIMARY_RETRY_LIMIT)
        error_class = str(failure.get("error_class", "") or "")
        if error_class in HARD_GUARD_RETRY_ERRORS:
            attempt_limit = max(attempt_limit, HARD_GUARD_RETRY_LIMIT)
        same_error_failures = [
            item for item in list(audit_meta.get("guard_pipeline", []))
            if isinstance(item, dict)
            and str(item.get("status", "")) == "fail"
            and str(item.get("error_class", "") or "") == error_class
        ]
        same_error_retry_exhausted = bool(
            error_class
            and len(same_error_failures) >= MAX_SAME_GUARD_RETRY_ATTEMPTS
        )
        retryable = bool(failure.get("retryable", False))
        defer_until_next_turn = bool(failure.get("defer_until_next_turn", False))
        non_side_effect_retry = self._should_limit_retry_to_non_side_effects(
            failure=failure,
            calls=tool_results,
        )
        contract_repair_priority = self._should_prioritize_contract_repair(
            failure=failure,
        )

        can_retry_now = (
            retryable
            and not defer_until_next_turn
            and not same_error_retry_exhausted
            and attempt_index < attempt_limit
        )
        if can_retry_now:
            next_attempt = attempt_index + 1
            guard_retry.update(
                {
                    "attempt_index": next_attempt,
                    "attempt_limit": attempt_limit,
                    "stopped_reason": "",
                    "next_action": "execute_primary_retry",
                }
            )
            audit_meta["guard_retry"] = guard_retry
            if non_side_effect_retry:
                audit_meta["retry_mode"] = "non_side_effect_repair"
                audit_meta["retry_mode_reason"] = str(failure.get("error_class", ""))
            elif contract_repair_priority:
                audit_meta["retry_mode"] = "contract_repair_priority"
                audit_meta["retry_mode_reason"] = str(failure.get("error_class", ""))
            else:
                audit_meta.pop("retry_mode", None)
                audit_meta.pop("retry_mode_reason", None)
            audit_meta["validation_status"] = "guards_passed"
            return {
                "transition_state": "execute_primary_retry",
                "audit_meta": audit_meta,
            }

        guard_retry.update(
            {
                "attempt_index": attempt_index,
                "attempt_limit": attempt_limit,
                "stopped_reason": (
                    f"{failure.get('error_class', 'guard_blocked')}_repeat_exhausted"
                    if same_error_retry_exhausted
                    else str(failure.get("error_class", "guard_blocked"))
                ),
                "next_action": "wait_next_wakeup",
            }
        )
        audit_meta["guard_retry"] = guard_retry
        audit_meta.pop("retry_mode", None)
        audit_meta.pop("retry_mode_reason", None)
        audit_meta["validation_status"] = "guards_blocked_wait_next_turn"
        return {
            "transition_state": "verify",
            "audit_meta": audit_meta,
        }

    def _node_execute_primary_retry(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "execute_primary_retry")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "execute_primary_retry")
        state = dict(state)
        state["audit_meta"] = audit_meta
        proposal_text = str(audit_meta.get("proposal_text", ""))
        guard_feedback = self._build_guard_feedback_text(audit_meta)
        retry_mode = str(audit_meta.get("retry_mode", "") or "")
        if retry_mode == "non_side_effect_repair":
            stage_instruction = (
                "You are retrying execute_primary after a guard rejection. "
                "Primary already produced side effects in this wakeup, so retry is decision-repair only. "
                "Do not place new trade/protective/cancel/transfer/leverage actions in retry. "
                "Trust live execution refresh over older prompt snapshots. "
                "Only use lightweight verification and alarm tools when strictly needed. "
                "For short_memory_ops/long_memory_ops, JSON patch op MUST be one of add/remove/replace; never use append. "
                "If the rejection is a contract/persistence problem and retry runtime refresh did not materially change market structure, "
                "preserve the same economic decision instead of downgrading to passive observe by inertia. "
                "Repair every remaining guard contradiction in the projected short-memory snapshot before you stop; "
                "do not fix only the first mismatch and leave the next one behind."
            )
        elif retry_mode == "contract_repair_priority":
            stage_instruction = (
                "You are retrying execute_primary after a contract/guard rejection before any primary side effect was committed. "
                "Preserve the same economic decision unless retry runtime refresh materially changed the market. "
                "Fix the primary guard failure and every additional_detected_issues item in the same retry draft. "
                "Trust live execution refresh over older prompt snapshots. "
                "For protective STOP_MARKET/TAKE_PROFIT_MARKET orders, choose one legal shape only: "
                "full-close protection uses close_position=true with quantity omitted; "
                "partial protection uses quantity with close_position=false. "
                "If hypothesis_action=rollover but expiry is unchanged, normalize it to keep; only use rollover when the new expiry is strictly later. "
                "If the original confirm/recheck deadline is already past by retry time, do not preserve that stale deadline; refresh the live state and either conclude the old watch now or write a genuinely future recheck window. "
                "If your text contains a concrete future wait/recheck condition, you must actually execute set_alarm now or cite a real live Pending Alarms id. "
                "Do not leave one contract contradiction behind for the next retry."
            )
        else:
            stage_instruction = (
                "You are retrying execute_primary after a hard guard rejection. "
                "Fix the rejected shape/sequence based on guard feedback, avoid repeating the same invalid call, "
                "and continue only with legal tool actions. "
                "Trust live execution refresh over older prompt snapshots. "
                "For protective STOP_MARKET/TAKE_PROFIT_MARKET orders, choose one legal shape only: "
                "full-close protection uses close_position=true with quantity omitted; "
                "partial protection uses quantity with close_position=false. "
                "If the earlier attempt had already chosen a coherent range plan and the retry refresh does not materially change the market, "
                "repair the contract and complete the range path rather than backing away from it just to escape the guard. "
                "Guard rules are literal and cumulative: keep editing memory patches/declared fields until the projected snapshot fully aligns with runtime precheck."
            )
        return self._invoke_execute_stage(
            state,
            stage_name="execute_primary_retry",
            stage_instruction=stage_instruction,
            proposal_context=proposal_text,
            next_transition_on_complete="execute_primary_guards_entry",
            guard_feedback=guard_feedback,
        )

    def _node_execute_primary_guards_decision(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "execute_primary_guards_decision")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "execute_primary_guards_decision")
        guard_retry = dict(audit_meta.get("guard_retry", {}))
        attempt_index = int(guard_retry.get("attempt_index", 0) or 0)
        audit_meta["validation_status"] = "guards_retried_passed" if attempt_index > 0 else "guards_passed"
        if audit_meta.get("pending_capacity_refresh"):
            return {
                "transition_state": "refresh_precheck",
                "audit_meta": audit_meta,
            }
        return {
            "transition_state": "verify",
            "audit_meta": audit_meta,
        }

    def _route_after_guard_node(self, state: DecisionState) -> str:
        if state.get("transition_state") == "guard_reject_router":
            return "guard_reject_router"
        return "execute_primary_guards_decision"

    def _route_after_guard_reject_router(self, state: DecisionState) -> str:
        if state.get("transition_state") == "execute_primary_retry":
            return "execute_primary_retry"
        return "verify"

    def _route_after_execute_primary_guards_decision(self, state: DecisionState) -> str:
        if state.get("transition_state") == "refresh_precheck":
            return "refresh_precheck"
        return "verify"

    def _node_refresh_precheck(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "refresh_precheck")
        return self._run_precheck(
            state,
            stage_name="refresh_precheck",
            audit_key="refreshed_precheck",
            next_transition="execute_followup",
            prompt_header="REFRESHED PRECHECK RESULTS",
            precheck_source="refreshed",
        )

    def _node_execute_followup(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "execute_followup")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "execute_followup")
        state = dict(state)
        state["audit_meta"] = audit_meta
        proposal_text = str(audit_meta.get("proposal_text", ""))
        refresh_reason = ", ".join(audit_meta.get("capacity_refresh_reason", [])) or "capacity changed in execute_primary"
        return self._invoke_execute_stage(
            state,
            stage_name="execute_followup",
            stage_instruction=(
                "You are in execute_followup. "
                "The system has already refreshed the fixed precheck trio after capacity-changing actions: "
                f"{refresh_reason}. "
                "Do not call get_usdt_futures_position, get_usdt_futures_account, or get_usdt_futures_max_open_position. "
                "Use the refreshed precheck already injected into the prompt. "
                "The prompt also includes [POST-PRIMARY RUNTIME REFRESH] with the latest open orders and pending alarms after execute_primary. "
                "Use that refreshed runtime state first so you only perform remaining actions. "
                "Do not repeat successful execute_primary housekeeping actions (set_alarm / delete_alarm / cancel_usdt_futures_order). "
                "If an opening trade already succeeded earlier in this wakeup, do not submit another opening MARKET/LIMIT trade. "
                "Followup may only handle still-needed protection, cleanup, verification, or alarm work. "
                "Complete only the remaining capacity-dependent follow-up actions if they are still needed; "
                "otherwise return the final structured decision without extra tool calls."
            ),
            proposal_context=proposal_text,
            next_transition_on_complete="verify",
        )

    def _node_verify(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "verify")
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "verify")
        if not audit_meta.get("verify_required"):
            audit_meta["verify"] = {
                "skipped": True,
                "reason": "no post-action verification required",
                "verified_at": datetime.now().isoformat(),
            }
            return {
                "transition_state": "apply_memory_patches",
                "audit_meta": audit_meta,
            }

        token = self._with_tool_stage("verify")
        try:
            symbol = "ETHUSDT"
            pos = get_usdt_futures_position(symbol)
            acct = get_usdt_futures_account()
            open_orders = get_usdt_futures_open_orders(symbol)
        finally:
            reset_tool_stage(token)
        audit_meta["verify"] = {
            "symbol": symbol,
            "position": pos,
            "account": acct,
            "open_orders": open_orders,
            "verified_at": datetime.now().isoformat(),
        }

        return {
            "transition_state": "apply_memory_patches",
            "audit_meta": audit_meta,
        }

    def _node_apply_memory_patches(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "apply_memory_patches")
        decision = dict(state.get("decision", {}))
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "apply_memory_patches")
        validation_status = str(audit_meta.get("validation_status", "") or "")
        if validation_status == "guards_blocked_wait_next_turn":
            blocked_updates = self._prepare_guard_blocked_memory_updates(decision, audit_meta)
            self._reconcile_decision_contract_with_memory(
                decision=decision,
                current_short=self._load_or_migrate_short_memory(),
                next_short=blocked_updates["short"],
                short_ops=list(blocked_updates["short_ops"]),
            )
            decision["short_memory_snapshot"] = blocked_updates["short"]
            decision["long_memory_snapshot"] = blocked_updates["long"]
            decision["short_memory_ops"] = blocked_updates["short_ops"]
            decision["long_memory_ops"] = blocked_updates["long_ops"]
            decision["shortterm"] = ""
            decision["experience"] = ""
            audit_meta["short_memory_snapshot"] = blocked_updates["short"]
            audit_meta["long_memory_snapshot"] = blocked_updates["long"]
            audit_meta["memory_apply"] = {
                "skipped": len(blocked_updates["short_ops"]) == 0 and len(blocked_updates["long_ops"]) == 0,
                "reason": blocked_updates["reason"],
                "short_ops_count": len(blocked_updates["short_ops"]),
                "long_ops_count": len(blocked_updates["long_ops"]),
                "applied_at": datetime.now().isoformat(),
            }
            return {
                "transition_state": "persist_memory",
                "decision": decision,
                "audit_meta": audit_meta,
            }

        updated_memories = self._prepare_updated_memories(decision)
        decision["short_memory_snapshot"] = updated_memories["short"]
        decision["long_memory_snapshot"] = updated_memories["long"]
        decision["short_memory_ops"] = updated_memories["short_ops"]
        decision["long_memory_ops"] = updated_memories["long_ops"]
        decision["shortterm"] = ""
        decision["experience"] = ""

        audit_meta["short_memory_snapshot"] = updated_memories["short"]
        audit_meta["long_memory_snapshot"] = updated_memories["long"]
        audit_meta["memory_apply"] = {
            "skipped": len(updated_memories["short_ops"]) == 0 and len(updated_memories["long_ops"]) == 0,
            "reason": updated_memories.get("reason", ""),
            "short_ops_count": len(updated_memories["short_ops"]),
            "long_ops_count": len(updated_memories["long_ops"]),
            "applied_at": datetime.now().isoformat(),
        }
        return {
            "transition_state": "persist_memory",
            "decision": decision,
            "audit_meta": audit_meta,
        }

    def _node_persist_memory(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "persist_memory")
        decision = dict(state.get("decision", {}))
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "persist_memory")

        short_snapshot = decision.get("short_memory_snapshot")
        long_snapshot = decision.get("long_memory_snapshot")

        if isinstance(short_snapshot, dict):
            short_snapshot = self._prune_short_memory_noise(short_snapshot, now=datetime.now())
            decision["short_memory_snapshot"] = short_snapshot
            safe_json_dump(short_snapshot, SHORT_MEMORY_JSON_PATH, use_lock=True)
        if isinstance(long_snapshot, dict):
            safe_json_dump(long_snapshot, LONG_MEMORY_JSON_PATH, use_lock=True)

        audit_meta["memory_persist"] = {
            "short_path": SHORT_MEMORY_JSON_PATH if isinstance(short_snapshot, dict) else "",
            "long_path": LONG_MEMORY_JSON_PATH if isinstance(long_snapshot, dict) else "",
            "persisted_at": datetime.now().isoformat(),
        }
        return {
            "transition_state": "manage_or_exit",
            "decision": decision,
            "audit_meta": audit_meta,
        }

    def _node_manage_or_exit(self, state: DecisionState) -> Dict[str, Any]:
        self._assert_transition(state, "manage_or_exit")

        decision = dict(state.get("decision", {}))
        audit_meta = dict(state.get("audit_meta", {}))
        audit_meta = self._append_stage_flow(audit_meta, "manage_or_exit")

        structured_memory = self.load_structured_memory()
        structured_memory.update(
            {
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_transition_state": "manage_or_exit",
                "last_event_batch": state.get("event_batch", []),
                "last_decision": decision,
                "last_audit_meta": audit_meta,
            }
        )
        self.save_structured_memory(structured_memory)

        self.write_memory_snapshots(structured_memory)

        return {
            "transition_state": "done",
            "decision": decision,
            "audit_meta": audit_meta,
        }

    def load_structured_memory(self) -> Dict[str, Any]:
        data = safe_json_read(STRUCTURED_MEMORY_PATH, "Structured Memory", use_lock=True)
        if isinstance(data, dict):
            return data
        return {}

    def save_structured_memory(self, data: Dict[str, Any]) -> None:
        safe_json_dump(data, STRUCTURED_MEMORY_PATH, use_lock=True)

    def _load_long_pipeline_bundle(self) -> Dict[str, Any]:
        return {
            "raw_long_inputs": safe_json_read(LONG_RAW_INPUTS_PATH, "Long Raw Inputs", use_lock=True) or {},
            "kept_long_inputs": safe_json_read(LONG_KEPT_INPUTS_PATH, "Long Kept Inputs", use_lock=True) or {},
            "dropped_long_inputs": safe_json_read(LONG_DROPPED_INPUTS_PATH, "Long Dropped Inputs", use_lock=True) or {},
            "long_horizon_view": safe_json_read(LONG_HORIZON_VIEW_PATH, "Long Horizon View", use_lock=True) or {},
            "long_reflection": self._load_or_bootstrap_long_reflection(),
        }

    def write_memory_snapshots(self, structured_memory: Dict[str, Any]) -> None:
        pass

    def write_turn_context_snapshots(self, log_dir: str, state: Optional[DecisionState] = None) -> None:
        context_dir = os.path.join(log_dir, "context")
        os.makedirs(context_dir, exist_ok=True)
        bundle = self._load_long_pipeline_bundle()
        if isinstance(state, dict):
            raw_obj = state.get("raw_long_inputs_obj")
            kept_obj = state.get("kept_long_inputs_obj")
            dropped_obj = state.get("dropped_long_inputs_obj")
            long_view_obj = state.get("long_horizon_view_obj")
            long_reflection_obj = state.get("long_reflection_obj")
            if isinstance(raw_obj, dict) and raw_obj:
                bundle["raw_long_inputs"] = raw_obj
            if isinstance(kept_obj, dict) and kept_obj:
                bundle["kept_long_inputs"] = kept_obj
            if isinstance(dropped_obj, dict) and dropped_obj:
                bundle["dropped_long_inputs"] = dropped_obj
            if isinstance(long_view_obj, dict) and long_view_obj:
                bundle["long_horizon_view"] = long_view_obj
            if isinstance(long_reflection_obj, dict) and long_reflection_obj:
                bundle["long_reflection"] = long_reflection_obj

        safe_json_dump(bundle.get("long_reflection", {}), os.path.join(context_dir, "long_reflection.json"), use_lock=False)
        safe_json_dump(bundle.get("raw_long_inputs", {}), os.path.join(context_dir, "raw_long_inputs.json"), use_lock=False)
        safe_json_dump(bundle.get("kept_long_inputs", {}), os.path.join(context_dir, "kept_long_inputs.json"), use_lock=False)
        safe_json_dump(bundle.get("dropped_long_inputs", {}), os.path.join(context_dir, "dropped_long_inputs.json"), use_lock=False)
        safe_json_dump(bundle.get("long_horizon_view", {}), os.path.join(context_dir, "long_horizon_view.json"), use_lock=False)
        safe_json_dump(self._load_or_bootstrap_daily_execution_reflection(), os.path.join(context_dir, "daily_execution_reflection.json"), use_lock=False)
        safe_json_dump(self._load_or_bootstrap_post_stop_reflection(), os.path.join(context_dir, "post_stop_reflection.json"), use_lock=False)

    def load_tasks_from_file(self) -> List[Dict[str, Any]]:
        tasks = safe_json_read(TASKS_JSON_PATH, "Tasks File")
        return tasks if isinstance(tasks, list) else []

    def save_tasks_to_file(self, tasks: List[Dict[str, Any]]) -> None:
        safe_json_dump(tasks, TASKS_JSON_PATH)

    def clear_tasks_file(self) -> None:
        self.save_tasks_to_file([])

    def load_alert_history(self) -> Dict[str, Any]:
        history = safe_json_read(ALERT_HISTORY_PATH, "Alert History File")
        return history if isinstance(history, dict) else {}

    def save_alert_history(self, history: Dict[str, Any]) -> None:
        safe_json_dump(history, ALERT_HISTORY_PATH)

    def _load_range_plan_snapshot_text(self) -> str:
        plan = read_range_plan_state()
        if not isinstance(plan, dict):
            return "No active range plan."
        if str(plan.get("status", "")).strip().lower() == "active":
            return json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True)
        return "No active range plan."

    def _range_plan_central_noise_suppressed(self, event_type: str, content: str) -> bool:
        if event_type != "binance_alert":
            return False
        plan = read_range_plan_state()
        if not isinstance(plan, dict) or str(plan.get("status", "")).strip().lower() != "active":
            return False
        text = str(content or "").upper()
        if any(keyword in text for keyword in ["LIQUIDATION", "MARGIN", "RISK"]):
            return False
        try:
            lower = float(plan.get("lower_breakout", 0.0) or 0.0)
            upper = float(plan.get("upper_breakout", 0.0) or 0.0)
            last_price = float((plan.get("audit", {}) or {}).get("last_price", 0.0) or 0.0)
        except Exception:
            return False
        width = upper - lower
        if width <= 0 or last_price <= 0:
            return False
        center_low = lower + width * 0.2
        center_high = upper - width * 0.2
        return center_low < last_price < center_high

    async def add_event(self, event_type: str, content: str):
        now = datetime.now()
        content_preview = " ".join(str(content or "").split())
        if len(content_preview) > 220:
            content_preview = content_preview[:220] + "..."
        event = {
            "type": event_type,
            "content": content,
            "timestamp": now.isoformat(),
            "trigger_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

        if event_type == "self_check" and not self.is_processing:
            print(
                f"[WAKEUP_SIGNAL] type={event_type} mode=immediate trigger_at={event['trigger_at']} preview={content_preview}",
                flush=True,
            )
            self.is_processing = True
            self.pending_events = [event]
            asyncio.create_task(self.run_immediate_decision())
            return
        if event_type == "heartbeat":
            print("Ignoring heartbeat event: heartbeat is not a model wakeup trigger.", flush=True)
            return
        if self._range_plan_central_noise_suppressed(event_type, content):
            print(
                f"[RANGE_PLAN] Suppressed central-noise wakeup type={event_type} preview={content_preview}",
                flush=True,
            )
            return

        if event_type in {"order_fill", "alarm_wakeup", "daily_long_review", "range_breakout", "range_breakout_watch", "range_breakout_reverted", "range_plan_exception", "range_plan_expired"}:
            delay = 10
        elif event_type in {"whale"}:
            delay = 480
        else:
            delay = 180

        event["trigger_at"] = (now + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")

        tasks = self.load_tasks_from_file()
        tasks.append(event)
        self.save_tasks_to_file(tasks)
        print(
            f"[WAKEUP_SIGNAL] type={event_type} mode=queued delay_s={delay} trigger_at={event['trigger_at']} preview={content_preview}",
            flush=True,
        )
        await self.event_queue.put(event_type)

    async def run_immediate_decision(self):
        try:
            self.current_task = asyncio.current_task()
            await self.make_decision()
        finally:
            self.is_processing = False
            self.current_task = None
            self.pending_events = []

    async def whale_monitor(self):
        if not ENABLE_WHALE_MONITOR:
            print("Whale monitor disabled by ENABLE_WHALE_MONITOR=false", flush=True)
            return
        api_key = os.getenv("WHALE_ALERT_API_KEY", "").strip()
        if not api_key:
            print("WhaleAlert monitor disabled: missing WHALE_ALERT_API_KEY", flush=True)
            return
        try:
            min_amount_usd = float(os.getenv("WHALE_ALERT_MIN_AMOUNT_USD", "5000000"))
        except ValueError:
            min_amount_usd = 5_000_000.0
        try:
            reconnect_delay_seconds = max(3, int(os.getenv("WHALE_ALERT_RECONNECT_SECONDS", "8")))
        except ValueError:
            reconnect_delay_seconds = 8

        channel_id = os.getenv("WHALE_ALERT_CHANNEL_ID", "coinautomation_eth").strip() or "coinautomation_eth"
        ws_url = f"wss://leviathan.whale-alert.io/ws?api_key={api_key}"
        subscription_msg = {
            "type": "subscribe_alerts",
            "id": channel_id,
            "blockchains": ["ethereum"],
            "symbols": ["eth"],
            "tx_types": ["transfer"],
            "min_value_usd": float(min_amount_usd),
        }

        if callable(whalealert_load_data_func):
            try:
                whalealert_load_data_func()
            except Exception as e:
                print(f"WhaleAlert local model preload error: {type(e).__name__}: {e}", flush=True)
        print(
            "WhaleAlert monitor started: "
            f"channel_id={channel_id}, min_amount_usd={min_amount_usd}, "
            f"reconnect_delay_seconds={reconnect_delay_seconds}",
            flush=True,
        )

        while True:
            try:
                print("WhaleAlert connecting: wss://leviathan.whale-alert.io/ws?api_key=***", flush=True)
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps(subscription_msg))
                    print(
                        "WhaleAlert subscribe sent: "
                        f"id={channel_id}, blockchains={subscription_msg['blockchains']}, "
                        f"symbols={subscription_msg['symbols']}, tx_types={subscription_msg['tx_types']}, "
                        f"min_value_usd={subscription_msg['min_value_usd']}",
                        flush=True,
                    )
                    while True:
                        message = await ws.recv()
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            print("WhaleAlert monitor warning: non-JSON message skipped", flush=True)
                            continue

                        msg_type = str(data.get("type", "")).strip()
                        if msg_type == "subscribed_alerts":
                            print(
                                "WhaleAlert subscribed: "
                                f"id={data.get('id')}, channel_id={data.get('channel_id', '')}",
                                flush=True,
                            )
                            continue

                        if msg_type != "alert":
                            continue

                        if callable(whalealert_model_func):
                            try:
                                whalealert_model_func(message)
                            except Exception as e:
                                print(f"WhaleAlert local model update error: {type(e).__name__}: {e}", flush=True)

                        report = whalealert_protocol_func(message) if callable(whalealert_protocol_func) else ""
                        if report:
                            print("WhaleAlert alert received: enqueue whale wakeup", flush=True)
                            await self.add_event("whale", report)
                        else:
                            print("WhaleAlert alert received but report empty", flush=True)
            except websockets.ConnectionClosed as e:
                print(
                    f"WhaleAlert connection closed: code={getattr(e, 'code', 'unknown')} "
                    f"reason={getattr(e, 'reason', '')}; reconnecting in {reconnect_delay_seconds}s",
                    flush=True,
                )
            except Exception as e:
                print(f"WhaleAlert monitor exception: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc()
            await asyncio.sleep(reconnect_delay_seconds)

    async def http_trigger_server(self):
        async def handle_trigger(request):
            event_type = request.query.get("type", "self_check")
            event_content = request.query.get(
                "content",
                "请进行系统自检，重点检查当前市场趋势和账户仓位状态。",
            )
            await self.add_event(event_type, event_content)
            return web.Response(text=f"Event triggered: {event_type}")

        app = web.Application()
        app.router.add_get("/", handle_trigger)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 1829)
        await site.start()

    async def binance_monitor(self):
        thresholds = {
            "RSI": 6.5,
            "PRICE": 0.0075,
            "RISK": 0.05,
        }

        while True:
            try:
                alerts_data = safe_json_read(BINANCE_ALERTS_PATH, "Binance Alerts File")
                if alerts_data and isinstance(alerts_data, dict):
                    current_history = self.load_alert_history()
                    now_ts = time.time()
                    trigger_messages: List[str] = []

                    for alert_type, alert_list in alerts_data.items():
                        if not alert_list:
                            continue

                        current_item = alert_list[0]
                        current_val = float(current_item.get("value", 0))
                        last_record = current_history.get(alert_type, {})

                        if isinstance(last_record, dict):
                            last_time = float(last_record.get("time", 0))
                            last_val = last_record.get("value")
                        elif isinstance(last_record, (int, float)):
                            last_time = float(last_record)
                            last_val = None
                        else:
                            last_time = 0
                            last_val = None

                        if now_ts - last_time < 240:
                            continue

                        should_trigger = False
                        if last_val is None:
                            should_trigger = True
                        else:
                            diff = abs(current_val - float(last_val))
                            if "RSI" in alert_type and diff >= thresholds["RSI"]:
                                should_trigger = True
                            elif "PRICE" in alert_type and float(last_val) != 0:
                                rel_change = diff / float(last_val)
                                if rel_change >= thresholds["PRICE"]:
                                    should_trigger = True
                            elif (
                                "MARGIN" in alert_type
                                or "LIQUIDATION" in alert_type
                                or "RISK" in alert_type
                            ) and diff >= thresholds["RISK"]:
                                should_trigger = True
                            elif now_ts - last_time > 1800:
                                should_trigger = True

                        if should_trigger:
                            current_history[alert_type] = {"time": now_ts, "value": current_val}
                            trigger_messages.append(
                                f"Type: {alert_type}\nDetails: {json.dumps(alert_list, ensure_ascii=False, indent=2)}"
                            )

                    if trigger_messages:
                        self.save_alert_history(current_history)
                        await self.add_event("binance_alert", "\n\n".join(trigger_messages))
            except Exception as e:
                print(f"Binance monitor error: {e}", flush=True)
                traceback.print_exc()

            await asyncio.sleep(8)

    async def heartbeat_monitor(self):
        while True:
            await asyncio.sleep(3600)
            await self.add_event("heartbeat", "System heartbeat check.")

    async def fill_monitor(self):
        while True:
            try:
                fills_data = safe_json_read(FILLS_JSON_PATH, "Fills File")
                if fills_data and isinstance(fills_data, list):
                    range_fill_classification = classify_range_fill_batch(fills_data)
                    if range_fill_classification.get("active") and range_fill_classification.get("suppress"):
                        print(
                            "[RANGE_PLAN] Suppressed tracked order_fill wakeup: "
                            + ", ".join(range_fill_classification.get("known_order_ids", [])),
                            flush=True,
                        )
                    else:
                        content = "\n".join([f"- {item.get('message', '')}" for item in fills_data])
                        if range_fill_classification.get("active"):
                            unknown_order_ids = range_fill_classification.get("unknown_order_ids", [])
                            content = (
                                "[Range Plan Exception]\n"
                                f"Unexpected fills while range automation is active: {unknown_order_ids}\n\n"
                                + content
                            )
                            await self.add_event("range_plan_exception", content)
                        else:
                            await self.add_event("order_fill", content)
                    safe_json_dump([], FILLS_JSON_PATH)
            except Exception as e:
                print(f"Fill monitor error: {e}", flush=True)

            await asyncio.sleep(1)

    async def range_plan_monitor(self):
        while True:
            try:
                result = maintain_range_plan()
                event = result.get("event") if isinstance(result, dict) else None
                if isinstance(event, dict):
                    event_key = str(event.get("key", "") or "").strip()
                    if event_key and event_key != self.last_range_event_key:
                        await self.add_event(
                            str(event.get("type", "range_plan_exception") or "range_plan_exception"),
                            str(event.get("content", "") or ""),
                        )
                        acknowledge_range_plan_event(event_key)
                        self.last_range_event_key = event_key
            except Exception as e:
                print(f"Range plan monitor error: {e}", flush=True)

            await asyncio.sleep(3)

    def _check_and_trigger_overdue_alarms(self) -> List[Dict[str, Any]]:
        triggered_alarms: List[Dict[str, Any]] = []
        alarms = safe_json_read(CLOCK_JSON_PATH, "Clock JSON")
        if not alarms or not isinstance(alarms, list):
            return triggered_alarms

        now = datetime.now()
        updated = False
        new_alarms = []
        report_text = ""
        alarm_metrics: Dict[str, float] = {}
        if os.path.exists(BINANCE_REPORT_PATH):
            try:
                with open(BINANCE_REPORT_PATH, "r", encoding="utf-8") as f:
                    report_text = f.read()
                alarm_metrics = _extract_alarm_metrics_from_report(report_text)
            except Exception:
                alarm_metrics = {}

        for alarm in alarms:
            if alarm.get("status") != "pending":
                new_alarms.append(alarm)
                continue

            trigger_time = datetime.strptime(alarm["trigger_time"], "%Y-%m-%d %H:%M:%S")
            condition = _normalize_alarm_condition_obj(alarm.get("condition"))

            if condition:
                if _alarm_condition_matches(condition, alarm_metrics):
                    if _recent_system_alert_matches_condition(condition):
                        new_alarms.append(alarm)
                        continue
                    triggered_alarm = dict(alarm)
                    triggered_alarm["triggered_by"] = "condition"
                    if "expr" in condition:
                        triggered_alarm["trigger_metric_value"] = {
                            metric: alarm_metrics.get(metric)
                            for metric in _collect_alarm_condition_metrics(condition)
                        }
                    else:
                        triggered_alarm["trigger_metric_value"] = alarm_metrics.get(condition["metric"])
                    triggered_alarms.append(triggered_alarm)
                    updated = True
                    continue
                if now >= trigger_time:
                    updated = True
                    continue
            elif now >= trigger_time:
                triggered_alarm = dict(alarm)
                triggered_alarm["triggered_by"] = "time"
                triggered_alarms.append(triggered_alarm)
                updated = True
                continue

            new_alarms.append(alarm)

        if updated:
            safe_json_dump(new_alarms, CLOCK_JSON_PATH)
            for alarm in triggered_alarms:
                created_at = alarm.get("created_at", "unknown")
                condition = _normalize_alarm_condition_obj(alarm.get("condition"))
                if alarm.get("triggered_by") == "condition" and condition:
                    if "expr" in condition:
                        observed = alarm.get("trigger_metric_value")
                        observed_text = ""
                        if isinstance(observed, dict) and observed:
                            observed_text = ", observed=" + ", ".join(
                                f"{metric}={_format_alarm_numeric(value)}"
                                for metric, value in observed.items()
                            )
                        trigger_reason = (
                            "Conditional trigger fired before deadline: "
                            f"expr={condition['expr']}{observed_text}, "
                            f"deadline={alarm.get('trigger_time')}"
                        )
                    else:
                        trigger_reason = (
                            "Conditional trigger fired before deadline: "
                            f"{condition['metric']} {condition['operator']} {_format_alarm_numeric(condition['value'])}, "
                            f"current={_format_alarm_numeric(alarm.get('trigger_metric_value'))}, "
                            f"deadline={alarm.get('trigger_time')}"
                        )
                else:
                    trigger_reason = f"Scheduled trigger time reached: {alarm.get('trigger_time')}"
                alarm["formatted_prompt"] = (
                    f"This is the alarm you set at {created_at}.\n"
                    f"Trigger Reason: {trigger_reason}\n"
                    f"{alarm.get('prompt', '')}"
                )

        return triggered_alarms

    async def alarm_monitor(self):
        while True:
            try:
                triggered_alarms = self._check_and_trigger_overdue_alarms()
                for alarm in triggered_alarms:
                    await self.add_event(
                        "alarm_wakeup",
                        alarm.get("formatted_prompt", alarm.get("prompt", "")),
                    )
            except Exception as e:
                print(f"Alarm monitor error: {e}", flush=True)

            await asyncio.sleep(5)

    def get_market_context(self) -> tuple:
        binance_data = "No Binance data available."
        if os.path.exists(BINANCE_REPORT_PATH):
            with open(BINANCE_REPORT_PATH, "r", encoding="utf-8") as f:
                binance_data = f.read()

        account_data = "No Account data available."
        if os.path.exists(BINANCE_ACCOUNT_PATH):
            with open(BINANCE_ACCOUNT_PATH, "r", encoding="utf-8") as f:
                account_data = f.read()

        news_data_text, news_ids = news_protocol_func(10)
        poly_data, _ = poly_protocol_func(20)

        memory_snapshots = self._load_memory_snapshots()
        experience_data = memory_snapshots["long_text"]
        shortmemory_data = memory_snapshots["short_text"]

        pending_alarms = "No pending alarms."
        alarms = safe_json_read(CLOCK_JSON_PATH, "Clock JSON Context")
        if alarms and isinstance(alarms, list):
            lines = []
            for item in alarms:
                if item.get("status", "pending") == "pending":
                    lines.append(_format_pending_alarm_line(item))
            if lines:
                pending_alarms = "\n".join(lines)

        range_plan_data = self._load_range_plan_snapshot_text()

        return (
            binance_data,
            account_data,
            news_data_text,
            news_ids,
            poly_data,
            experience_data,
            shortmemory_data,
            pending_alarms,
            range_plan_data,
        )

    def _render_prompt_template(self, template: str, replacements: Dict[str, str]) -> tuple:
        def apply_replacements(text: str) -> str:
            out = text
            for placeholder, value in replacements.items():
                out = out.replace(placeholder, str(value))
            return out

        parts = template.split("# Context", 1)
        if len(parts) < 2:
            return LANGCHAIN_SYSTEM_PROMPT.strip(), apply_replacements(template)

        role_part = parts[0].strip()
        rest_part = parts[1].strip()
        marker = None
        for candidate in [
            "# Autonomous Execution",
            "# Core Principles",
            "# Tool Rules",
            "# Decision Policy",
            "# Memory Policy",
            "# Operational Workflow",
            "# Output Requirement",
            "# Strategy Constraints & Rules",
        ]:
            idx = rest_part.find(candidate)
            if idx != -1:
                marker = candidate
                break

        if marker:
            context_part, static_tail = rest_part.split(marker, 1)
            dynamic_context_template = "# Context\n\n" + context_part.strip()
            static_instructions = marker + static_tail
            system_prompt = f"{role_part}\n\n{static_instructions}".strip()
            user_prompt = apply_replacements(dynamic_context_template)
            return system_prompt, user_prompt

        system_prompt = role_part if role_part else LANGCHAIN_SYSTEM_PROMPT.strip()
        user_prompt = apply_replacements("# Context\n\n" + rest_part)
        return system_prompt, user_prompt

    def build_prompt(
        self,
        event_type: str,
        event_content: str,
        binance_data: str,
        account_data: str,
        news_gated: str,
        polymarket_gated: str,
        shortmemory_data: str,
        pending_alarms: str,
        range_plan_data: str,
        experience_data: str = "No historical experience yet.",
    ) -> tuple:
        alarm_prompt = "No specific alarm instructions for this turn."
        if "alarm_wakeup" in event_type:
            marker = "(alarm_wakeup):"
            if marker in event_content:
                alarm_prompt = event_content.split(marker, 1)[1].strip()

        replacements = {
            "{{current_time}}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "{{event_type}}": event_type,
            "{{event_content}}": event_content,
            "{{account_data}}": account_data,
            "{{binance_data}}": binance_data,
            "{{news_gated}}": news_gated,
            "{{polymarket_gated}}": polymarket_gated,
            "{{shortmemory_data}}": shortmemory_data,
            "{{alarm_prompt}}": alarm_prompt,
            "{{pending_alarms}}": pending_alarms,
            "{{range_plan_data}}": range_plan_data,
            "{{experience_data}}": experience_data,
        }

        if os.path.exists(PROMPT_TEMPLATE_PATH):
            try:
                with open(PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                    template = f.read()
                return self._render_prompt_template(template, replacements)
            except Exception as e:
                print(f"Error building prompt from template: {e}", flush=True)

        system_prompt = LANGCHAIN_SYSTEM_PROMPT.strip()
        user_prompt = (
            f"Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Event Type: {event_type}\n\n"
            f"Event Content:\n{event_content}\n\n"
            f"Alarm Prompt:\n{alarm_prompt}\n\n"
            f"Pending Alarms:\n{pending_alarms}\n\n"
            f"Range Automation Plan:\n{range_plan_data}\n\n"
            f"Market Snapshot (Binance):\n{binance_data}\n\n"
            f"Account Snapshot:\n{account_data}\n\n"
            f"News Gated:\n{news_gated}\n\n"
            f"Polymarket Gated:\n{polymarket_gated}\n\n"
            f"Short-term Snapshot:\n{shortmemory_data}\n\n"
            f"Experience (Long-term):\n{experience_data}\n"
        )

        return system_prompt, user_prompt

    async def send_push(self, msg: str):
        push_urls = [
            "http://127.0.0.1:9182/push",
            "http://localhost:9182/push",
        ]
        last_error: Optional[str] = None
        try:
            async with httpx.AsyncClient() as client:
                for push_url in push_urls:
                    try:
                        resp = await client.post(
                            push_url,
                            json={"msg": msg},
                            timeout=10.0,
                        )
                    except Exception as e:
                        last_error = f"{push_url} -> {type(e).__name__}: {e!r}"
                        continue
                    if 200 <= resp.status_code < 300:
                        return
                    body_preview = (resp.text or "").strip().replace("\n", " ")[:300]
                    last_error = f"{push_url} -> HTTP {resp.status_code}: {body_preview}"
        except Exception as e:
            print(f"Push error: {type(e).__name__}: {e!r}", flush=True)
            traceback.print_exc()
            return
        print(f"Push error: {last_error or 'unknown error'}", flush=True)

    async def process_merged_events(self):
        while True:
            try:
                tasks = self.load_tasks_from_file()
                if not tasks:
                    await self.event_queue.get()
                    self.event_queue.task_done()
                    tasks = self.load_tasks_from_file()

                if not tasks:
                    continue

                trigger_times = []
                for task in tasks:
                    try:
                        trigger_times.append(datetime.strptime(task["trigger_at"], "%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        trigger_times.append(datetime.now() + timedelta(seconds=10))

                min_trigger = min(trigger_times)
                wait_time = (min_trigger - datetime.now()).total_seconds()

                if wait_time > 0:
                    try:
                        await asyncio.wait_for(self.event_queue.get(), timeout=wait_time)
                        self.event_queue.task_done()
                        continue
                    except asyncio.TimeoutError:
                        pass

                while not self.event_queue.empty():
                    self.event_queue.get_nowait()
                    try:
                        self.event_queue.task_done()
                    except ValueError:
                        pass

                tasks_to_process = self.load_tasks_from_file()
                if tasks_to_process:
                    self.is_processing = True
                    self.pending_events = tasks_to_process
                    self.clear_tasks_file()

                    self.current_task = asyncio.create_task(self.make_decision())
                    try:
                        await self.current_task
                    finally:
                        self.is_processing = False
                        self.current_task = None
                        self.pending_events = []
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in process_merged_events: {e}", flush=True)
                await asyncio.sleep(1)

    async def make_decision(self):
        overdue_alarms = self._check_and_trigger_overdue_alarms()
        if overdue_alarms:
            for alarm in overdue_alarms:
                alarm_preview = " ".join(
                    str(alarm.get("formatted_prompt", alarm.get("prompt", "")) or "").split()
                )
                if len(alarm_preview) > 220:
                    alarm_preview = alarm_preview[:220] + "..."
                print(
                    "[WAKEUP_SIGNAL] type=alarm_wakeup mode=immediate_overdue "
                    f"trigger_at={datetime.now().strftime('%Y-%m-%d %H:%M:%S')} preview={alarm_preview}",
                    flush=True,
                )
                self.pending_events.append(
                    {
                        "type": "alarm_wakeup",
                        "content": alarm.get("formatted_prompt", alarm.get("prompt", "")),
                        "timestamp": datetime.now().isoformat(),
                    }
                )

        if not self.pending_events:
            return

        daily_review_events = [evt for evt in self.pending_events if str(evt.get("type", "")) == "daily_long_review"]
        if daily_review_events:
            await self._run_daily_long_review_events(daily_review_events)
            self.pending_events = [evt for evt in self.pending_events if str(evt.get("type", "")) != "daily_long_review"]
            if not self.pending_events:
                return

        combined_type = " & ".join(sorted(set([e["type"] for e in self.pending_events])))
        combined_content = "\n\n---\n\n".join(
            [f"Event {idx + 1} ({evt['type']}):\n{evt['content']}" for idx, evt in enumerate(self.pending_events)]
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        interaction_log_dir = os.path.join(LOGS_DIR, timestamp)
        os.makedirs(interaction_log_dir, exist_ok=True)

        recorder: List[Dict[str, Any]] = []
        token = set_tool_recorder(recorder)

        try:
            initial_state: DecisionState = {
                "messages": [],
                "event_batch": self.pending_events,
                "event_type": combined_type,
                "full_market_snapshot": "",
                "market_snapshot": "",
                "account_snapshot": "",
                "news_snapshot": "",
                "poly_snapshot": "",
                "experience_snapshot": "",
                "shortmemory_snapshot": "",
                "long_reflection_obj": {},
                "long_memory_snapshot_obj": {},
                "short_memory_snapshot_obj": {},
                "pending_alarms_snapshot": "",
                "decision": {},
                "tool_results": recorder,
                "transition_state": "observe",
                "audit_meta": {
                    "event_type": combined_type,
                    "event_details": combined_content,
                },
                "system_prompt": "",
                "user_prompt": "",
                "event_details": combined_content,
                "thread_id": f"production-main-{timestamp}",
                "interaction_log_dir": interaction_log_dir,
            }

            trace_id = None
            run_thread_id = f"production-main-{timestamp}"
            tracing_active_this_turn = self.langsmith_enabled
            turn_langsmith_degraded_reason = "" if self.langsmith_enabled else self.langsmith_degraded_reason
            try:
                if self.langsmith_enabled and tracing_context is not None:
                    invoke_started = False
                    invoke_completed = False
                    try:
                        os.environ["LANGCHAIN_TRACING_V2"] = "true"
                        os.environ["LANGSMITH_TRACING"] = "true"
                        os.environ["LANGSMITH_API_KEY"] = LANGSMITH_API_KEY
                        os.environ["LANGSMITH_PROJECT"] = LANGSMITH_PROJECT
                        with tracing_context(enabled=True, project_name=LANGSMITH_PROJECT):
                            invoke_started = True
                            result_state = self.graph.invoke(
                                initial_state,
                                config={"configurable": {"thread_id": run_thread_id}},
                            )
                            invoke_completed = True
                            if get_current_run_tree is not None:
                                run_tree = get_current_run_tree()
                                if run_tree is not None:
                                    trace_id = str(getattr(run_tree, "trace_id", "") or "") or None
                    except Exception as trace_exc:
                        if invoke_started and not invoke_completed:
                            raise
                        tracing_active_this_turn = False
                        turn_langsmith_degraded_reason = f"{type(trace_exc).__name__}: {trace_exc}"
                        os.environ["LANGCHAIN_TRACING_V2"] = "false"
                        os.environ["LANGSMITH_TRACING"] = "false"
                        print(
                            (
                                "[LangSmith] tracing degraded for current turn only; "
                                f"skip tracing and keep local logs: {turn_langsmith_degraded_reason}"
                            ),
                            flush=True,
                        )
                        # LangSmith failure must never escalate to state_machine_error.
                        # If tool calls already happened, do not re-invoke to avoid duplicate side effects.
                        if recorder:
                            fallback_user_prompt = (
                                f"Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                                f"Event Type: {combined_type}\n\nEvent Content:\n{combined_content}"
                            )
                            degraded_decision = DecisionOutput(
                                execution_txt="LangSmith tracing 异常，但本轮已执行工具调用；主流程继续并保留执行结果。",
                                explanation=(
                                    f"LangSmith degraded after tool activity: "
                                    f"{type(trace_exc).__name__}: {trace_exc}"
                                ),
                                memory_management_reasoning="本轮发生 tracing 降级，不额外写入记忆，等待下轮恢复常规决策。",
                                decision_basis="tracing_degraded_after_tool_activity",
                                conflict_check="LangSmith tracing 不影响交易主流程",
                                falsification_point="下轮唤醒验证策略链路与 LangSmith 状态",
                                next_alarm_reason="保持原计划节奏",
                                state_change_evidence="none",
                                experience="",
                                shortterm="",
                            ).model_dump()
                            result_state = dict(initial_state)
                            result_state["decision"] = degraded_decision
                            result_state["user_prompt"] = fallback_user_prompt
                            degraded_audit = dict(initial_state.get("audit_meta", {}))
                            degraded_audit["validation_status"] = "langsmith_degraded_continue"
                            degraded_audit["guard_retry"] = {
                                "attempt_index": 0,
                                "attempt_limit": EXECUTE_PRIMARY_RETRY_LIMIT,
                                "stopped_reason": "langsmith_degraded_after_tool_activity",
                                "next_action": "continue_without_tracing",
                            }
                            result_state["audit_meta"] = degraded_audit
                        else:
                            result_state = self.graph.invoke(
                                initial_state,
                                config={"configurable": {"thread_id": run_thread_id}},
                            )
                else:
                    result_state = self.graph.invoke(
                        initial_state,
                        config={"configurable": {"thread_id": run_thread_id}},
                    )
            except Exception as e:
                if trace_id is None:
                    trace_id = f"fallback-{uuid.uuid4()}"
                is_recoverable_api_error = _is_recoverable_llm_api_error(e)
                if is_recoverable_api_error:
                    fallback_decision = DecisionOutput(
                        execution_txt="LLM/API 调用失败，本轮降级为等待，不执行交易动作。",
                        explanation=f"LLM/API degraded: {type(e).__name__}: {e}",
                        memory_management_reasoning="保留上一轮结构化记忆，不写入新的交易决策。",
                        decision_basis="api_degraded_wait_next_turn",
                        conflict_check="外部 API 暂时异常，主流程降级继续",
                        falsification_point="下轮唤醒重试，若持续失败再人工排查网络/API 状态",
                        next_alarm_reason="等待下一次唤醒后自动重试",
                        state_change_evidence="none",
                        experience="",
                        shortterm="",
                    ).model_dump()
                    final_validation_errors = [f"api_call_failed: {type(e).__name__}: {e}"]
                    validation_events = [{"tag": "api_call_failed", "reason": str(e)}]
                    validation_status = "api_degraded_wait_next_turn"
                    triggered_rules = "api_call_failed"
                    guard_stopped_reason = "api_call_failed"
                    guard_next_action = "wait_next_wakeup"
                else:
                    fallback_decision = DecisionOutput(
                        execution_txt="状态图执行被阻断，未执行交易动作。",
                        explanation=f"State machine blocked: {type(e).__name__}: {e}",
                        memory_management_reasoning="保留上一轮结构化记忆，不写入新的交易决策。",
                        decision_basis="状态迁移异常",
                        conflict_check="非法状态迁移或节点执行异常，已阻断",
                        falsification_point="修复状态图节点后重试",
                        next_alarm_reason="状态机异常，待人工修复后恢复",
                        state_change_evidence="none",
                        experience="",
                        shortterm="",
                    ).model_dump()
                    final_validation_errors = [f"state_machine_error: {type(e).__name__}: {e}"]
                    validation_events = [{"tag": "state_machine_error", "reason": str(e)}]
                    validation_status = "guards_blocked_wait_next_turn"
                    triggered_rules = "state_machine_error"
                    guard_stopped_reason = "state_machine_error"
                    guard_next_action = "wait_next_wakeup"

                log_data = {
                    "content": fallback_decision,
                    "usage": {},
                    "tool_calls": recorder,
                    "attempts": [],
                    "unparsed_fields": [],
                    "consistency_violations": [],
                    "final_validation_errors": final_validation_errors,
                    "validation_events": validation_events,
                    "validation_status": validation_status,
                    "reanswer_count": 0,
                    "triggered_rules": triggered_rules,
                    "triggered_rules_first_fail": triggered_rules,
                    "triggered_rules_all_fail": triggered_rules,
                    "triggered_rules_final": triggered_rules,
                    "resolved_rules": "none",
                    "rejected_tool_calls": [],
                    "guard_pipeline": [],
                    "guard_checks_all": [],
                    "guard_failures": [],
                    "resolved_guard_failures": [],
                    "guard_retry": {
                        "attempt_index": 0,
                        "attempt_limit": EXECUTE_PRIMARY_RETRY_LIMIT,
                        "stopped_reason": guard_stopped_reason,
                        "next_action": guard_next_action,
                    },
                    "total_turns": 1,
                    "all_turns": [],
                    "timestamp": datetime.now().isoformat(),
                    "account_data": str(initial_state.get("account_snapshot", "")),
                    "audit_meta": dict(initial_state.get("audit_meta", {})),
                    "langsmith": {
                        "enabled": tracing_active_this_turn,
                        "project": LANGSMITH_PROJECT,
                        "thread_id": run_thread_id,
                        "trace_id": trace_id,
                        "degraded_reason": turn_langsmith_degraded_reason,
                    },
                }
                fallback_system_prompt = ""
                fallback_user_prompt = ""
                prompt_source_state = locals().get("result_state") or locals().get("initial_state") or {}
                if isinstance(prompt_source_state, dict):
                    fallback_system_prompt = str(prompt_source_state.get("system_prompt", ""))
                    fallback_user_prompt = str(prompt_source_state.get("user_prompt", ""))
                if not fallback_user_prompt:
                    fallback_user_prompt = (
                        f"Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"Event Type: {combined_type}\n\nEvent Content:\n{combined_content}"
                    )
                with open(os.path.join(interaction_log_dir, "input.md"), "w", encoding="utf-8") as f:
                    f.write(
                        f"--- SYSTEM PROMPT ---\n{fallback_system_prompt}"
                        f"\n\n--- USER PROMPT ---\n{fallback_user_prompt}"
                    )
                safe_json_dump(log_data, os.path.join(interaction_log_dir, "output.json"), use_lock=False)
                self.write_turn_context_snapshots(interaction_log_dir, initial_state)
                if is_recoverable_api_error:
                    await self.send_push(f"【API降级】{type(e).__name__}: {e}")
                else:
                    await self.send_push(f"【状态机异常】{type(e).__name__}: {e}")
                return

            if trace_id is None:
                trace_id = f"fallback-{uuid.uuid4()}"

            decision = dict(result_state.get("decision", {}))
            for key in DecisionOutput.model_fields.keys():
                if key.endswith("_ops") or key == "tool_intents":
                    decision.setdefault(key, [])
                else:
                    decision.setdefault(key, "")
            audit_meta = dict(result_state.get("audit_meta", {}))
            long_horizon_view_obj = result_state.get("long_horizon_view_obj", {})
            if not isinstance(long_horizon_view_obj, dict):
                long_horizon_view_obj = {}
            preferred_direction = ""
            long_horizon_status = ""
            if isinstance(long_horizon_view_obj.get("short_term_guidance", {}), dict):
                preferred_direction = str(long_horizon_view_obj.get("short_term_guidance", {}).get("preferred_direction", "") or "")
            if isinstance(long_horizon_view_obj.get("meta", {}), dict):
                long_horizon_status = str(long_horizon_view_obj.get("meta", {}).get("status", "") or "")
            audit_meta["counter_long_horizon_bias"] = self._compute_counter_long_horizon_bias(preferred_direction, recorder)
            audit_meta["long_horizon_status_at_trade"] = long_horizon_status or audit_meta.get("long_horizon_status", "")
            runtime_guard_summary = self._build_runtime_guard_summary(audit_meta)
            rejected_tool_calls = list(audit_meta.get("rejected_tool_calls", []))
            guard_pipeline = list(audit_meta.get("guard_pipeline", []))
            guard_failures = list(runtime_guard_summary.get("guard_failures", []))
            resolved_guard_failures = list(runtime_guard_summary.get("resolved_guard_failures", []))
            guard_retry = dict(audit_meta.get("guard_retry", {}))
            validation_status = str(audit_meta.get("validation_status", runtime_guard_summary["validation_status"]))
            final_validation_errors: List[str] = []
            if validation_status == "guards_blocked_wait_next_turn":
                blocked_reason = str(guard_retry.get("stopped_reason", "guard_blocked"))
                final_validation_errors = [f"guard_blocked: {blocked_reason}"]
                planned_execution = str(decision.get("execution_txt", "") or "").strip()
                if self._has_successful_side_effect_calls(recorder):
                    if planned_execution:
                        decision["execution_txt"] = (
                            "本轮最终被 guard 拦下；但在最终拦截前已执行部分动作，"
                            "最终账户/挂单状态以 verify 段落为准。 "
                            f"{planned_execution}"
                        )
                    else:
                        decision["execution_txt"] = (
                            "本轮最终被 guard 拦下；但在最终拦截前已执行部分动作，"
                            "最终账户/挂单状态以 verify 段落为准。"
                        )
                elif planned_execution:
                    decision["execution_txt"] = (
                        "本轮最终被 guard 拦下；未执行新的下单、保护单或闹钟动作。"
                        f" 原计划（未执行）: {planned_execution}"
                    )
                else:
                    decision["execution_txt"] = "本轮最终被 guard 拦下；未执行新的下单、保护单或闹钟动作。"
            validation_events = list(audit_meta.get("retry_warnings", []))

            log_data = {
                "content": decision,
                "usage": {},
                "tool_calls": recorder,
                "attempts": list(audit_meta.get("attempt_records", [])),
                "unparsed_fields": list(audit_meta.get("decision_unparsed_fields", [])),
                "consistency_violations": [],
                "risk_review_warnings": list(audit_meta.get("risk_review_warnings", [])),
                "final_validation_errors": final_validation_errors,
                "validation_events": validation_events,
                "validation_status": validation_status,
                "reanswer_count": int(guard_retry.get("attempt_index", 0) or 0),
                "triggered_rules": runtime_guard_summary["triggered_rules"],
                "triggered_rules_first_fail": runtime_guard_summary.get("triggered_rules_first_fail", "none"),
                "triggered_rules_all_fail": runtime_guard_summary.get("triggered_rules_all_fail", "none"),
                "triggered_rules_final": runtime_guard_summary.get("triggered_rules_final", "none"),
                "resolved_rules": runtime_guard_summary.get("resolved_rules", "none"),
                "rejected_tool_calls": rejected_tool_calls,
                "guard_pipeline": guard_pipeline,
                "guard_checks_all": guard_pipeline,
                "guard_failures": guard_failures,
                "resolved_guard_failures": resolved_guard_failures,
                "guard_retry": guard_retry,
                "total_turns": 1,
                "all_turns": [],
                "timestamp": datetime.now().isoformat(),
                "account_data": str(result_state.get("account_snapshot", "")),
                "audit_meta": audit_meta,
                "langsmith": {
                    "enabled": tracing_active_this_turn,
                    "project": LANGSMITH_PROJECT,
                    "thread_id": run_thread_id,
                    "trace_id": trace_id,
                    "degraded_reason": turn_langsmith_degraded_reason,
                },
            }

            with open(os.path.join(interaction_log_dir, "input.md"), "w", encoding="utf-8") as f:
                f.write(
                    f"--- SYSTEM PROMPT ---\n{result_state.get('system_prompt', '')}\n\n"
                    f"--- USER PROMPT ---\n{result_state.get('user_prompt', '')}"
                )

            safe_json_dump(log_data, os.path.join(interaction_log_dir, "output.json"), use_lock=False)
            self._update_post_stop_reflection_from_turn(result_state, decision, audit_meta, log_dir=timestamp)
            self.write_turn_context_snapshots(interaction_log_dir, result_state)

            push_items = [
                ("执行事件详情", combined_content),
                ("执行总结 (Final)", decision.get("execution_txt", "")),
                ("策略深度解释", decision.get("explanation", "")),
                ("记忆与任务管理原因", decision.get("memory_management_reasoning", "")),
                (
                    "校验与重答摘要",
                    "\n".join([f"{k}={v}" for k, v in runtime_guard_summary.items()]),
                ),
            ]
            for title, content in push_items:
                await self.send_push(f"【{title}】\n{content}")
            await self.send_push("=" * 50)
        except asyncio.CancelledError as e:
            self._persist_emergency_turn_log(
                interaction_log_dir=interaction_log_dir,
                combined_type=combined_type,
                combined_content=combined_content,
                exc=e,
                state=locals().get("result_state") or locals().get("initial_state"),
                recorder=recorder,
                stage="make_decision_cancelled",
            )
            raise
        except BaseException as e:
            self._persist_emergency_turn_log(
                interaction_log_dir=interaction_log_dir,
                combined_type=combined_type,
                combined_content=combined_content,
                exc=e,
                state=locals().get("result_state") or locals().get("initial_state"),
                recorder=recorder,
                stage="make_decision_unhandled",
            )
            raise
        finally:
            reset_tool_recorder(token)

    async def run(self):
        print(
            "fronttest is deprecated for production workflow and is intentionally not started from strategy.py",
            flush=True,
        )
        monitor_tasks = [
            self.binance_monitor(),
            self.fill_monitor(),
            self.range_plan_monitor(),
            self.alarm_monitor(),
            self.daily_review_monitor(),
            self.http_trigger_server(),
            self.process_merged_events(),
        ]
        if ENABLE_WHALE_MONITOR:
            monitor_tasks.insert(0, self.whale_monitor())
        else:
            print("Whale monitor is currently disabled.", flush=True)
        await asyncio.gather(*monitor_tasks)


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CoinAutomation Strategy Manager starting...", flush=True)
    manager = StrategyManager()
    try:
        asyncio.run(manager.run())
    except KeyboardInterrupt:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Stopping strategy manager...", flush=True)
