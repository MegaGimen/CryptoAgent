#!/usr/bin/env python3
"""Reset structured memory JSON files while preserving schema skeletons.

This script is intentionally standalone. It does not import strategy.py,
because strategy startup currently performs heavy integrity checks and expects
runtime env vars. The reset contract here is:

1. Clear remembered content.
2. Preserve JSON structure required by runtime JSON Patch paths.
3. Reset long-review/runtime state so the next wakeup starts from a clean slate.

Targets reset by default:
- mem/short.json
- mem/long.json
- mem/long_reflection.json
- mem/daily_execution_reflection.json
- mem/post_stop_reflection.json
- mem/long_pipeline/long_review_state.json
- strategy_state.json

Targets intentionally NOT reset here:
- mem/range_plan.json
- mem/institution_kb.json
- mem/search_cache.json
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parent
MEM_DIR = PROJECT_ROOT / "mem"
LONG_PIPELINE_DIR = MEM_DIR / "long_pipeline"
MEMORY_SCHEMA_VERSION = 1


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_short_memory() -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "updated_at": now_text(),
            "source": "reset_script",
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


def default_long_memory() -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "updated_at": now_text(),
            "source": "reset_script",
        },
        "validated_rules": [],
        "invalidated_rules": [],
        "operator_notes": [],
    }


def default_long_reflection() -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "updated_at": now_text(),
            "source": "reset_script",
        },
        "active_hypotheses": [],
        "invalidated_hypotheses": [],
        "news_case_log": [],
    }


def default_closed_position_feedback() -> Dict[str, Any]:
    return {
        "window_days": 0,
        "source_file": "",
        "source_updated_at": "",
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


def default_daily_execution_reflection() -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "updated_at": now_text(),
            "source": "reset_script",
        },
        "closed_position_feedback": default_closed_position_feedback(),
        "carryover": [],
        "behavior_biases": [],
        "watch_items": [],
        "recent_episodes": [],
    }


def default_post_stop_reflection() -> Dict[str, Any]:
    return {
        "meta": {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "updated_at": now_text(),
            "source": "reset_script",
        },
        "active_reflection": {},
        "recent_reflections": [],
    }


def default_long_review_state() -> Dict[str, Any]:
    return {
        "last_review_date": "",
        "last_triggered_at": "",
        "last_run_status": "",
        "last_run_log_dir": "",
        "last_error": "",
    }


def default_structured_state() -> Dict[str, Any]:
    return {}


RESET_TARGETS = {
    MEM_DIR / "short.json": default_short_memory,
    MEM_DIR / "long.json": default_long_memory,
    MEM_DIR / "long_reflection.json": default_long_reflection,
    MEM_DIR / "daily_execution_reflection.json": default_daily_execution_reflection,
    MEM_DIR / "post_stop_reflection.json": default_post_stop_reflection,
    LONG_PIPELINE_DIR / "long_review_state.json": default_long_review_state,
    PROJECT_ROOT / "strategy_state.json": default_structured_state,
}


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def main() -> None:
    for path, builder in RESET_TARGETS.items():
        payload = deepcopy(builder())
        write_json(path, payload)
        print(f"reset: {path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
