from env_config import load_project_env
load_project_env()

import asyncio
import json
import os
import re
import traceback
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from tools import parse_json_with_fallbacks, safe_json_dump, safe_json_read

try:
    from langchain_openai import ChatOpenAI
except ImportError as e:
    raise RuntimeError("langchain-openai is required for the BTC long hunter.") from e


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LLM_API_KEY = os.getenv("LLMAPIKEY")
LLM_BASE_URL = os.getenv("LLMBASEURL")
LLM_MODEL_ID = os.getenv("LLMMODELID", "gpt-4.1")
DASHSCOPE_ENABLE_THINKING = os.getenv("DASHSCOPE_ENABLE_THINKING", "false").strip().lower() == "true"
VOLCENGINE_ENABLE_THINKING = os.getenv("VOLCENGINE_ENABLE_THINKING", "false").strip().lower() == "true"

SYMBOL = os.getenv("LONG_HUNTER_SYMBOL", "BTCUSDT").strip().upper() or "BTCUSDT"
WEEKLY_REVIEW_WEEKDAY = int(os.getenv("LONG_HUNTER_WEEKDAY", "5"))  # Saturday, datetime.weekday()
WEEKLY_REVIEW_TIME = os.getenv("LONG_HUNTER_TIME", "08:10")
LONG_HUNTER_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "long_hunter_prompt.md")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
MEM_DIR = os.path.join(PROJECT_ROOT, "mem")
SNAPSHOT_DIR = os.path.join(PROJECT_ROOT, "snapshot")
SNAPSHOT_NEWS_DIR = os.path.join(SNAPSHOT_DIR, "news")
SNAPSHOT_POLY_DIR = os.path.join(SNAPSHOT_DIR, "polymarket")
SNAPSHOT_WHALE_DIR = os.path.join(SNAPSHOT_DIR, "whale")
PREDICTIONS_PATH = os.path.join(MEM_DIR, "predictions.json")
LONG_HUNTER_STATE_PATH = os.path.join(MEM_DIR, "long_hunter_state.json")
NEWS_GATED_PATH = os.path.join(PROJECT_ROOT, "news", "gated.json")
POLY_GATED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "gated.json")
WHALE_SUMMARY_API_URL = "https://cryptonews-api.com/api/v1/whale-summary"
WHALE_SUMMARY_API_TOKEN = os.getenv("LONG_HUNTER_WHALE_API_TOKEN", "").strip()
WHALE_SUMMARY_WINDOWS = ("last1hour", "last24hours", "last7days", "last30days")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _env_symbol_list(name: str, default: str) -> List[str]:
    raw = str(os.getenv(name, default) or "").strip()
    symbols = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return symbols or [item.strip().upper() for item in default.split(",") if item.strip()]


MACRO_COMPARE_SYMBOLS = _env_symbol_list(
    "LONG_HUNTER_MACRO_SYMBOLS",
    "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT",
)
CAPITAL_SCENARIO_INITIAL_CAPITAL = _env_float("LONG_HUNTER_CAPITAL_SCENARIO_INITIAL_CAPITAL", 100000.0)
CAPITAL_SCENARIO_VA_MONTHLY_GROWTH = _env_float("LONG_HUNTER_VA_MONTHLY_GROWTH", 2500.0)
CAPITAL_SCENARIO_DCA_MONTHLY_AMOUNT = _env_float("LONG_HUNTER_DCA_MONTHLY_AMOUNT", 2000.0)
CAPITAL_SCENARIO_REBALANCE_WEIGHT = _env_float("LONG_HUNTER_REBALANCE_WEIGHT", 0.55)

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(MEM_DIR, exist_ok=True)
os.makedirs(SNAPSHOT_NEWS_DIR, exist_ok=True)
os.makedirs(SNAPSHOT_POLY_DIR, exist_ok=True)
os.makedirs(SNAPSHOT_WHALE_DIR, exist_ok=True)


def _bj_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _bj_now_text() -> str:
    return _bj_now().strftime("%Y-%m-%d %H:%M:%S")


def _format_bj(dt: datetime) -> str:
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _parse_bj_text(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone(timedelta(hours=8)))


def _weekly_schedule_parts() -> Tuple[int, int]:
    hour_text, minute_text = WEEKLY_REVIEW_TIME.split(":", 1)
    return int(hour_text), int(minute_text)


def _default_next_run_at(reference: Optional[datetime] = None) -> datetime:
    now = reference or _bj_now()
    target_hour, target_minute = _weekly_schedule_parts()
    days_ahead = (WEEKLY_REVIEW_WEEKDAY - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=target_hour,
        minute=target_minute,
        second=0,
        microsecond=0,
    )
    if candidate < now:
        candidate += timedelta(days=7)
    return candidate


def _default_long_hunter_state(reference: Optional[datetime] = None) -> Dict[str, Any]:
    next_run_at = _default_next_run_at(reference)
    return {
        "symbol": SYMBOL,
        "next_run_at_bj": _format_bj(next_run_at),
        "last_run_at_bj": "",
        "last_run_trigger": "",
        "last_run_event_type": "",
        "last_run_log_dir": "",
        "last_daily_snapshot_date_bj": "",
    }


def _load_long_hunter_state() -> Dict[str, Any]:
    now = _bj_now()
    state = _default_long_hunter_state(now)
    existing = _load_json(LONG_HUNTER_STATE_PATH, {})
    if isinstance(existing, dict):
        state.update(existing)
    next_run_at = _parse_bj_text(state.get("next_run_at_bj"))
    if next_run_at is None:
        state["next_run_at_bj"] = _format_bj(_default_next_run_at(now))
    state["symbol"] = SYMBOL
    return state


def _save_long_hunter_state(state: Dict[str, Any]) -> None:
    safe_json_dump(state, LONG_HUNTER_STATE_PATH, use_lock=True)


def _snapshot_basename(dt: Optional[datetime] = None) -> str:
    now = dt or _bj_now()
    return now.strftime("%Y%m%d_%H%M%S")


def _write_daily_snapshot(now: Optional[datetime] = None) -> Dict[str, Any]:
    current = now or _bj_now()
    news_gated, polymarket_gated, _ = _load_gated_signals()
    whale_summary_raw, whale_summary_calls, whale_summary_md = build_whale_summary_context()
    meta = {
        "snapshot_at_bj": _format_bj(current),
        "snapshot_key": _snapshot_basename(current),
        "symbol": SYMBOL,
    }
    snapshot_key = meta["snapshot_key"]

    safe_json_dump(
        {"meta": meta, "gated": news_gated},
        os.path.join(SNAPSHOT_NEWS_DIR, f"{snapshot_key}.json"),
        use_lock=False,
    )
    safe_json_dump(
        {"meta": meta, "gated": polymarket_gated},
        os.path.join(SNAPSHOT_POLY_DIR, f"{snapshot_key}.json"),
        use_lock=False,
    )
    safe_json_dump(
        {"meta": meta, "raw": whale_summary_raw, "calls": whale_summary_calls, "narrative_md": whale_summary_md},
        os.path.join(SNAPSHOT_WHALE_DIR, f"{snapshot_key}.json"),
        use_lock=False,
    )
    return meta


def _update_long_hunter_state_after_run(
    *,
    run_id: str,
    event_type: str,
    trigger_kind: str,
    next_run_at: datetime,
    run_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    state = _load_long_hunter_state()
    state.update(
        {
            "symbol": SYMBOL,
            "next_run_at_bj": _format_bj(next_run_at),
            "last_run_at_bj": _format_bj(run_at or _bj_now()),
            "last_run_trigger": trigger_kind,
            "last_run_event_type": event_type,
            "last_run_log_dir": run_id,
        }
    )
    _save_long_hunter_state(state)
    return state


def _new_chat_model(*, temperature: float = 0.35) -> ChatOpenAI:
    if not LLM_API_KEY:
        raise RuntimeError("LLMAPIKEY is required.")
    auth_key = LLM_API_KEY.replace("Bearer ", "", 1) if LLM_API_KEY.startswith("Bearer ") else LLM_API_KEY
    kwargs: Dict[str, Any] = {
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


def _should_try_structured_output() -> bool:
    base_url = str(LLM_BASE_URL or "").lower()
    model_id = str(LLM_MODEL_ID or "").lower()
    return any(token in base_url for token in ("volces", "volcengine", "ark")) or any(
        token in model_id for token in ("doubao", "ark", "coding")
    )


def _long_hunter_response_format() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "long_hunter_prediction",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "narrative_report": {"type": "string"},
                    "forecasts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "forecast_id": {"type": "string"},
                                "target_date": {"type": "string"},
                                "horizon_days": {"type": "number"},
                                "price_target": {"type": "number"},
                                "price_low": {"type": "number"},
                                "price_high": {"type": "number"},
                                "direction": {"type": "string"},
                                "confidence": {"type": "number"},
                                "thesis": {"type": "string"},
                                "drivers": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "invalidation_conditions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": [
                                "target_date",
                                "horizon_days",
                                "price_target",
                                "price_low",
                                "price_high",
                                "direction",
                                "confidence",
                                "thesis",
                                "drivers",
                                "invalidation_conditions",
                            ],
                        },
                    },
                    "prior_prediction_review": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "prior_run_id": {"type": "string"},
                                "status": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["status", "reason"],
                        },
                    },
                    "risk_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "narrative_report",
                    "forecasts",
                    "prior_prediction_review",
                    "risk_notes",
                ],
            },
        },
    }


def _read_text(path: str, fallback: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return fallback


def _load_json(path: str, default: Any) -> Any:
    data = safe_json_read(path, os.path.basename(path), use_lock=True)
    return deepcopy(default) if data is None else data


def _parse_llm_json(text: str, description: str) -> Dict[str, Any]:
    print(text)
    parsed = parse_json_with_fallbacks(text, description)
    if isinstance(parsed, dict):
        return parsed
    print(f"Invalid JSON type for {description}: expected object, got {type(parsed).__name__}", flush=True)
    print(f"--- FULL {description} START ---", flush=True)
    print(str(text or "").strip(), flush=True)
    print(f"--- FULL {description} END ---", flush=True)
    raise ValueError(f"Invalid JSON object from {description}")


def _get_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    response = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_buy_base",
            "taker_buy_quote",
            "ignore",
        ],
    )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
    for col in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["trades"] = pd.to_numeric(df["trades"], errors="coerce")
    return df


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    close = out["close"]
    high = out["high"]
    low = out["low"]
    volume = out["volume"]

    out["ma7"] = close.rolling(7).mean()
    out["ma25"] = close.rolling(25).mean()
    out["ma99"] = close.rolling(99).mean()
    out["ema12"] = close.ewm(span=12, adjust=False).mean()
    out["ema26"] = close.ewm(span=26, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    out["ema200"] = close.ewm(span=200, adjust=False).mean()

    out["rsi"] = _compute_rsi14_wilder(close)

    out["macd"] = out["ema12"] - out["ema26"]
    out["signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["histo"] = out["macd"] - out["signal"]

    out["boll_mid"] = close.rolling(20).mean()
    boll_std = close.rolling(20).std()
    out["boll_upper"] = out["boll_mid"] + 2 * boll_std
    out["boll_lower"] = out["boll_mid"] - 2 * boll_std

    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    out["rsv"] = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    out["k"] = out["rsv"].ewm(span=3, adjust=False).mean()
    out["d"] = out["k"].ewm(span=3, adjust=False).mean()
    out["j"] = 3 * out["k"] - 2 * out["d"]

    tp = (high + low + close) / 3
    tp_sma = tp.rolling(20).mean()
    mad = (tp - tp_sma).abs().rolling(20).mean()
    out["cci"] = (tp - tp_sma) / (0.015 * mad.replace(0, np.nan))
    out["wr"] = (high9 - close) / (high9 - low9).replace(0, np.nan) * -100
    out["obv"] = (np.sign(close.diff()) * volume).fillna(0).cumsum()

    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    atr = tr.rolling(14).mean()
    plus_di = 100 * plus_dm.rolling(14).sum() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(14).sum() / atr.replace(0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    out["adx14"] = dx.rolling(14).mean()
    out["volume_ma20"] = volume.rolling(20).mean()
    out["volume_ratio20"] = volume / out["volume_ma20"].replace(0, np.nan)
    return out


def _safe_float(value: Any) -> Optional[float]:
    try:
        val = float(value)
    except Exception:
        return None
    if np.isnan(val) or np.isinf(val):
        return None
    return val


def _round_float(value: Any, digits: int = 4) -> Optional[float]:
    val = _safe_float(value)
    return round(val, digits) if val is not None else None


def _pct_change(series: pd.Series, periods: int) -> Optional[float]:
    if len(series) <= periods:
        return None
    current = _safe_float(series.iloc[-1])
    past = _safe_float(series.iloc[-periods - 1])
    if current is None or past is None or abs(past) < 1e-9:
        return None
    return round((current - past) / past * 100, 4)


def _last_rows(df: pd.DataFrame, count: int) -> List[Dict[str, Any]]:
    fields = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "ma7",
        "ma25",
        "ma99",
        "ema50",
        "ema200",
        "rsi",
        "macd",
        "signal",
        "histo",
        "boll_upper",
        "boll_mid",
        "boll_lower",
        "k",
        "d",
        "j",
        "cci",
        "wr",
        "obv",
        "atr14",
        "adx14",
        "volume_ma20",
        "volume_ratio20",
    ]
    rows = []
    for _, row in df.tail(count).iterrows():
        item: Dict[str, Any] = {}
        for field in fields:
            value = row.get(field)
            if field == "timestamp":
                item[field] = value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value)
            else:
                item[field] = _round_float(value)
        rows.append(item)
    return rows


def _market_summary(df: pd.DataFrame, period_name: str) -> Dict[str, Any]:
    if df.empty:
        return {"period": period_name, "error": "no_data"}
    latest = df.iloc[-1]
    close = df["close"]
    high_52w = _safe_float(df.tail(365)["high"].max()) if period_name == "daily" else _safe_float(df["high"].max())
    low_52w = _safe_float(df.tail(365)["low"].min()) if period_name == "daily" else _safe_float(df["low"].min())
    current = _safe_float(latest.get("close"))
    drawdown_from_high = None
    recovery_from_low = None
    if current is not None and high_52w and high_52w > 0:
        drawdown_from_high = round((current - high_52w) / high_52w * 100, 4)
    if current is not None and low_52w and low_52w > 0:
        recovery_from_low = round((current - low_52w) / low_52w * 100, 4)
    return {
        "period": period_name,
        "latest_time_bj": latest["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
        "current_price": _round_float(current, 2),
        "return_30_periods_pct": _pct_change(close, 30),
        "return_90_periods_pct": _pct_change(close, 90),
        "return_180_periods_pct": _pct_change(close, 180),
        "high_52w_or_available": _round_float(high_52w, 2),
        "low_52w_or_available": _round_float(low_52w, 2),
        "drawdown_from_high_pct": drawdown_from_high,
        "recovery_from_low_pct": recovery_from_low,
        "trend_stack": {
            "close_above_ma25": bool(current is not None and _safe_float(latest.get("ma25")) is not None and current > float(latest["ma25"])),
            "close_above_ma99": bool(current is not None and _safe_float(latest.get("ma99")) is not None and current > float(latest["ma99"])),
            "close_above_ema200": bool(current is not None and _safe_float(latest.get("ema200")) is not None and current > float(latest["ema200"])),
        },
        "latest_indicators": {
            "ma7": _round_float(latest.get("ma7"), 2),
            "ma25": _round_float(latest.get("ma25"), 2),
            "ma99": _round_float(latest.get("ma99"), 2),
            "ema50": _round_float(latest.get("ema50"), 2),
            "ema200": _round_float(latest.get("ema200"), 2),
            "rsi14": _round_float(latest.get("rsi"), 2),
            "macd": _round_float(latest.get("macd"), 4),
            "macd_signal": _round_float(latest.get("signal"), 4),
            "macd_histo": _round_float(latest.get("histo"), 4),
            "boll_upper": _round_float(latest.get("boll_upper"), 2),
            "boll_mid": _round_float(latest.get("boll_mid"), 2),
            "boll_lower": _round_float(latest.get("boll_lower"), 2),
            "kdj_k": _round_float(latest.get("k"), 2),
            "kdj_d": _round_float(latest.get("d"), 2),
            "kdj_j": _round_float(latest.get("j"), 2),
            "cci": _round_float(latest.get("cci"), 2),
            "wr": _round_float(latest.get("wr"), 2),
            "obv": _round_float(latest.get("obv"), 2),
            "atr14": _round_float(latest.get("atr14"), 2),
            "adx14": _round_float(latest.get("adx14"), 2),
            "volume_ma20": _round_float(latest.get("volume_ma20"), 2),
            "volume_ratio20": _round_float(latest.get("volume_ratio20"), 4),
        },
    }


def build_btc_market_context(symbol: str = SYMBOL) -> Dict[str, Any]:
    daily = _add_indicators(_get_klines(symbol, "1d", 730))
    monthly = _add_indicators(_get_klines(symbol, "1M", 96))
    current_price = None
    if not daily.empty:
        current_price = _round_float(daily.iloc[-1].get("close"), 2)
    return {
        "symbol": symbol,
        "generated_at_bj": _bj_now_text(),
        "current_price": current_price,
        "daily": {
            "summary": _market_summary(daily, "daily"),
            "recent_rows": _last_rows(daily, 90),
        },
        "monthly": {
            "summary": _market_summary(monthly, "monthly"),
            "recent_rows": _last_rows(monthly, 36),
        },
    }


def _close_series_from_df(df: pd.DataFrame) -> pd.Series:
    if df.empty or "close" not in df.columns:
        return pd.Series(dtype=float)
    close = pd.to_numeric(df["close"], errors="coerce")
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        close.index = ts
    close = close[~close.index.duplicated(keep="last")]
    close = close.sort_index()
    return close.dropna()


def _compute_rsi14_wilder(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(100.0)


def _compute_frank_style_stat_features(close: pd.Series) -> Dict[str, float]:
    if len(close) < 210:
        raise ValueError("not enough close bars to compute Frank-style stats")
    sma_50 = close.rolling(50).mean()
    sma_200 = close.rolling(200).mean()
    std_50 = close.rolling(50).std()
    last_close = float(close.iloc[-1])
    last_sma50 = float(sma_50.iloc[-1])
    last_sma200 = float(sma_200.iloc[-1])
    last_std50 = float(std_50.iloc[-1]) if not np.isnan(std_50.iloc[-1]) else 1e-9

    distance_to_sma50_pct = (last_close / last_sma50 - 1) * 100 if last_sma50 else 0.0
    distance_to_sma200_pct = (last_close / last_sma200 - 1) * 100 if last_sma200 else 0.0
    zscore_50 = (last_close - last_sma50) / (last_std50 if last_std50 else 1e-9)
    rsi_now = float(_compute_rsi14_wilder(close).iloc[-1])

    rolling_std = close.rolling(20).std()
    bollinger_upper = close.rolling(20).mean() + 2 * rolling_std
    upper_ref = float(bollinger_upper.iloc[-1]) if not np.isnan(bollinger_upper.iloc[-1]) else last_close
    resistance_60d = float(close.tail(60).max())
    upside_ref = max(upper_ref, resistance_60d)
    upside_potential_pct = max((upside_ref / last_close - 1) * 100, 0.0)

    downside_ref = float(close.tail(60).min())
    downside_risk_pct = max((1 - downside_ref / last_close) * 100, 0.0)

    returns = close.pct_change().dropna()
    vol_annual = float(returns.std() * np.sqrt(252) * 100)
    roi_30d = float((close.iloc[-1] / close.iloc[-30] - 1) * 100) if len(close) >= 30 else float("nan")
    drawdown = (close / close.cummax() - 1).min() * 100

    confidence = 50.0
    confidence += np.clip(-distance_to_sma200_pct * 0.8, 0, 25)
    confidence += np.clip((30 - rsi_now) * 1.5, 0, 20)
    confidence += np.clip(-distance_to_sma50_pct * 0.5, 0, 10)
    confidence -= np.clip((rsi_now - 70) * 1.0, 0, 20)
    confidence = float(np.clip(confidence, 0, 100))

    p_up = confidence / 100.0
    p_down = 1 - p_up
    ev_pct = p_up * upside_potential_pct - p_down * downside_risk_pct

    return {
        "close": last_close,
        "sma50": last_sma50,
        "sma200": last_sma200,
        "zscore_50": float(zscore_50),
        "distance_to_sma50_pct": float(distance_to_sma50_pct),
        "distance_to_sma200_pct": float(distance_to_sma200_pct),
        "rsi": rsi_now,
        "upside_potential_pct": float(upside_potential_pct),
        "downside_risk_pct": float(downside_risk_pct),
        "confidence_level_pct": confidence,
        "expected_value_pct": float(ev_pct),
        "roi_30d_pct": roi_30d,
        "vol_annual_pct": vol_annual,
        "max_drawdown_pct": float(drawdown),
    }


def _calculate_va_signal(
    initial_capital: float,
    monthly_target_growth: float,
    scenario_month: int,
    current_asset_value: float,
) -> Dict[str, Any]:
    target = initial_capital + (scenario_month * monthly_target_growth)
    gap = target - current_asset_value
    if gap > 0:
        scenario_status = "below_value_path"
        notional_gap = gap
    elif gap < 0:
        scenario_status = "above_value_path"
        notional_gap = abs(gap)
    else:
        scenario_status = "on_value_path"
        notional_gap = 0.0
    return {
        "strategy": "VA",
        "scenario_status": scenario_status,
        "notional_gap": round(notional_gap, 2),
        "target_value": round(target, 2),
        "current_value": round(current_asset_value, 2),
    }


def _calculate_dca_signal(monthly_amount: float) -> Dict[str, Any]:
    return {
        "strategy": "DCA",
        "scenario_status": "fixed_periodic_reference",
        "notional_gap": round(max(monthly_amount, 0.0), 2),
        "note": "Fixed periodic exposure reference regardless of current valuation.",
    }


def _calculate_rebalance_signal(
    total_portfolio_value: float,
    target_asset_weight: float,
    current_asset_value: float,
) -> Dict[str, Any]:
    target_asset_value = total_portfolio_value * target_asset_weight
    diff = target_asset_value - current_asset_value
    if diff > 0:
        scenario_status = "below_target_weight"
    elif diff < 0:
        scenario_status = "above_target_weight"
    else:
        scenario_status = "at_target_weight"
    return {
        "strategy": "REBALANCE",
        "scenario_status": scenario_status,
        "notional_gap": round(abs(diff), 2),
        "target_value": round(target_asset_value, 2),
        "current_value": round(current_asset_value, 2),
    }


def _backtest_monthly_strategies(
    close: pd.Series,
    initial_capital: float,
    monthly_target_growth: float,
    dca_amount: float,
    target_weight: float,
) -> pd.DataFrame:
    if close.empty:
        raise ValueError("empty close series")
    monthly_close = close.resample("ME").last().dropna()
    if len(monthly_close) < 8:
        raise ValueError("backtest requires at least 8 monthly bars")

    records: List[Dict[str, Any]] = []
    va_units = 0.0
    va_cash = initial_capital
    dca_units = 0.0
    dca_cash = initial_capital
    rb_units = 0.0
    rb_cash = initial_capital

    for idx, (_, price) in enumerate(monthly_close.items(), start=1):
        va_port = va_cash + va_units * price
        va_signal = _calculate_va_signal(
            initial_capital=initial_capital,
            monthly_target_growth=monthly_target_growth,
            scenario_month=idx,
            current_asset_value=va_port,
        )
        va_flow = (
            va_signal["notional_gap"]
            if va_signal["scenario_status"] == "below_value_path"
            else -va_signal["notional_gap"]
        )
        if va_flow >= 0:
            va_cash -= va_flow
            va_units += va_flow / price if price > 0 else 0
        else:
            sell_amt = min(abs(va_flow), va_units * price)
            va_units -= sell_amt / price if price > 0 else 0
            va_cash += sell_amt
        va_port = va_cash + va_units * price

        dca_flow = max(dca_amount, 0.0)
        dca_cash -= dca_flow
        dca_units += dca_flow / price if price > 0 else 0
        dca_port = dca_cash + dca_units * price

        rb_port = rb_cash + rb_units * price
        rb_signal = _calculate_rebalance_signal(
            total_portfolio_value=rb_port,
            target_asset_weight=target_weight,
            current_asset_value=rb_units * price,
        )
        rb_flow = (
            rb_signal["notional_gap"]
            if rb_signal["scenario_status"] == "below_target_weight"
            else -rb_signal["notional_gap"]
        )
        if rb_flow >= 0:
            spend = min(rb_flow, rb_cash)
            rb_cash -= spend
            rb_units += spend / price if price > 0 else 0
        else:
            sell_amt = min(abs(rb_flow), rb_units * price)
            rb_units -= sell_amt / price if price > 0 else 0
            rb_cash += sell_amt
        rb_port = rb_cash + rb_units * price

        records.append(
            {
                "Date": monthly_close.index[idx - 1],
                "VA": va_port,
                "DCA": dca_port,
                "REBALANCE": rb_port,
            }
        )
    return pd.DataFrame(records).set_index("Date")


def _backtest_metrics(equity_curve: pd.Series) -> Dict[str, float]:
    returns = equity_curve.pct_change().dropna()
    total_return = (equity_curve.iloc[-1] / equity_curve.iloc[0] - 1) * 100
    ann_return = ((1 + total_return / 100) ** (12 / max(len(equity_curve), 1)) - 1) * 100
    ann_vol = returns.std() * np.sqrt(12) * 100 if not returns.empty else 0.0
    sharpe_like = ann_return / ann_vol if ann_vol > 0 else np.nan
    max_drawdown = (equity_curve / equity_curve.cummax() - 1).min() * 100
    return {
        "final_value": float(equity_curve.iloc[-1]),
        "total_return_pct": float(total_return),
        "annualized_return_pct": float(ann_return),
        "annualized_vol_pct": float(ann_vol),
        "sharpe_like": float(sharpe_like) if not np.isnan(sharpe_like) else np.nan,
        "max_drawdown_pct": float(max_drawdown),
    }


def _build_macro_compare_context() -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for macro_symbol in MACRO_COMPARE_SYMBOLS:
        try:
            df = _get_klines(macro_symbol, "1d", 420)
            close = _close_series_from_df(df)
            stats = _compute_frank_style_stat_features(close)
            rows.append(
                {
                    "symbol": macro_symbol,
                    "close": _round_float(stats.get("close"), 2),
                    "confidence_level_pct": _round_float(stats.get("confidence_level_pct"), 2),
                    "expected_value_pct": _round_float(stats.get("expected_value_pct"), 2),
                    "roi_30d_pct": _round_float(stats.get("roi_30d_pct"), 2),
                    "vol_annual_pct": _round_float(stats.get("vol_annual_pct"), 2),
                    "rsi": _round_float(stats.get("rsi"), 2),
                    "rsi14": _round_float(stats.get("rsi"), 2),
                    "error": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "symbol": macro_symbol,
                    "close": None,
                    "confidence_level_pct": None,
                    "expected_value_pct": None,
                    "roi_30d_pct": None,
                    "vol_annual_pct": None,
                    "rsi": None,
                    "rsi14": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    valid_rows = [row for row in rows if not row.get("error")]
    confidence_sorted = sorted(valid_rows, key=lambda item: float(item.get("confidence_level_pct") or -1), reverse=True)
    ev_sorted = sorted(valid_rows, key=lambda item: float(item.get("expected_value_pct") or -99999), reverse=True)
    return {
        "universe": MACRO_COMPARE_SYMBOLS,
        "rows": rows,
        "leaders": {
            "confidence_top": [item.get("symbol") for item in confidence_sorted[:3]],
            "expected_value_top": [item.get("symbol") for item in ev_sorted[:3]],
        },
    }


def _build_capital_scenario_context(
    *,
    current_price: Optional[float],
    monthly_count: int,
) -> Dict[str, Any]:
    normalized_month = max(int(monthly_count), 1)
    initial_capital = CAPITAL_SCENARIO_INITIAL_CAPITAL
    monthly_growth = CAPITAL_SCENARIO_VA_MONTHLY_GROWTH
    dca_amount = CAPITAL_SCENARIO_DCA_MONTHLY_AMOUNT
    target_weight = min(max(CAPITAL_SCENARIO_REBALANCE_WEIGHT, 0.0), 1.0)
    current_asset_value = (current_price or 0.0) * 1.0
    total_portfolio_value = initial_capital

    va_signal = _calculate_va_signal(
        initial_capital=initial_capital,
        monthly_target_growth=monthly_growth,
        scenario_month=normalized_month,
        current_asset_value=current_asset_value,
    )
    dca_signal = _calculate_dca_signal(dca_amount)
    rebalance_signal = _calculate_rebalance_signal(
        total_portfolio_value=total_portfolio_value,
        target_asset_weight=target_weight,
        current_asset_value=current_asset_value,
    )
    plan_rows: List[Dict[str, Any]] = []
    for step in range(1, 7):
        month_t = normalized_month + step
        plan_rows.append(
            {
                "month": month_t,
                "va_target_value": round(initial_capital + month_t * monthly_growth, 2),
                "dca_amount": round(max(dca_amount, 0.0), 2),
                "rebalance_target_weight": round(target_weight, 4),
            }
        )

    return {
        "params": {
            "initial_capital": round(initial_capital, 2),
            "va_monthly_target_growth": round(monthly_growth, 2),
            "dca_monthly_amount": round(max(dca_amount, 0.0), 2),
            "rebalance_target_weight": round(target_weight, 4),
            "assumed_total_portfolio_value": round(total_portfolio_value, 2),
            "assumed_current_asset_value": round(current_asset_value, 2),
            "scenario_month": normalized_month,
        },
        "signals": {
            "va": va_signal,
            "dca": dca_signal,
            "rebalance": rebalance_signal,
        },
        "forward_plan_6m": plan_rows,
    }


def _build_monthly_backtest_context(close: pd.Series) -> Dict[str, Any]:
    initial_capital = CAPITAL_SCENARIO_INITIAL_CAPITAL
    monthly_growth = CAPITAL_SCENARIO_VA_MONTHLY_GROWTH
    dca_amount = CAPITAL_SCENARIO_DCA_MONTHLY_AMOUNT
    target_weight = min(max(CAPITAL_SCENARIO_REBALANCE_WEIGHT, 0.0), 1.0)
    try:
        bt_df = _backtest_monthly_strategies(
            close=close,
            initial_capital=initial_capital,
            monthly_target_growth=monthly_growth,
            dca_amount=dca_amount,
            target_weight=target_weight,
        )
        metrics = {
            "VA": _backtest_metrics(bt_df["VA"]),
            "DCA": _backtest_metrics(bt_df["DCA"]),
            "REBALANCE": _backtest_metrics(bt_df["REBALANCE"]),
        }
        best_strategy = sorted(
            metrics.items(),
            key=lambda kv: float(kv[1].get("final_value") or -1),
            reverse=True,
        )[0][0]
        equity_tail: List[Dict[str, Any]] = []
        for stamp, row in bt_df.tail(12).iterrows():
            equity_tail.append(
                {
                    "date": stamp.strftime("%Y-%m-%d"),
                    "VA": _round_float(row.get("VA"), 2),
                    "DCA": _round_float(row.get("DCA"), 2),
                    "REBALANCE": _round_float(row.get("REBALANCE"), 2),
                }
            )
        return {
            "error": "",
            "params": {
                "initial_capital": round(initial_capital, 2),
                "va_monthly_target_growth": round(monthly_growth, 2),
                "dca_monthly_amount": round(dca_amount, 2),
                "rebalance_target_weight": round(target_weight, 4),
            },
            "window": {
                "from": bt_df.index.min().strftime("%Y-%m-%d"),
                "to": bt_df.index.max().strftime("%Y-%m-%d"),
                "months": int(len(bt_df)),
            },
            "metrics": metrics,
            "best_strategy_by_final_value": best_strategy,
            "equity_tail": equity_tail,
        }
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "params": {
                "initial_capital": round(initial_capital, 2),
                "va_monthly_target_growth": round(monthly_growth, 2),
                "dca_monthly_amount": round(dca_amount, 2),
                "rebalance_target_weight": round(target_weight, 4),
            },
            "window": {},
            "metrics": {},
            "best_strategy_by_final_value": "",
            "equity_tail": [],
        }


def build_quant_research_context(symbol: str = SYMBOL) -> Dict[str, Any]:
    daily_df = _get_klines(symbol, "1d", 730)
    close = _close_series_from_df(daily_df)
    stats = _compute_frank_style_stat_features(close)
    monthly_count = int(close.resample("ME").last().dropna().shape[0]) if not close.empty else 1
    return {
        "symbol": symbol,
        "generated_at_bj": _bj_now_text(),
        "ev_score": {
            "confidence_level_pct": _round_float(stats.get("confidence_level_pct"), 2),
            "expected_value_pct": _round_float(stats.get("expected_value_pct"), 2),
            "upside_potential_pct": _round_float(stats.get("upside_potential_pct"), 2),
            "downside_risk_pct": _round_float(stats.get("downside_risk_pct"), 2),
        },
        "risk_metrics": {
            "roi_30d_pct": _round_float(stats.get("roi_30d_pct"), 2),
            "vol_annual_pct": _round_float(stats.get("vol_annual_pct"), 2),
            "max_drawdown_pct": _round_float(stats.get("max_drawdown_pct"), 2),
            "distance_to_sma50_pct": _round_float(stats.get("distance_to_sma50_pct"), 2),
            "distance_to_sma200_pct": _round_float(stats.get("distance_to_sma200_pct"), 2),
            "zscore_50": _round_float(stats.get("zscore_50"), 2),
            "rsi": _round_float(stats.get("rsi"), 2),
            "rsi14": _round_float(stats.get("rsi"), 2),
        },
        "macro_compare": _build_macro_compare_context(),
        "capital_scenarios": _build_capital_scenario_context(
            current_price=_safe_float(stats.get("close")),
            monthly_count=monthly_count,
        ),
        "monthly_backtest": _build_monthly_backtest_context(close),
    }


def _fmt_value(value: Any, digits: int = 4) -> str:
    num = _safe_float(value)
    if num is None:
        return "-"
    return f"{round(num, digits):.{digits}f}"


def _fmt_int(value: Any) -> str:
    num = _safe_float(value)
    if num is None:
        return "-"
    return f"{int(round(num)):,}"


def _fmt_usd(value: Any) -> str:
    num = _safe_float(value)
    if num is None:
        return "-"
    abs_num = abs(num)
    if abs_num >= 1_000_000_000:
        return f"${num / 1_000_000_000:.2f}B"
    if abs_num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    return f"${num:,.2f}"


def _redact_whale_secret(text: Any) -> str:
    safe = str(text or "")
    if WHALE_SUMMARY_API_TOKEN:
        safe = safe.replace(WHALE_SUMMARY_API_TOKEN, "[redacted]")
    return re.sub(r"token=([^&\s]+)", "token=[redacted]", safe)


def _whale_http_status_reason(status_code: Any) -> str:
    if status_code in (401, 403):
        return "api_auth_failed_or_forbidden"
    if status_code == 429:
        return "api_rate_limited"
    if isinstance(status_code, int) and 500 <= status_code < 600:
        return "api_server_error"
    return "api_http_error"


def _fetch_whale_summary_window(date_window: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    call_record: Dict[str, Any] = {
        "source": "cryptonews_whale_summary",
        "url": WHALE_SUMMARY_API_URL,
        "ticker": "BTC",
        "date": date_window,
        "token_configured": bool(WHALE_SUMMARY_API_TOKEN),
        "ok": False,
        "status_code": None,
        "error": "",
    }
    if not WHALE_SUMMARY_API_TOKEN:
        call_record["error"] = "missing_api_token"
        return {"error": "missing_api_token"}, call_record
    response: Optional[requests.Response] = None
    try:
        response = requests.get(
            WHALE_SUMMARY_API_URL,
            params={"tickers": "BTC", "date": date_window, "token": WHALE_SUMMARY_API_TOKEN},
            timeout=30,
        )
        call_record["status_code"] = response.status_code
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            call_record["error"] = f"invalid_payload_type:{type(payload).__name__}"
            return {"error": "invalid_payload_type", "raw": payload}, call_record
        call_record["ok"] = True
        return payload, call_record
    except requests.HTTPError:
        status_code = response.status_code if response is not None else None
        call_record["status_code"] = status_code
        call_record["error"] = _whale_http_status_reason(status_code)
        return {"error": call_record["error"]}, call_record
    except Exception as exc:
        call_record["error"] = _redact_whale_secret(f"{type(exc).__name__}: {exc}")
        return {"error": call_record["error"]}, call_record


def _window_label(window: str) -> str:
    labels = {
        "last1hour": "近1小时",
        "last24hours": "近24小时",
        "last7days": "近7天",
        "last30days": "近30天",
    }
    return labels.get(window, window)


def _whale_window_to_narrative(window: str, payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return f"- {_window_label(window)}：巨鲸摘要返回格式异常。"
    if payload.get("error"):
        return f"- {_window_label(window)}：巨鲸摘要获取失败，{payload.get('error')}。"

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not data:
        return f"- {_window_label(window)}：未返回可用的 data 字段。"

    total_transactions = _fmt_int(data.get("total_transactions"))
    total_volume = _fmt_usd(data.get("total_volume_usd"))
    net_flow = _safe_float(data.get("net_exchange_flow"))
    net_flow_text = _fmt_usd(net_flow)
    if net_flow is None:
        flow_bias = "未知"
    elif net_flow < 0:
        flow_bias = "净流出（偏离交易所，偏中长期囤积）"
    elif net_flow > 0:
        flow_bias = "净流入（偏向交易所，潜在抛压上升）"
    else:
        flow_bias = "净流平衡"

    inflow = data.get("exchange_inflow") if isinstance(data.get("exchange_inflow"), dict) else {}
    outflow = data.get("exchange_outflow") if isinstance(data.get("exchange_outflow"), dict) else {}
    inflow_text = f"{_fmt_int(inflow.get('count'))} 笔 / {_fmt_usd(inflow.get('volume_usd'))}"
    outflow_text = f"{_fmt_int(outflow.get('count'))} 笔 / {_fmt_usd(outflow.get('volume_usd'))}"

    biggest = data.get("biggest_transaction") if isinstance(data.get("biggest_transaction"), dict) else {}
    biggest_amount = biggest.get("amount")
    biggest_chain = str(biggest.get("chain") or "-")
    biggest_time = str(biggest.get("date") or "-")
    biggest_usd = _fmt_usd(biggest.get("amount_usd"))

    return (
        f"- {_window_label(window)}：共监测 {total_transactions} 笔，成交额约 {total_volume}；"
        f"交易所净流 {net_flow_text}，口径判断为“{flow_bias}”。"
        f" 其中流入 {inflow_text}，流出 {outflow_text}。"
        f" 单笔最大转账为 {biggest_amount or '-'} BTC（约 {biggest_usd}，链={biggest_chain}，时间={biggest_time}）。"
    )


def build_whale_summary_context() -> Tuple[Dict[str, Any], List[Dict[str, Any]], str]:
    raw_by_window: Dict[str, Any] = {}
    call_records: List[Dict[str, Any]] = []
    lines = [
        "# BTC Whale Summary (Natural Language Parse)",
        "",
        "来源：CryptoNews Whale Summary API；以下为将 JSON 指标转写后的自然语言摘要，供长期叙事参考。",
        "",
    ]
    for window in WHALE_SUMMARY_WINDOWS:
        payload, call_record = _fetch_whale_summary_window(window)
        raw_by_window[window] = payload
        call_records.append(call_record)
        lines.append(_whale_window_to_narrative(window, payload))

    return raw_by_window, call_records, "\n".join(lines).strip()


def _market_context_to_markdown(market_context: Dict[str, Any]) -> str:
    symbol = str(market_context.get("symbol") or SYMBOL)
    generated_at_bj = str(market_context.get("generated_at_bj") or _bj_now_text())
    current_price = _fmt_value(market_context.get("current_price"), 2)

    lines: List[str] = [
        f"# Market Context ({symbol})",
        "",
        f"Generated At (BJ): {generated_at_bj}",
        f"Current Price: {current_price}",
        "",
        "## Summary",
        "",
        "| Period | Latest Time (BJ) | Price | 30p % | 90p % | 180p % | Drawdown % | Recovery % | RSI14 | MACD Histo | ADX14 | Vol Ratio20 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for period_key in ("daily", "monthly"):
        block = market_context.get(period_key) if isinstance(market_context.get(period_key), dict) else {}
        summary = block.get("summary") if isinstance(block.get("summary"), dict) else {}
        indicators = summary.get("latest_indicators") if isinstance(summary.get("latest_indicators"), dict) else {}
        lines.append(
            "| "
            + " | ".join(
                [
                    period_key,
                    str(summary.get("latest_time_bj") or "-"),
                    _fmt_value(summary.get("current_price"), 2),
                    _fmt_value(summary.get("return_30_periods_pct"), 2),
                    _fmt_value(summary.get("return_90_periods_pct"), 2),
                    _fmt_value(summary.get("return_180_periods_pct"), 2),
                    _fmt_value(summary.get("drawdown_from_high_pct"), 2),
                    _fmt_value(summary.get("recovery_from_low_pct"), 2),
                    _fmt_value(indicators.get("rsi14"), 2),
                    _fmt_value(indicators.get("macd_histo"), 4),
                    _fmt_value(indicators.get("adx14"), 2),
                    _fmt_value(indicators.get("volume_ratio20"), 2),
                ]
            )
            + " |"
        )

    for period_key, tail_size in (("daily", 12), ("monthly", 12)):
        block = market_context.get(period_key) if isinstance(market_context.get(period_key), dict) else {}
        rows = block.get("recent_rows") if isinstance(block.get("recent_rows"), list) else []
        lines.extend(
            [
                "",
                f"## Recent {period_key.title()} Candles (Last {tail_size})",
                "",
                "| Time (BJ) | Open | High | Low | Close | Vol | RSI | MACD | Signal | Histo | ATR14 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in rows[-tail_size:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(item.get("timestamp") or "-"),
                        _fmt_value(item.get("open"), 2),
                        _fmt_value(item.get("high"), 2),
                        _fmt_value(item.get("low"), 2),
                        _fmt_value(item.get("close"), 2),
                        _fmt_value(item.get("volume"), 2),
                        _fmt_value(item.get("rsi"), 2),
                        _fmt_value(item.get("macd"), 4),
                        _fmt_value(item.get("signal"), 4),
                        _fmt_value(item.get("histo"), 4),
                        _fmt_value(item.get("atr14"), 2),
                    ]
                )
                + " |"
            )
    return "\n".join(lines)


def _quant_research_to_markdown(research: Dict[str, Any]) -> str:
    if not isinstance(research, dict) or not research:
        return "No quant research context available."
    ev = research.get("ev_score") if isinstance(research.get("ev_score"), dict) else {}
    risk = research.get("risk_metrics") if isinstance(research.get("risk_metrics"), dict) else {}
    macro = research.get("macro_compare") if isinstance(research.get("macro_compare"), dict) else {}
    capital = research.get("capital_scenarios") if isinstance(research.get("capital_scenarios"), dict) else {}
    monthly_bt = research.get("monthly_backtest") if isinstance(research.get("monthly_backtest"), dict) else {}
    rows = macro.get("rows") if isinstance(macro.get("rows"), list) else []
    risk_rsi = risk.get("rsi14") if risk.get("rsi14") is not None else risk.get("rsi")

    lines: List[str] = [
        "# Quant Research Context",
        "",
        f"Generated At (BJ): {_compact_text(research.get('generated_at_bj'))}",
        "",
        "## Heuristic EV Calibration",
        "",
        "- Interpretation: `confidence_level_pct` is a heuristic rebound / mean-reversion score derived from distance to SMA200, RSI14, and distance to SMA50.",
        "- Interpretation: `confidence_level_pct` is not a calibrated probability and must not be read as forecast confidence.",
        "- Interpretation: `expected_value_pct` is a local-range payoff score using the heuristic confidence, upside reference=max(Bollinger upper, 60d high), and downside reference=the drawdown to 60d low measured from the current price.",
        "- Interpretation: a negative `expected_value_pct` means the current local upside/downside asymmetry is unfavorable; it does not automatically mean the long-horizon thesis is bearish.",
        "",
        f"- Heuristic Confidence (%): {_fmt_value(ev.get('confidence_level_pct'), 2)}",
        f"- Local Range EV (%): {_fmt_value(ev.get('expected_value_pct'), 2)}",
        f"- Upside Potential (%): {_fmt_value(ev.get('upside_potential_pct'), 2)}",
        f"- Downside Risk (%): {_fmt_value(ev.get('downside_risk_pct'), 2)}",
        "",
        "## Risk Metrics",
        "",
        f"- ROI 30d (%): {_fmt_value(risk.get('roi_30d_pct'), 2)}",
        f"- Annualized Volatility (%): {_fmt_value(risk.get('vol_annual_pct'), 2)}",
        f"- Max Drawdown (%): {_fmt_value(risk.get('max_drawdown_pct'), 2)}",
        f"- Distance to SMA50 (%): {_fmt_value(risk.get('distance_to_sma50_pct'), 2)}",
        f"- Distance to SMA200 (%): {_fmt_value(risk.get('distance_to_sma200_pct'), 2)}",
        f"- ZScore vs SMA50: {_fmt_value(risk.get('zscore_50'), 2)}",
        f"- RSI14: {_fmt_value(risk_rsi, 2)}",
        "",
        "## Macro Compare",
        "",
        f"Universe: {_compact_text(', '.join(str(x) for x in macro.get('universe', [])))}",
        f"Heuristic Confidence Leaders: {_compact_text(', '.join(str(x) for x in ((macro.get('leaders') or {}).get('confidence_top') or [])))}",
        f"Local Range EV Leaders: {_compact_text(', '.join(str(x) for x in ((macro.get('leaders') or {}).get('expected_value_top') or [])))}",
        "",
        "| Symbol | Close | Heuristic Confidence % | Local Range EV % | ROI30d % | Vol Ann % | RSI14 | Error |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in rows:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    _compact_text(item.get("symbol")),
                    _fmt_value(item.get("close"), 2),
                    _fmt_value(item.get("confidence_level_pct"), 2),
                    _fmt_value(item.get("expected_value_pct"), 2),
                    _fmt_value(item.get("roi_30d_pct"), 2),
                    _fmt_value(item.get("vol_annual_pct"), 2),
                    _fmt_value(item.get("rsi14") if item.get("rsi14") is not None else item.get("rsi"), 2),
                    _compact_text(item.get("error")),
                ]
            )
            + " |"
        )

    signals = capital.get("signals") if isinstance(capital.get("signals"), dict) else {}
    lines.extend(
        [
            "",
            "## Capital Management Scenarios (VA / DCA / REBALANCE)",
            "",
            f"- VA: status={_compact_text((signals.get('va') or {}).get('scenario_status'))}, notional_gap={_fmt_value((signals.get('va') or {}).get('notional_gap'), 2)}",
            f"- DCA: status={_compact_text((signals.get('dca') or {}).get('scenario_status'))}, notional_gap={_fmt_value((signals.get('dca') or {}).get('notional_gap'), 2)}",
            f"- REBALANCE: status={_compact_text((signals.get('rebalance') or {}).get('scenario_status'))}, notional_gap={_fmt_value((signals.get('rebalance') or {}).get('notional_gap'), 2)}",
            "",
            "## Monthly Backtest",
            "",
        ]
    )
    bt_error = str(monthly_bt.get("error") or "").strip()
    if bt_error:
        lines.append(f"- Backtest Error: {bt_error}")
    else:
        lines.extend(
            [
                f"- Window: {_compact_text((monthly_bt.get('window') or {}).get('from'))} -> {_compact_text((monthly_bt.get('window') or {}).get('to'))}; months={_fmt_int((monthly_bt.get('window') or {}).get('months'))}",
                f"- Best Strategy by Final Value: {_compact_text(monthly_bt.get('best_strategy_by_final_value'))}",
            ]
        )
        metrics = monthly_bt.get("metrics") if isinstance(monthly_bt.get("metrics"), dict) else {}
        lines.extend(
            [
                "",
                "| Strategy | Final Value | Total Return % | Ann Return % | Ann Vol % | Sharpe-like | Max Drawdown % |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for key in ("VA", "DCA", "REBALANCE"):
            row = metrics.get(key) if isinstance(metrics.get(key), dict) else {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        key,
                        _fmt_value(row.get("final_value"), 2),
                        _fmt_value(row.get("total_return_pct"), 2),
                        _fmt_value(row.get("annualized_return_pct"), 2),
                        _fmt_value(row.get("annualized_vol_pct"), 2),
                        _fmt_value(row.get("sharpe_like"), 2),
                        _fmt_value(row.get("max_drawdown_pct"), 2),
                    ]
                )
                + " |"
            )

    return "\n".join(lines)


def _compact_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    return re.sub(r"\s+", " ", text)


def _news_gated_to_markdown(news_gated: List[Dict[str, Any]]) -> str:
    if not news_gated:
        return "No gated news signals."
    lines: List[str] = []
    for idx, item in enumerate(news_gated, 1):
        if not isinstance(item, dict):
            continue
        lines.extend(
            [
                f"{idx}.",
                f"Summary (gated reason): {_compact_text(item.get('gate_reason'))}",
                f"Title: {_compact_text(item.get('title'))}",
                f"Text: {_compact_text(item.get('text'))}",
                "",
            ]
        )
    return "\n".join(lines).strip() or "No gated news signals."


def _format_poly_sub_markets(sub_markets: Any) -> str:
    if not isinstance(sub_markets, list) or not sub_markets:
        return "-"
    parts: List[str] = []
    for sub_idx, sub in enumerate(sub_markets, 1):
        if not isinstance(sub, dict):
            continue
        prices = sub.get("prices")
        prices_text = json.dumps(prices, ensure_ascii=False, separators=(",", ":")) if isinstance(prices, dict) else "-"
        parts.append(
            f"{sub_idx}) question={_compact_text(sub.get('question'))}; "
            f"deadline={_compact_text(sub.get('deadline'))}; "
            f"market_id={_compact_text(sub.get('market_id'))}; "
            f"prices={prices_text}"
        )
    return " | ".join(parts) if parts else "-"


def _polymarket_gated_to_markdown(polymarket_gated: List[Dict[str, Any]]) -> str:
    if not polymarket_gated:
        return "No gated polymarket signals."
    preferred_keys = [
        "event_id",
        "id",
        "event_title",
        "category",
        "url",
        "last_updated",
        "importance_score",
        "gated_at",
    ]
    lines: List[str] = []
    for idx, item in enumerate(polymarket_gated, 1):
        if not isinstance(item, dict):
            continue
        lines.append(f"{idx}.")
        lines.append(f"Summary (gated reason): {_compact_text(item.get('gate_reason'))}")
        for key in preferred_keys:
            if key in item:
                lines.append(f"{key}: {_compact_text(item.get(key))}")
        if "sub_markets" in item:
            lines.append(f"sub_markets: {_format_poly_sub_markets(item.get('sub_markets'))}")
        extra_keys = [k for k in item.keys() if k not in set(preferred_keys + ["gate_reason", "sub_markets"])]
        for key in extra_keys:
            value = item.get(key)
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                value_text = _compact_text(value)
            lines.append(f"{key}: {value_text}")
        lines.append("")
    return "\n".join(lines).strip() or "No gated polymarket signals."


def _event_id(item: Dict[str, Any], fallback_prefix: str, idx: int) -> str:
    return str(item.get("id") or item.get("event_id") or item.get("news_url") or item.get("url") or f"{fallback_prefix}_{idx}")


def _load_gated_signals() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, List[str]]]:
    news = _load_json(NEWS_GATED_PATH, [])
    poly = _load_json(POLY_GATED_PATH, [])
    if not isinstance(news, list):
        news = []
    if not isinstance(poly, list):
        poly = []
    news_ids = [_event_id(item, "news", idx) for idx, item in enumerate(news)]
    poly_ids = [_event_id(item, "polymarket", idx) for idx, item in enumerate(poly)]
    return news, poly, {"news_ids": news_ids, "polymarket_ids": poly_ids}


def _load_protocol_markdown() -> Tuple[str, str, Dict[str, List[str]]]:
    news_text = "No gated news available."
    poly_text = "No gated Polymarket events available."
    refs: Dict[str, List[str]] = {"news_ids": [], "polymarket_ids": []}

    try:
        from news.protocol import protocol as news_protocol

        text, ids = news_protocol()
        if isinstance(text, str) and text.strip():
            news_text = text
        if isinstance(ids, list):
            refs["news_ids"] = [str(x) for x in ids if str(x).strip()]
    except Exception as exc:
        news_text = f"Error loading news protocol text: {exc}"

    try:
        from polymarket.protocol import protocol as poly_protocol

        text, ids = poly_protocol()
        if isinstance(text, str) and text.strip():
            poly_text = text
        if isinstance(ids, list):
            refs["polymarket_ids"] = [str(x) for x in ids if str(x).strip()]
    except Exception as exc:
        poly_text = f"Error loading polymarket protocol text: {exc}"

    return news_text, poly_text, refs


def _load_predictions() -> List[Dict[str, Any]]:
    data = _load_json(PREDICTIONS_PATH, [])
    return data if isinstance(data, list) else []


def _is_valid_prior_prediction_record(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if str(item.get("run_id") or "").strip() == "":
        return False
    forecasts = item.get("forecasts")
    if not isinstance(forecasts, list) or not forecasts:
        return False
    valid_forecast_found = False
    for fc in forecasts:
        if not isinstance(fc, dict):
            continue
        horizon = int(_safe_float(fc.get("horizon_days")) or 0)
        if horizon >= 30:
            valid_forecast_found = True
            break
    if not valid_forecast_found:
        return False
    # Keep long-hunter prediction-only records and skip polluted execution-like rows.
    meta = item.get("meta")
    if isinstance(meta, dict):
        if meta.get("prediction_only") is False:
            return False
        if meta.get("agent_type") not in (None, "", "long_hunter"):
            return False
    return True


def _parse_forecast_target_date(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text[:10], "%Y-%m-%d")
    except Exception:
        return None
    return parsed.replace(tzinfo=timezone(timedelta(hours=8)))


def _days_to_target_text(target_date: Any, *, reference_time: datetime) -> str:
    target_dt = _parse_forecast_target_date(target_date)
    if target_dt is None:
        return "未知"
    days = (target_dt.date() - reference_time.date()).days
    if days > 0:
        return f"还有 {days} 天"
    if days < 0:
        return f"已过 {-days} 天"
    return "今天到期"


def _extract_invalidation_conditions(forecast: Dict[str, Any]) -> List[str]:
    raw = forecast.get("invalidation_conditions")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _reason_has_time_anchor(reason: str) -> bool:
    text = str(reason or "").strip()
    if not text:
        return False
    if re.search(r"\d{4}-\d{2}-\d{2}", text):
        return True
    anchor_tokens = ("截至", "当前", "目标日", "距离目标", "到期", "天")
    return any(token in text for token in anchor_tokens)


def _reason_maps_invalidation(reason: str, invalidation_conditions: List[str]) -> bool:
    text = str(reason or "").strip().lower()
    if not text:
        return False
    hint_tokens = ("失效", "invalidation", "触发", "跌破", "升级", "失败", "崩溃", "净流出")
    if any(token in text for token in hint_tokens):
        return True
    for cond in invalidation_conditions:
        cond_text = str(cond).strip().lower()
        if not cond_text:
            continue
        probe = cond_text[:12]
        if len(probe) >= 4 and probe in text:
            return True
    return False


def _build_prior_review_quality_audit(
    prediction: Dict[str, Any],
    prior_predictions: List[Dict[str, Any]],
    *,
    reference_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = reference_time or _bj_now()
    reviews = prediction.get("prior_prediction_review") if isinstance(prediction.get("prior_prediction_review"), list) else []
    prior_map: Dict[str, Dict[str, Any]] = {}
    for row in prior_predictions:
        if not isinstance(row, dict):
            continue
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            continue
        forecasts = row.get("forecasts") if isinstance(row.get("forecasts"), list) else []
        first_fc = forecasts[0] if forecasts and isinstance(forecasts[0], dict) else {}
        prior_map[run_id] = {
            "target_date": str(first_fc.get("target_date") or "").strip(),
            "invalidation_conditions": _extract_invalidation_conditions(first_fc),
        }

    missing_time_anchor_count = 0
    condition_mapping_missing_count = 0
    premature_weakened_count = 0
    unresolved_prior_run_id_count = 0

    for item in reviews:
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("prior_run_id") or "").strip()
        reason = str(item.get("reason") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        if not _reason_has_time_anchor(reason):
            missing_time_anchor_count += 1
        prior_meta = prior_map.get(run_id)
        if prior_meta is None:
            unresolved_prior_run_id_count += 1
            continue
        conditions = prior_meta.get("invalidation_conditions") if isinstance(prior_meta.get("invalidation_conditions"), list) else []
        mapped = _reason_maps_invalidation(reason, conditions)
        if status in {"weakened", "invalidated"} and not mapped:
            condition_mapping_missing_count += 1
        target_dt = _parse_forecast_target_date(prior_meta.get("target_date"))
        if target_dt is not None and now.date() < target_dt.date() and status in {"weakened", "invalidated"} and not mapped:
            premature_weakened_count += 1

    return {
        "prior_review_total": len([item for item in reviews if isinstance(item, dict)]),
        "prior_review_missing_time_anchor_count": missing_time_anchor_count,
        "prior_review_condition_mapping_missing_count": condition_mapping_missing_count,
        "prior_review_premature_weakened_count": premature_weakened_count,
        "prior_review_unresolved_prior_run_id_count": unresolved_prior_run_id_count,
    }


def _prior_predictions_to_natural_language(
    prior_predictions: List[Dict[str, Any]],
    *,
    reference_time: Optional[datetime] = None,
) -> str:
    now = reference_time or _bj_now()
    valid_rows = [row for row in prior_predictions if _is_valid_prior_prediction_record(row)]
    if not valid_rows:
        return "暂无可用的历史预判记录。"

    rows = valid_rows[-24:]
    lines: List[str] = []
    lines.append("以下均为你自己之前做出的 BTC 长期预判，请把它们当作同一研究脉络的历史版本：")
    lines.append("prior_review 必须先看目标日期与原始失效条件，不得仅因“尚未到达目标价”就判 weakened。")
    lines.append("本轮可用证据来源键：market_context / news_gated / polymarket_gated / whale_summary / quant_research。")
    for idx, row in enumerate(rows, 1):
        run_id = str(row.get("run_id") or "").strip()
        created_at = str(row.get("created_at_bj") or "").strip() or "未知时间"
        current_price = _safe_float(row.get("current_price"))
        price_text = f"{current_price:.2f}" if current_price is not None else "未知"
        forecasts = row.get("forecasts") if isinstance(row.get("forecasts"), list) else []
        first_fc = forecasts[0] if forecasts and isinstance(forecasts[0], dict) else {}
        horizon = int(_safe_float(first_fc.get("horizon_days")) or 0)
        direction = str(first_fc.get("direction") or "unknown").strip().lower() or "unknown"
        target = _safe_float(first_fc.get("price_target"))
        target_text = f"{target:.2f}" if target is not None else "未知"
        target_date = str(first_fc.get("target_date") or "").strip() or "未知"
        days_to_target = _days_to_target_text(target_date, reference_time=now)
        invalidation_conditions = _extract_invalidation_conditions(first_fc)
        thesis = str(first_fc.get("thesis") or "").strip()
        thesis_short = thesis[:160] + "..." if len(thesis) > 160 else thesis

        lines.append(f"{idx}. 你在 {created_at}（run_id={run_id}）的预判：")
        lines.append(
            f"   - 当时价格约 {price_text}；主预测方向={direction}；horizon_days={horizon}；price_target={target_text}。"
        )
        lines.append(f"   - 目标日期={target_date}；相对当前（{_format_bj(now)}）{days_to_target}。")
        if thesis_short:
            lines.append(f"   - 你当时的核心论点：{thesis_short}")
        if invalidation_conditions:
            lines.append("   - 原始失效条件：")
            for cond_idx, cond in enumerate(invalidation_conditions, 1):
                lines.append(f"     {cond_idx}) {cond}")
        else:
            lines.append("   - 原始失效条件：未提供明确条目。")

        review = row.get("prior_prediction_review")
        if isinstance(review, list) and review:
            recent = review[-2:]
            for rv in recent:
                if not isinstance(rv, dict):
                    continue
                prior_run_id = str(rv.get("prior_run_id") or "").strip() or "unknown"
                status = str(rv.get("status") or "not_enough_data").strip()
                reason = str(rv.get("reason") or "").strip()
                reason_short = reason[:140] + "..." if len(reason) > 140 else reason
                lines.append(f"   - 你后来对更早预判（{prior_run_id}）的复盘结论：{status}。{reason_short}")
    return "\n".join(lines)


def _render_prompt(
    template: str,
    *,
    run_id: str,
    event_content: str,
    market_context: Dict[str, Any],
    quant_research_context: Dict[str, Any],
    news_gated_md: str,
    polymarket_gated_md: str,
    whale_summary_md: str,
    prior_predictions: List[Dict[str, Any]],
) -> Tuple[str, str]:
    prompt_now = _bj_now()
    replacements = {
        "{{current_time}}": _format_bj(prompt_now),
        "{{run_id}}": run_id,
        "{{symbol}}": SYMBOL,
        "{{event_content}}": event_content,
        "{{market_context}}": _market_context_to_markdown(market_context),
        "{{quant_research}}": _quant_research_to_markdown(quant_research_context),
        "{{news_gated}}": str(news_gated_md or "").strip() or "No gated news available.",
        "{{polymarket_gated}}": str(polymarket_gated_md or "").strip() or "No gated Polymarket events available.",
        "{{whale_summary}}": str(whale_summary_md or "").strip() or "No whale summary available.",
        "{{prior_predictions}}": _prior_predictions_to_natural_language(prior_predictions, reference_time=prompt_now),
    }
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)

    marker = "# Context"
    if marker in rendered:
        system_prompt, user_prompt = rendered.split(marker, 1)
        return system_prompt.strip(), f"{marker}{user_prompt}".strip()
    return rendered.strip(), ""


def _normalize_forecast(run_id: str, idx: int, forecast: Dict[str, Any], created_at: datetime) -> Dict[str, Any]:
    item = deepcopy(forecast) if isinstance(forecast, dict) else {}
    item["forecast_id"] = str(item.get("forecast_id") or f"{run_id}_f{idx + 1}")
    horizon = int(_safe_float(item.get("horizon_days")) or 0)
    target_date_raw = str(item.get("target_date") or "").strip()
    if horizon < 30 and target_date_raw:
        try:
            target_dt = datetime.strptime(target_date_raw[:10], "%Y-%m-%d").replace(tzinfo=created_at.tzinfo)
            horizon = max(0, (target_dt.date() - created_at.date()).days)
        except Exception:
            horizon = 0
    if horizon < 30:
        raise ValueError(f"forecast {item['forecast_id']} horizon_days must be >= 30")
    if not target_date_raw:
        item["target_date"] = (created_at + timedelta(days=horizon)).strftime("%Y-%m-%d")
    item["horizon_days"] = horizon
    for field in ["price_target", "price_low", "price_high", "confidence"]:
        parsed = _safe_float(item.get(field))
        if parsed is not None:
            item[field] = parsed
    item["direction"] = str(item.get("direction") or "range").lower()
    item["thesis"] = str(item.get("thesis") or "")
    item["drivers"] = item.get("drivers") if isinstance(item.get("drivers"), list) else []
    item["invalidation_conditions"] = (
        item.get("invalidation_conditions") if isinstance(item.get("invalidation_conditions"), list) else []
    )
    return item


def _normalize_prediction_payload(
    run_id: str,
    payload: Dict[str, Any],
    *,
    market_context: Dict[str, Any],
    refs: Dict[str, List[str]],
) -> Dict[str, Any]:
    created_at = _bj_now()
    forecasts_raw = payload.get("forecasts")
    if not isinstance(forecasts_raw, list) or not forecasts_raw:
        raise ValueError("long hunter output must contain non-empty forecasts[]")
    forecasts = [_normalize_forecast(run_id, idx, item, created_at) for idx, item in enumerate(forecasts_raw)]
    return {
        "run_id": run_id,
        "created_at_bj": created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": SYMBOL,
        "current_price": market_context.get("current_price"),
        "narrative_report": str(payload.get("narrative_report") or ""),
        "forecasts": forecasts,
        "prior_prediction_review": payload.get("prior_prediction_review") if isinstance(payload.get("prior_prediction_review"), list) else [],
        "gated_signal_refs": refs,
        "risk_notes": payload.get("risk_notes") if isinstance(payload.get("risk_notes"), list) else [],
        "meta": {
            "agent_type": "long_hunter",
            "schema_version": 1,
            "prediction_only": True,
            "tools_enabled": False,
        },
    }


class LongHunterManager:
    def __init__(self) -> None:
        self.model = _new_chat_model(temperature=0.35)

    def _build_event_content(self, event_type: str, event_content: str) -> str:
        base = str(event_content or "").strip()
        if base:
            return base
        return f"Scheduled weekly BTC long-horizon review. event_type={event_type}"

    def run_once(self, event_type: str = "weekly_long_hunter", event_content: str = "") -> Dict[str, Any]:
        run_id = _bj_now().strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(LOGS_DIR, run_id)
        context_dir = os.path.join(log_dir, "context")
        os.makedirs(context_dir, exist_ok=True)

        news_gated, polymarket_gated, refs_from_gated = _load_gated_signals()
        news_protocol_md, poly_protocol_md, refs_from_protocol = _load_protocol_markdown()
        refs = {
            "news_ids": refs_from_protocol.get("news_ids") or refs_from_gated.get("news_ids") or [],
            "polymarket_ids": refs_from_protocol.get("polymarket_ids") or refs_from_gated.get("polymarket_ids") or [],
        }
        market_context = build_btc_market_context(SYMBOL)
        try:
            quant_research_context = build_quant_research_context(SYMBOL)
        except Exception as exc:
            quant_research_context = {
                "symbol": SYMBOL,
                "generated_at_bj": _bj_now_text(),
                "error": f"{type(exc).__name__}: {exc}",
                "ev_score": {},
                "risk_metrics": {},
                "macro_compare": {"universe": MACRO_COMPARE_SYMBOLS, "rows": [], "leaders": {}},
                "capital_scenarios": {},
                "monthly_backtest": {"error": f"{type(exc).__name__}: {exc}"},
            }
        monthly_backtest_context = (
            quant_research_context.get("monthly_backtest")
            if isinstance(quant_research_context.get("monthly_backtest"), dict)
            else {}
        )
        whale_summary_raw, whale_summary_calls, whale_summary_md = build_whale_summary_context()
        prior_predictions = _load_predictions()
        event_text = self._build_event_content(event_type, event_content)
        template = _read_text(LONG_HUNTER_PROMPT_PATH)
        if not template:
            raise RuntimeError(f"Prompt missing or empty: {LONG_HUNTER_PROMPT_PATH}")
        system_prompt, user_prompt = _render_prompt(
            template,
            run_id=run_id,
            event_content=event_text,
            market_context=market_context,
            quant_research_context=quant_research_context,
            news_gated_md=news_protocol_md,
            polymarket_gated_md=poly_protocol_md,
            whale_summary_md=whale_summary_md,
            prior_predictions=prior_predictions,
        )

        safe_json_dump(news_gated, os.path.join(context_dir, "news_gated.json"), use_lock=False)
        safe_json_dump(polymarket_gated, os.path.join(context_dir, "polymarket_gated.json"), use_lock=False)
        safe_json_dump(market_context, os.path.join(context_dir, "market_context.json"), use_lock=False)
        safe_json_dump(quant_research_context, os.path.join(context_dir, "quant_research.json"), use_lock=False)
        safe_json_dump(monthly_backtest_context, os.path.join(context_dir, "backtest_metrics.json"), use_lock=False)
        safe_json_dump(whale_summary_raw, os.path.join(context_dir, "whale_summary_raw.json"), use_lock=False)
        safe_json_dump(whale_summary_calls, os.path.join(context_dir, "whale_summary_calls.json"), use_lock=False)
        with open(os.path.join(context_dir, "whale_summary.md"), "w", encoding="utf-8") as handle:
            handle.write(whale_summary_md)
        safe_json_dump(prior_predictions, os.path.join(context_dir, "prior_predictions.json"), use_lock=False)
        with open(os.path.join(log_dir, "input.md"), "w", encoding="utf-8") as handle:
            handle.write(f"--- SYSTEM PROMPT ---\n{system_prompt}\n\n--- USER PROMPT ---\n{user_prompt}")

        raw_response = ""
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            structured_output_enabled = _should_try_structured_output()
            model = self.model
            if structured_output_enabled:
                auth_key = LLM_API_KEY.replace("Bearer ", "", 1) if LLM_API_KEY.startswith("Bearer ") else LLM_API_KEY
                kwargs: Dict[str, Any] = {
                    "api_key": auth_key,
                    "model": LLM_MODEL_ID,
                    "temperature": 0.35,
                    "model_kwargs": {"response_format": _long_hunter_response_format()},
                }
                if LLM_BASE_URL:
                    kwargs["base_url"] = LLM_BASE_URL
                    if "dashscope.aliyuncs.com" in LLM_BASE_URL:
                        kwargs["extra_body"] = {"enable_thinking": DASHSCOPE_ENABLE_THINKING}
                    elif "volces.com/api/coding" in LLM_BASE_URL and VOLCENGINE_ENABLE_THINKING:
                        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                model = ChatOpenAI(**kwargs)
            try:
                response = model.invoke(messages)
            except Exception:
                if not structured_output_enabled:
                    raise
                print("⚠️ Long hunter structured output failed, retrying with plain JSON prompt.", flush=True)
                response = self.model.invoke(messages)
            raw_response = str(getattr(response, "content", "") or "")
            print("raw response")
            print(response)
            print('-'*50)
            parsed = _parse_llm_json(raw_response, "long_hunter_response")
            prediction = _normalize_prediction_payload(run_id, parsed, market_context=market_context, refs=refs)
            prior_review_quality = _build_prior_review_quality_audit(
                prediction,
                prior_predictions,
                reference_time=_bj_now(),
            )
            all_predictions = prior_predictions + [prediction]
            safe_json_dump(all_predictions, PREDICTIONS_PATH, use_lock=True)
            safe_json_dump(prediction, os.path.join(log_dir, "prediction.json"), use_lock=False)
            log_data = {
                "agent_type": "long_hunter",
                "content": prediction,
                "tool_calls": [],
                "validation_status": "prediction_only",
                "final_validation_errors": [],
                "timestamp": datetime.now().isoformat(),
                "audit_meta": {
                    "event_type": event_type,
                    "event_details": event_text,
                    "run_id": run_id,
                    "symbol": SYMBOL,
                    "tools_enabled": False,
                    "whale_summary_calls": whale_summary_calls,
                    "quant_research_error": quant_research_context.get("error", ""),
                    "prior_review_quality": prior_review_quality,
                },
            }
        except Exception as exc:
            error_prediction = {
                "run_id": run_id,
                "created_at_bj": _bj_now_text(),
                "symbol": SYMBOL,
                "current_price": market_context.get("current_price"),
                "narrative_report": "Long hunter run failed before producing a valid prediction.",
                "forecasts": [],
                "prior_prediction_review": [],
                "gated_signal_refs": refs,
                "meta": {"agent_type": "long_hunter", "schema_version": 1, "prediction_only": True, "tools_enabled": False},
            }
            prior_review_quality = _build_prior_review_quality_audit(
                error_prediction,
                prior_predictions if isinstance(prior_predictions, list) else [],
                reference_time=_bj_now(),
            )
            safe_json_dump(error_prediction, os.path.join(log_dir, "prediction.json"), use_lock=False)
            log_data = {
                "agent_type": "long_hunter",
                "content": error_prediction,
                "tool_calls": [],
                "validation_status": "prediction_failed",
                "final_validation_errors": [f"{type(exc).__name__}: {exc}"],
                "timestamp": datetime.now().isoformat(),
                "audit_meta": {
                    "event_type": event_type,
                    "event_details": event_text,
                    "run_id": run_id,
                    "symbol": SYMBOL,
                    "tools_enabled": False,
                    "whale_summary_calls": whale_summary_calls,
                    "quant_research_error": quant_research_context.get("error", ""),
                    "prior_review_quality": prior_review_quality,
                },
                "traceback": traceback.format_exc(),
            }
        if raw_response:
            with open(os.path.join(log_dir, "long_hunter_raw_response.txt"), "w", encoding="utf-8") as handle:
                handle.write(raw_response)
        safe_json_dump(log_data, os.path.join(log_dir, "output.json"), use_lock=False)
        return log_data

    async def weekly_monitor(self) -> None:
        while True:
            now = _bj_now()
            state = _load_long_hunter_state()
            next_run_at = _parse_bj_text(state.get("next_run_at_bj"))
            if next_run_at and now >= next_run_at:
                result = self.run_once("weekly_long_hunter", "Scheduled weekly BTC long-horizon review.")
                run_id = str(((result.get("audit_meta") or {}).get("run_id")) or _bj_now().strftime("%Y%m%d_%H%M%S"))
                _update_long_hunter_state_after_run(
                    run_id=run_id,
                    event_type="weekly_long_hunter",
                    trigger_kind="scheduled",
                    next_run_at=next_run_at + timedelta(days=7),
                    run_at=now,
                )

            today_bj = now.strftime("%Y-%m-%d")
            last_snapshot_date = str(state.get("last_daily_snapshot_date_bj") or "").strip()
            if now.hour == 23 and now.minute == 59 and last_snapshot_date != today_bj:
                snapshot_meta = _write_daily_snapshot(now)
                state = _load_long_hunter_state()
                state["last_daily_snapshot_date_bj"] = today_bj
                state["last_daily_snapshot_at_bj"] = snapshot_meta.get("snapshot_at_bj") or _format_bj(now)
                state["last_daily_snapshot_key"] = snapshot_meta.get("snapshot_key") or _snapshot_basename(now)
                _save_long_hunter_state(state)
            await asyncio.sleep(60)

    async def run(self) -> None:
        await self.weekly_monitor()


def trigger_long_hunter_run(
    *,
    event_type: str,
    event_content: str,
    trigger_kind: str,
    next_run_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    run_at = _bj_now()
    manager = LongHunterManager()
    result = manager.run_once(event_type, event_content)
    run_id = str(((result.get("audit_meta") or {}).get("run_id")) or run_at.strftime("%Y%m%d_%H%M%S"))
    _update_long_hunter_state_after_run(
        run_id=run_id,
        event_type=event_type,
        trigger_kind=trigger_kind,
        next_run_at=next_run_at or _default_next_run_at(run_at),
        run_at=run_at,
    )
    return result


def trigger_manual_long_hunter(event_content: str = "") -> Dict[str, Any]:
    now = _bj_now()
    return trigger_long_hunter_run(
        event_type="manual_long_hunter",
        event_content=event_content,
        trigger_kind="manual",
        next_run_at=now + timedelta(days=7),
    )


def get_long_hunter_state_snapshot() -> Dict[str, Any]:
    return _load_long_hunter_state()


async def main() -> None:
    manager = LongHunterManager()
    await manager.run()


if __name__ == "__main__":
    asyncio.run(main())
