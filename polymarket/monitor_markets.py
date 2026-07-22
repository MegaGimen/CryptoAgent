import requests
import json
import time
import os
import sys
from datetime import datetime
from typing import List, Dict
from filelock import FileLock, Timeout

# Add project root to path for importing run_first_gate
PROJECT_ROOT = "/home/coinautomation"
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from run_first_gate import run_poly_gate
from run_second_gate import run_poly_second_gate
from tools import filter_sensitive_items

# Configuration
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MONITOR_FILE = os.path.join(SCRIPT_DIR, "monitor.json")
INTERVAL_SECONDS = 5  # 1 minutes
TARGET_LABELS = {"Crypto", "Geopolitics", "Global Politics", "Ukraine & Russia", "Politics"}

def fetch_all_active_events():
    """
    Fetch all active and not closed events from Polymarket Gamma API.
    """
    base_url = "https://gamma-api.polymarket.com/events"
    all_events = []
    limit = 100
    offset = 0
    
    while True:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "sortBy": "createdAt",
            "ascending": "false"
        }
        try:
            response = requests.get(base_url, params=params, timeout=30)
            response.raise_for_status()
            events = response.json()
            if not events:
                break
            
            all_events.extend(events)
            
            if len(events) < limit:
                break
            offset += limit
            # Limit total fetch to avoid long wait
            if offset >= 1000:
                break
        except Exception as e:
            print(f"Error fetching events: {e}")
            break
            
    return all_events

def format_price(price_str):
    try:
        price = float(price_str)
        return f"{price * 100:.1f}%"
    except (ValueError, TypeError):
        return price_str

def format_date(date_str, fallback_year=None):
    """
    Standardize date format to YYYY-MM-DD.
    """
    if not date_str or date_str == 'N/A':
        return 'N/A'
    
    if 'T' in date_str:
        return date_str.split('T')[0]
    
    try:
        dt = datetime.strptime(date_str, "%B %d, %Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        try:
            dt = datetime.strptime(date_str, "%B %d")
            year = fallback_year if fallback_year else datetime.now().year
            return dt.replace(year=int(year)).strftime("%Y-%m-%d")
        except Exception:
            return date_str

def process_events(events):
    """
    Process events into a structured dictionary.
    Filters out markets with deadlines before today.
    """
    grouped_data = {}
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    for event in events:
        event_title = event.get('title', 'No Title')
        event_id = event.get('id')
        markets = event.get('markets', [])
        
        # Extract labels from tags (tags can be list of dicts)
        tags_raw = event.get('tags', [])
        tags = set()
        for tag in tags_raw:
            if isinstance(tag, dict):
                tags.add(tag.get('label'))
            elif isinstance(tag, str):
                tags.add(tag)
        
        # Determine category based on tags
        category = "Other"
        if any(label in tags for label in TARGET_LABELS):
            if "Crypto" in tags:
                category = "Crypto"
            else:
                category = "Politics/Geopolitics"
        else:
            # Skip if not in target labels
            continue
            
        url = f"https://polymarket.com/event/{event.get('slug', '')}"
        
        sub_markets = []
        for market in markets:
            # Skip closed markets
            if market.get('closed') or not market.get('active'):
                continue
                
            deadline = format_date(market.get('endDate'))
            if deadline != 'N/A' and deadline < today_str:
                continue
                
            outcomes = json.loads(market.get('outcomes', '[]'))
            prices_raw = json.loads(market.get('outcomePrices', '[]'))
            
            prices = {}
            if len(outcomes) == 2 and len(prices_raw) == 2:
                prices = {
                    outcomes[0]: format_price(prices_raw[0]),
                    outcomes[1]: format_price(prices_raw[1])
                }
            elif len(outcomes) == len(prices_raw):
                for i in range(len(outcomes)):
                    prices[outcomes[i]] = format_price(prices_raw[i])
            
            sub_markets.append({
                "market_id": market.get('id'),
                "question": market.get('question', ''),
                "deadline": deadline,
                "prices": prices
            })
            
        if sub_markets:
            grouped_data[event_id] = {
                "event_title": event_title,
                "category": category,
                "url": url,
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sub_markets": sub_markets
            }
            
    return grouped_data

def update_monitor_json(new_data):
    """
    Merge new snapshot into monitor.json, keeping only the latest.
    Uses FileLock for cross-process synchronization.
    """
    lock_path = f"{MONITOR_FILE}.lock"
    lock = FileLock(lock_path, timeout=10)
    
    try:
        with lock:
            if os.path.exists(MONITOR_FILE):
                try:
                    with open(MONITOR_FILE, 'r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                except Exception:
                    existing_data = {}
            else:
                existing_data = {}
                
            for event_id, event_info in new_data.items():
                # Directly update with new info
                existing_data[event_id] = event_info
            
            temp_file = f"{MONITOR_FILE}.tmp"
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(existing_data, f, indent=4, ensure_ascii=False)
                os.replace(temp_file, MONITOR_FILE)
            except Exception as e:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                raise e
    except Timeout:
        print(f"Lock timeout for {MONITOR_FILE}")
    except Exception as e:
        print(f"Error updating {MONITOR_FILE}: {e}")

def run_monitor():
    print(f"Starting PolyData Market Monitor...")
    print(f"Monitoring: {', '.join(TARGET_LABELS)}")
    print(f"Saving to: {MONITOR_FILE}")
    
    while True:
        try:
            # Check if we should run SecondGate (every 3 hours)
            last_run_file = os.path.join(PROJECT_ROOT, "polymarket", "secondgate", "last_run.txt")
            should_run_second = False
            now = time.time()
            if not os.path.exists(last_run_file):
                should_run_second = True
            else:
                try:
                    with open(last_run_file, "r") as f:
                        last_run = float(f.read().strip())
                        if now - last_run > 3 * 3600:
                            should_run_second = True
                except:
                    should_run_second = True

            if should_run_second:
                try:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running Poly SecondGate cleanup...")
                    run_poly_second_gate()
                    with open(last_run_file, "w") as f:
                        f.write(str(now))
                except Exception as e:
                    print(f"Error running Poly SecondGate: {e}")

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Polling latest events...")
            events = fetch_all_active_events()
            new_data = process_events(events)
            
            # Filter sensitive items from new_data dict before updating monitor.json
            filtered_data = {}
            for k, v in new_data.items():
                if filter_sensitive_items([v]): # If list is not empty, it passed the filter
                    filtered_data[k] = v
            new_data = filtered_data
            
            update_monitor_json(new_data)
            print(f"Updated {len(new_data)} events in monitor.json")
            
            # Trigger FirstGate
            try:
                run_poly_gate()
            except Exception as e:
                print(f"Error triggering Poly FirstGate: {e}")
        except Exception as e:
            print(f"Monitoring loop error: {e}")
            
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    run_monitor()
