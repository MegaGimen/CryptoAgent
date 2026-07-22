import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from env_config import load_project_env
load_project_env()
import json
import os
from datetime import datetime
import argparse
from filelock import FileLock, Timeout

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _get_min_score() -> int:
    raw = os.getenv("NEWS_PROTOCOL_MIN_SCORE", "6").strip()
    try:
        return int(raw)
    except Exception:
        return 6


def _safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _is_emergency(item) -> bool:
    if not isinstance(item, dict):
        return False
    value = item.get("emergency", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def protocol(n=None):
    """
    Reads gated.json from the current directory, which contains BTC-relevant news items
    that have already passed the FirstGate filtering.
    Returns all gated news items formatted as a string.
    """
    json_path = os.path.join(os.path.dirname(__file__), 'gated.json')
    
    if not os.path.exists(json_path):
        return "No gated news available.", []
    
    lock_path = f"{json_path}.lock"
    lock = FileLock(lock_path, timeout=10)
    
    try:
        with lock:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
    except Timeout:
        return "Error: Could not acquire lock for gated.json", []
    except Exception as e:
        return f"Error reading gated.json: {str(e)}", []
    
    # gated.json 已经是经过筛选的高质量信息
    # 过滤掉 importance_score 低于环境变量阈值的条目，并按分数降序排序
    MIN_SCORE = _get_min_score()
    filtered_news = [
        item for item in data
        if _is_emergency(item) or _safe_int(item.get('importance_score', 0)) >= MIN_SCORE
    ]
    emergency_news = sorted(
        [item for item in filtered_news if _is_emergency(item)],
        key=lambda x: _safe_int(x.get('importance_score', 0)),
        reverse=True,
    )
    regular_news = sorted(
        [item for item in filtered_news if not _is_emergency(item)],
        key=lambda x: _safe_int(x.get('importance_score', 0)),
        reverse=True,
    )
    latest_news = emergency_news + regular_news
    
    output = []
    id_list = []
    output.append("# News Section (Gated)")
    output.append("")
    output.append(f"The following news items have been filtered for BTC long-horizon relevance (Importance Score >= {MIN_SCORE}).")
    if emergency_news:
        output.append("")
        output.append("Emergency=true items are grouped first. They are highly time-sensitive and deserve attention, but they must be weighed with regular gated news, market structure, whale context, and quantitative evidence; do not focus only on emergency items.")
    output.append("")

    def append_news_items(section_title, items, start_index):
        if not items:
            return start_index
        output.append(section_title)
        output.append("")
        current_index = start_index
        for news in items:
            title = news.get('title', 'No Title')
            text = news.get('text', 'No Summary')
            sentiment = news.get('sentiment', 'Neutral')
            date = news.get('date', 'Unknown Date')
            topics = news.get('topics', [])
            news_id = news.get('id', 'No ID')
            gate_reason = news.get('gate_reason', 'No reason provided')
            score = news.get('importance_score', 'N/A')
            emergency = _is_emergency(news)
            
            id_list.append(news_id)
            output.append(f"## News{current_index}: {title} [Score: {score}]")
            output.append(f"Emergency: {'true' if emergency else 'false'}")
            output.append(f"Time: {date}")
            output.append(f"Summary: {text}")
            output.append(f"Topics: {', '.join(topics) if topics else 'None'}")
            output.append(f"Sentiment: {sentiment}")
            output.append(f"Gating Reason: {gate_reason}")
            output.append("")
            current_index += 1
        return current_index

    next_index = 1
    next_index = append_news_items("## Emergency News (Time-Sensitive)", emergency_news, next_index)
    append_news_items("## Regular Gated News", regular_news, next_index)

    if not latest_news:
        output.append("No gated news passed the current protocol filter.")

    return "\n".join(output).rstrip(), id_list

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process news data.')
    parser.add_argument('n', type=int, nargs='?', default=10, help='Number of latest news to return (default: 10)')
    args = parser.parse_args()
    
    text, ids = protocol(args.n)
    print(text)
