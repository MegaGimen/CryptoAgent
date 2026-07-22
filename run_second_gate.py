from env_config import load_project_env
load_project_env()

import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from langchain_openai import ChatOpenAI
from tools import safe_json_dump, filter_sensitive_items, parse_json_with_fallbacks

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROJECT_ROOT = "/home/coinautomation"
LLM_API_KEY = os.getenv("LLMAPIKEY", "")
LLM_BASE_URL = os.getenv("LLMBASEURL")
LLM_MODEL_ID = os.getenv("LLMMODELID", "gpt-4.1")
NEWS_GATED_PATH = os.path.join(PROJECT_ROOT, "news", "gated.json")
POLY_GATED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "gated.json")

NEWS_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "news_secondgate_prompt.md")
POLY_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "polymarket_secondgate_prompt.md")

BATCH_SIZE = 15
LLM_REQUEST_TIMEOUT_SECONDS = 120
LLM_MAX_RETRIES = 1
NEWS_SECONDGATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "news", "secondgate", "progress.json")
POLY_SECONDGATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "polymarket", "secondgate", "progress.json")
NEWS_SECONDGATE_SKIPPED_PATH = os.path.join(PROJECT_ROOT, "news", "secondgate", "skipped_items.json")
POLY_SECONDGATE_SKIPPED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "secondgate", "skipped_items.json")
BEIJING_TZ = timezone(timedelta(hours=8))


def _should_try_structured_output() -> bool:
    base_url = str(LLM_BASE_URL or "").lower()
    model_id = str(LLM_MODEL_ID or "").lower()
    return any(token in base_url for token in ("volces", "volcengine", "ark")) or any(
        token in model_id for token in ("doubao", "ark", "coding")
    )


def _secondgate_response_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "gate_keep_results",
            "schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                    "additionalProperties": False,
                },
            },
        },
    }

def load_json(path, default=None):
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading {path}: {e}")
        return default

def save_json(path, data):
    safe_json_dump(data, path, use_lock=True)


def _fmt_bj(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _total_batches(total_items: int, batch_size: int) -> int:
    if total_items <= 0 or batch_size <= 0:
        return 0
    return (total_items + batch_size - 1) // batch_size


def _eta_bj(start_ts: float, completed_batches: int, total_batches: int) -> str:
    if start_ts <= 0 or completed_batches <= 0 or total_batches <= 0:
        return ""
    remaining = max(0, total_batches - completed_batches)
    if remaining <= 0:
        return _fmt_bj(time.time())
    elapsed = max(0.001, time.time() - start_ts)
    avg_per_batch = elapsed / completed_batches
    return _fmt_bj(time.time() + avg_per_batch * remaining)


def _write_secondgate_progress(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe_json_dump(payload, path, use_lock=True)


def _write_news_secondgate_progress(payload: dict) -> None:
    _write_secondgate_progress(NEWS_SECONDGATE_PROGRESS_PATH, payload)


def _write_poly_secondgate_progress(payload: dict) -> None:
    _write_secondgate_progress(POLY_SECONDGATE_PROGRESS_PATH, payload)


def write_stage_audit(stage_dir, payload):
    os.makedirs(stage_dir, exist_ok=True)
    audit_path = os.path.join(stage_dir, f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    save_json(audit_path, payload)


def _build_secondgate_user_payload(batch, *, source_type: str):
    now_bj = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return "\n\n".join(
        [
            "Current Time Context",
            f"- source_type: {source_type}",
            f"- current_time_beijing: {now_bj}",
            f"- current_time_utc: {now_utc}",
            "- timeliness_rule: Use the current time as the anchor to decide whether each gated item is still live or already stale/expired/resolved.",
            "Current Gated Batch JSON",
            json.dumps(batch, ensure_ascii=False, indent=2),
        ]
    )


def _item_identifier(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("id") or item.get("news_url") or item.get("event_id") or "")


def _find_batch_item_by_id(batch: List[Dict[str, Any]], item_id: str) -> Dict[str, Any]:
    for item in batch:
        if _item_identifier(item) == item_id:
            return item
    return {"id": item_id}


def _append_secondgate_skipped_items(path: str, entries: List[Dict[str, Any]]) -> None:
    if not entries:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = load_json(path, [])
    if not isinstance(existing, list):
        existing = []
    existing.extend(entries)
    save_json(path, existing)


def _build_secondgate_skipped_entries(
    *,
    source_type: str,
    batch: List[Dict[str, Any]],
    batch_idx: int,
    terminal_failures: List[Dict[str, Any]],
    audit_path: str,
) -> List[Dict[str, Any]]:
    skipped_at_bj = _fmt_bj(time.time())
    entries: List[Dict[str, Any]] = []
    for failure in terminal_failures:
        item_ids = [str(item_id) for item_id in (failure.get("item_ids") or []) if str(item_id)]
        for item_id in item_ids:
            raw_item = _find_batch_item_by_id(batch, item_id)
            entries.append(
                {
                    "source_type": source_type,
                    "item_id": item_id,
                    "batch_idx": batch_idx,
                    "skipped_at_bj": skipped_at_bj,
                    "reason": "Skipped by SecondGate after recursive bisection isolated a single item that still failed structured invocation.",
                    "error_type": failure.get("error_type", ""),
                    "error_message": failure.get("error_message", ""),
                    "log_file": failure.get("log_file", ""),
                    "bisect_audit_path": audit_path,
                    "raw_item": raw_item,
                }
            )
    return entries


def _build_model():
    auth_key = LLM_API_KEY.replace("Bearer ", "", 1) if LLM_API_KEY.startswith("Bearer ") else LLM_API_KEY
    kwargs = {
        "api_key": auth_key,
        "model": LLM_MODEL_ID,
        "temperature": 0.2,
        "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
        "max_retries": LLM_MAX_RETRIES,
        "model_kwargs": {"response_format": _secondgate_response_format()},
    }
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    return ChatOpenAI(**kwargs)


def _invoke_model_structured(
    prompt_template: str,
    user_data: str,
    *,
    log_file_path: Optional[str] = None,
    attempt_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw_output = None
    parsed_json = None
    error_type = ""
    error_message = ""
    invoke_started_at = time.time()
    invoke_elapsed = 0.0

    try:
        model = _build_model()
        messages = [
            {"role": "system", "content": prompt_template},
            {"role": "user", "content": user_data}
        ]

        logging.info("SecondGate invoke start (mode=structured_json_schema)")
        response = model.invoke(messages)
        raw_output = response.content
        invoke_elapsed = time.time() - invoke_started_at
        logging.info(f"SecondGate invoke done (mode=structured_json_schema, elapsed={invoke_elapsed:.2f}s)")

        if not raw_output:
            error_type = "empty_output"
            error_message = "Model returned empty JSON block."
            logging.error(error_message)
        else:
            try:
                parsed_json = parse_json_with_fallbacks(raw_output, "SecondGate JSON")
                if not isinstance(parsed_json, list):
                    error_type = "non_array_output"
                    error_message = "SecondGate parsed output is not a JSON array."
                    logging.error(f"{error_message} Raw output: {str(raw_output)[:500]}")
                    parsed_json = None
            except json.JSONDecodeError:
                error_type = "json_parse_error"
                error_message = "Failed to parse JSON after fallbacks."
                logging.error(f"{error_message} Model raw output: {str(raw_output)[:500]}")
    except Exception as exc:
        invoke_elapsed = time.time() - invoke_started_at
        error_type = exc.__class__.__name__
        error_message = str(exc)
        logging.error(f"Structured SecondGate invocation failed: {error_message}")

    ok = isinstance(parsed_json, list)
    result = {
        "ok": ok,
        "parsed_output": parsed_json,
        "raw_output": raw_output,
        "error_type": "" if ok else error_type,
        "error_message": "" if ok else error_message,
        "structured_output_enabled": True,
        "fallback_attempted": False,
        "invoke_elapsed_seconds": round(invoke_elapsed, 3),
        "attempt_metadata": attempt_metadata or {},
    }

    if log_file_path:
        log_data = {
            "input_system": prompt_template,
            "input_user": user_data,
            "raw_output": raw_output,
            "parsed_output": parsed_json,
            "structured_output_enabled": True,
            "fallback_attempted": False,
            "error_type": result["error_type"],
            "error_message": result["error_message"],
            "invoke_elapsed_seconds": result["invoke_elapsed_seconds"],
            "attempt_metadata": attempt_metadata or {},
        }
        save_json(log_file_path, log_data)

    return result


def invoke_model(prompt_template, user_data=None, log_file_path=None):
    """
    Invoke the LLM in structured JSON mode only.
    Plain-output fallback is intentionally disabled; callers should bisect failed batches.
    """
    result = _invoke_model_structured(prompt_template, user_data or "", log_file_path=log_file_path)
    return result.get("parsed_output") if result.get("ok") else None


def invoke_model_with_bisection(
    prompt_template: str,
    batch: List[Dict[str, Any]],
    *,
    source_type: str,
    log_dir: str,
    batch_idx: int,
    timestamp: str,
    initial_result: Dict[str, Any],
    initial_log_file: str,
) -> Dict[str, Any]:
    audit_path = os.path.join(log_dir, f"bisect_{timestamp}_{batch_idx}.json")
    audit: Dict[str, Any] = {
        "source_type": source_type,
        "batch_idx": batch_idx,
        "created_at_bj": _fmt_bj(time.time()),
        "batch_size": len(batch),
        "item_ids": [_item_identifier(item) for item in batch],
        "strategy": "structured_output_only_recursive_bisection",
        "nodes": [],
        "terminal_failures": [],
        "status": "running",
    }

    def write_audit() -> None:
        save_json(audit_path, audit)

    def invoke_range(start: int, end: int, depth: int) -> Optional[List[Dict[str, Any]]]:
        sub_batch = batch[start:end]
        node_index = len(audit["nodes"]) + 1
        item_ids = [_item_identifier(item) for item in sub_batch]
        node_log_file = os.path.join(
            log_dir,
            f"batch_{timestamp}_{batch_idx}_bisect_{node_index}_{start}_{end}.json",
        )
        metadata = {
            "source_type": source_type,
            "batch_idx": batch_idx,
            "bisect_node": node_index,
            "depth": depth,
            "range_start": start,
            "range_end": end,
            "item_count": len(sub_batch),
            "item_ids": item_ids,
        }
        node = dict(metadata)
        node["log_file"] = node_log_file
        node["status"] = "running"
        audit["nodes"].append(node)
        write_audit()

        user_payload = _build_secondgate_user_payload(sub_batch, source_type=source_type)
        result = _invoke_model_structured(
            prompt_template,
            user_payload,
            log_file_path=node_log_file,
            attempt_metadata=metadata,
        )

        if result.get("ok"):
            parsed_output = result.get("parsed_output") or []
            node["status"] = "passed"
            node["kept_count"] = len(parsed_output)
            node["invoke_elapsed_seconds"] = result.get("invoke_elapsed_seconds")
            write_audit()
            return parsed_output

        node["status"] = "failed"
        node["error_type"] = result.get("error_type")
        node["error_message"] = result.get("error_message")
        node["raw_output_prefix"] = str(result.get("raw_output") or "")[:500]
        node["invoke_elapsed_seconds"] = result.get("invoke_elapsed_seconds")
        write_audit()

        if len(sub_batch) <= 1:
            terminal = {
                "range_start": start,
                "range_end": end,
                "depth": depth,
                "item_ids": item_ids,
                "error_type": result.get("error_type"),
                "error_message": result.get("error_message"),
                "raw_output_prefix": str(result.get("raw_output") or "")[:500],
                "log_file": node_log_file,
            }
            audit["terminal_failures"].append(terminal)
            write_audit()
            return []

        mid = start + (end - start) // 2
        left_results = invoke_range(start, mid, depth + 1)
        right_results = invoke_range(mid, end, depth + 1)
        return (left_results or []) + (right_results or [])

    root_node = {
        "source_type": source_type,
        "batch_idx": batch_idx,
        "bisect_node": 0,
        "depth": 0,
        "range_start": 0,
        "range_end": len(batch),
        "item_count": len(batch),
        "item_ids": [_item_identifier(item) for item in batch],
        "log_file": initial_log_file,
        "status": "failed",
        "initial_attempt": True,
        "error_type": initial_result.get("error_type"),
        "error_message": initial_result.get("error_message"),
        "raw_output_prefix": str(initial_result.get("raw_output") or "")[:500],
        "invoke_elapsed_seconds": initial_result.get("invoke_elapsed_seconds"),
    }
    audit["nodes"].append(root_node)
    write_audit()

    if len(batch) <= 1:
        audit["terminal_failures"].append(
            {
                "range_start": 0,
                "range_end": len(batch),
                "depth": 0,
                "item_ids": [_item_identifier(item) for item in batch],
                "error_type": initial_result.get("error_type"),
                "error_message": initial_result.get("error_message"),
                "raw_output_prefix": str(initial_result.get("raw_output") or "")[:500],
                "log_file": initial_log_file,
            }
        )
        combined_results = []
    else:
        mid = len(batch) // 2
        left_results = invoke_range(0, mid, 1)
        right_results = invoke_range(mid, len(batch), 1)
        combined_results = (left_results or []) + (right_results or [])

    audit["status"] = "passed"
    audit["completed_at_bj"] = _fmt_bj(time.time())
    audit["combined_keep_count"] = len(combined_results)
    audit["skipped_terminal_count"] = len(audit["terminal_failures"])
    write_audit()
    return {
        "ok": True,
        "keep_results": combined_results,
        "audit_path": audit_path,
        "terminal_failures": audit["terminal_failures"],
    }

def run_news_second_gate():
    """Clean up News Gated pool (SecondGate)."""
    gated_news = load_json(NEWS_GATED_PATH, [])
    if not gated_news:
        now_ts = time.time()
        _write_news_secondgate_progress(
            {
                "gate": "news_secondgate",
                "status": "idle",
                "status_label": "Idle",
                "message": "No gated news items.",
                "total_items": 0,
                "batch_size": BATCH_SIZE,
                "total_batches": 0,
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "No gated news items",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        return

    # Optional: Hard-filter any sensitive items that might have snuck in
    original_gated_news = list(gated_news)
    gated_news = filter_sensitive_items(gated_news)
    if not gated_news:
        write_stage_audit(
            os.path.join(PROJECT_ROOT, "news", "secondgate"),
            {
                "status": "aborted_all_filtered_by_censorship",
                "before": len(original_gated_news),
                "after": 0,
                "target_file": NEWS_GATED_PATH,
                "message": "Refused to overwrite live news gated pool with [] because every item was removed by filter_sensitive_items before second-gate cleanup.",
                "sample_ids": [str(item.get("id") or item.get("news_url") or "") for item in original_gated_news[:20]],
            },
        )
        logging.error("News SecondGate aborted: censorship filter removed the entire live gated pool. Preserving existing gated.json.")
        now_ts = time.time()
        _write_news_secondgate_progress(
            {
                "gate": "news_secondgate",
                "status": "aborted",
                "status_label": "Aborted",
                "message": "All gated news items were removed by censorship filter before second-gate cleanup.",
                "total_items": len(original_gated_news),
                "batch_size": BATCH_SIZE,
                "total_batches": _total_batches(len(original_gated_news), BATCH_SIZE),
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "0 batches",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": "All items filtered by censorship before cleanup",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        return

    logging.info("Running News SecondGate (Timeliness Cleanup)...")
    if not os.path.exists(NEWS_PROMPT_PATH):
        logging.error(f"Prompt missing: {NEWS_PROMPT_PATH}")
        now_ts = time.time()
        _write_news_secondgate_progress(
            {
                "gate": "news_secondgate",
                "status": "aborted",
                "status_label": "Aborted",
                "message": "SecondGate prompt file is missing.",
                "total_items": len(gated_news),
                "batch_size": BATCH_SIZE,
                "total_batches": _total_batches(len(gated_news), BATCH_SIZE),
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "0 batches",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": f"Prompt missing: {NEWS_PROMPT_PATH}",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        return

    with open(NEWS_PROMPT_PATH, "r", encoding='utf-8') as f:
        news_prompt = f.read()
        
    all_keep_results = []
    skipped_items = []
    skipped_items = []
    start_ts = time.time()
    total_items = len(gated_news)
    total_batches = _total_batches(total_items, BATCH_SIZE)
    _write_news_secondgate_progress(
        {
            "gate": "news_secondgate",
            "status": "running",
            "status_label": "Running",
            "message": "SecondGate is cleaning the gated news pool.",
            "total_items": total_items,
            "batch_size": BATCH_SIZE,
            "total_batches": total_batches,
            "started_batches": 0,
            "completed_batches": 0,
            "current_batch": 0,
            "progress_pct": 0.0,
            "progress_pct_text": "0.0%",
            "progress_text": f"0/{total_batches} batches",
            "started_at_bj": _fmt_bj(start_ts),
            "last_update_bj": _fmt_bj(start_ts),
            "completed_at_bj": "",
            "eta_bj": "",
            "last_error": "",
            "updated_at_bj": _fmt_bj(start_ts),
        }
    )
    
    # Process in batches to avoid token limit
    log_dir = os.path.join(PROJECT_ROOT, "news", "secondgatelog")
    os.makedirs(log_dir, exist_ok=True)
    
    for i in range(0, len(gated_news), BATCH_SIZE):
        batch = gated_news[i:i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        now_ts = time.time()
        _write_news_secondgate_progress(
            {
                "gate": "news_secondgate",
                "status": "running",
                "status_label": "Running",
                "message": "SecondGate is cleaning the gated news pool.",
                "total_items": total_items,
                "batch_size": BATCH_SIZE,
                "total_batches": total_batches,
                "started_batches": batch_idx,
                "completed_batches": max(0, batch_idx - 1),
                "current_batch": batch_idx,
                "progress_pct": round((max(0, batch_idx - 1) / max(1, total_batches)) * 100.0, 2),
                "progress_pct_text": f"{(max(0, batch_idx - 1) / max(1, total_batches)) * 100.0:.1f}%",
                "progress_text": f"{max(0, batch_idx - 1)}/{total_batches} batches (batch {batch_idx} running)",
                "started_at_bj": _fmt_bj(start_ts),
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": _eta_bj(start_ts, max(1, batch_idx - 1), total_batches),
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        logging.info(f"Processing News SecondGate batch {batch_idx} ({len(batch)} items)...")
        
        batch_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = os.path.join(log_dir, f"batch_{batch_ts}_{batch_idx}.json")
        batch_user_payload = _build_secondgate_user_payload(batch, source_type="news")
        bisect_result: Dict[str, Any] = {}
        initial_result = _invoke_model_structured(
            news_prompt,
            batch_user_payload,
            log_file_path=log_file,
            attempt_metadata={
                "source_type": "news",
                "batch_idx": batch_idx,
                "depth": 0,
                "range_start": 0,
                "range_end": len(batch),
                "item_count": len(batch),
                "item_ids": [_item_identifier(item) for item in batch],
                "initial_attempt": True,
            },
        )
        if initial_result.get("ok"):
            keep_results = initial_result.get("parsed_output") or []
        else:
            logging.warning(
                "News SecondGate batch %s structured invocation failed; starting recursive bisection.",
                batch_idx,
            )
            bisect_result = invoke_model_with_bisection(
                news_prompt,
                batch,
                source_type="news",
                log_dir=log_dir,
                batch_idx=batch_idx,
                timestamp=batch_ts,
                initial_result=initial_result,
                initial_log_file=log_file,
            )
            keep_results = bisect_result.get("keep_results") if bisect_result.get("ok") else None
            batch_skipped_items = _build_secondgate_skipped_entries(
                source_type="news",
                batch=batch,
                batch_idx=batch_idx,
                terminal_failures=bisect_result.get("terminal_failures") or [],
                audit_path=bisect_result.get("audit_path", ""),
            )
            if batch_skipped_items:
                _append_secondgate_skipped_items(NEWS_SECONDGATE_SKIPPED_PATH, batch_skipped_items)
                skipped_items.extend(batch_skipped_items)
        
        if keep_results is None:
            logging.error(f"News SecondGate batch {i//BATCH_SIZE + 1} aborted after structured bisection failure. Remaining batches will not be processed. No cleanup will be performed.")
            now_ts = time.time()
            completed_batches = i // BATCH_SIZE
            pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
            failure_ids = [
                ",".join(failure.get("item_ids") or [])
                for failure in (bisect_result.get("terminal_failures") if "bisect_result" in locals() else []) or []
            ]
            _write_news_secondgate_progress(
                {
                    "gate": "news_secondgate",
                    "status": "aborted",
                    "status_label": "Aborted",
                    "message": "SecondGate structured batch invocation failed after bisection.",
                    "total_items": total_items,
                    "batch_size": BATCH_SIZE,
                    "total_batches": total_batches,
                    "started_batches": batch_idx,
                    "completed_batches": completed_batches,
                    "current_batch": batch_idx,
                    "progress_pct": round(pct, 2),
                    "progress_pct_text": f"{pct:.1f}%",
                    "progress_text": f"{completed_batches}/{total_batches} batches",
                    "started_at_bj": _fmt_bj(start_ts),
                    "last_update_bj": _fmt_bj(now_ts),
                    "completed_at_bj": "",
                    "eta_bj": "",
                    "last_error": f"Batch {batch_idx} structured invocation failed after bisection; terminal_items={failure_ids}",
                    "updated_at_bj": _fmt_bj(now_ts),
                    "bisect_audit_path": bisect_result.get("audit_path") if "bisect_result" in locals() else "",
                }
            )
            return
            
        all_keep_results.extend(keep_results)
        now_ts = time.time()
        completed_batches = batch_idx
        pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
        _write_news_secondgate_progress(
            {
                "gate": "news_secondgate",
                "status": "running",
                "status_label": "Running",
                "message": "SecondGate is cleaning the gated news pool.",
                "total_items": total_items,
                "batch_size": BATCH_SIZE,
                "total_batches": total_batches,
                "started_batches": batch_idx,
                "completed_batches": completed_batches,
                "current_batch": batch_idx,
                "progress_pct": round(pct, 2),
                "progress_pct_text": f"{pct:.1f}%",
                "progress_text": f"{completed_batches}/{total_batches} batches",
                "started_at_bj": _fmt_bj(start_ts),
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": _eta_bj(start_ts, completed_batches, total_batches),
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
                "skipped_items": len(skipped_items),
                "skipped_bucket_path": NEWS_SECONDGATE_SKIPPED_PATH,
            }
        )
        
    keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
    
    new_gated_news = []
    for item in gated_news:
        nid = item.get("id")
        if nid in keep_ids:
            reason = next((r.get("reason") for r in all_keep_results if str(r.get("id")) == nid), item.get("gate_reason", ""))
            item["gate_reason"] = reason
            new_gated_news.append(item)
            
    save_json(NEWS_GATED_PATH, new_gated_news)
    write_stage_audit(
        os.path.join(PROJECT_ROOT, "news", "secondgate"),
        {
            "before": len(gated_news),
            "after": len(new_gated_news),
            "keep_results": all_keep_results,
            "skipped_items": skipped_items,
        },
    )
    end_ts = time.time()
    _write_news_secondgate_progress(
        {
            "gate": "news_secondgate",
            "status": "completed",
            "status_label": "Completed",
            "message": "SecondGate completed successfully.",
            "total_items": total_items,
            "batch_size": BATCH_SIZE,
            "total_batches": total_batches,
            "started_batches": total_batches,
            "completed_batches": total_batches,
            "current_batch": total_batches,
            "progress_pct": 100.0,
            "progress_pct_text": "100.0%",
            "progress_text": f"{len(new_gated_news)}/{total_items} kept",
            "started_at_bj": _fmt_bj(start_ts),
            "last_update_bj": _fmt_bj(end_ts),
            "completed_at_bj": _fmt_bj(end_ts),
            "eta_bj": _fmt_bj(end_ts),
            "last_error": "",
            "updated_at_bj": _fmt_bj(end_ts),
            "kept_items": len(new_gated_news),
            "skipped_items": len(skipped_items),
            "skipped_bucket_path": NEWS_SECONDGATE_SKIPPED_PATH,
        }
    )
    logging.info(f"News SecondGate completed. {len(new_gated_news)}/{len(gated_news)} items kept.")

def run_poly_second_gate():
    """Clean up Polymarket Gated pool (SecondGate)."""
    gated_poly = load_json(POLY_GATED_PATH, [])
    if not gated_poly:
        now_ts = time.time()
        _write_poly_secondgate_progress(
            {
                "gate": "polymarket_secondgate",
                "status": "idle",
                "status_label": "Idle",
                "message": "No gated polymarket items.",
                "total_items": 0,
                "batch_size": BATCH_SIZE,
                "total_batches": 0,
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "No gated polymarket items",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        return

    # Optional: Hard-filter any sensitive items that might have snuck in
    original_gated_poly = list(gated_poly)
    gated_poly = filter_sensitive_items(gated_poly)
    if not gated_poly:
        write_stage_audit(
            os.path.join(PROJECT_ROOT, "polymarket", "secondgate"),
            {
                "status": "aborted_all_filtered_by_censorship",
                "before": len(original_gated_poly),
                "after": 0,
                "target_file": POLY_GATED_PATH,
                "message": "Refused to overwrite live polymarket gated pool with [] because every item was removed by filter_sensitive_items before second-gate cleanup.",
                "sample_ids": [str(item.get("id") or item.get("event_id") or "") for item in original_gated_poly[:20]],
            },
        )
        logging.error("Polymarket SecondGate aborted: censorship filter removed the entire live gated pool. Preserving existing gated.json.")
        now_ts = time.time()
        _write_poly_secondgate_progress(
            {
                "gate": "polymarket_secondgate",
                "status": "aborted",
                "status_label": "Aborted",
                "message": "All gated polymarket items were removed by censorship filter before second-gate cleanup.",
                "total_items": len(original_gated_poly),
                "batch_size": BATCH_SIZE,
                "total_batches": _total_batches(len(original_gated_poly), BATCH_SIZE),
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "0 batches",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": "All items filtered by censorship before cleanup",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        return

    logging.info("Running Polymarket SecondGate (Timeliness Cleanup)...")
    if not os.path.exists(POLY_PROMPT_PATH):
        logging.error(f"Prompt missing: {POLY_PROMPT_PATH}")
        now_ts = time.time()
        _write_poly_secondgate_progress(
            {
                "gate": "polymarket_secondgate",
                "status": "aborted",
                "status_label": "Aborted",
                "message": "SecondGate prompt file is missing.",
                "total_items": len(gated_poly),
                "batch_size": BATCH_SIZE,
                "total_batches": _total_batches(len(gated_poly), BATCH_SIZE),
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "0 batches",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": f"Prompt missing: {POLY_PROMPT_PATH}",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        return

    with open(POLY_PROMPT_PATH, "r", encoding='utf-8') as f:
        poly_prompt = f.read()
        
    all_keep_results = []
    start_ts = time.time()
    total_items = len(gated_poly)
    total_batches = _total_batches(total_items, BATCH_SIZE)
    _write_poly_secondgate_progress(
        {
            "gate": "polymarket_secondgate",
            "status": "running",
            "status_label": "Running",
            "message": "SecondGate is cleaning the gated polymarket pool.",
            "total_items": total_items,
            "batch_size": BATCH_SIZE,
            "total_batches": total_batches,
            "started_batches": 0,
            "completed_batches": 0,
            "current_batch": 0,
            "progress_pct": 0.0,
            "progress_pct_text": "0.0%",
            "progress_text": f"0/{total_batches} batches",
            "started_at_bj": _fmt_bj(start_ts),
            "last_update_bj": _fmt_bj(start_ts),
            "completed_at_bj": "",
            "eta_bj": "",
            "last_error": "",
            "updated_at_bj": _fmt_bj(start_ts),
        }
    )
    
    # Process in batches to avoid token limit
    log_dir = os.path.join(PROJECT_ROOT, "polymarket", "secondgatelog")
    os.makedirs(log_dir, exist_ok=True)
    
    for i in range(0, len(gated_poly), BATCH_SIZE):
        batch = gated_poly[i:i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        now_ts = time.time()
        _write_poly_secondgate_progress(
            {
                "gate": "polymarket_secondgate",
                "status": "running",
                "status_label": "Running",
                "message": "SecondGate is cleaning the gated polymarket pool.",
                "total_items": total_items,
                "batch_size": BATCH_SIZE,
                "total_batches": total_batches,
                "started_batches": batch_idx,
                "completed_batches": max(0, batch_idx - 1),
                "current_batch": batch_idx,
                "progress_pct": round((max(0, batch_idx - 1) / max(1, total_batches)) * 100.0, 2),
                "progress_pct_text": f"{(max(0, batch_idx - 1) / max(1, total_batches)) * 100.0:.1f}%",
                "progress_text": f"{max(0, batch_idx - 1)}/{total_batches} batches (batch {batch_idx} running)",
                "started_at_bj": _fmt_bj(start_ts),
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": _eta_bj(start_ts, max(1, batch_idx - 1), total_batches),
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )
        logging.info(f"Processing Polymarket SecondGate batch {batch_idx} ({len(batch)} items)...")
        
        batch_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = os.path.join(log_dir, f"batch_{batch_ts}_{batch_idx}.json")
        batch_user_payload = _build_secondgate_user_payload(batch, source_type="polymarket")
        bisect_result: Dict[str, Any] = {}
        initial_result = _invoke_model_structured(
            poly_prompt,
            batch_user_payload,
            log_file_path=log_file,
            attempt_metadata={
                "source_type": "polymarket",
                "batch_idx": batch_idx,
                "depth": 0,
                "range_start": 0,
                "range_end": len(batch),
                "item_count": len(batch),
                "item_ids": [_item_identifier(item) for item in batch],
                "initial_attempt": True,
            },
        )
        if initial_result.get("ok"):
            keep_results = initial_result.get("parsed_output") or []
        else:
            logging.warning(
                "Polymarket SecondGate batch %s structured invocation failed; starting recursive bisection.",
                batch_idx,
            )
            bisect_result = invoke_model_with_bisection(
                poly_prompt,
                batch,
                source_type="polymarket",
                log_dir=log_dir,
                batch_idx=batch_idx,
                timestamp=batch_ts,
                initial_result=initial_result,
                initial_log_file=log_file,
            )
            keep_results = bisect_result.get("keep_results") if bisect_result.get("ok") else None
            batch_skipped_items = _build_secondgate_skipped_entries(
                source_type="polymarket",
                batch=batch,
                batch_idx=batch_idx,
                terminal_failures=bisect_result.get("terminal_failures") or [],
                audit_path=bisect_result.get("audit_path", ""),
            )
            if batch_skipped_items:
                _append_secondgate_skipped_items(POLY_SECONDGATE_SKIPPED_PATH, batch_skipped_items)
                skipped_items.extend(batch_skipped_items)
        
        if keep_results is None:
            logging.error(f"Polymarket SecondGate batch {i//BATCH_SIZE + 1} aborted after structured bisection failure. Remaining batches will not be processed. No cleanup will be performed.")
            now_ts = time.time()
            completed_batches = i // BATCH_SIZE
            pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
            failure_ids = [
                ",".join(failure.get("item_ids") or [])
                for failure in (bisect_result.get("terminal_failures") if "bisect_result" in locals() else []) or []
            ]
            _write_poly_secondgate_progress(
                {
                    "gate": "polymarket_secondgate",
                    "status": "aborted",
                    "status_label": "Aborted",
                    "message": "SecondGate structured batch invocation failed after bisection.",
                    "total_items": total_items,
                    "batch_size": BATCH_SIZE,
                    "total_batches": total_batches,
                    "started_batches": batch_idx,
                    "completed_batches": completed_batches,
                    "current_batch": batch_idx,
                    "progress_pct": round(pct, 2),
                    "progress_pct_text": f"{pct:.1f}%",
                    "progress_text": f"{completed_batches}/{total_batches} batches",
                    "started_at_bj": _fmt_bj(start_ts),
                    "last_update_bj": _fmt_bj(now_ts),
                    "completed_at_bj": "",
                    "eta_bj": "",
                    "last_error": f"Batch {batch_idx} structured invocation failed after bisection; terminal_items={failure_ids}",
                    "updated_at_bj": _fmt_bj(now_ts),
                    "bisect_audit_path": bisect_result.get("audit_path") if "bisect_result" in locals() else "",
                }
            )
            return
            
        all_keep_results.extend(keep_results)
        now_ts = time.time()
        completed_batches = batch_idx
        pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
        _write_poly_secondgate_progress(
            {
                "gate": "polymarket_secondgate",
                "status": "running",
                "status_label": "Running",
                "message": "SecondGate is cleaning the gated polymarket pool.",
                "total_items": total_items,
                "batch_size": BATCH_SIZE,
                "total_batches": total_batches,
                "started_batches": batch_idx,
                "completed_batches": completed_batches,
                "current_batch": batch_idx,
                "progress_pct": round(pct, 2),
                "progress_pct_text": f"{pct:.1f}%",
                "progress_text": f"{completed_batches}/{total_batches} batches",
                "started_at_bj": _fmt_bj(start_ts),
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": _eta_bj(start_ts, completed_batches, total_batches),
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
                "skipped_items": len(skipped_items),
                "skipped_bucket_path": POLY_SECONDGATE_SKIPPED_PATH,
            }
        )
        
    keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
    
    new_gated_poly = []
    for item in gated_poly:
        pid = item.get("id")
        if pid in keep_ids:
            reason = next((r.get("reason") for r in all_keep_results if str(r.get("id")) == pid), item.get("gate_reason", ""))
            item["gate_reason"] = reason
            new_gated_poly.append(item)
            
    save_json(POLY_GATED_PATH, new_gated_poly)
    write_stage_audit(
        os.path.join(PROJECT_ROOT, "polymarket", "secondgate"),
        {
            "before": len(gated_poly),
            "after": len(new_gated_poly),
            "keep_results": all_keep_results,
            "skipped_items": skipped_items,
        },
    )
    end_ts = time.time()
    _write_poly_secondgate_progress(
        {
            "gate": "polymarket_secondgate",
            "status": "completed",
            "status_label": "Completed",
            "message": "SecondGate completed successfully.",
            "total_items": total_items,
            "batch_size": BATCH_SIZE,
            "total_batches": total_batches,
            "started_batches": total_batches,
            "completed_batches": total_batches,
            "current_batch": total_batches,
            "progress_pct": 100.0,
            "progress_pct_text": "100.0%",
            "progress_text": f"{len(new_gated_poly)}/{total_items} kept",
            "started_at_bj": _fmt_bj(start_ts),
            "last_update_bj": _fmt_bj(end_ts),
            "completed_at_bj": _fmt_bj(end_ts),
            "eta_bj": _fmt_bj(end_ts),
            "last_error": "",
            "updated_at_bj": _fmt_bj(end_ts),
            "kept_items": len(new_gated_poly),
            "skipped_items": len(skipped_items),
            "skipped_bucket_path": POLY_SECONDGATE_SKIPPED_PATH,
        }
    )
    logging.info(f"Polymarket SecondGate completed. {len(new_gated_poly)}/{len(gated_poly)} items kept.")

if __name__ == "__main__":
    # For testing purposes
    run_news_second_gate()
    run_poly_second_gate()
