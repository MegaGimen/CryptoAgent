import json
import os
from datetime import datetime, timedelta
import sys
from filelock import FileLock, Timeout

def protocol(n=None):
    """
    Reads gated.json from the current directory, which contains Polymarket events 
    that have already passed the FirstGate filtering.
    Returns all gated market items formatted as a string.
    """
    json_path = os.path.join(os.path.dirname(__file__), 'gated.json')
    
    if not os.path.exists(json_path):
        return "No gated Polymarket events available.", []
    
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
    # 过滤掉 importance_score 低于 6 的事件，并按分数降序排序
    MIN_SCORE = 6
    filtered_data = [item for item in data if int(item.get('importance_score', 0)) >= MIN_SCORE]
    sorted_data = sorted(filtered_data, key=lambda x: x.get('importance_score', 0), reverse=True)
    
    all_markets = []
    
    for event in sorted_data:
        event_id = event.get('event_id') or event.get('id')
        event_title = event.get('event_title', 'No Title')
        gate_reason = event.get('gate_reason', 'No reason provided')
        score = event.get('importance_score', 'N/A')
        sub_markets = event.get('sub_markets', [])
        
        for sub_market in sub_markets:
            deadline_str = sub_market.get('deadline', 'N/A')
            yes_str = sub_market['prices'].get('Yes', '0.0%').replace('%', '')
            no_str = sub_market['prices'].get('No', '0.0%').replace('%', '')
            
            market_info = {
                'event_id': event_id,
                'event_title': event_title,
                'question': sub_market['question'],
                'deadline': deadline_str,
                'yes': yes_str,
                'no': no_str,
                'gate_reason': gate_reason,
                'importance_score': score
            }
            all_markets.append(market_info)
            
    # Format the result with a clear explanation for the LLM
    output = [
        "# Polymarket Prediction Market Data (Gated)\n",
        f"The following Polymarket events have been filtered for high relevance (Importance Score >= {MIN_SCORE}).\n"
    ]
    
    # 返回所有符合分数要求的预测市场
    top_markets = all_markets
    
    event_ids = []
    for i, market in enumerate(top_markets, 1):
        output.append(f"Market Prediction {i}: {market['event_title']} [Score: {market['importance_score']}]")
        output.append(f"Question: \"{market['question']}\"")
        output.append(f"Deadline: {market['deadline']}")
        output.append(f"Probability of A (Yes/Occur): {market['yes']}%; Probability of B (No/Not Occur): {market['no']}%")
        output.append(f"Gating Reason: {market['gate_reason']}")
        output.append("") # Empty line for readability
        if market['event_id'] not in event_ids:
            event_ids.append(market['event_id'])
        
    return "\n".join(output).strip(), event_ids

if __name__ == "__main__":
    # Default n is 20
    n = 200000
    if len(sys.argv) > 1:
        try:
            n = int(sys.argv[1])
        except ValueError:
            pass
            
    text, ids = protocol(n)
    print(text)
