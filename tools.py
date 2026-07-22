import os
import json
import re
from typing import Any, Dict, List
from binance import Client
from filelock import FileLock, Timeout

from env_config import load_project_env
load_project_env()

CENSORSHIP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "censorship.json")

def load_censorship_keywords() -> List[str]:
    if not os.path.exists(CENSORSHIP_FILE):
        return []
    try:
        with open(CENSORSHIP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("sensitive_keywords", [])
    except Exception as e:
        print(f"Error loading censorship file: {e}")
        return []

def contains_sensitive_words(text: str, keywords: List[str]) -> bool:
    if not text or not keywords:
        return False
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return True
    return False

def filter_sensitive_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filter out any dictionary item that contains sensitive keywords in its string values.
    """
    keywords = load_censorship_keywords()
    if not keywords:
        return items

    filtered_items = []
    for item in items:
        is_sensitive = False
        # Convert item to string for simple full-text search across all fields
        item_str = json.dumps(item, ensure_ascii=False).lower()
        for kw in keywords:
            if kw.lower() in item_str:
                is_sensitive = True
                print(f"⚠️ Censorship blocked item due to keyword: '{kw}'")
                break
        
        if not is_sensitive:
            filtered_items.append(item)
            
    return filtered_items


def safe_json_loads(s: str, description: str = "JSON", log_on_error: bool = True):
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        if log_on_error:
            print(f"❌ JSON Decode Error ({description}): {str(e)}", flush=True)
            print(f"--- FULL {description} START ---", flush=True)
            print(s, flush=True)
            print(f"--- FULL {description} END ---", flush=True)
        raise


def safe_json_dump(data: Any, file_path: str, use_lock: bool = True) -> None:
    lock_path = f"{file_path}.lock"
    temp_path = f"{file_path}.tmp"

    def _write():
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, file_path)

    if use_lock:
        lock = FileLock(lock_path, timeout=10)
        try:
            with lock:
                _write()
        except Timeout:
            raise TimeoutError(f"Could not acquire lock for {file_path}")
    else:
        _write()


def safe_json_read(file_path: str, description: str = "JSON", use_lock: bool = True):
    if not os.path.exists(file_path):
        return None

    lock_path = f"{file_path}.lock"

    def _read():
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return None
            return safe_json_loads(content, description)

    if use_lock:
        lock = FileLock(lock_path, timeout=10)
        try:
            with lock:
                return _read()
        except Timeout:
            return None
    return _read()


def to_coin_symbol(symbol: str) -> str:
    s = symbol.upper().replace('USDT', '').replace('USD', '')
    return f"{s}USD_PERP"


def test_max_open(symbol='ETHUSDT', leverage=5):
    client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'))
    coin_symbol = to_coin_symbol(symbol)

    try:
        client.futures_coin_change_leverage(symbol=coin_symbol, leverage=int(leverage))
    except Exception as e:
        print(f'Leverage set note: {e}')

    info = client.futures_coin_exchange_info()
    sym_info = next((s for s in info.get('symbols', []) if s.get('symbol') == coin_symbol), None)
    if not sym_info:
        print('symbol not found')
        return

    margin_asset = sym_info.get('marginAsset')
    contract_size = float(sym_info.get('contractSize', 10))
    balances = client.futures_coin_account_balance()
    bal = next((b for b in balances if b.get('asset') == margin_asset), None)
    available_margin = float(bal.get('withdrawAvailable', 0)) if bal else 0.0
    ticker = client.futures_coin_symbol_ticker(symbol=coin_symbol)
    price = float(ticker[0]['price']) if isinstance(ticker, list) else float(ticker['price'])

    max_contracts = int((available_margin * leverage * price) / max(contract_size, 1e-9))
    print({
        'symbol': coin_symbol,
        'margin_asset': margin_asset,
        'available_margin': available_margin,
        'leverage': leverage,
        'price': price,
        'contract_size': contract_size,
        'max_contracts': max_contracts,
    })


if __name__ == '__main__':
    test_max_open(leverage=5)
