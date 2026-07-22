from env_config import load_project_env
load_project_env()

import os
import json
import time
import logging
import subprocess
import sys
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from langchain_openai import ChatOpenAI
from tools import safe_json_dump, filter_sensitive_items, parse_json_with_fallbacks
from search_tool import web_search
from simple_rag import rag_instance
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage, AIMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROJECT_ROOT = "/home/coinautomation"
LLM_API_KEY = os.getenv("LLMAPIKEY", "")
LLM_BASE_URL = os.getenv("LLMBASEURL")
LLM_MODEL_ID = os.getenv("LLMMODELID", "gpt-4.1")
NEWS_DATA_PATH = os.path.join(PROJECT_ROOT, "news", "news_data.json")
POLYMARKET_DATA_PATH = os.path.join(PROJECT_ROOT, "polymarket", "monitor.json")

NEWS_GATED_PATH = os.path.join(PROJECT_ROOT, "news", "gated.json")
POLY_GATED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "gated.json")

NEWS_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "news_firstgate_prompt.md")
POLY_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "polymarket_firstgate_prompt.md")

BATCH_SIZE = 50
LLM_REQUEST_TIMEOUT_SECONDS = 120
LLM_MAX_RETRIES = 1
FIRSTGATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "news", "firstgate", "progress.json")
POLY_FIRSTGATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "polymarket", "firstgate", "progress.json")
NEWS_SCORE_CALIBRATION_PATH = os.path.join(PROJECT_ROOT, "news", "firstgate", "score_calibration.json")
POLY_SCORE_CALIBRATION_PATH = os.path.join(PROJECT_ROOT, "polymarket", "firstgate", "score_calibration.json")
BEIJING_TZ = timezone(timedelta(hours=8))


def _should_try_structured_output() -> bool:
    base_url = str(LLM_BASE_URL or "").lower()
    model_id = str(LLM_MODEL_ID or "").lower()
    return any(token in base_url for token in ("volces", "volcengine", "ark")) or any(
        token in model_id for token in ("doubao", "ark", "coding")
    )


def _firstgate_response_format(*, include_emergency: bool = False) -> dict:
    properties = {
        "id": {"type": "string"},
        "decision": {"type": "string", "enum": ["keep", "drop"]},
        "raw_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "reason": {"type": "string"},
    }
    required = ["id", "decision", "raw_score", "reason"]
    if include_emergency:
        properties["emergency"] = {"type": "boolean"}
        required.append("emergency")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "gate_evaluation_results",
            "schema": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
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


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return default


def _clamp_score(value: Any, default: int = 5) -> int:
    parsed = _safe_int(value, default)
    if parsed is None:
        parsed = default
    return max(1, min(10, parsed))


def _percentile(sorted_scores: List[int], ratio: float) -> Optional[float]:
    if not sorted_scores:
        return None
    if len(sorted_scores) == 1:
        return float(sorted_scores[0])
    idx = (len(sorted_scores) - 1) * ratio
    lo = int(idx)
    hi = min(lo + 1, len(sorted_scores) - 1)
    frac = idx - lo
    return float(sorted_scores[lo] * (1 - frac) + sorted_scores[hi] * frac)


def _score_distribution_summary(scores: List[int]) -> Dict[str, Any]:
    clean_scores = [_clamp_score(score) for score in scores]
    sorted_scores = sorted(clean_scores)
    counts = {str(score): 0 for score in range(1, 11)}
    for score in sorted_scores:
        counts[str(score)] += 1
    if not sorted_scores:
        return {
            "sample_count": 0,
            "min": None,
            "q1": None,
            "median": None,
            "mean": None,
            "q3": None,
            "max": None,
            "std": None,
            "counts": counts,
        }
    return {
        "sample_count": len(sorted_scores),
        "min": sorted_scores[0],
        "q1": round(_percentile(sorted_scores, 0.25) or 0.0, 2),
        "median": round(_percentile(sorted_scores, 0.5) or 0.0, 2),
        "mean": round(sum(sorted_scores) / len(sorted_scores), 3),
        "q3": round(_percentile(sorted_scores, 0.75) or 0.0, 2),
        "max": sorted_scores[-1],
        "std": round(statistics.pstdev(sorted_scores), 3),
        "counts": counts,
    }


def _collect_scores_from_results(results: Any) -> List[int]:
    if not isinstance(results, list):
        return []
    scores: List[int] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if "raw_score" in item:
            scores.append(_clamp_score(item.get("raw_score"), default=5))
            continue
        if "importance_score" in item:
            scores.append(_clamp_score(item.get("importance_score"), default=5))
    return scores


def _load_score_calibration(
    *,
    source_type: str,
    gated_path: str,
    firstgate_log_dir: str,
    output_path: str,
) -> Dict[str, Any]:
    full_eval_scores: List[int] = []
    legacy_keep_scores: List[int] = []
    log_files_scanned = 0

    if os.path.isdir(firstgate_log_dir):
        for filename in sorted(os.listdir(firstgate_log_dir)):
            if not filename.endswith(".json") or not filename.startswith(("batch_", "log_")):
                continue
            path = os.path.join(firstgate_log_dir, filename)
            payload = load_json(path, {})
            if not isinstance(payload, dict):
                continue
            log_files_scanned += 1
            evaluated_results = payload.get("evaluated_results")
            if isinstance(evaluated_results, list) and evaluated_results:
                full_eval_scores.extend(_collect_scores_from_results(evaluated_results))
                continue
            parsed_output = payload.get("parsed_output")
            if isinstance(parsed_output, list) and parsed_output:
                has_decision = any(isinstance(item, dict) and "decision" in item for item in parsed_output)
                target_scores = _collect_scores_from_results(parsed_output)
                if has_decision:
                    full_eval_scores.extend(target_scores)
                else:
                    legacy_keep_scores.extend(target_scores)
                continue
            keep_results = payload.get("keep_results")
            if isinstance(keep_results, list) and keep_results:
                legacy_keep_scores.extend(_collect_scores_from_results(keep_results))

    sample_mode = "full_evaluated"
    calibration_scores = list(full_eval_scores)
    if not calibration_scores:
        sample_mode = "legacy_keep_only"
        calibration_scores = list(legacy_keep_scores)

    if not calibration_scores:
        gated_rows = load_json(gated_path, [])
        if isinstance(gated_rows, list):
            calibration_scores = _collect_scores_from_results(gated_rows)
        if calibration_scores:
            sample_mode = "gated_keep_only"

    summary = _score_distribution_summary(calibration_scores)
    payload = {
        "source_type": source_type,
        "updated_at": datetime.now().isoformat(),
        "sample_mode": sample_mode,
        "log_files_scanned": log_files_scanned,
        "full_evaluated_count": len(full_eval_scores),
        "legacy_keep_only_count": len(legacy_keep_scores),
        "distribution": summary,
    }
    save_json(output_path, payload)
    return payload


def _format_score_calibration(calibration: Dict[str, Any]) -> str:
    distribution = calibration.get("distribution") if isinstance(calibration, dict) else {}
    counts = distribution.get("counts") if isinstance(distribution, dict) else {}
    sample_mode = str((calibration or {}).get("sample_mode") or "unknown")
    caveat = (
        "Historical reference includes full keep/drop evaluations."
        if sample_mode == "full_evaluated"
        else "Historical reference is bootstrapped from legacy kept-item scores only; it underrepresents the low-score tail, so use it as a scale anchor rather than a quota."
    )
    count_text = ", ".join(f"{score}:{counts.get(str(score), 0)}" for score in range(1, 11))
    lines = [
        "Score Calibration Reference",
        f"- sample_mode: {sample_mode}",
        f"- sample_count: {distribution.get('sample_count', 0)}",
        f"- min/q1/median/mean/q3/max: {distribution.get('min')} / {distribution.get('q1')} / {distribution.get('median')} / {distribution.get('mean')} / {distribution.get('q3')} / {distribution.get('max')}",
        f"- std: {distribution.get('std')}",
        f"- bucket_counts: {count_text}",
        f"- caveat: {caveat}",
        "- scoring_anchors:",
        "  1-2 = essentially no BTC transmission chain or pure noise.",
        "  3-4 = weak or indirect relevance; mostly background and usually drop.",
        "  5-6 = meaningful but borderline monthly relevance; keep only if the BTC transmission chain is clear.",
        "  7-8 = clearly useful for BTC monthly narrative, liquidity, regulation, market structure, or macro regime.",
        "  9-10 = rare, regime-shifting, high-conviction developments that can materially reshape BTC's medium-term path.",
        "- use_rule: score against the anchors first, then sanity-check against the historical distribution to reduce drift. Do not force today's outputs to match historical percentages.",
    ]
    return "\n".join(lines)


def _prepare_firstgate_batch_for_model(
    batch: List[Dict[str, Any]],
    *,
    suppress_empty_full_content: bool = False,
) -> List[Dict[str, Any]]:
    prepared_batch: List[Dict[str, Any]] = []
    for item in batch:
        prepared = dict(item or {})
        if suppress_empty_full_content and not str(prepared.get("full_content") or "").strip():
            # Empty full_content is expected on many feeds. Omitting the blank field
            # keeps the model focused on title/text/source instead of treating the
            # empty body as a drop signal by itself.
            prepared.pop("full_content", None)
        prepared_batch.append(prepared)
    return prepared_batch


def _build_firstgate_user_payload(
    batch: List[Dict[str, Any]],
    calibration: Dict[str, Any],
    *,
    source_type: str,
    suppress_empty_full_content: bool = False,
) -> str:
    model_batch = _prepare_firstgate_batch_for_model(
        batch,
        suppress_empty_full_content=suppress_empty_full_content,
    )
    now_bj = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return "\n\n".join(
        [
            "Current Time Context",
            f"- source_type: {source_type}",
            f"- current_time_beijing: {now_bj}",
            f"- current_time_utc: {now_utc}",
            "- timeliness_rule: Use the current time as the reference point when judging freshness, urgency, deadline distance, and whether an item's transmission chain is still live.",
            _format_score_calibration(calibration),
            "Candidate Items JSON",
            json.dumps(model_batch, ensure_ascii=False, indent=2),
        ]
    )


def _normalize_firstgate_results(
    raw_results: Any,
    *,
    batch: List[Dict[str, Any]],
    include_emergency: bool = False,
    require_all_ids: bool = False,
    keep_only_contract: bool = False,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(raw_results, list):
        return None, None, "Model output is not a JSON array."

    batch_ids = [str(item.get("id") or item.get("event_id") or item.get("news_url") or item.get("url") or "") for item in batch]
    batch_id_set = {item_id for item_id in batch_ids if item_id}
    has_decision_schema = any(isinstance(item, dict) and "decision" in item for item in raw_results)

    normalized: List[Dict[str, Any]] = []
    seen_ids = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id not in batch_id_set:
            continue
        if item_id in seen_ids:
            return None, None, f"Duplicate id in FirstGate result: {item_id}"
        seen_ids.add(item_id)

        decision = str(item.get("decision") or "").strip().lower()
        if has_decision_schema:
            if decision not in {"keep", "drop"}:
                return None, None, f"Invalid decision for FirstGate result id={item_id}: {decision}"
            score = _clamp_score(item.get("raw_score"), default=5)
        else:
            decision = "keep"
            score = _clamp_score(item.get("importance_score"), default=5)

        normalized_item = {
            "id": item_id,
            "decision": decision,
            "raw_score": score,
            "importance_score": score,
            "reason": str(item.get("reason") or "").strip(),
        }
        if include_emergency:
            normalized_item["emergency"] = _coerce_bool(item.get("emergency", False))
        normalized.append(normalized_item)

    if require_all_ids and has_decision_schema and seen_ids != batch_id_set:
        missing_ids = sorted(batch_id_set - seen_ids)
        return None, None, f"Missing FirstGate results for ids: {missing_ids[:10]}"

    if keep_only_contract:
        keep_results = list(normalized)
    else:
        keep_results = [item for item in normalized if item.get("decision") == "keep"]
    return normalized, keep_results, None


def _fmt_bj(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _firstgate_total_batches(total_items: int, batch_size: int) -> int:
    if total_items <= 0 or batch_size <= 0:
        return 0
    return (total_items + batch_size - 1) // batch_size


def _firstgate_eta_bj(start_ts: float, completed_batches: int, total_batches: int) -> str:
    if start_ts <= 0 or completed_batches <= 0 or total_batches <= 0:
        return ""
    remaining = max(0, total_batches - completed_batches)
    if remaining <= 0:
        return _fmt_bj(time.time())
    elapsed = max(0.001, time.time() - start_ts)
    avg_per_batch = elapsed / completed_batches
    return _fmt_bj(time.time() + avg_per_batch * remaining)


def _write_gate_progress(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe_json_dump(payload, path, use_lock=True)


def _write_firstgate_progress(payload: dict) -> None:
    _write_gate_progress(FIRSTGATE_PROGRESS_PATH, payload)


def _write_poly_firstgate_progress(payload: dict) -> None:
    _write_gate_progress(POLY_FIRSTGATE_PROGRESS_PATH, payload)


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def _compact_event_text(value, limit=500):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _build_emergency_event_content(emergency_items):
    lines = [
        "News FirstGate detected major emergency news items after completing the current gating wave.",
        "These items are marked emergency=true in news/gated.json.",
        "They are highly time-sensitive and deserve immediate attention, but they are not the only relevant evidence; evaluate them together with regular gated news, market structure, whale summary, and quant overlay.",
        "",
        "Emergency news items:",
    ]
    for idx, item in enumerate(emergency_items[:10], 1):
        lines.extend(
            [
                f"{idx}. {item.get('title') or 'No Title'}",
                f"   id: {item.get('id') or item.get('news_url') or ''}",
                f"   time: {item.get('date') or item.get('published_at') or item.get('gated_at') or ''}",
                f"   score: {item.get('importance_score', 'N/A')}",
                f"   reason: {_compact_event_text(item.get('gate_reason'))}",
                f"   summary: {_compact_event_text(item.get('text') or item.get('full_content'), 700)}",
                f"   url: {item.get('news_url') or item.get('url') or ''}",
                "",
            ]
        )
    if len(emergency_items) > 10:
        lines.append(f"... plus {len(emergency_items) - 10} additional emergency=true items in news/gated.json.")
    return "\n".join(lines).strip()


def _prune_contradictory_news_gated_items(items: Any) -> List[Dict[str, Any]]:
    pruned: List[Dict[str, Any]] = []
    dropped = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        gate_reason = str(item.get("gate_reason") or "").strip().lower()
        if gate_reason.startswith("drop -"):
            dropped += 1
            continue
        pruned.append(item)
    if dropped:
        logging.warning("Pruned %s contradictory news items from live gated pool before FirstGate append.", dropped)
    return pruned


def _trigger_emergency_long_hunter(emergency_items):
    if not emergency_items:
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    firstgate_dir = os.path.join(PROJECT_ROOT, "news", "firstgate")
    os.makedirs(firstgate_dir, exist_ok=True)
    event_content = _build_emergency_event_content(emergency_items)
    audit_file = os.path.join(firstgate_dir, f"emergency_wakeup_{timestamp}.json")
    log_file = os.path.join(firstgate_dir, f"emergency_wakeup_{timestamp}.log")
    payload = {
        "created_at": datetime.now().isoformat(),
        "event_type": "news_firstgate_emergency",
        "trigger_kind": "emergency_news",
        "event_content": event_content,
        "emergency_ids": [str(item.get("id") or "") for item in emergency_items],
        "emergency_count": len(emergency_items),
    }
    save_json(audit_file, {**payload, "status": "spawn_requested", "log_file": log_file})

    child_code = "\n".join(
        [
            "import json, sys, traceback",
            "payload = json.loads(sys.argv[1])",
            "try:",
            "    from strategy import trigger_long_hunter_run",
            "    result = trigger_long_hunter_run(",
            "        event_type=payload['event_type'],",
            "        event_content=payload['event_content'],",
            "        trigger_kind=payload['trigger_kind'],",
            "    )",
            "    audit_meta = result.get('audit_meta') or {}",
            "    print(json.dumps({'status': 'completed', 'run_id': audit_meta.get('run_id', '')}, ensure_ascii=False), flush=True)",
            "except Exception as exc:",
            "    print(json.dumps({'status': 'failed', 'error': f'{type(exc).__name__}: {exc}', 'traceback': traceback.format_exc()}, ensure_ascii=False), flush=True)",
            "    raise",
        ]
    )
    try:
        with open(log_file, "a", encoding="utf-8") as log_handle:
            subprocess.Popen(
                [sys.executable, "-c", child_code, json.dumps(payload, ensure_ascii=False)],
                cwd=PROJECT_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        logging.info(f"Emergency news detected; spawned Long Hunter wakeup for {len(emergency_items)} item(s).")
    except Exception as exc:
        logging.error(f"Failed to spawn emergency Long Hunter wakeup: {exc}")


def invoke_model(prompt_template, user_data=None, log_file_path=None, include_emergency=False):
    """
    Invoke the LLM using langchain_openai ChatOpenAI with tool calling support.
    """
    last_raw_output = None
    fallback_attempted = False
    fallback_retry_pending = False
    try:
        # Construct kwargs
        auth_key = LLM_API_KEY.replace("Bearer ", "", 1) if LLM_API_KEY.startswith("Bearer ") else LLM_API_KEY
        def build_model(enable_structured_output: bool):
            kwargs = {
                "api_key": auth_key,
                "model": LLM_MODEL_ID,
                "temperature": 0.2,
                "timeout": LLM_REQUEST_TIMEOUT_SECONDS,
                "max_retries": LLM_MAX_RETRIES,
            }
            if LLM_BASE_URL:
                kwargs["base_url"] = LLM_BASE_URL
            if enable_structured_output:
                kwargs["model_kwargs"] = {"response_format": _firstgate_response_format(include_emergency=include_emergency)}
            return ChatOpenAI(**kwargs)

        structured_output_enabled = _should_try_structured_output()
        model = build_model(structured_output_enabled)

        # Define tools
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_institution_background",
                    "description": "Get fixed background, definition, and profile information about a crypto institution, project, or entity. DO NOT use this for news, current events, or recent price action.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entity_name": {
                                "type": "string",
                                "description": "The name of the institution or project to look up (e.g., 'Jump Crypto', 'Lido Finance')."
                            }
                        },
                        "required": ["entity_name"],
                        "additionalProperties": False,
                    }
                }
            }
        ]

        model_with_tools = model.bind_tools(tools)

        # Prepare messages
        messages = [
            SystemMessage(content=prompt_template),
            HumanMessage(content=user_data or "")
        ]

        # Tool execution loop
        max_iterations = 5
        iteration = 0
        all_messages = list(messages)
        response = None

        while iteration < max_iterations:
            mode = "structured_json_schema" if structured_output_enabled else "plain_json_prompt"
            logging.info(f"FirstGate invoke start (iteration={iteration + 1}, mode={mode})")
            invoke_started_at = time.time()
            try:
                response = model_with_tools.invoke(all_messages)
            except Exception as exc:
                if structured_output_enabled:
                    logging.warning(f"Structured output failed in FirstGate invoke; falling back to plain JSON prompt mode. Error: {exc}")
                    fallback_attempted = True
                    fallback_retry_pending = True
                    structured_output_enabled = False
                    model = build_model(False)
                    model_with_tools = model.bind_tools(tools)
                    logging.info("Retrying FirstGate invoke in plain JSON prompt mode now.")
                    continue
                raise

            invoke_elapsed = time.time() - invoke_started_at
            logging.info(f"FirstGate invoke done (iteration={iteration + 1}, mode={mode}, elapsed={invoke_elapsed:.2f}s)")
            all_messages.append(response)
            last_raw_output = response.content
            if fallback_retry_pending:
                logging.info("FirstGate fallback invoke succeeded in plain JSON prompt mode.")
                fallback_retry_pending = False

            if not response.tool_calls:
                break

            for tool_call in response.tool_calls:
                if tool_call["name"] == "get_institution_background":
                    entity_name = tool_call["args"].get("entity_name")
                    logging.info(f"LLM requesting background for: {entity_name}")

                    # 1. Try RAG first
                    rag_result = rag_instance.query(entity_name)
                    if rag_result:
                        logging.info(f"RAG Hit for: {entity_name}")
                        result = f"[RAG KNOWLEDGE BASE]: {rag_result}"
                    else:
                        # 2. Fallback to web search
                        logging.info(f"RAG Miss. Falling back to search for: {entity_name}")
                        result = web_search(entity_name)

                    all_messages.append(ToolMessage(content=result, tool_call_id=tool_call["id"]))

            iteration += 1

        if fallback_attempted and fallback_retry_pending:
            logging.info("FirstGate fallback invoke succeeded in plain JSON prompt mode.")

        parsed_json = None
        if not response or not response.content:
            logging.error("Model returned empty content.")
        else:
            try:
                parsed_json = parse_json_with_fallbacks(response.content, "FirstGate JSON")
                if not isinstance(parsed_json, list):
                    logging.error(f"FirstGate parsed output is not a JSON array. Raw output: {str(response.content)[:500]}")
                    parsed_json = None
            except json.JSONDecodeError:
                logging.error(f"Failed to parse JSON after fallbacks. Model raw output: {str(response.content)[:500]}")

        if fallback_attempted and parsed_json is None:
            raw_output = str(response.content) if response and response.content is not None else "<empty>"
            logging.error(f"FirstGate fallback failed after plain JSON mode. Model raw output: {raw_output[:1000]}")

        if log_file_path:
            # Convert message objects to a serializable format
            serialized_history = []
            for msg in all_messages:
                msg_data = {
                    "role": msg.type,
                    "content": msg.content
                }
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    msg_data["tool_calls"] = msg.tool_calls
                if hasattr(msg, "tool_call_id"):
                    msg_data["tool_call_id"] = msg.tool_call_id
                serialized_history.append(msg_data)

            log_data = {
                "input_system": prompt_template,
                "input_user": user_data,
                "raw_output": response.content if response else "",
                "parsed_output": parsed_json,
                "messages_history": serialized_history,
                "structured_output_enabled": structured_output_enabled,
                "fallback_attempted": fallback_attempted,
            }
            save_json(log_file_path, log_data)

        return parsed_json
            
    except Exception as e:
        if fallback_attempted:
            raw_output = str(last_raw_output) if last_raw_output is not None else "<empty>"
            logging.error(f"FirstGate fallback invoke failed with exception. Last raw output: {raw_output[:1000]}")
        logging.error(f"LLM Invocation Error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return None

def run_news_gate():
    """Process incremental news through FirstGate."""
    raw_news = load_json(NEWS_DATA_PATH, [])
    loaded_gated_news = load_json(NEWS_GATED_PATH, [])
    gated_news = _prune_contradictory_news_gated_items(loaded_gated_news)
    gated_pool_pruned = len(gated_news) != len(loaded_gated_news)
    
    history_path = os.path.join(PROJECT_ROOT, "news", "firstgate", "evaluated_ids.json")
    evaluated_news_ids = set(load_json(history_path, []))
    
    new_news = []
    for item in raw_news:
        nid = str(item.get("id") or item.get("news_url") or item.get("title"))
        if nid and nid not in evaluated_news_ids:
            item["id"] = nid
            new_news.append(item)
    
    if new_news:
        # 0. Pre-filter sensitive items
        filtered_new_news = filter_sensitive_items(new_news)
        
        # If items were filtered out, mark them as evaluated so we don't keep retrying them
        for item in new_news:
            if item not in filtered_new_news:
                evaluated_news_ids.add(item["id"])
                
        if not filtered_new_news:
            logging.info("All new news items were blocked by censorship filter.")
            now_ts = time.time()
            _write_firstgate_progress(
                {
                    "gate": "news_firstgate",
                    "status": "idle",
                    "status_label": "Idle",
                    "message": "All new items were blocked by censorship filter.",
                    "total_items": 0,
                    "batch_size": BATCH_SIZE,
                    "total_batches": 0,
                    "started_batches": 0,
                    "completed_batches": 0,
                    "current_batch": 0,
                    "progress_pct": 0.0,
                    "progress_pct_text": "0.0%",
                    "progress_text": "No runnable items",
                    "started_at_bj": "",
                    "last_update_bj": _fmt_bj(now_ts),
                    "completed_at_bj": "",
                    "eta_bj": "",
                    "last_error": "",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            save_json(history_path, list(evaluated_news_ids))
            return
            
        logging.info(f"Found {len(filtered_new_news)} new news items. Running News FirstGate...")
        if not os.path.exists(NEWS_PROMPT_PATH):
            logging.error(f"Prompt file missing: {NEWS_PROMPT_PATH}")
            now_ts = time.time()
            _write_firstgate_progress(
                {
                    "gate": "news_firstgate",
                    "status": "aborted",
                    "status_label": "Aborted",
                    "message": "FirstGate prompt file is missing.",
                    "total_items": len(filtered_new_news),
                    "batch_size": BATCH_SIZE,
                    "total_batches": _firstgate_total_batches(len(filtered_new_news), BATCH_SIZE),
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
                    "last_error": f"Prompt file missing: {NEWS_PROMPT_PATH}",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            return
            
        with open(NEWS_PROMPT_PATH, "r", encoding='utf-8') as f:
            news_prompt = f.read()

        score_calibration = _load_score_calibration(
            source_type="news",
            gated_path=NEWS_GATED_PATH,
            firstgate_log_dir=os.path.join(PROJECT_ROOT, "news", "firstgatelog"),
            output_path=NEWS_SCORE_CALIBRATION_PATH,
        )
        all_evaluated_results = []
        all_keep_results = []
        has_error = False
        batch_traces = []
        start_ts = time.time()
        total_items = len(filtered_new_news)
        total_batches = _firstgate_total_batches(total_items, BATCH_SIZE)
        _write_firstgate_progress(
            {
                "gate": "news_firstgate",
                "status": "running",
                "status_label": "Running",
                "message": "FirstGate is processing new news items.",
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
        log_dir = os.path.join(PROJECT_ROOT, "news", "firstgatelog")
        os.makedirs(log_dir, exist_ok=True)
        
        for i in range(0, len(filtered_new_news), BATCH_SIZE):
            batch = filtered_new_news[i:i + BATCH_SIZE]
            batch_idx = i // BATCH_SIZE + 1
            now_ts = time.time()
            _write_firstgate_progress(
                {
                    "gate": "news_firstgate",
                    "status": "running",
                    "status_label": "Running",
                    "message": "FirstGate is processing new news items.",
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
                    "eta_bj": _firstgate_eta_bj(start_ts, max(1, batch_idx - 1), total_batches),
                    "last_error": "",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            logging.info(f"Processing News FirstGate batch {batch_idx} ({len(batch)} items)...")

            batch_user_payload = _build_firstgate_user_payload(
                batch,
                score_calibration,
                source_type="news",
                suppress_empty_full_content=True,
            )
            batch_log_file = os.path.join(
                log_dir,
                f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_idx}.json",
            )
            raw_results = invoke_model(
                news_prompt,
                batch_user_payload,
                log_file_path=batch_log_file,
                include_emergency=True,
            )

            if raw_results is None:
                logging.error(f"News FirstGate batch {i//BATCH_SIZE + 1} aborted due to model invocation failure.")
                now_ts = time.time()
                completed_batches = i // BATCH_SIZE
                pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
                _write_firstgate_progress(
                    {
                        "gate": "news_firstgate",
                        "status": "aborted",
                        "status_label": "Aborted",
                        "message": "FirstGate batch invocation failed.",
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
                        "last_error": f"Batch {batch_idx} model invocation failed",
                        "updated_at_bj": _fmt_bj(now_ts),
                    }
                )
                has_error = True
                break

            evaluated_results, keep_results, normalize_error = _normalize_firstgate_results(
                raw_results,
                batch=batch,
                include_emergency=True,
                require_all_ids=False,
                keep_only_contract=True,
            )
            if evaluated_results is None or keep_results is None:
                logging.error(f"News FirstGate batch {batch_idx} normalization failed: {normalize_error}")
                now_ts = time.time()
                completed_batches = i // BATCH_SIZE
                pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
                _write_firstgate_progress(
                    {
                        "gate": "news_firstgate",
                        "status": "aborted",
                        "status_label": "Aborted",
                        "message": "FirstGate batch normalization failed.",
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
                        "last_error": normalize_error or f"Batch {batch_idx} normalization failed",
                        "updated_at_bj": _fmt_bj(now_ts),
                    }
                )
                has_error = True
                break
                
            decision_mismatch_ids = [
                str(item.get("id") or "")
                for item in evaluated_results
                if str(item.get("decision") or "").strip().lower() not in {"", "keep"}
            ]
            if decision_mismatch_ids:
                logging.warning(
                    "News FirstGate keep-only batch %s returned non-keep decisions: %s",
                    batch_idx,
                    decision_mismatch_ids,
                )

            batch_traces.append(
                {
                    "batch_idx": batch_idx,
                    "batch_size": len(batch),
                    "input": batch,
                    "evaluated_results": evaluated_results,
                    "keep_results": keep_results,
                    "decision_mismatch_ids": decision_mismatch_ids,
                }
            )
            all_evaluated_results.extend(evaluated_results)
            all_keep_results.extend(keep_results)
            now_ts = time.time()
            completed_batches = batch_idx
            pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
            _write_firstgate_progress(
                {
                    "gate": "news_firstgate",
                    "status": "running",
                    "status_label": "Running",
                    "message": "FirstGate is processing new news items.",
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
                    "eta_bj": _firstgate_eta_bj(start_ts, completed_batches, total_batches),
                    "last_error": "",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            
        if has_error:
            logging.error("News FirstGate aborted due to partial batch failure. IDs will not be marked as evaluated to allow retry.")
            return
            
        keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
        emergency_news_items = []
        
        for item in filtered_new_news:
            nid = item["id"]
            evaluated_news_ids.add(nid)
            if nid in keep_ids:
                result_item = next((r for r in all_keep_results if str(r.get("id")) == nid), {})
                item["gate_reason"] = result_item.get("reason", "")
                # Ensure importance_score is an integer
                try:
                    item["importance_score"] = int(result_item.get("importance_score", 5))
                except (ValueError, TypeError):
                    item["importance_score"] = 5
                item["emergency"] = _coerce_bool(result_item.get("emergency", False))
                item["gated_at"] = datetime.now().isoformat()
                gated_news.append(item)
                if item["emergency"]:
                    emergency_news_items.append(item)
        
        # Sort gated news by importance_score (descending)
        gated_news.sort(key=lambda x: x.get("importance_score", 0), reverse=True)
                
        save_json(NEWS_GATED_PATH, gated_news)
        save_json(history_path, list(evaluated_news_ids))
        _trigger_emergency_long_hunter(emergency_news_items)

        summary_log_file = os.path.join(
            log_dir, f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        save_json(
            summary_log_file,
            {
                "source": "news_firstgate",
                "total_batches": len(batch_traces),
                "total_input_items": len(filtered_new_news),
                "total_evaluated_results": len(all_evaluated_results),
                "total_keep_results": len(all_keep_results),
                "decision_mismatch_count": sum(len(batch.get("decision_mismatch_ids", [])) for batch in batch_traces),
                "emergency_count": len(emergency_news_items),
                "emergency_ids": [str(item.get("id") or "") for item in emergency_news_items],
                "score_calibration": score_calibration,
                "batches": batch_traces,
            },
        )
        
        # Save log
        log_file = os.path.join(PROJECT_ROOT, "news", "firstgate", f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        save_json(
            log_file,
            {
                "incremental": filtered_new_news,
                "evaluated_results": all_evaluated_results,
                "keep_results": all_keep_results,
                "decision_mismatch_count": sum(len(batch.get("decision_mismatch_ids", [])) for batch in batch_traces),
                "emergency_count": len(emergency_news_items),
                "emergency_ids": [str(item.get("id") or "") for item in emergency_news_items],
                "score_calibration": score_calibration,
            },
        )
        end_ts = time.time()
        _write_firstgate_progress(
            {
                "gate": "news_firstgate",
                "status": "completed",
                "status_label": "Completed",
                "message": "FirstGate completed successfully.",
                "total_items": total_items,
                "batch_size": BATCH_SIZE,
                "total_batches": total_batches,
                "started_batches": total_batches,
                "completed_batches": total_batches,
                "current_batch": total_batches,
                "progress_pct": 100.0,
                "progress_pct_text": "100.0%",
                "progress_text": f"{total_batches}/{total_batches} batches",
                "started_at_bj": _fmt_bj(start_ts),
                "last_update_bj": _fmt_bj(end_ts),
                "completed_at_bj": _fmt_bj(end_ts),
                "eta_bj": _fmt_bj(end_ts),
                "last_error": "",
                "updated_at_bj": _fmt_bj(end_ts),
                "keep_count": len(keep_ids),
                "emergency_count": len(emergency_news_items),
            }
        )
        logging.info(f"News FirstGate completed. {len(keep_ids)} items added to gated.json")
    else:
        if gated_pool_pruned:
            save_json(NEWS_GATED_PATH, gated_news)
        now_ts = time.time()
        _write_firstgate_progress(
            {
                "gate": "news_firstgate",
                "status": "idle",
                "status_label": "Idle",
                "message": "No new news items.",
                "total_items": 0,
                "batch_size": BATCH_SIZE,
                "total_batches": 0,
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "No new news items",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )

def run_poly_gate():
    """Process incremental Polymarket data through FirstGate."""
    raw_poly_dict = load_json(POLYMARKET_DATA_PATH, {})
    # Convert grouped data to list if necessary
    raw_poly = []
    if isinstance(raw_poly_dict, dict):
        for event_id, info in raw_poly_dict.items():
            info["event_id"] = event_id
            raw_poly.append(info)
            
    gated_poly = load_json(POLY_GATED_PATH, [])
    
    poly_history_path = os.path.join(PROJECT_ROOT, "polymarket", "firstgate", "evaluated_ids.json")
    evaluated_poly_ids = set(load_json(poly_history_path, []))
    
    new_poly = []
    for item in raw_poly:
        pid = str(item.get("event_id") or item.get("question"))
        if pid and pid not in evaluated_poly_ids:
            item["id"] = pid
            new_poly.append(item)
            
    if new_poly:
        # 0. Pre-filter sensitive items
        filtered_new_poly = filter_sensitive_items(new_poly)
        
        # If items were filtered out, mark them as evaluated so we don't keep retrying them
        for item in new_poly:
            if item not in filtered_new_poly:
                evaluated_poly_ids.add(item["id"])
                
        if not filtered_new_poly:
            logging.info("All new Polymarket items were blocked by censorship filter.")
            now_ts = time.time()
            _write_poly_firstgate_progress(
                {
                    "gate": "polymarket_firstgate",
                    "status": "idle",
                    "status_label": "Idle",
                    "message": "All new items were blocked by censorship filter.",
                    "total_items": 0,
                    "batch_size": BATCH_SIZE,
                    "total_batches": 0,
                    "started_batches": 0,
                    "completed_batches": 0,
                    "current_batch": 0,
                    "progress_pct": 0.0,
                    "progress_pct_text": "0.0%",
                    "progress_text": "No runnable items",
                    "started_at_bj": "",
                    "last_update_bj": _fmt_bj(now_ts),
                    "completed_at_bj": "",
                    "eta_bj": "",
                    "last_error": "",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            save_json(poly_history_path, list(evaluated_poly_ids))
            return
            
        logging.info(f"Found {len(filtered_new_poly)} new polymarket items. Running Poly FirstGate...")
        if not os.path.exists(POLY_PROMPT_PATH):
            logging.error(f"Prompt file missing: {POLY_PROMPT_PATH}")
            now_ts = time.time()
            _write_poly_firstgate_progress(
                {
                    "gate": "polymarket_firstgate",
                    "status": "aborted",
                    "status_label": "Aborted",
                    "message": "FirstGate prompt file is missing.",
                    "total_items": len(filtered_new_poly),
                    "batch_size": BATCH_SIZE,
                    "total_batches": _firstgate_total_batches(len(filtered_new_poly), BATCH_SIZE),
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
                    "last_error": f"Prompt file missing: {POLY_PROMPT_PATH}",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            return

        with open(POLY_PROMPT_PATH, "r", encoding='utf-8') as f:
            poly_prompt = f.read()

        score_calibration = _load_score_calibration(
            source_type="polymarket",
            gated_path=POLY_GATED_PATH,
            firstgate_log_dir=os.path.join(PROJECT_ROOT, "polymarket", "firstgatelog"),
            output_path=POLY_SCORE_CALIBRATION_PATH,
        )
        all_evaluated_results = []
        all_keep_results = []
        has_error = False
        batch_traces = []
        start_ts = time.time()
        total_items = len(filtered_new_poly)
        total_batches = _firstgate_total_batches(total_items, BATCH_SIZE)
        _write_poly_firstgate_progress(
            {
                "gate": "polymarket_firstgate",
                "status": "running",
                "status_label": "Running",
                "message": "FirstGate is processing new polymarket items.",
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
        log_dir = os.path.join(PROJECT_ROOT, "polymarket", "firstgatelog")
        os.makedirs(log_dir, exist_ok=True)
        
        for i in range(0, len(filtered_new_poly), BATCH_SIZE):
            batch = filtered_new_poly[i:i + BATCH_SIZE]
            batch_idx = i // BATCH_SIZE + 1
            now_ts = time.time()
            _write_poly_firstgate_progress(
                {
                    "gate": "polymarket_firstgate",
                    "status": "running",
                    "status_label": "Running",
                    "message": "FirstGate is processing new polymarket items.",
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
                    "eta_bj": _firstgate_eta_bj(start_ts, max(1, batch_idx - 1), total_batches),
                    "last_error": "",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            logging.info(f"Processing Poly FirstGate batch {batch_idx} ({len(batch)} items)...")

            batch_user_payload = _build_firstgate_user_payload(
                batch,
                score_calibration,
                source_type="polymarket",
            )
            batch_log_file = os.path.join(
                log_dir,
                f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_idx}.json",
            )
            raw_results = invoke_model(poly_prompt, batch_user_payload, log_file_path=batch_log_file)

            if raw_results is None:
                logging.error(f"Poly FirstGate batch {i//BATCH_SIZE + 1} aborted due to model invocation failure.")
                now_ts = time.time()
                completed_batches = i // BATCH_SIZE
                pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
                _write_poly_firstgate_progress(
                    {
                        "gate": "polymarket_firstgate",
                        "status": "aborted",
                        "status_label": "Aborted",
                        "message": "FirstGate batch invocation failed.",
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
                        "last_error": f"Batch {batch_idx} model invocation failed",
                        "updated_at_bj": _fmt_bj(now_ts),
                    }
                )
                has_error = True
                break

            evaluated_results, keep_results, normalize_error = _normalize_firstgate_results(
                raw_results,
                batch=batch,
                include_emergency=False,
                require_all_ids=True,
                keep_only_contract=False,
            )
            if evaluated_results is None or keep_results is None:
                logging.error(f"Poly FirstGate batch {batch_idx} normalization failed: {normalize_error}")
                now_ts = time.time()
                completed_batches = i // BATCH_SIZE
                pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
                _write_poly_firstgate_progress(
                    {
                        "gate": "polymarket_firstgate",
                        "status": "aborted",
                        "status_label": "Aborted",
                        "message": "FirstGate batch normalization failed.",
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
                        "last_error": normalize_error or f"Batch {batch_idx} normalization failed",
                        "updated_at_bj": _fmt_bj(now_ts),
                    }
                )
                has_error = True
                break
                
            batch_traces.append(
                {
                    "batch_idx": batch_idx,
                    "batch_size": len(batch),
                    "input": batch,
                    "evaluated_results": evaluated_results,
                    "keep_results": keep_results,
                }
            )
            all_evaluated_results.extend(evaluated_results)
            all_keep_results.extend(keep_results)
            now_ts = time.time()
            completed_batches = batch_idx
            pct = 0.0 if total_batches <= 0 else min(99.9, (completed_batches / max(1, total_batches)) * 100.0)
            _write_poly_firstgate_progress(
                {
                    "gate": "polymarket_firstgate",
                    "status": "running",
                    "status_label": "Running",
                    "message": "FirstGate is processing new polymarket items.",
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
                    "eta_bj": _firstgate_eta_bj(start_ts, completed_batches, total_batches),
                    "last_error": "",
                    "updated_at_bj": _fmt_bj(now_ts),
                }
            )
            
        if has_error:
            logging.error("Poly FirstGate aborted due to partial batch failure. IDs will not be marked as evaluated to allow retry.")
            return
            
        keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
        
        for item in filtered_new_poly:
            pid = item["id"]
            evaluated_poly_ids.add(pid)
            if pid in keep_ids:
                result_item = next((r for r in all_keep_results if str(r.get("id")) == pid), {})
                item["gate_reason"] = result_item.get("reason", "")
                # Ensure importance_score is an integer
                try:
                    item["importance_score"] = int(result_item.get("importance_score", 5))
                except (ValueError, TypeError):
                    item["importance_score"] = 5
                item["gated_at"] = datetime.now().isoformat()
                gated_poly.append(item)
        
        # Sort gated poly by importance_score (descending)
        gated_poly.sort(key=lambda x: x.get("importance_score", 0), reverse=True)
                
        save_json(POLY_GATED_PATH, gated_poly)
        save_json(poly_history_path, list(evaluated_poly_ids))

        summary_log_file = os.path.join(
            log_dir, f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        save_json(
            summary_log_file,
            {
                "source": "polymarket_firstgate",
                "total_batches": len(batch_traces),
                "total_input_items": len(filtered_new_poly),
                "total_evaluated_results": len(all_evaluated_results),
                "total_keep_results": len(all_keep_results),
                "score_calibration": score_calibration,
                "batches": batch_traces,
            },
        )

        log_file = os.path.join(PROJECT_ROOT, "polymarket", "firstgate", f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        save_json(
            log_file,
            {
                "incremental": filtered_new_poly,
                "evaluated_results": all_evaluated_results,
                "keep_results": all_keep_results,
                "score_calibration": score_calibration,
            },
        )
        end_ts = time.time()
        _write_poly_firstgate_progress(
            {
                "gate": "polymarket_firstgate",
                "status": "completed",
                "status_label": "Completed",
                "message": "FirstGate completed successfully.",
                "total_items": total_items,
                "batch_size": BATCH_SIZE,
                "total_batches": total_batches,
                "started_batches": total_batches,
                "completed_batches": total_batches,
                "current_batch": total_batches,
                "progress_pct": 100.0,
                "progress_pct_text": "100.0%",
                "progress_text": f"{total_batches}/{total_batches} batches",
                "started_at_bj": _fmt_bj(start_ts),
                "last_update_bj": _fmt_bj(end_ts),
                "completed_at_bj": _fmt_bj(end_ts),
                "eta_bj": _fmt_bj(end_ts),
                "last_error": "",
                "updated_at_bj": _fmt_bj(end_ts),
                "keep_count": len(keep_ids),
            }
        )
        logging.info(f"Poly FirstGate completed. {len(keep_ids)} items added to gated.json")
    else:
        now_ts = time.time()
        _write_poly_firstgate_progress(
            {
                "gate": "polymarket_firstgate",
                "status": "idle",
                "status_label": "Idle",
                "message": "No new polymarket items.",
                "total_items": 0,
                "batch_size": BATCH_SIZE,
                "total_batches": 0,
                "started_batches": 0,
                "completed_batches": 0,
                "current_batch": 0,
                "progress_pct": 0.0,
                "progress_pct_text": "0.0%",
                "progress_text": "No new polymarket items",
                "started_at_bj": "",
                "last_update_bj": _fmt_bj(now_ts),
                "completed_at_bj": "",
                "eta_bj": "",
                "last_error": "",
                "updated_at_bj": _fmt_bj(now_ts),
            }
        )

def main():
    run_news_gate()
    run_poly_gate()

if __name__ == "__main__":
    main()
