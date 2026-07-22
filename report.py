from env_config import load_project_env
load_project_env()

import json
import os
import re
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from langchain_openai import ChatOpenAI

from strategy import get_long_hunter_state_snapshot, trigger_manual_long_hunter
from tools import safe_json_dump, safe_json_read


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
MEM_DIR = os.path.join(PROJECT_ROOT, "mem")
PREDICTIONS_PATH = os.path.join(MEM_DIR, "predictions.json")
NEWS_GATED_PATH = os.path.join(PROJECT_ROOT, "news", "gated.json")
POLY_GATED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "gated.json")
CACHE_VERSION = "long_hunter_v4_quant_overlay"
SERVICE_UNITS = ["report", "god", "polymarket", "news"]
SIGNAL_BUCKETS = ("active", "secondgate_skipped", "secondgate_dropped", "firstgate_dropped")
DEFAULT_SIGNAL_PAGE_SIZE = 20
MAX_SIGNAL_PAGE_SIZE = 100
NEWS_FIRST_GATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "news", "firstgate", "progress.json")
NEWS_SECOND_GATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "news", "secondgate", "progress.json")
NEWS_SECOND_GATE_LAST_RUN_PATH = os.path.join(PROJECT_ROOT, "news", "secondgate", "last_run.txt")
NEWS_SECOND_GATE_SKIPPED_PATH = os.path.join(PROJECT_ROOT, "news", "secondgate", "skipped_items.json")
POLY_FIRST_GATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "polymarket", "firstgate", "progress.json")
POLY_SECOND_GATE_PROGRESS_PATH = os.path.join(PROJECT_ROOT, "polymarket", "secondgate", "progress.json")
POLY_SECOND_GATE_LAST_RUN_PATH = os.path.join(PROJECT_ROOT, "polymarket", "secondgate", "last_run.txt")
POLY_SECOND_GATE_SKIPPED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "secondgate", "skipped_items.json")
NEWS_SECOND_GATE_INTERVAL_SECONDS = 3 * 3600
POLY_SECOND_GATE_INTERVAL_SECONDS = 3 * 3600
GATE_CACHE_PATHS = (
    NEWS_GATED_PATH,
    POLY_GATED_PATH,
    os.path.join(PROJECT_ROOT, "news", "firstgatelog"),
    os.path.join(PROJECT_ROOT, "news", "secondgatelog"),
    NEWS_SECOND_GATE_SKIPPED_PATH,
    os.path.join(PROJECT_ROOT, "polymarket", "firstgatelog"),
    os.path.join(PROJECT_ROOT, "polymarket", "secondgatelog"),
    POLY_SECOND_GATE_SKIPPED_PATH,
)

LLM_API_KEY = os.getenv("LLMAPIKEY")
LLM_BASE_URL = os.getenv("LLMBASEURL")
LLM_MODEL_ID = os.getenv("LLMMODELID", "gpt-4.1")

app = Flask(__name__)
_MEMO_CACHE: Dict[str, Dict[str, Any]] = {}


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


def _load_json_file(path: str, default: Any) -> Any:
    data = safe_json_read(path, os.path.basename(path), use_lock=True)
    return deepcopy(default) if data is None else data


def _run_command(args: List[str], timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _path_signature(path: str) -> tuple:
    try:
        stat = os.stat(path)
        return (path, int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (path, 0, 0)


def _paths_signature(paths: List[str] | tuple) -> tuple:
    return tuple(_path_signature(path) for path in paths)


def _memoized(name: str, signature: Any, builder) -> Any:
    cached = _MEMO_CACHE.get(name)
    if cached and cached.get("signature") == signature:
        return cached.get("value")
    value = builder()
    _MEMO_CACHE[name] = {"signature": signature, "value": value}
    return value


def _normalize_service_name(value: Any) -> str:
    name = str(value or "").strip()
    if name.endswith(".service"):
        name = name[:-8]
    return name


def _service_unit(name: str) -> str:
    return f"{name}.service"


def _service_logs(name: str, lines: int = 120) -> str:
    result = _run_command(
        ["journalctl", "-u", _service_unit(name), "-n", str(lines), "--no-pager", "-o", "short-iso"],
        timeout=12,
    )
    output = (result.stdout or result.stderr or "").strip()
    return output or "No logs."


def _service_snapshot(name: str, include_logs: bool = True, log_lines: int = 120) -> Dict[str, Any]:
    show = _run_command(
        [
            "systemctl",
            "show",
            _service_unit(name),
            "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,Description",
            "--value",
        ]
    )
    lines = (show.stdout or "").splitlines()
    while len(lines) < 6:
        lines.append("")
    snapshot = {
        "name": name,
        "unit": _service_unit(name),
        "load_state": lines[0].strip(),
        "active_state": lines[1].strip(),
        "sub_state": lines[2].strip(),
        "unit_file_state": lines[3].strip(),
        "main_pid": lines[4].strip(),
        "description": lines[5].strip(),
        "logs": "",
    }
    if include_logs:
        snapshot["logs"] = _service_logs(name, lines=log_lines)
    return snapshot


def _all_service_snapshots(include_logs: bool = True, log_lines: int = 120) -> List[Dict[str, Any]]:
    return [_service_snapshot(name, include_logs=include_logs, log_lines=log_lines) for name in SERVICE_UNITS]


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not isinstance(dt, datetime):
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _load_gate_next_run(last_run_path: str, interval_seconds: int) -> Dict[str, Any]:
    result = {
        "last_run_at_bj": "",
        "next_run_at_bj": "",
        "seconds_until_next": None,
    }
    try:
        raw = _read_text(last_run_path).strip()
        if not raw:
            return result
        last_epoch = float(raw)
        last_dt = datetime.fromtimestamp(last_epoch)
        next_dt = last_dt + timedelta(seconds=interval_seconds)
        result["last_run_at_bj"] = _fmt_dt(last_dt)
        result["next_run_at_bj"] = _fmt_dt(next_dt)
        result["seconds_until_next"] = int((next_dt - datetime.now()).total_seconds())
    except Exception:
        return result
    return result


def _default_gate_progress(gate_name: str) -> Dict[str, Any]:
    return {
        "gate": gate_name,
        "status": "idle",
        "status_label": "Idle",
        "message": "No progress data yet.",
        "total_items": 0,
        "batch_size": 0,
        "total_batches": 0,
        "started_batches": 0,
        "completed_batches": 0,
        "current_batch": 0,
        "progress_pct": 0.0,
        "progress_pct_text": "0.0%",
        "progress_text": "No progress data yet.",
        "started_at_bj": "",
        "last_update_bj": "",
        "completed_at_bj": "",
        "eta_bj": "",
        "last_error": "",
        "updated_at_bj": _fmt_dt(datetime.now()),
    }


def _load_gate_progress_file(path: str, gate_name: str) -> Dict[str, Any]:
    payload = _load_json_file(path, {})
    if not isinstance(payload, dict):
        return _default_gate_progress(gate_name)
    merged = _default_gate_progress(gate_name)
    merged.update(payload)
    merged["gate"] = gate_name
    merged["updated_at_bj"] = str(payload.get("updated_at_bj") or merged["updated_at_bj"])
    return merged


def _load_gate_progress_snapshot() -> Dict[str, Any]:
    now = datetime.now()
    news_secondgate = _load_gate_progress_file(NEWS_SECOND_GATE_PROGRESS_PATH, "news_secondgate")
    news_secondgate.update(_load_gate_next_run(NEWS_SECOND_GATE_LAST_RUN_PATH, NEWS_SECOND_GATE_INTERVAL_SECONDS))
    poly_secondgate = _load_gate_progress_file(POLY_SECOND_GATE_PROGRESS_PATH, "polymarket_secondgate")
    poly_secondgate.update(_load_gate_next_run(POLY_SECOND_GATE_LAST_RUN_PATH, POLY_SECOND_GATE_INTERVAL_SECONDS))
    news = {
        "firstgate": _load_gate_progress_file(NEWS_FIRST_GATE_PROGRESS_PATH, "news_firstgate"),
        "secondgate": news_secondgate,
    }
    polymarket = {
        "firstgate": _load_gate_progress_file(POLY_FIRST_GATE_PROGRESS_PATH, "polymarket_firstgate"),
        "secondgate": poly_secondgate,
    }
    return {
        "updated_at_bj": _fmt_dt(now),
        "source": "state_files",
        "news": news,
        "polymarket": polymarket,
        "firstgate": news["firstgate"],
        "secondgate": news["secondgate"],
    }


def _run_dirs() -> List[str]:
    if not os.path.exists(LOG_DIR):
        return []
    names = []
    for name in os.listdir(LOG_DIR):
        if re.fullmatch(r"\d{8}_\d{6}", name) and os.path.isdir(os.path.join(LOG_DIR, name)):
            if os.path.exists(os.path.join(LOG_DIR, name, "prediction.json")):
                names.append(name)
    return sorted(names, reverse=True)


def _load_run(run_id: str) -> Optional[Dict[str, Any]]:
    if not re.fullmatch(r"\d{8}_\d{6}", run_id or ""):
        return None
    folder = os.path.join(LOG_DIR, run_id)
    if not os.path.isdir(folder):
        return None
    prediction = _load_json_file(os.path.join(folder, "prediction.json"), {})
    output = _load_json_file(os.path.join(folder, "output.json"), {})
    input_md = _read_text(os.path.join(folder, "input.md"))
    raw_response = _read_text(os.path.join(folder, "long_hunter_raw_response.txt"))
    context_dir = os.path.join(folder, "context")
    context = {
        "news_gated": _load_json_file(os.path.join(context_dir, "news_gated.json"), []),
        "polymarket_gated": _load_json_file(os.path.join(context_dir, "polymarket_gated.json"), []),
        "market_context": _load_json_file(os.path.join(context_dir, "market_context.json"), {}),
        "quant_research": _load_json_file(os.path.join(context_dir, "quant_research.json"), {}),
        "backtest_metrics": _load_json_file(os.path.join(context_dir, "backtest_metrics.json"), {}),
        "whale_summary_raw": _load_json_file(os.path.join(context_dir, "whale_summary_raw.json"), {}),
        "whale_summary_calls": _load_json_file(os.path.join(context_dir, "whale_summary_calls.json"), []),
        "whale_summary_md": _read_text(os.path.join(context_dir, "whale_summary.md")),
        "prior_predictions": _load_json_file(os.path.join(context_dir, "prior_predictions.json"), []),
    }
    return {
        "run_id": run_id,
        "prediction": prediction if isinstance(prediction, dict) else {},
        "output": output if isinstance(output, dict) else {},
        "input_md": input_md,
        "raw_response": raw_response,
        "context": context,
    }


def _run_summary(run_id: str) -> Dict[str, Any]:
    folder = os.path.join(LOG_DIR, run_id)
    prediction_path = os.path.join(folder, "prediction.json")
    output_path = os.path.join(folder, "output.json")
    signature = (_path_signature(prediction_path), _path_signature(output_path))

    def build_summary() -> Dict[str, Any]:
        prediction = _load_json_file(prediction_path, {})
        output = _load_json_file(output_path, {})
        if not isinstance(prediction, dict):
            prediction = {}
        if not isinstance(output, dict):
            output = {}
        forecasts = prediction.get("forecasts", []) if isinstance(prediction.get("forecasts"), list) else []
        return {
            "run_id": run_id,
            "created_at_bj": prediction.get("created_at_bj", ""),
            "symbol": prediction.get("symbol", "BTCUSDT"),
            "current_price": prediction.get("current_price"),
            "forecast_count": len(forecasts),
            "directions": sorted({str(item.get("direction", "unknown")) for item in forecasts if isinstance(item, dict)}),
            "targets": [
                {
                    "target_date": item.get("target_date"),
                    "horizon_days": item.get("horizon_days"),
                    "price_target": item.get("price_target"),
                    "price_low": item.get("price_low"),
                    "price_high": item.get("price_high"),
                    "direction": item.get("direction"),
                    "confidence": item.get("confidence"),
                }
                for item in forecasts
                if isinstance(item, dict)
            ],
            "narrative_preview": str(prediction.get("narrative_report", ""))[:260],
            "validation_status": output.get("validation_status", ""),
        }

    return dict(_memoized(f"run_summary:{run_id}", signature, build_summary))


def _chat_path(run_id: str) -> str:
    return os.path.join(LOG_DIR, run_id, "chat.json")


def _chat_trace_path(run_id: str) -> str:
    return os.path.join(LOG_DIR, run_id, "chat_trace.jsonl")


def _append_chat_trace(run_id: str, payload: Dict[str, Any]) -> None:
    record = {"logged_at": datetime.now().isoformat(), **payload}
    try:
        with open(_chat_trace_path(run_id), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        app.logger.warning("failed to append chat trace for %s: %s", run_id, exc)


def _clean_chat_history(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", ""))
        if role not in {"user", "assistant"} or not content.strip():
            continue
        stripped = content.strip()
        if role == "assistant" and stripped in {"{", "[", "{\n", "[\n"}:
            continue
        normalized: Dict[str, Any] = {"role": role, "content": content}
        created_at = str(item.get("created_at", "")).strip()
        if created_at:
            normalized["created_at"] = created_at
        cleaned.append(normalized)
    return cleaned


def _load_chat(run_id: str) -> List[Dict[str, Any]]:
    data = _load_json_file(_chat_path(run_id), [])
    return _clean_chat_history(data)


def _new_chat_model() -> ChatOpenAI:
    if not LLM_API_KEY:
        raise RuntimeError("LLMAPIKEY is required.")
    auth_key = LLM_API_KEY.replace("Bearer ", "", 1) if LLM_API_KEY.startswith("Bearer ") else LLM_API_KEY
    kwargs: Dict[str, Any] = {
        "api_key": auth_key,
        "model": LLM_MODEL_ID,
        "temperature": 0.25,
    }
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    return ChatOpenAI(**kwargs)


def _extract_chat_message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = getattr(value, "content", None)
    if content is not None and content is not value:
        return _extract_chat_message_text(content)
    if isinstance(value, list):
        return "".join(_extract_chat_message_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        return _extract_chat_message_text(value.get("content"))
    return str(value)


def _extract_input_prompt_sections(input_md: str) -> Dict[str, str]:
    text = str(input_md or "")
    system_marker = "--- SYSTEM PROMPT ---"
    user_marker = "--- USER PROMPT ---"
    if system_marker in text and user_marker in text:
        after_system = text.split(system_marker, 1)[1]
        system_text, user_text = after_system.split(user_marker, 1)
        return {
            "system": system_text.strip(),
            "user": user_text.strip(),
        }
    return {"system": "", "user": text.strip()}


def _strip_prediction_contract(user_prompt: str) -> str:
    text = str(user_prompt or "").strip()
    for marker in ("\n# Decision Policy\n", "\n## Decision Policy\n", "\n# Output Requirement\n", "\n## Output Requirement\n"):
        if marker in text:
            return text.split(marker, 1)[0].rstrip()
    return text


def _prediction_to_chat_text(prediction: Dict[str, Any], run_id: str = "") -> str:
    parts: List[str] = []
    header_bits = []
    if run_id:
        header_bits.append(f"Run ID: {run_id}")
    symbol = str(prediction.get("symbol", "") or "").strip()
    if symbol:
        header_bits.append(f"Symbol: {symbol}")
    created_at = str(prediction.get("created_at_bj", "") or "").strip()
    if created_at:
        header_bits.append(f"Created: {created_at}")
    if header_bits:
        parts.append("这是该轮 Long Hunter 的正式回复。 " + " | ".join(header_bits))
    narrative = str(prediction.get("narrative_report", "") or "").strip()
    if narrative:
        parts.append(narrative)
    forecasts = prediction.get("forecasts", [])
    if isinstance(forecasts, list) and forecasts:
        forecast_lines = []
        for item in forecasts:
            if not isinstance(item, dict):
                continue
            forecast_lines.append(
                " / ".join(
                    part
                    for part in [
                        f"{item.get('horizon_days', '?')}d",
                        str(item.get("direction", "unknown")),
                        f"target {item.get('price_target', '-')}",
                        f"range {item.get('price_low', '-')}-{item.get('price_high', '-')}",
                        f"confidence {item.get('confidence', '-')}",
                    ]
                    if part
                )
                + (f" | thesis: {item.get('thesis', '')}" if item.get("thesis") else "")
            )
        if forecast_lines:
            parts.append("结构化预测摘要:\n" + "\n".join(f"- {line}" for line in forecast_lines))
    risk_notes = prediction.get("risk_notes", [])
    if isinstance(risk_notes, list):
        compact_risks = [str(item).strip() for item in risk_notes if str(item).strip()]
        if compact_risks:
            parts.append("主要风险:\n" + "\n".join(f"- {item}" for item in compact_risks[:8]))
    return "\n\n".join(part for part in parts if part).strip()


def _coerce_chat_plain_text(answer: str, run: Dict[str, Any]) -> str:
    raw = str(answer or "")
    stripped = raw.strip()
    if not stripped:
        return ""
    parsed = _parse_json_blob(stripped)
    if isinstance(parsed, dict) and ("narrative_report" in parsed or "forecasts" in parsed):
        return _prediction_to_chat_text(parsed, str(run.get("run_id", "") or ""))
    if stripped in {"{", "[", "{\n", "[\n"}:
        return _prediction_to_chat_text(run.get("prediction", {}), str(run.get("run_id", "") or "")) or raw
    return raw


def _build_chat_messages(run_id: str, run: Dict[str, Any], history: List[Dict[str, Any]], user_message: str) -> List[Dict[str, str]]:
    prompt_sections = _extract_input_prompt_sections(str(run.get("input_md", "") or ""))
    original_system = prompt_sections.get("system", "").strip()
    original_user = _strip_prediction_contract(prompt_sections.get("user", ""))

    system_prompt = (
        "You are CoinAutomation's BTC Long Hunter follow-up chat companion. "
        "Continue the original run as a plain-text conversation in Chinese. "
        "The original prediction has already been produced, so do not output JSON, markdown fences, or a fresh schema. "
        "Treat the seeded assistant message below as the original model answer for this run, then answer the user's follow-up based on that frozen context. "
        "Answer as the same BTC long-horizon forecaster, using the frozen research context. "
        "If the user asks about newer market data, state clearly that this chat is anchored to the original run context."
    )
    if original_system:
        system_prompt += "\n\nOriginal Long Hunter system role:\n" + original_system

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if original_user:
        messages.append(
            {
                "role": "user",
                "content": "Frozen original user context for this run:\n\n" + original_user,
            }
        )
    else:
        fallback_context = {
            "run_id": run_id,
            "prediction": run.get("prediction", {}),
            "context": run.get("context", {}),
        }
        messages.append(
            {
                "role": "user",
                "content": "Frozen structured run context:\n" + json.dumps(fallback_context, ensure_ascii=False, indent=2),
            }
        )

    seeded_assistant = _prediction_to_chat_text(run.get("prediction", {}), run_id)
    if seeded_assistant:
        messages.append({"role": "assistant", "content": seeded_assistant})

    for item in history[-20:]:
        role = str(item.get("role", "user"))
        content = str(item.get("content", ""))
        if role in {"user", "assistant"} and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    return messages


def _append_chat_history(run_id: str, history: List[Dict[str, Any]], user_message: str, assistant_message: str) -> List[Dict[str, Any]]:
    now = datetime.now().isoformat()
    updated_history = list(history)
    updated_history.append({"role": "user", "content": user_message, "created_at": now})
    updated_history.append({"role": "assistant", "content": assistant_message, "created_at": now})
    safe_json_dump(updated_history, _chat_path(run_id), use_lock=True)
    return updated_history


def _normalize_gated_item(source_type: str, item: Dict[str, Any], idx: int) -> Dict[str, Any]:
    if source_type == "news":
        title = item.get("title") or item.get("headline") or item.get("news_url") or f"news_{idx}"
        source = item.get("source_name") or item.get("source") or "news"
        event_time = item.get("date") or item.get("published_at") or item.get("gated_at") or ""
    else:
        title = item.get("event_title") or item.get("question") or item.get("url") or f"polymarket_{idx}"
        source = item.get("category") or "polymarket"
        event_time = item.get("last_updated") or item.get("gated_at") or ""
    return {
        "id": str(item.get("id") or item.get("event_id") or item.get("news_url") or item.get("url") or f"{source_type}_{idx}"),
        "source_type": source_type,
        "title": str(title),
        "source": str(source),
        "event_time": str(event_time),
        "gated_at": str(item.get("gated_at", "")),
        "recorded_at": str(item.get("recorded_at") or item.get("gated_at") or event_time or ""),
        "importance_score": item.get("importance_score", ""),
        "gate_stage": str(item.get("gate_stage", "live")),
        "gate_decision": item.get("gate_decision", "live_kept"),
        "reason": str(item.get("gate_reason") or item.get("reason") or ""),
        "url": str(item.get("news_url") or item.get("url") or ""),
        "raw": item,
    }


def _parse_json_blob(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _detect_gate_stage(source_file: str) -> str:
    normalized = str(source_file or "").replace("\\", "/")
    if "/firstgatelog/" in normalized or "/firstgate/" in normalized:
        return "firstgate"
    if "/secondgatelog/" in normalized or "/secondgate/" in normalized:
        return "secondgate"
    if normalized.endswith("/gated.json") or normalized.endswith("gated.json"):
        return "live"
    return "unknown"


def _extract_sort_stamp(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[T_ ](\d{2}):(\d{2}):(\d{2})", text)
    if match:
        return "".join(match.groups())
    match = re.search(r"(\d{8})_(\d{6})", text)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return ""


def _format_recorded_at(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[T_ ](\d{2}):(\d{2}):(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}:{match.group(6)}"
    match = re.search(r"(\d{8})_(\d{6})", text)
    if match:
        date_part = match.group(1)
        time_part = match.group(2)
        return (
            f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} "
            f"{time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"
        )
    return text


def _record_sort_key(item: Dict[str, Any]) -> tuple:
    return (
        _extract_sort_stamp(item.get("recorded_at") or item.get("gated_at") or item.get("event_time") or item.get("source_file") or ""),
        str(item.get("source_file") or ""),
        str(item.get("gate_stage") or ""),
        str(item.get("gate_decision") or ""),
    )


def _make_gate_history_record(
    *,
    source_type: str,
    decision: str,
    source_file: str,
    raw_item: Dict[str, Any],
    result_item: Optional[Dict[str, Any]] = None,
    idx: int = 0,
) -> Dict[str, Any]:
    result_item = result_item if isinstance(result_item, dict) else {}
    merged = dict(raw_item) if isinstance(raw_item, dict) else {}
    gate_stage = _detect_gate_stage(source_file)
    recorded_at = merged.get("gated_at") or _format_recorded_at(source_file)
    if result_item:
        result_score = result_item.get("importance_score", result_item.get("raw_score", merged.get("importance_score", "")))
        result_reason = result_item.get("reason", result_item.get("gate_reason", merged.get("gate_reason", "")))
        merged.update(
            {
                "id": result_item.get("id", merged.get("id")),
                "importance_score": result_score,
                "gate_reason": result_reason,
                "gate_stage": gate_stage,
                "gate_decision": decision,
                "recorded_at": recorded_at,
            }
        )
    elif decision == "dropped":
        merged.setdefault("gate_reason", "Dropped by gate output: no keep result was returned for this item in the batch.")
        merged["gate_stage"] = gate_stage
        merged["recorded_at"] = recorded_at
    record = _normalize_gated_item(source_type, merged, idx)
    record["gate_stage"] = gate_stage
    record["gate_decision"] = decision
    record["source_file"] = source_file
    record["recorded_at"] = str(merged.get("recorded_at") or recorded_at or "")
    record["raw"] = merged
    return record


def _load_gate_history_for_source(source_type: str, root_name: str) -> List[Dict[str, Any]]:
    root = os.path.join(PROJECT_ROOT, root_name)
    records: List[Dict[str, Any]] = []
    candidates = [
        os.path.join(root, "firstgatelog"),
        os.path.join(root, "secondgatelog"),
    ]
    for folder in candidates:
        if not os.path.isdir(folder):
            continue
        for filename in sorted(os.listdir(folder), reverse=True)[:120]:
            if not filename.endswith(".json"):
                continue
            path = os.path.join(folder, filename)
            payload = _load_json_file(path, {})
            if not isinstance(payload, dict):
                continue
            source_file = os.path.relpath(path, PROJECT_ROOT)
            parsed_output = payload.get("parsed_output")
            keep_results = payload.get("keep_results")
            result_rows = parsed_output if isinstance(parsed_output, list) else (keep_results if isinstance(keep_results, list) else [])
            raw_rows = _parse_json_blob(payload.get("input_user"))
            if not isinstance(raw_rows, list):
                raw_rows = []
            if raw_rows and isinstance(raw_rows[0], str):
                raw_rows = []
            if not raw_rows:
                batches = payload.get("batches")
                if isinstance(batches, list):
                    for batch in batches:
                        if not isinstance(batch, dict):
                            continue
                        batch_input = batch.get("input")
                        batch_results = batch.get("evaluated_results") or batch.get("keep_results")
                        if isinstance(batch_input, list):
                            raw_rows.extend(item for item in batch_input if isinstance(item, dict))
                        if isinstance(batch_results, list):
                            result_rows.extend(item for item in batch_results if isinstance(item, dict))

            by_id = {str(item.get("id") or item.get("event_id") or item.get("news_url") or item.get("url") or ""): item for item in raw_rows if isinstance(item, dict)}
            kept_ids = set()
            for idx, result in enumerate(result_rows):
                if not isinstance(result, dict):
                    continue
                rid = str(result.get("id") or "")
                decision = str(result.get("decision") or "keep").strip().lower()
                if decision == "keep":
                    kept_ids.add(rid)
                raw_item = by_id.get(rid, {"id": rid})
                records.append(
                    _make_gate_history_record(
                        source_type=source_type,
                        decision="kept" if decision == "keep" else "dropped",
                        source_file=source_file,
                        raw_item=raw_item,
                        result_item=result,
                        idx=idx,
                    )
                )

            for idx, raw_item in enumerate(raw_rows):
                if not isinstance(raw_item, dict):
                    continue
                rid = str(raw_item.get("id") or raw_item.get("event_id") or raw_item.get("news_url") or raw_item.get("url") or "")
                if not rid:
                    continue
                if any(
                    isinstance(result, dict)
                    and str(result.get("id") or "") == rid
                    and str(result.get("decision") or "").strip().lower() == "drop"
                    for result in result_rows
                ):
                    continue
                if rid and rid in kept_ids:
                    continue
                records.append(
                    _make_gate_history_record(
                        source_type=source_type,
                        decision="dropped",
                        source_file=source_file,
                        raw_item=raw_item,
                        idx=idx,
                    )
                )
    return records


def _load_secondgate_skipped_records(source_type: str, path: str) -> List[Dict[str, Any]]:
    payload = _load_json_file(path, [])
    if not isinstance(payload, list):
        return []
    source_file = os.path.relpath(path, PROJECT_ROOT)
    records: List[Dict[str, Any]] = []
    for idx, entry in enumerate(payload):
        if not isinstance(entry, dict):
            continue
        raw_item = entry.get("raw_item") if isinstance(entry.get("raw_item"), dict) else {}
        merged = dict(raw_item)
        item_id = str(entry.get("item_id") or merged.get("id") or merged.get("event_id") or merged.get("news_url") or merged.get("url") or "")
        if item_id:
            merged["id"] = item_id
        skipped_at = str(entry.get("skipped_at_bj") or "")
        merged.update(
            {
                "gate_stage": "secondgate",
                "gate_decision": "skipped",
                "gate_reason": str(entry.get("reason") or "Skipped by SecondGate terminal singleton failure."),
                "recorded_at": skipped_at,
                "skipped_at_bj": skipped_at,
                "skip_error_type": entry.get("error_type", ""),
                "skip_error_message": entry.get("error_message", ""),
                "skip_log_file": entry.get("log_file", ""),
                "skip_bisect_audit_path": entry.get("bisect_audit_path", ""),
                "skip_batch_idx": entry.get("batch_idx", ""),
            }
        )
        record = _normalize_gated_item(source_type, merged, idx)
        record["gate_stage"] = "secondgate"
        record["gate_decision"] = "skipped"
        record["source_file"] = source_file
        record["recorded_at"] = skipped_at
        record["raw"] = merged
        records.append(record)
    return records


def _load_live_gated_records() -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for source_type, path in (("news", NEWS_GATED_PATH), ("polymarket", POLY_GATED_PATH)):
        payload = _load_json_file(path, [])
        if not isinstance(payload, list):
            continue
        source_file = os.path.relpath(path, PROJECT_ROOT)
        for idx, item in enumerate(payload):
            if not isinstance(item, dict):
                continue
            merged = dict(item)
            merged["gate_stage"] = "live"
            merged["gate_decision"] = "live_kept"
            merged["recorded_at"] = merged.get("gated_at") or merged.get("date") or merged.get("last_updated") or ""
            record = _normalize_gated_item(source_type, merged, idx)
            record["gate_stage"] = "live"
            record["gate_decision"] = "live_kept"
            record["recorded_at"] = str(merged.get("recorded_at") or "")
            record["source_file"] = source_file
            record["raw"] = merged
            records.append(record)
    return records


def _gate_data_signature() -> tuple:
    return _paths_signature(GATE_CACHE_PATHS)


def _build_gated_records() -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = _load_live_gated_records()
    records.extend(_load_gate_history_for_source("news", "news"))
    records.extend(_load_gate_history_for_source("polymarket", "polymarket"))
    records.extend(_load_secondgate_skipped_records("news", NEWS_SECOND_GATE_SKIPPED_PATH))
    records.extend(_load_secondgate_skipped_records("polymarket", POLY_SECOND_GATE_SKIPPED_PATH))

    seen = set()
    deduped = []
    for item in records:
        key = (
            item.get("source_type"),
            item.get("id"),
            item.get("gate_stage"),
            item.get("gate_decision"),
            item.get("source_file", ""),
            item.get("recorded_at") or item.get("gated_at", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return sorted(
        deduped,
        key=_record_sort_key,
        reverse=True,
    )


def _load_gated_records() -> List[Dict[str, Any]]:
    return _memoized("gated_records", _gate_data_signature(), _build_gated_records)


def _pick_preferred_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_signal_source(value: Any) -> str:
    source = str(value or "all").strip().lower()
    return source if source in {"all", "news", "polymarket"} else "all"


def _parse_int_with_bounds(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _signal_matches_search(item: Dict[str, Any], keyword: str) -> bool:
    normalized = str(keyword or "").strip().lower()
    if not normalized:
        return True
    history = item.get("history", [])
    history_text = ""
    if isinstance(history, list):
        history_text = " ".join(
            " ".join(
                [
                    str((entry or {}).get("gate_stage") or ""),
                    str((entry or {}).get("gate_decision") or ""),
                    str((entry or {}).get("reason") or ""),
                    str((entry or {}).get("source_file") or ""),
                ]
            )
            for entry in history
            if isinstance(entry, dict)
        )
    haystack = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("id") or ""),
            str(item.get("source") or ""),
            str(item.get("reason") or ""),
            str(item.get("event_time") or ""),
            history_text,
        ]
    ).lower()
    return normalized in haystack


def _serialize_stage_record(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    return {
        "gate_stage": record.get("gate_stage"),
        "gate_decision": record.get("gate_decision"),
        "recorded_at": record.get("recorded_at"),
        "importance_score": record.get("importance_score"),
        "reason": record.get("reason"),
        "source_file": record.get("source_file"),
        "raw": record.get("raw"),
    }


def _build_signal_lifecycle_base(source: str) -> Dict[str, Any]:
    records = _load_gated_records()
    if source in {"news", "polymarket"}:
        records = [item for item in records if item.get("source_type") == source]
    lifecycles: Dict[str, Dict[str, Any]] = {}
    for record in records:
        source_type = str(record.get("source_type") or "")
        signal_id = str(record.get("id") or "")
        key = f"{source_type}::{signal_id}"
        lifecycle = lifecycles.setdefault(
            key,
            {
                "key": key,
                "id": signal_id,
                "source_type": source_type,
                "title": "",
                "source": "",
                "url": "",
                "event_time": "",
                "importance_score": "",
                "history": [],
                "_firstgate": None,
                "_secondgate": None,
                "_live": None,
            },
        )
        lifecycle["title"] = _pick_preferred_text(lifecycle.get("title"), record.get("title"))
        lifecycle["source"] = _pick_preferred_text(lifecycle.get("source"), record.get("source"))
        lifecycle["url"] = _pick_preferred_text(lifecycle.get("url"), record.get("url"))
        lifecycle["event_time"] = _pick_preferred_text(lifecycle.get("event_time"), record.get("event_time"))
        lifecycle["importance_score"] = _pick_preferred_text(record.get("importance_score"), lifecycle.get("importance_score"))
        lifecycle["history"].append(record)

        gate_stage = str(record.get("gate_stage") or "")
        stage_key = None
        if gate_stage == "firstgate":
            stage_key = "_firstgate"
        elif gate_stage == "secondgate":
            stage_key = "_secondgate"
        elif gate_stage == "live":
            stage_key = "_live"
        if stage_key:
            current = lifecycle.get(stage_key)
            if current is None or _record_sort_key(record) >= _record_sort_key(current):
                lifecycle[stage_key] = record

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "active": [],
        "secondgate_skipped": [],
        "secondgate_dropped": [],
        "firstgate_dropped": [],
    }

    for lifecycle in lifecycles.values():
        history = sorted(lifecycle["history"], key=_record_sort_key, reverse=True)
        firstgate = lifecycle.get("_firstgate")
        secondgate = lifecycle.get("_secondgate")
        live_record = lifecycle.get("_live")
        latest_record = history[0] if history else (live_record or secondgate or firstgate or {})

        if secondgate and str(secondgate.get("gate_decision")) == "skipped":
            bucket = "secondgate_skipped"
        elif secondgate and str(secondgate.get("gate_decision")) == "dropped":
            bucket = "secondgate_dropped"
        elif live_record or (secondgate and str(secondgate.get("gate_decision")) == "kept") or (firstgate and str(firstgate.get("gate_decision")) == "kept"):
            bucket = "active"
        else:
            bucket = "firstgate_dropped"

        lifecycle_view = {
            "key": lifecycle["key"],
            "id": lifecycle["id"],
            "source_type": lifecycle["source_type"],
            "title": lifecycle["title"] or lifecycle["id"],
            "source": lifecycle["source"],
            "url": lifecycle["url"],
            "event_time": lifecycle["event_time"],
            "importance_score": lifecycle["importance_score"],
            "bucket": bucket,
            "reason": _pick_preferred_text(
                (live_record or {}).get("reason"),
                (secondgate or {}).get("reason"),
                (firstgate or {}).get("reason"),
            ),
            "firstgate": _serialize_stage_record(firstgate),
            "secondgate": _serialize_stage_record(secondgate),
            "live": _serialize_stage_record(live_record),
            "history": [_serialize_stage_record(item) for item in history],
            "latest_recorded_at": _pick_preferred_text(
                (latest_record or {}).get("recorded_at"),
                (live_record or {}).get("recorded_at"),
                (secondgate or {}).get("recorded_at"),
                (firstgate or {}).get("recorded_at"),
            ),
        }
        buckets[bucket].append(lifecycle_view)

    for bucket_name in buckets:
        buckets[bucket_name].sort(
            key=lambda item: (
                _extract_sort_stamp(item.get("latest_recorded_at") or item.get("event_time") or ""),
                str(item.get("importance_score") or ""),
                str(item.get("title") or ""),
            ),
            reverse=True,
        )

    total_summary = {bucket_name: len(buckets[bucket_name]) for bucket_name in SIGNAL_BUCKETS}
    total_count = sum(total_summary.values())

    return {
        "source": source,
        "buckets": buckets,
        "total_summary": total_summary,
        "total_count": total_count,
    }


def _load_signal_lifecycle_base(source: str) -> Dict[str, Any]:
    return _memoized(
        f"signal_lifecycle_base:{source}",
        (_gate_data_signature(), source),
        lambda: _build_signal_lifecycle_base(source),
    )


def _load_signal_lifecycles(
    source: str = "all",
    search: str = "",
    page_by_bucket: Optional[Dict[str, int]] = None,
    page_size: int = DEFAULT_SIGNAL_PAGE_SIZE,
) -> Dict[str, Any]:
    source = _normalize_signal_source(source)
    base = _load_signal_lifecycle_base(source)
    buckets = {
        bucket_name: list((base.get("buckets") or {}).get(bucket_name, []))
        for bucket_name in SIGNAL_BUCKETS
    }
    total_summary = dict(base.get("total_summary") or {})
    total_count = int(base.get("total_count") or 0)

    search_text = str(search or "").strip()
    if search_text:
        for bucket_name in SIGNAL_BUCKETS:
            buckets[bucket_name] = [item for item in buckets[bucket_name] if _signal_matches_search(item, search_text)]

    filtered_summary = {bucket_name: len(buckets[bucket_name]) for bucket_name in SIGNAL_BUCKETS}
    filtered_count = sum(filtered_summary.values())
    normalized_page_size = max(1, min(int(page_size or DEFAULT_SIGNAL_PAGE_SIZE), MAX_SIGNAL_PAGE_SIZE))

    paged_buckets: Dict[str, List[Dict[str, Any]]] = {}
    pagination: Dict[str, Dict[str, Any]] = {}
    page_by_bucket = page_by_bucket or {}
    for bucket_name in SIGNAL_BUCKETS:
        items = buckets[bucket_name]
        total = len(items)
        total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
        requested_page = _parse_int_with_bounds(page_by_bucket.get(bucket_name), 1, 1, 100000)
        page = min(requested_page, total_pages)
        start = (page - 1) * normalized_page_size
        end = start + normalized_page_size
        paged_buckets[bucket_name] = items[start:end]
        pagination[bucket_name] = {
            "page": page,
            "page_size": normalized_page_size,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "from": (start + 1) if total > 0 else 0,
            "to": min(end, total),
        }

    return {
        "source": source,
        "search": search_text,
        "page_size": normalized_page_size,
        "count": filtered_count,
        "total_count": total_count,
        "summary": filtered_summary,
        "total_summary": total_summary,
        "buckets": paged_buckets,
        "pagination": pagination,
        "signals": paged_buckets["active"] + paged_buckets["secondgate_skipped"] + paged_buckets["secondgate_dropped"] + paged_buckets["firstgate_dropped"],
    }


def _score_bucket_template() -> Dict[str, int]:
    return {str(score): 0 for score in range(1, 11)}


def _coerce_score_bucket(value: Any) -> Optional[int]:
    try:
        score = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if 1 <= score <= 10:
        return score
    return None


def _accumulate_score_counts(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = _score_bucket_template()
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        score = _coerce_score_bucket(item.get("importance_score"))
        if score is None:
            continue
        counts[str(score)] += 1
        total += 1
    return {"total_scored": total, "counts": counts}


def _compute_source_stats(source: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    relevant = [
        item for item in records
        if source == "all" or str(item.get("source_type") or "") == source
    ]
    lifecycle_payload = _load_signal_lifecycles(
        source=source,
        search="",
        page_by_bucket={bucket_name: 1 for bucket_name in SIGNAL_BUCKETS},
        page_size=MAX_SIGNAL_PAGE_SIZE,
    )
    summary = lifecycle_payload.get("total_summary") if isinstance(lifecycle_payload, dict) else {}
    active = int((summary or {}).get("active") or 0)
    secondgate_skipped = int((summary or {}).get("secondgate_skipped") or 0)
    secondgate_dropped = int((summary or {}).get("secondgate_dropped") or 0)
    firstgate_dropped = int((summary or {}).get("firstgate_dropped") or 0)
    dropped = secondgate_dropped + firstgate_dropped
    keep_drop_ratio = round(active / dropped, 3) if dropped else None

    stage_breakdown: Dict[str, Dict[str, int]] = {
        "firstgate": {"kept": 0, "dropped": 0, "total": 0},
        "secondgate": {"kept": 0, "dropped": 0, "total": 0},
        "live": {"kept": 0, "dropped": 0, "total": 0},
    }
    for item in relevant:
        stage = str(item.get("gate_stage") or "")
        decision = str(item.get("gate_decision") or "").strip().lower()
        if stage not in stage_breakdown:
            continue
        if decision in {"live_kept", "kept"}:
            stage_breakdown[stage]["kept"] += 1
            stage_breakdown[stage]["total"] += 1
        elif decision == "dropped":
            stage_breakdown[stage]["dropped"] += 1
            stage_breakdown[stage]["total"] += 1
        elif decision == "skipped":
            stage_breakdown[stage]["skipped"] = stage_breakdown[stage].get("skipped", 0) + 1
            stage_breakdown[stage]["total"] += 1

    live_records = [item for item in relevant if str(item.get("gate_stage") or "") == "live"]
    historical_records = [item for item in relevant if str(item.get("gate_stage") or "") in {"firstgate", "secondgate"}]

    return {
        "record_count": len(relevant),
        "lifecycle": {
            "active": active,
            "secondgate_skipped": secondgate_skipped,
            "secondgate_dropped": secondgate_dropped,
            "firstgate_dropped": firstgate_dropped,
            "dropped_total": dropped,
            "keep_drop_ratio": keep_drop_ratio,
        },
        "stage_breakdown": stage_breakdown,
        "importance_distribution": {
            "live_pool": _accumulate_score_counts(live_records),
            "history": _accumulate_score_counts(historical_records),
        },
    }


def _load_gate_stats_snapshot() -> Dict[str, Any]:
    def build_snapshot() -> Dict[str, Any]:
        records = _load_gated_records()
        return {
            "updated_at_bj": _fmt_dt(datetime.now()),
            "sources": {
                "all": _compute_source_stats("all", records),
                "news": _compute_source_stats("news", records),
                "polymarket": _compute_source_stats("polymarket", records),
            },
        }

    return _memoized("gate_stats_snapshot", _gate_data_signature(), build_snapshot)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/runs")
def api_runs():
    runs = [_run_summary(run_id) for run_id in _run_dirs()]
    predictions = _load_json_file(PREDICTIONS_PATH, [])
    return jsonify(
        {
            "cache_version": CACHE_VERSION,
            "runs": runs,
            "prediction_count": len(predictions) if isinstance(predictions, list) else 0,
        }
    )


@app.route("/api/runs/<run_id>")
def api_run_detail(run_id: str):
    run = _load_run(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    return jsonify(run)


@app.route("/api/runs/<run_id>/chat", methods=["GET"])
def api_chat_get(run_id: str):
    if not _load_run(run_id):
        return jsonify({"error": "run not found"}), 404
    return jsonify({"run_id": run_id, "messages": _load_chat(run_id)})


@app.route("/api/runs/<run_id>/chat", methods=["POST"])
def api_chat_post(run_id: str):
    run = _load_run(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    payload = request.get_json(silent=True) or {}
    user_message = str(payload.get("message", "")).strip()
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    history = _load_chat(run_id)
    messages = _build_chat_messages(run_id, run, history, user_message)
    _append_chat_trace(
        run_id,
        {
            "type": "request",
            "mode": "invoke",
            "user_message": user_message,
            "history_count": len(history),
            "message_count": len(messages),
        },
    )

    model = _new_chat_model()
    response = model.invoke(messages)
    raw_answer = _extract_chat_message_text(response)
    assistant_message = _coerce_chat_plain_text(raw_answer, run)
    updated_history = _append_chat_history(run_id, history, user_message, assistant_message)
    _append_chat_trace(
        run_id,
        {
            "type": "completed",
            "mode": "invoke",
            "raw_answer": raw_answer,
            "answer": assistant_message,
            "response_metadata": getattr(response, "response_metadata", {}) or {},
        },
    )
    return jsonify(
        {
            "run_id": run_id,
            "messages": updated_history,
            "answer": assistant_message,
            "log_path": _chat_trace_path(run_id),
        }
    )


@app.route("/api/runs/<run_id>/chat/stream", methods=["POST"])
def api_chat_stream(run_id: str):
    run = _load_run(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    payload = request.get_json(silent=True) or {}
    user_message = str(payload.get("message", "")).strip()
    if not user_message:
        return jsonify({"error": "message is required"}), 400

    history = _load_chat(run_id)
    messages = _build_chat_messages(run_id, run, history, user_message)
    _append_chat_trace(
        run_id,
        {
            "type": "request",
            "mode": "stream",
            "user_message": user_message,
            "history_count": len(history),
            "message_count": len(messages),
        },
    )

    def generate():
        model = _new_chat_model()
        raw_chunks: List[str] = []
        visible_chunks: List[str] = []
        suppress_raw_stream = False
        first_nonempty_seen = False
        chunk_count = 0
        try:
            for chunk in model.stream(messages):
                delta = _extract_chat_message_text(chunk)
                if not delta:
                    continue
                raw_chunks.append(delta)
                chunk_count += 1
                _append_chat_trace(
                    run_id,
                    {
                        "type": "chunk",
                        "index": chunk_count,
                        "delta": delta,
                    },
                )
                if not first_nonempty_seen:
                    preview = "".join(raw_chunks).lstrip()
                    if preview:
                        first_nonempty_seen = True
                        suppress_raw_stream = preview.startswith("{") or preview.startswith("[")
                if suppress_raw_stream:
                    continue
                visible_chunks.append(delta)
                yield json.dumps({"type": "chunk", "delta": delta}, ensure_ascii=False) + "\n"

            raw_answer = "".join(raw_chunks)
            assistant_message = _coerce_chat_plain_text(raw_answer, run)
            if suppress_raw_stream:
                yield json.dumps({"type": "replace", "content": assistant_message}, ensure_ascii=False) + "\n"
            elif assistant_message != "".join(visible_chunks):
                yield json.dumps({"type": "replace", "content": assistant_message}, ensure_ascii=False) + "\n"

            updated_history = _append_chat_history(run_id, history, user_message, assistant_message)
            _append_chat_trace(
                run_id,
                {
                    "type": "completed",
                    "mode": "stream",
                    "raw_answer": raw_answer,
                    "answer": assistant_message,
                    "chunk_count": chunk_count,
                },
            )
            yield json.dumps(
                {
                    "type": "done",
                    "run_id": run_id,
                    "answer": assistant_message,
                    "messages": updated_history,
                    "log_path": _chat_trace_path(run_id),
                },
                ensure_ascii=False,
            ) + "\n"
        except Exception as exc:
            partial = _coerce_chat_plain_text("".join(raw_chunks), run)
            _append_chat_trace(
                run_id,
                {
                    "type": "error",
                    "mode": "stream",
                    "error": str(exc),
                    "partial": partial,
                    "chunk_count": chunk_count,
                },
            )
            yield json.dumps(
                {
                    "type": "error",
                    "error": str(exc),
                    "partial": partial,
                    "log_path": _chat_trace_path(run_id),
                },
                ensure_ascii=False,
            ) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/long-hunter/status")
def api_long_hunter_status():
    state = get_long_hunter_state_snapshot()
    run_ids = _run_dirs()
    latest_run_id = run_ids[0] if run_ids else ""
    response = jsonify(
        {
            "state": state,
            "latest_run_id": latest_run_id,
            "latest_run": _run_summary(latest_run_id) if latest_run_id else None,
            "gate_progress": _load_gate_progress_snapshot(),
        }
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/gate-stats")
def api_gate_stats():
    response = jsonify(_load_gate_stats_snapshot())
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/long-hunter/trigger", methods=["POST"])
def api_long_hunter_trigger():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    result = trigger_manual_long_hunter(message)
    run_id = str(((result.get("audit_meta") or {}).get("run_id")) or "")
    return jsonify(
        {
            "ok": True,
            "run_id": run_id,
            "validation_status": result.get("validation_status", ""),
            "status": get_long_hunter_state_snapshot(),
            "run": _run_summary(run_id) if run_id else None,
        }
    )


@app.route("/api/services")
def api_services():
    try:
        log_lines = max(20, min(int(request.args.get("lines", "120")), 300))
    except (TypeError, ValueError):
        log_lines = 120
    return jsonify(
        {
            "services": _all_service_snapshots(include_logs=True, log_lines=log_lines),
            "count": len(SERVICE_UNITS),
        }
    )


@app.route("/api/services/<service_name>/action", methods=["POST"])
def api_service_action(service_name: str):
    name = _normalize_service_name(service_name)
    if name not in SERVICE_UNITS:
        return jsonify({"error": "unknown service"}), 404
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip().lower()
    if action not in {"start", "stop", "restart"}:
        return jsonify({"error": "unsupported action"}), 400
    result = _run_command(["systemctl", action, _service_unit(name)], timeout=20)
    snapshot = _service_snapshot(name, include_logs=True, log_lines=120)
    return jsonify(
        {
            "ok": result.returncode == 0,
            "service": snapshot,
            "action": action,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
            "returncode": result.returncode,
        }
    )


@app.route("/api/gated-signals")
def api_gated_signals():
    source = _normalize_signal_source(request.args.get("source", "all"))
    search = str(request.args.get("search", "")).strip()
    page_size = _parse_int_with_bounds(
        request.args.get("page_size"),
        default=DEFAULT_SIGNAL_PAGE_SIZE,
        minimum=1,
        maximum=MAX_SIGNAL_PAGE_SIZE,
    )
    page_by_bucket = {
        bucket_name: _parse_int_with_bounds(
            request.args.get(f"page_{bucket_name}"),
            default=1,
            minimum=1,
            maximum=100000,
        )
        for bucket_name in SIGNAL_BUCKETS
    }
    return jsonify(
        _load_signal_lifecycles(
            source=source,
            search=search,
            page_by_bucket=page_by_bucket,
            page_size=page_size,
        )
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=1520, debug=False)
