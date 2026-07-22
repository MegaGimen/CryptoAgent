import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from env_config import load_project_env
load_project_env()
import requests
import pandas as pd
import numpy as np
import math
import matplotlib.pyplot as plt
import mplfinance as mpf
import time
import json
import os
import hashlib
from typing import Dict, List, Optional, Tuple
from filelock import FileLock, Timeout
from binance import Client
from binance.enums import *
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

# ================== CONFIGURATION ==================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYMBOL = "BTCUSDT"
INTERVAL = "1d"    # Default Daily
LIMIT = 150         # 150 candles to support MA99 calculation

# Monitoring Thresholds (Informational only, AI judges risk)
ALERT_THRESHOLDS = {
    "RSI_OVERBOUGHT": 70,
    "RSI_OVERSOLD": 30,
    "PRICE_CHANGE_1H": 1.5,    # 1h price change threshold (%)
    "PRICE_CHANGE_15M": 0.8,   # 15m price change threshold (%)
    "VOL_SPIKE": 2.0,          # Volume spike threshold (multiplier of avg)
    "RISK_RATIO_INFO": 2.0,    # Risk ratio info trigger (Margin Level <= 2.0)
    "LIQ_DIST_INFO": 15.0      # Distance to liquidation price info trigger (%)
}
# ===================================================

# Interval mapping (code: (name, suffix, sampling_rows))
INTERVAL_CONFIG = {
    "1d": ("daily", "daily", 20),
    "12h": ("12 hours", "12h", 10),
    "4h": ("4h", "4h", 15),
    "1h": ("1h", "1h", 30),
    "15m": ("15 minutes", "15m", 50)
}

# --- ACCOUNT QUERY FUNCTIONS ---

def get_spot_balance():
    """Fetch spot account balance (non-zero assets only)"""
    account = client.get_account()
    balances = account.get('balances', [])
    return [b for b in balances if float(b['free']) > 0 or float(b['locked']) > 0]

def _to_usdt_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    if s.endswith("_PERP"):
        s = s[:-5]
    if "_" in s:
        s = s.split("_", 1)[0]
    if s.endswith("USDT"):
        return s
    if s.endswith("USD"):
        return f"{s[:-3]}USDT"
    return f"{s}USDT"


_to_coin_symbol = _to_usdt_symbol

def get_usdt_futures_account_balance():
    """Fetch USD(S)-M futures account balances (non-zero only)."""
    balances = client.futures_account_balance()
    return [b for b in balances if float(b.get('balance', 0)) > 0 or float(b.get('withdrawAvailable', 0)) > 0]

def get_usdt_futures_positions(symbol: str = SYMBOL):
    """Fetch active USD(S)-M futures positions."""
    usdt_symbol = _to_usdt_symbol(symbol)
    try:
        positions = client.futures_position_information(symbol=usdt_symbol)
    except Exception as e:
        print(f"❌ Failed to fetch usdt futures positions: {e}")
        return []
    return [p for p in positions if p.get('symbol') == usdt_symbol and abs(float(p.get('positionAmt', 0))) > 0]

def get_usdt_futures_open_orders(symbol: str = SYMBOL):
    """Fetch open USD(S)-M futures orders, including conditional/algo protection orders."""
    usdt_symbol = _to_usdt_symbol(symbol)
    try:
        standard_orders = client.futures_get_open_orders(symbol=usdt_symbol)
        try:
            conditional_orders = client.futures_get_open_orders(symbol=usdt_symbol, conditional=True)
        except Exception:
            conditional_orders = client.futures_get_open_algo_orders(symbol=usdt_symbol)

        merged = []
        seen = set()
        for source, rows in (("standard", standard_orders), ("conditional", conditional_orders)):
            if not isinstance(rows, list):
                continue
            for raw in rows:
                if not isinstance(raw, dict):
                    continue
                row = dict(raw)
                order_ref = str(
                    row.get("orderId")
                    or row.get("algoId")
                    or row.get("clientOrderId")
                    or row.get("clientAlgoId")
                    or ""
                )
                order_type = str(row.get("type", row.get("orderType", "")) or "").upper()
                status = str(row.get("status", row.get("algoStatus", "")) or "").upper()
                stop_price = row.get("stopPrice", row.get("triggerPrice"))
                row["orderId"] = order_ref
                row["order_ref"] = order_ref
                row["type"] = order_type
                row["orderType"] = order_type
                row["status"] = status
                row["origQty"] = row.get("origQty", row.get("quantity", "0"))
                row["executedQty"] = row.get("executedQty", row.get("cumQty", "0"))
                if stop_price not in (None, ""):
                    row["stopPrice"] = stop_price
                    row.setdefault("triggerPrice", stop_price)
                row["order_source"] = source
                row["is_conditional"] = source == "conditional"
                dedupe_key = (order_ref, source)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                merged.append(row)
        return merged
    except Exception as e:
        print(f"❌ Failed to fetch usdt futures open orders: {e}")
        return []

def get_usdt_futures_max_open_position(symbol: str = SYMBOL):
    """Estimate max open base-asset quantity from available margin/leverage/price."""
    usdt_symbol = _to_usdt_symbol(symbol)
    try:
        info = client.futures_exchange_info()
        sym_info = next((s for s in info.get('symbols', []) if s.get('symbol') == usdt_symbol), None)
        if not sym_info:
            return {}
        margin_asset = sym_info.get('marginAsset', sym_info.get('quoteAsset', 'USDT'))
        step_size = 0.0
        for f in sym_info.get('filters', []):
            if f.get('filterType') == 'LOT_SIZE':
                step_size = float(f.get('stepSize', 0))
        qty_precision = int(sym_info.get('quantityPrecision', 3))
        balances = client.futures_account_balance()
        bal = next((b for b in balances if b.get('asset') == margin_asset), None)
        available_margin = float(bal.get('withdrawAvailable', 0)) if bal else 0.0
        ticker = client.futures_symbol_ticker(symbol=usdt_symbol)
        price = float(ticker[0]['price']) if isinstance(ticker, list) else float(ticker['price'])
        pos_list = client.futures_position_information(symbol=usdt_symbol)
        pos = next((p for p in pos_list if p.get('symbol') == usdt_symbol), None)
        leverage = int(float(pos.get('leverage', 5))) if pos else 5
        raw_qty = (available_margin * leverage) / max(price, 1e-9)
        if step_size > 0:
            raw_qty = math.floor((raw_qty + 1e-12) / step_size) * step_size
        max_qty = round(max(raw_qty, 0.0), qty_precision)
        return {
            "symbol": usdt_symbol,
            "margin_asset": margin_asset,
            "available_margin": available_margin,
            "leverage": leverage,
            "current_price": price,
            "step_size": step_size,
            "max_quantity": max(0.0, max_qty),
        }
    except Exception as e:
        print(f"❌ Failed to estimate max quantity: {e}")
        return {}


get_coin_futures_account_balance = get_usdt_futures_account_balance
get_coin_futures_positions = get_usdt_futures_positions
get_coin_futures_open_orders = get_usdt_futures_open_orders
get_coin_futures_max_open_position = get_usdt_futures_max_open_position

def get_total_balance_usdt():
    """获取现货+U本位账户折合 USDT 的总资产（估算）"""
    total_usdt = 0.0
    
    # 获取所有资产的当前价格 (仅限 USDT 对)
    try:
        prices = {t['symbol']: float(t['price']) for t in client.get_all_tickers() if t['symbol'].endswith('USDT')}
        prices['USDT'] = 1.0
    except:
        return 0.0

    def convert_to_usdt(asset, amount):
        if asset == 'USDT': return amount
        symbol = f"{asset}USDT"
        return amount * prices.get(symbol, 0)

    # 1. 现货
    spot = get_spot_balance()
    for b in spot:
        total_usdt += convert_to_usdt(b['asset'], float(b['free']) + float(b['locked']))

    # 2. U 本位账户资产
    for b in get_usdt_futures_account_balance():
        total_usdt += convert_to_usdt(b['asset'], float(b.get('balance', 0)))

    # 3. 未实现盈亏（折算）
    for p in get_usdt_futures_positions(SYMBOL):
        pnl = float(p.get('unRealizedProfit', 0))
        margin_asset = str(p.get('marginAsset', 'USDT'))
        total_usdt += convert_to_usdt(margin_asset, pnl)

    return total_usdt

def log_balance():
    """每 15 分钟记录一次总资产"""
    csv_file = "/home/coinautomation/account_history.csv"
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    total_balance = get_total_balance_usdt()
    
    file_exists = os.path.isfile(csv_file)
    with open(csv_file, 'a', encoding='utf-8') as f:
        if not file_exists:
            f.write("timestamp,total_usdt\n")
        f.write(f"{timestamp},{total_balance:.2f}\n")
    print(f"📊 Balance logged: {total_balance:.2f} USDT")

# --- MARKET DATA FUNCTIONS ---

def get_binance_klines(symbol, interval, limit):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        data = requests.get(url, params=params).json()
    except Exception as e:
        print(f"❌ Failed to fetch {symbol} {interval} klines: {e}")
        return None

    df = pd.DataFrame(data, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades',
        'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])

    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c])

    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms') + pd.Timedelta(hours=8)
    df.set_index('timestamp', inplace=True)
    return df

def get_24h_stats(symbol):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    return requests.get(url, params={"symbol": symbol}).json()

def add_all_indicators(df):
    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    # 1. Moving Averages (Binance standard)
    df['ma7'] = close.rolling(7).mean()
    df['ma25'] = close.rolling(25).mean()
    df['ma99'] = close.rolling(99).mean()
    
    # 2. RSI (Binance uses Wilder's Smoothing, equivalent to EWM with alpha=1/period)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Using adjust=False and alpha=1/period matches Wilder's RMA (Relative Moving Average)
    df['rsi'] = 100 - (100 / (1 + gain.ewm(alpha=1/14, adjust=False).mean() / loss.ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan))).fillna(100)

    # 3. MACD (Standard EMA 12, 26, 9)
    df['ema12'] = close.ewm(span=12, adjust=False).mean()
    df['ema26'] = close.ewm(span=26, adjust=False).mean()
    df['macd'] = df['ema12'] - df['ema26']
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['histo'] = df['macd'] - df['signal']

    # 4. Bollinger Bands (SMA 20, StdDev 2)
    df['mid'] = close.rolling(20).mean()
    std = close.rolling(20).std()
    df['upper'] = df['mid'] + 2 * std
    df['lower'] = df['mid'] - 2 * std

    # 5. KDJ (Binance standard: RSV=(C-L9)/(H9-L9)*100, K=SMA(RSV,3), D=SMA(RSV,3) ... wait, Binance uses EWM for K/D)
    low9 = low.rolling(9).min()
    high9 = high.rolling(9).max()
    df['rsv'] = (close - low9) / (high9 - low9).replace(0, np.nan) * 100
    df['k'] = df['rsv'].ewm(span=3, adjust=False).mean()
    df['d'] = df['k'].ewm(span=3, adjust=False).mean()
    df['j'] = 3 * df['k'] - 2 * df['d']

    # 6. CCI (Standard)
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(20).mean()
    mad = (tp - sma_tp).abs().rolling(20).mean()
    df['cci'] = (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))

    # 7. WR (Williams %R)
    df['wr'] = (high9 - close) / (high9 - low9).replace(0, np.nan) * (-100)

    # 8. OBV (Standard)
    df['obv'] = (np.sign(close.diff()) * volume).fillna(0).cumsum()
    return df


def _safe_pct_change(current: float, previous: float) -> float:
    if abs(previous) < 1e-9:
        return 0.0
    return (current - previous) / previous * 100.0


def _count_directional_bars(series: pd.Series) -> Tuple[str, int]:
    values = [float(v) for v in series.dropna().tolist()]
    if len(values) < 2:
        return "flat", 0
    direction = "flat"
    count = 0
    for idx in range(len(values) - 1, 0, -1):
        delta = values[idx] - values[idx - 1]
        if abs(delta) < 1e-9:
            break
        current_direction = "up" if delta > 0 else "down"
        if direction == "flat":
            direction = current_direction
            count = 1
            continue
        if current_direction != direction:
            break
        count += 1
    return direction, count


def _classify_price_momentum(df: pd.DataFrame) -> str:
    if len(df) < 3:
        return "mixed"
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    latest_pct = _safe_pct_change(float(latest.close), float(prev.close))
    prev_pct = _safe_pct_change(float(prev.close), float(prev2.close))
    if abs(latest_pct) < 0.15 and abs(prev_pct) < 0.15:
        return "mixed"
    if latest_pct >= 0:
        return "strengthening_up" if latest_pct > max(prev_pct, 0.0) + 0.05 else "weakening_up"
    return "strengthening_down" if abs(latest_pct) > abs(min(prev_pct, 0.0)) + 0.05 else "weakening_down"


def _classify_rsi_state(df: pd.DataFrame) -> str:
    if len(df) < 2:
        return "flat"
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    delta = float(latest.rsi) - float(prev.rsi)
    current = float(latest.rsi)
    if current <= 35 and delta > 1.0:
        return "oversold_rebounding"
    if current >= 65 and delta < -1.0:
        return "overbought_fading"
    if abs(delta) < 1.0:
        return "flat"
    return "mid_rising" if delta > 0 else "mid_falling"


def _classify_macd_histo_state(df: pd.DataFrame) -> str:
    if len(df) < 2:
        return "near_flat"
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    latest_histo = float(latest.histo)
    prev_histo = float(prev.histo)
    if abs(latest_histo) < 0.2 and abs(prev_histo) < 0.2:
        return "near_flat"
    if latest_histo >= 0:
        return "expanding_bullish" if latest_histo > prev_histo else "contracting_bullish"
    return "expanding_bearish" if latest_histo < prev_histo else "contracting_bearish"


def _format_persistence_label(direction: str, count: int) -> str:
    if direction == "flat" or count <= 0:
        return "flat_or_choppy"
    return f"{direction}_{count}bars"


def build_momentum_ruler(df: pd.DataFrame, interval_name: str) -> Dict[str, str]:
    latest = df.iloc[-1]
    avg_vol = float(df['volume'].tail(20).mean()) if len(df) else 0.0
    volume_ratio = float(latest.volume) / avg_vol if avg_vol > 1e-9 else 1.0
    price_momentum = _classify_price_momentum(df)
    rsi_state = _classify_rsi_state(df)
    macd_histo_state = _classify_macd_histo_state(df)
    persistence_dir, persistence_count = _count_directional_bars(df['close'].tail(8))
    persistence = _format_persistence_label(persistence_dir, persistence_count)
    volume_state = "volume_confirming" if volume_ratio >= 1.3 else "volume_neutral"

    price_explain = {
        "strengthening_up": "价格上行动能继续增强",
        "weakening_up": "价格仍在上行，但推进速度放缓",
        "strengthening_down": "价格下行动能继续增强",
        "weakening_down": "价格仍在下行，但跌速放缓",
        "mixed": "价格缺乏清晰单边推进",
    }
    rsi_explain = {
        "oversold_rebounding": "RSI 从低位修复",
        "overbought_fading": "RSI 从高位回落",
        "mid_rising": "RSI 中位抬升",
        "mid_falling": "RSI 中位走弱",
        "flat": "RSI 变化有限",
    }
    macd_explain = {
        "expanding_bullish": "MACD 柱线在零轴上方扩张",
        "contracting_bullish": "MACD 柱线在零轴上方收敛",
        "expanding_bearish": "MACD 柱线在零轴下方扩张",
        "contracting_bearish": "MACD 柱线在零轴下方收敛",
        "near_flat": "MACD 柱线接近平坦",
    }
    volume_explain = "成交量在放大确认" if volume_state == "volume_confirming" else "成交量未明显放大"
    momentum_summary = (
        f"{interval_name} 动量尺子：{price_explain[price_momentum]}；"
        f"{rsi_explain[rsi_state]}；{macd_explain[macd_histo_state]}；"
        f"连续性 {persistence}；{volume_explain}。"
    )
    return {
        "price_momentum": price_momentum,
        "rsi_state": rsi_state,
        "macd_histo_state": macd_histo_state,
        "persistence": persistence,
        "volume_state": volume_state,
        "momentum_summary": momentum_summary,
    }

def plot_stats(df, suffix=""):
    plt.rcParams['figure.facecolor'] = '#0a0a0a'
    plt.rcParams['axes.facecolor'] = '#0a0a0a'
    plot_df = df.tail(100)

    add_plots = [
        mpf.make_addplot(plot_df['ma7'], color='#00d8ff', width=1.5, label='MA7'),
        mpf.make_addplot(plot_df['ma25'], color='#ffd600', width=1.5, label='MA25'),
        mpf.make_addplot(plot_df['upper'], color='#ff4444', width=1, linestyle='--'),
        mpf.make_addplot(plot_df['lower'], color='#44ff44', width=1, linestyle='--'),
        mpf.make_addplot(plot_df['rsi'], panel=1, color='#ff9900', ylim=(0,100), label='RSI(14)'),
        mpf.make_addplot(plot_df['macd'], panel=2, color='#00ffcc', label='MACD'),
        mpf.make_addplot(plot_df['signal'], panel=2, color='#ffcc00', label='SIGNAL'),
        mpf.make_addplot(plot_df['histo'], panel=2, type='bar', color='#8888ff', label='HISTO'),
        mpf.make_addplot(plot_df['k'], panel=3, color='#ff44cc', label='K'),
        mpf.make_addplot(plot_df['d'], panel=3, color='#44ccff', label='D'),
    ]

    style = mpf.make_mpf_style(
        base_mpf_style='binance',
        marketcolors=mpf.make_marketcolors(up='#00d884', down='#ff5c5c', inherit=True)
    )

    filename = f"stats-{suffix}.png" if suffix else "stats.png"
    fig, axes = mpf.plot(
        plot_df, type='candle', volume=True, style=style,
        addplot=add_plots, panel_ratios=(4,1.5,2,1.5),
        figratio=(14,10), returnfig=True
    )

    fig.suptitle(f'{SYMBOL} {suffix} Chart', color='white', fontsize=16)
    plt.tight_layout()
    fig.savefig(filename, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"✅ Chart saved → {filename}")

def generate_account_report():
    """Generate markdown/json account report for spot + coin-m futures."""
    print("📡 Generating account report...")
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    coin_symbol = _to_coin_symbol(SYMBOL)
    mark_price = 0.0
    alerts_structured = {}
    alert_triggered = False

    def add_acc_alert(alert_type, msg, level, value, threshold):
        nonlocal alert_triggered
        alert_triggered = True
        alerts_structured.setdefault(alert_type, []).append({
            "message": msg,
            "level": level,
            "value": round(float(value), 4),
            "threshold": threshold
        })

    try:
        ticker = client.futures_coin_symbol_ticker(symbol=coin_symbol)
        mark_price = float(ticker[0]['price']) if isinstance(ticker, list) else float(ticker['price'])
    except Exception:
        mark_price = 0.0

    report_data = {
        "timestamp": ts,
        "spot": [],
        "futures_account": [],
        "futures_positions": [],
        "futures_open_orders": [],
        "futures_max_open": {},
    }

    md = "# Binance Account Balance Report\n\n"
    md += f"**Updated at:** {ts}\n\n"

    # 1) Spot
    md += "## Spot Account\n"
    spot = get_spot_balance()
    if spot:
        md += "| Asset | Free | Locked | Total |\n"
        md += "| :--- | :--- | :--- | :--- |\n"
        for b in spot:
            total = float(b['free']) + float(b['locked'])
            md += f"| {b['asset']} | {b['free']} | {b['locked']} | {total:.8f} |\n"
            report_data["spot"].append({
                "asset": b['asset'],
                "free": b['free'],
                "locked": b['locked'],
                "total": total
            })
    else:
        md += "No non-zero spot balance.\n"
    md += "\n"

    # 2) USD(S)-M balances
    md += "## USD(S)-M Futures Account\n"
    # 获取详细账户信息以计算保证金比率
    try:
        acc_info = client.futures_account()
        usdt_asset = next((a for a in acc_info['assets'] if a['asset'] == 'USDT'), None)
        if usdt_asset:
            maint_margin = float(usdt_asset['maintMargin'])
            margin_balance = float(usdt_asset['marginBalance'])
            wallet_balance = float(usdt_asset['walletBalance'])
            pos_margin = float(usdt_asset['positionInitialMargin'])
            order_margin = float(usdt_asset['openOrderInitialMargin'])
            available = float(usdt_asset['availableBalance'])
            unrealized = float(usdt_asset['unrealizedProfit'])
            
            margin_ratio = (maint_margin / margin_balance * 100) if margin_balance > 0 else 0
            
            md += "### Risk & Margin (USDT)\n"
            md += f"- **Margin Ratio**: {margin_ratio:.2f}%\n"
            md += f"- **Wallet Balance**: {wallet_balance:.8f} USDT\n"
            md += f"- **Margin Balance**: {margin_balance:.8f} USDT (Wallet + UnPnl)\n"
            md += f"- **Used Margin**: {pos_margin + order_margin:.8f} USDT (Pos: {pos_margin:.8f}, Order: {order_margin:.8f})\n"
            md += f"- **Available Balance**: {available:.8f} USDT (可用于开新仓/划转)\n"
            md += f"- **Unrealized PnL**: {unrealized:.8f} USDT\n\n"
            
            report_data["futures_risk"] = {
                "margin_ratio": margin_ratio,
                "wallet_balance": wallet_balance,
                "margin_balance": margin_balance,
                "used_margin": pos_margin + order_margin,
                "available_balance": available
            }
    except Exception as e:
        print(f"⚠️ Error fetching risk metrics: {e}")

    fut_balances = get_usdt_futures_account_balance()
    if fut_balances:
        md += "| Asset | Balance | Available |\n"
        md += "| :--- | :--- | :--- |\n"
        for b in fut_balances:
            asset = b.get('asset', '')
            bal = float(b.get('balance', 0))
            avail = float(b.get('withdrawAvailable', 0))
            md += f"| {asset} | {bal:.8f} | {avail:.8f} |\n"
            report_data["futures_account"].append({
                "asset": asset,
                "balance": bal,
                "available": avail
            })
    else:
        md += "No non-zero futures balance.\n"
    md += "\n"

    # 3) Positions
    md += "### Active Positions\n"
    positions = get_usdt_futures_positions(SYMBOL)
    if positions:
        md += "| Symbol | PositionSide | Quantity | EntryPrice | Leverage | MarginType | IsolatedMargin | UnrealizedPnL |\n"
        md += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        for p in positions:
            position_amt = float(p.get('positionAmt', 0))
            side = str(p.get('positionSide', 'BOTH')).upper()
            if side == 'BOTH':
                side = 'LONG' if position_amt > 0 else ('SHORT' if position_amt < 0 else 'BOTH')
            quantity = abs(position_amt)
            entry_price = float(p.get('entryPrice', 0))
            leverage = int(float(p.get('leverage', 0) or 0))
            margin_type = p.get('marginType', 'N/A')
            isolated_margin = float(p.get('isolatedMargin', 0))
            unrealized = float(p.get('unRealizedProfit', 0))
            liq_price = float(p.get('liquidationPrice', 0))

            md += (
                f"| {p.get('symbol')} | {side} | {quantity:.6f} | {entry_price:.2f} | {leverage} | "
                f"{margin_type} | {isolated_margin:.8f} | {unrealized:.8f} |\n"
            )
            report_data["futures_positions"].append({
                "symbol": p.get('symbol'),
                "position_side": side,
                "position_amount": position_amt,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_profit": unrealized,
                "liquidation_price": liq_price,
                "leverage": leverage,
                "margin_type": margin_type,
                "isolated_margin": isolated_margin
            })

            if liq_price > 0 and mark_price > 0:
                dist = abs(mark_price - liq_price) / mark_price * 100
                if dist < ALERT_THRESHOLDS["LIQ_DIST_INFO"]:
                    add_acc_alert(
                        "LIQUIDATION_INFO",
                        f"📏 [{p.get('symbol')}] Liquidation proximity. Mark: {mark_price:.2f}, Liq: {liq_price:.2f} (Dist: {dist:.1f}%)",
                        "INFO",
                        dist,
                        ALERT_THRESHOLDS["LIQ_DIST_INFO"],
                    )
    else:
        md += "No active futures positions.\n"
    md += "\n"

    # 4) Open orders
    md += "### Open Orders\n"
    open_orders = get_usdt_futures_open_orders(SYMBOL)
    if open_orders:
        md += "| OrderRef | Source | Symbol | Side | PositionSide | Type | Trigger | Price | Quantity | Executed | ClosePosition | Status |\n"
        md += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        for o in open_orders:
            trigger_price = o.get('stopPrice', o.get('triggerPrice', ''))
            md += (
                f"| {o.get('order_ref', o.get('orderId'))} | {o.get('order_source', 'standard')} | {o.get('symbol')} | {o.get('side')} | "
                f"{o.get('positionSide', 'BOTH')} | {o.get('type', o.get('orderType'))} | {trigger_price} | {o.get('price')} | "
                f"{o.get('origQty')} | {o.get('executedQty')} | {o.get('closePosition', False)} | {o.get('status')} |\n"
            )
            report_data["futures_open_orders"].append({
                "orderId": o.get('orderId'),
                "order_ref": o.get('order_ref', o.get('orderId')),
                "order_source": o.get('order_source', 'standard'),
                "algoId": o.get('algoId'),
                "symbol": o.get('symbol'),
                "side": o.get('side'),
                "positionSide": o.get('positionSide', 'BOTH'),
                "type": o.get('type', o.get('orderType')),
                "triggerPrice": trigger_price,
                "price": o.get('price'),
                "origQty": o.get('origQty'),
                "executedQty": o.get('executedQty'),
                "closePosition": o.get('closePosition', False),
                "status": o.get('status'),
            })
    else:
        md += "No open futures or conditional orders.\n"
    md += "\n"

    # 5) Max open quantity
    max_open = get_usdt_futures_max_open_position(SYMBOL)
    if max_open:
        report_data["futures_max_open"][max_open["symbol"]] = max_open
        md += "### Max Open Position\n"
        md += (
            f"- **{max_open['symbol']}**: max_quantity={max_open['max_quantity']}, "
            f"margin_asset={max_open['margin_asset']}, available_margin={max_open['available_margin']:.8f}, "
            f"leverage={max_open['leverage']}x, mark_price={max_open['current_price']:.2f}\n\n"
        )

    md += "---\n"
    md += "### 💡 AI Trading Notice\n"
    md += "- **USD(S)-M Futures Only**: 使用 U 本位合约工具链，禁止混用杠杆现货或币本位语义。\n"
    md += "- **Base Quantity**: `quantity` 为标的数量，必须按交易所 `step_size` 对齐。\n"
    md += "- **Pre-Entry Audit**: 开仓前先查询持仓、账户余额与最大可开仓位。\n"
    md += "- **Order Lifecycle**: 用 open orders + order query 管理保护单与触发单状态。\n"

    with open(os.path.join(BASE_DIR, "account.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(f"✅ Account report saved → {os.path.join(BASE_DIR, 'account.md')}")

    with open(os.path.join(BASE_DIR, "account.json"), "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=4, ensure_ascii=False)
    print(f"✅ Account JSON saved → {os.path.join(BASE_DIR, 'account.json')}")

    return alert_triggered, alerts_structured

def protocol(symbol=SYMBOL, save_plots=False):
    markdown = f"# {symbol} metrics on Binance\n\n"
    markdown += "> ### Metrics Definition\n"
    markdown += "> - **MA7/MA25**: Simple Moving Average for 7 and 25 periods. Used to identify trend direction.\n"
    markdown += "> - **RSI(14)**: Relative Strength Index. Used to identify potential overbought or oversold conditions based on market momentum.\n"
    markdown += "> - **MACD**: Moving Average Convergence Divergence. Difference between 12 and 26-period EMA.\n"
    markdown += "> - **Histo**: MACD Histogram. Represents the distance between MACD and its Signal line.\n"
    markdown += "> - **Upper/Lower**: Bollinger Bands (20, 2). Measures market volatility and potential reversal levels.\n"
    markdown += "> - **KDJ (K, D, J)**: Stochastic Oscillator. K is the fast line, D is the slow line, and J is the divergence line. Used to spot trend reversals.\n"
    markdown += "> - **CCI**: Commodity Channel Index. Measures the deviation of price from its average.\n"
    markdown += "> - **WR**: Williams %R. A momentum indicator measuring overbought and oversold levels.\n"
    markdown += "> - **OBV**: On-Balance Volume. Uses volume flow to predict changes in stock price.\n\n"

    all_data = {}
    alerts_text = []
    alerts_structured = {}
    alert_triggered = False

    for interval, (name, suffix, row_count) in INTERVAL_CONFIG.items():
        print(f"📡 Fetching {symbol} {name} data...")
        df = get_binance_klines(symbol, interval, LIMIT)
        if df is None: continue
        df = add_all_indicators(df)
        all_data[interval] = (df, name, suffix, row_count)

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        def add_alert(alert_type, metric_name, value, threshold):
            msg_map = {
                "RSI_OVERBOUGHT": f"🚨 **[{name}] RSI Overbought**: {value:.2f} (> {threshold})",
                "RSI_OVERSOLD": f"🚨 **[{name}] RSI Oversold**: {value:.2f} (< {threshold})",
                "PRICE_SURGE": f"🚨 **[{name}] Price Surge**: {value:+.2f}% in {interval}",
                "PRICE_PLUNGE": f"🚨 **[{name}] Price Plunge**: {value:+.2f}% in {interval}",
                "VOLUME_SPIKE": f"🚨 **[{name}] Volume Spike**: {latest.volume:.1f} ETH (x{value:.1f} of avg)"
            }
            alerts_text.append(msg_map[alert_type])
            if alert_type not in alerts_structured:
                alerts_structured[alert_type] = []
            alerts_structured[alert_type].append({
                "interval": name,
                "metric": metric_name,
                "value": round(float(value), 2),
                "threshold": threshold
            })

        if latest.rsi > ALERT_THRESHOLDS["RSI_OVERBOUGHT"]:
            add_alert("RSI_OVERBOUGHT", "RSI", latest.rsi, ALERT_THRESHOLDS["RSI_OVERBOUGHT"])
        elif latest.rsi < ALERT_THRESHOLDS["RSI_OVERSOLD"]:
            add_alert("RSI_OVERSOLD", "RSI", latest.rsi, ALERT_THRESHOLDS["RSI_OVERSOLD"])

        price_change = (latest.close - prev.close) / prev.close * 100
        if interval == "1h" and abs(price_change) > ALERT_THRESHOLDS["PRICE_CHANGE_1H"]:
            type_str = "PRICE_SURGE" if price_change > 0 else "PRICE_PLUNGE"
            add_alert(type_str, "price_change_1h", price_change, ALERT_THRESHOLDS["PRICE_CHANGE_1H"])
        elif interval == "15m" and abs(price_change) > ALERT_THRESHOLDS["PRICE_CHANGE_15M"]:
            type_str = "PRICE_SURGE" if price_change > 0 else "PRICE_PLUNGE"
            add_alert(type_str, "price_change_15m", price_change, ALERT_THRESHOLDS["PRICE_CHANGE_15M"])

        avg_vol = df['volume'].tail(20).mean()
        if latest.volume > avg_vol * ALERT_THRESHOLDS["VOL_SPIKE"]:
            add_alert("VOLUME_SPIKE", "volume_ratio", latest.volume/avg_vol, ALERT_THRESHOLDS["VOL_SPIKE"])

    markdown += "## Alerts\n"
    if alerts_text:
        alert_triggered = True
        for a in alerts_text:
            markdown += f"- {a}\n"
    else:
        markdown += "- No significant alerts triggered.\n"
    markdown += "\n"

    for interval, (df, name, suffix, row_count) in all_data.items():
        if save_plots:
            plot_stats(df, suffix=suffix)
        
        latest = df.iloc[-1]
        momentum = build_momentum_ruler(df, name)
        markdown += f"## {name} chart\n"
        markdown += "### Candlestick & All Indicators (Dynamic Sampling)\n"
        
        full_recent = df.tail(row_count).copy()
        
        # Dynamic Sampling Logic:
        # 1. Always keep the last 12 rows (recent details)
        # 2. For rows older than 12, sample every 2nd or 3rd row to save tokens
        if len(full_recent) > 15:
            recent_part = full_recent.iloc[-12:]
            older_part = full_recent.iloc[:-12]
            # Sample older part: every 3rd row for 15m/1h, every 2nd for others
            step = 3 if interval in ["15m", "1h"] else 2
            sampled_older = older_part.iloc[::step]
            recent_klines = pd.concat([sampled_older, recent_part])
        else:
            recent_klines = full_recent
            
        recent_klines.index = recent_klines.index.strftime('%Y-%m-%d %H:%M')
        
        markdown += "| Timestamp | Close | Volume | MA7 | MA25 | RSI | MACD | Histo | Upper | Lower | K | D | J | CCI | WR | OBV |\n"
        markdown += "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
        for ts, row in recent_klines.iterrows():
            markdown += f"| {ts} | {row.close:.2f} | {row.volume:.1f} | {row.ma7:.2f} | {row.ma25:.2f} | {row.rsi:.2f} | {row.macd:.2f} | {row.histo:.2f} | {row.upper:.2f} | {row.lower:.2f} | {row.k:.1f} | {row.d:.1f} | {row.j:.1f} | {row.cci:.1f} | {row.wr:.1f} | {row.obv:.0f} |\n"
        
        markdown += f"\n### Latest Indicators Summary\n"
        markdown += f"- **RSI(14)**: {latest.rsi:.2f}\n"
        markdown += f"- **MACD**: {latest.macd:.2f} (Signal: {latest.signal:.2f}, Histo: {latest.histo:.2f})\n"
        markdown += f"- **Bollinger Bands**: Upper {latest.upper:.2f} | Mid {latest.mid:.2f} | Lower {latest.lower:.2f}\n"
        markdown += f"- **KDJ**: K={latest.k:.1f}, D={latest.d:.1f}, J={latest.j:.1f}\n"
        markdown += f"- **CCI**: {latest.cci:.1f}\n"
        markdown += f"- **WR**: {latest.wr:.1f}\n"
        markdown += f"- **OBV**: {latest.obv:.0f}\n\n"
        markdown += "### Momentum Ruler\n"
        markdown += f"- **price_momentum**: `{momentum['price_momentum']}`\n"
        markdown += f"- **rsi_state**: `{momentum['rsi_state']}`\n"
        markdown += f"- **macd_histo_state**: `{momentum['macd_histo_state']}`\n"
        markdown += f"- **persistence**: `{momentum['persistence']}`\n"
        markdown += f"- **volume_state**: `{momentum['volume_state']}`\n"
        markdown += f"- **momentum_summary**: {momentum['momentum_summary']}\n\n"
          
    return markdown, alert_triggered, alerts_structured

def check_order_fills(symbol=SYMBOL):
    """检测最近成交的 U 本位合约订单并返回结构化警报"""
    all_orders = []
    coin_symbol = _to_coin_symbol(symbol)
    try:
        all_orders = client.futures_get_all_orders(symbol=coin_symbol, limit=20)
    except Exception as e:
        print(f"❌ Failed to fetch usdt futures orders: {e}")
        return False, []

    # 读取上次记录的已处理订单 ID
    history_file = "/home/coinautomation/Binance/order_history.json"
    processed_ids = []
    if os.path.exists(history_file):
        try:
            with open(history_file, "r") as f:
                processed_ids = json.load(f)
        except:
            processed_ids = []

    new_fills = []
    # 筛选状态为 FILLED 或 PARTIALLY_FILLED 且未被记录过的订单
    for o in all_orders:
        order_id = str(o['orderId'])
        
        # 如果订单已成交（FILLED）且之前没记录过
        if o['status'] in ['FILLED', 'PARTIALLY_FILLED'] and order_id not in processed_ids:
            # 计算成交均价 (U 本位合约使用 avgPrice)
            exec_qty = float(o['executedQty'])
            avg_price = float(o.get('avgPrice', 0))
            if avg_price == 0:
                avg_price = float(o.get('price', 0))
            
            action = "BOUGHT" if o['side'] == 'BUY' else "SOLD"
            status_text = "fully FILLED" if o['status'] == 'FILLED' else "PARTIALLY filled"
            
            # 生成更自然、更易读的英文描述
            narrative = (
                f"EXECUTION ALERT: Your Coin-M {o['type']} order {order_id} has been {status_text}. "
                f"You {action} {exec_qty} contracts on {o['symbol']} at an average price of {avg_price:.2f} USD. "
                f"Execution time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(o['updateTime']/1000))}."
            )
            
            new_fills.append({
                "message": narrative,
                "orderId": order_id,
                "symbol": o['symbol'],
                "side": o['side'],
                "positionSide": o.get('positionSide', 'BOTH'),
                "price": avg_price,
                "qty": exec_qty,
                "status": o['status']
            })
            # 只有完全成交才记录 ID 避免重复触发
            if o['status'] == 'FILLED':
                processed_ids.append(order_id)

    # 保持记录文件不要无限增长
    if len(processed_ids) > 100:
        processed_ids = processed_ids[-100:]

    if new_fills:
        with open(history_file, "w") as f:
            json.dump(processed_ids, f)
        return True, new_fills
    
    return False, []

# ================== MAIN PROGRAM ==================
if __name__ == "__main__":
    while True:
        try:
            print(f"\n🔄 {time.strftime('%Y-%m-%d %H:%M:%S')} Refreshing {SYMBOL} analysis and account info...")

            # 1. Update Market Report
            tick = get_24h_stats(SYMBOL)
            report_md, market_alert, market_alerts_json = protocol(SYMBOL, save_plots=True)

            with open(os.path.join(BASE_DIR, "metrics_report.md"), "w", encoding="utf-8") as f:
                f.write(report_md)
            print(f"✅ Market report saved → {os.path.join(BASE_DIR, 'metrics_report.md')}")

            # 2. Check for Order Fills (BEFORE generate_account_report)
            fill_triggered, fill_alerts = check_order_fills(SYMBOL)

            if fill_triggered:
                print(f"🎯 {len(fill_alerts)} new order fills detected! Writing to fills.json...")
                # NEW: Write fills with FileLock
                fills_json_path = os.path.join(BASE_DIR, "fills.json")
                lock_fills = FileLock(f"{fills_json_path}.lock", timeout=10)
                temp_fills = f"{fills_json_path}.tmp"
                try:
                    with lock_fills:
                        with open(temp_fills, "w", encoding="utf-8") as f:
                            json.dump(fill_alerts, f, indent=4, ensure_ascii=False)
                        os.replace(temp_fills, fills_json_path)
                except Timeout:
                    print(f"Lock timeout writing {fills_json_path}")
                except Exception as e:
                    if os.path.exists(temp_fills): os.remove(temp_fills)
                    print(f"Error writing {fills_json_path}: {e}")

            # 3. Update Account Report and Risk Alerts (ORDER_FILLED NO LONGER PART OF ALERTS)
            acc_alert, acc_alerts_json = generate_account_report()
            
            # Merge Alerts (Only technical and account risk alerts)
            all_alerts = {**market_alerts_json, **acc_alerts_json}
            alert_triggered = market_alert or acc_alert

            alerts_json_path = os.path.join(BASE_DIR, "alerts.json")
            lock_alerts = FileLock(f"{alerts_json_path}.lock", timeout=10)
            temp_alerts = f"{alerts_json_path}.tmp"
            try:
                with lock_alerts:
                    with open(temp_alerts, "w", encoding="utf-8") as f:
                        json.dump(all_alerts, f, indent=4, ensure_ascii=False)
                    os.replace(temp_alerts, alerts_json_path)
                print(f"✅ Structured alerts saved → {alerts_json_path}")
            except Timeout:
                print(f"Lock timeout writing {alerts_json_path}")
            except Exception as e:
                if os.path.exists(temp_alerts): os.remove(temp_alerts)
                print(f"Error writing {alerts_json_path}: {e}")

            # 4. Log balance (15 min interval check is handled by loop)
            log_balance()

            if alert_triggered:
                print("⚠️  WARNING: Alerts have been triggered!")
                if acc_alert:
                    for category, alerts in acc_alerts_json.items():
                        for a in alerts:
                            print(f"   - {a['message']}")

            print(f"📊 {SYMBOL} 24H Change: {tick['priceChangePercent']}% | Vol: {float(tick['quoteVolume']):.0f} USDT")
            print(f"⏳ Waiting 5 seconds for next refresh...")
            time.sleep(5)
            
        except KeyboardInterrupt:
            print("\n👋 Stopped by user.")
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            print("⏳ Retrying in 15 seconds...")
            time.sleep(15)
