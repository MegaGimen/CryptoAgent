import json
import os
import sys
from datetime import datetime
import argparse
from filelock import FileLock, Timeout

def protocol(n=None):
    """
    Reads gated.json from the current directory, which contains news items 
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
    # 过滤掉 importance_score 低于 6 的条目，并按分数降序排序
    MIN_SCORE = 6
    filtered_news = [item for item in data if int(item.get('importance_score', 0)) >= MIN_SCORE]
    latest_news = sorted(filtered_news, key=lambda x: x.get('importance_score', 0), reverse=True)
    
    output = []
    id_list = []
    output.append("# News Section (Gated)")
    output.append("")
    output.append(f"The following news items have been filtered for high relevance (Importance Score >= {MIN_SCORE}).")
    output.append("")
    
    for i, news in enumerate(latest_news, 1):
        title = news.get('title', 'No Title')
        text = news.get('text', 'No Summary')
        sentiment = news.get('sentiment', 'Neutral')
        date = news.get('date', 'Unknown Date')
        topics = news.get('topics', [])
        news_id = news.get('id', 'No ID')
        gate_reason = news.get('gate_reason', 'No reason provided')
        score = news.get('importance_score', 'N/A')
        
        id_list.append(news_id)
        output.append(f"## News{i}: {title} [Score: {score}]")
        output.append(f"Time: {date}")
        output.append(f"Summary: {text}")
        output.append(f"Topics: {', '.join(topics) if topics else 'None'}")
        output.append(f"Sentiment: {sentiment}")
        output.append(f"Gating Reason: {gate_reason}")
        
        if i < len(latest_news):
            output.append("")
            
    return "\n".join(output), id_list

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process news data.')
    parser.add_argument('n', type=int, nargs='?', default=10, help='Number of latest news to return (default: 10)')
    args = parser.parse_args()
    
    text, ids = protocol(args.n)
    print(text)
