import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from env_config import load_project_env
load_project_env()
import asyncio
import websockets
import json
import os
import time
from typing import Any, Dict, Optional
# 脚本所在的目录和持久化 JSON 文件名
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "whale_data.json")

# 全局巨鲸钱包注册表，用于在内存中对已知钱包进行建模
whale_wallets = {}

class WhaleWalletModel:
    """
    巨鲸钱包模型：用于跟踪特定钱包的资金流入、流出及相关统计数据。
    """
    def __init__(self, name, address, blockchain):
        self.name = name            # 钱包名称（由 Whale Alert 提供，如 "Binance"）
        self.address = address      # 钱包链上地址
        self.blockchain = blockchain # 所属区块链
        self.total_inflow_usd = 0.0 # 累计流入金额 (USD)
        self.total_outflow_usd = 0.0 # 累计流出金额 (USD)
        self.tx_count = 0           # 涉及的大额交易次数
        self.last_seen = 0          # 最后一次活跃时间戳

    def update(self, flow_type, value_usd, timestamp):
        """
        更新钱包的流动数据
        """
        if flow_type == "inflow":
            self.total_inflow_usd += value_usd
        elif flow_type == "outflow":
            self.total_outflow_usd += value_usd
        
        self.tx_count += 1
        self.last_seen = timestamp

    def to_dict(self):
        """
        将模型转换为字典，方便序列化为 JSON
        """
        return {
            "name": self.name,
            "address": self.address,
            "blockchain": self.blockchain,
            "modeling_stats": {
                "total_inflow_usd": round(self.total_inflow_usd, 2),
                "total_outflow_usd": round(self.total_outflow_usd, 2),
                "net_flow_usd": round(self.total_inflow_usd - self.total_outflow_usd, 2),
                "transaction_count": self.tx_count
            },
            "last_active": self.last_seen
        }

    @classmethod
    def from_dict(cls, data):
        """
        从字典数据恢复模型对象
        """
        model = cls(data["name"], data["address"], data["blockchain"])
        stats = data.get("modeling_stats", {})
        model.total_inflow_usd = stats.get("total_inflow_usd", 0.0)
        model.total_outflow_usd = stats.get("total_outflow_usd", 0.0)
        model.tx_count = stats.get("transaction_count", 0)
        model.last_seen = data.get("last_active", 0)
        return model

def load_whale_data():
    """
    从本地 JSON 文件加载之前的巨鲸建模数据
    """
    global whale_wallets
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for address, wallet_info in data.items():
                    whale_wallets[address] = WhaleWalletModel.from_dict(wallet_info)
            print(f"Loaded {len(whale_wallets)} whale wallets from {DATA_FILE}")
        except Exception as e:
            print(f"Error loading whale data: {e}")
    else:
        print("No existing whale data file found. Starting fresh.")

def save_whale_data():
    """
    将当前内存中的巨鲸建模数据持久化到本地 JSON 文件
    """
    try:
        data_to_save = {address: wallet.to_dict() for address, wallet in whale_wallets.items()}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving whale data: {e}")

def analyze_and_model_whale(message_str):
    """
    解析 Whale Alert 消息并对涉及的非 'unknown wallet' 进行建模。
    """
    try:
        data = json.loads(message_str)
        if data.get("type") != "alert":
            return None

        modeled_whales = []
        
        # 提取交易基本信息
        timestamp = data.get("timestamp")
        blockchain = data.get("blockchain")
        amounts = data.get("amounts", [])
        total_value_usd = sum(a.get("value_usd", 0) for a in amounts)
        
        # 提取地址信息（从 sub_transactions 中获取）
        sub_txs = data.get("transaction", {}).get("sub_transactions", [])
        from_address = None
        to_address = None
        if sub_txs:
            # 简化逻辑：取第一个输入和输出地址作为建模对象
            inputs = sub_txs[0].get("inputs", [])
            outputs = sub_txs[0].get("outputs", [])
            if inputs: from_address = inputs[0].get("address")
            if outputs: to_address = outputs[0].get("address")

        # 是否有数据更新
        data_updated = False

        # 处理发送方 (Outflow)
        from_name = data.get("from")
        if from_name and from_name != "unknown wallet" and from_address:
            if from_address not in whale_wallets:
                whale_wallets[from_address] = WhaleWalletModel(from_name, from_address, blockchain)
            whale = whale_wallets[from_address]
            whale.update("outflow", total_value_usd, timestamp)
            modeled_whales.append(whale.to_dict())
            data_updated = True

        # 处理接收方 (Inflow)
        to_name = data.get("to")
        if to_name and to_name != "unknown wallet" and to_address:
            if to_address not in whale_wallets:
                whale_wallets[to_address] = WhaleWalletModel(to_name, to_address, blockchain)
            whale = whale_wallets[to_address]
            whale.update("inflow", total_value_usd, timestamp)
            modeled_whales.append(whale.to_dict())
            data_updated = True

        # 如果数据有更新，执行持久化保存
        if data_updated:
            save_whale_data()

        return modeled_whales
    except Exception as e:
        print(f"Error modeling whale: {e}")
        return None

def protocol(message_str):
    """
    LLM 分析层：将原始推送消息和建模数据格式化为自然语言字符串。
    """
    try:
        data = json.loads(message_str)
        if data.get("type") != "alert":
            return None

        # 1. 基础交易信息提取
        from_name = data.get("from")
        to_name = data.get("to")
        blockchain = data.get("blockchain")
        amounts = data.get("amounts", [])
        total_value_usd = sum(a.get("value_usd", 0) for a in amounts)
        symbols = ", ".join(list(set(a.get("symbol") for a in amounts)))
        tx_type = data.get("transaction_type", "transfer")
        
        # 2. 提取地址
        sub_txs = data.get("transaction", {}).get("sub_transactions", [])
        from_address = None
        to_address = None
        if sub_txs:
            inputs = sub_txs[0].get("inputs", [])
            outputs = sub_txs[0].get("outputs", [])
            if inputs: from_address = inputs[0].get("address")
            if outputs: to_address = outputs[0].get("address")

        # 3. 构造自然语言开头
        report = [
            "Whale detected! Which means that a great amount of money is transfering on the Blockchain!",
            f"\n--- Transaction Overview ---",
            f"A total of {total_value_usd:,.2f} USD worth of {symbols} was moved via {tx_type} on the {blockchain} network.",
            f"Movement: From '{from_name}' ({from_address if from_address else 'unknown address'}) to '{to_name}' ({to_address if to_address else 'unknown address'})."
        ]

        # 4. 结合历史建模数据 (Whale Wallet Modeling)
        modeling_insights = []
        
        # 检查发送方是否有历史建模
        if from_name != "unknown wallet" and from_address in whale_wallets:
            m = whale_wallets[from_address]
            modeling_insights.append(
                f"SENDER ANALYSIS: The wallet '{m.name}' is a known entity in our database. "
                f"Historically, it has moved a total of {m.total_outflow_usd:,.2f} USD out and received {m.total_inflow_usd:,.2f} USD in. "
                f"Its current net flow recorded is {m.total_inflow_usd - m.total_outflow_usd:,.2f} USD across {m.tx_count} tracked major transactions."
            )

        # 检查接收方是否有历史建模
        if to_name != "unknown wallet" and to_address in whale_wallets:
            m = whale_wallets[to_address]
            modeling_insights.append(
                f"RECEIVER ANALYSIS: The wallet '{m.name}' is a known entity in our database. "
                f"Historically, it has received a total of {m.total_inflow_usd:,.2f} USD and moved {m.total_outflow_usd:,.2f} USD out. "
                f"Its current net flow recorded is {m.total_inflow_usd - m.total_outflow_usd:,.2f} USD across {m.tx_count} tracked major transactions."
            )

        if modeling_insights:
            report.append("\n--- Historical Whale Modeling Insights ---")
            report.extend(modeling_insights)
            report.append("\nThis historical context suggests the significance of this move in relation to the entity's past behavior.")
        else:
            report.append("\nNote: No prior historical modeling data exists for the identified non-unknown wallets in this transaction.")

        return "\n".join(report)

    except Exception as e:
        return f"Error generating LLM report: {e}"


async def _fetch_single_alert_report(
    min_amount_usd: float = 5_000_000.0,
    timeout_seconds: int = 45,
) -> Optional[str]:
    api_key = os.getenv("WHALE_ALERT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing WHALE_ALERT_API_KEY in environment.")

    url = f"wss://leviathan.whale-alert.io/ws?api_key={api_key}"
    subscription_msg = {
        "type": "subscribe_alerts",
        "blockchains": ["ethereum"],
        "symbols": ["eth"],
        "tx_types": ["transfer"],
        "min_value_usd": float(min_amount_usd),
    }

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(subscription_msg))

        deadline = time.monotonic() + max(5, int(timeout_seconds))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            try:
                message = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            if data.get("type") != "alert":
                continue

            amounts = data.get("amounts", [])
            total_value_usd = sum(a.get("value_usd", 0) for a in amounts)
            if total_value_usd < float(min_amount_usd):
                continue

            analyze_and_model_whale(message)
            report = protocol(message)
            if report:
                return report
            return None


def poll_and_get_wakeup_report(
    min_amount_usd: float = 5_000_000.0,
    timeout_seconds: int = 45,
) -> Dict[str, Any]:
    load_whale_data()
    error: Optional[str] = None
    report: Optional[str] = None

    try:
        report = asyncio.run(
            _fetch_single_alert_report(
                min_amount_usd=min_amount_usd,
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    return {
        "report": report,
        "new_count": 1 if report else 0,
        "pending_count_after_consume": 0,
        "error": error,
    }


async def connect():
    # 优先从环境变量读取并清理两端空格/制表符
    api_key = os.getenv("WHALE_ALERT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing WHALE_ALERT_API_KEY in environment.")

    # The WebSocket API URL with the API key included
    url = f"wss://leviathan.whale-alert.io/ws?api_key={api_key}"

    # The subscription message
    subscription_msg = {
        "type": "subscribe_alerts",
        "blockchains": ["ethereum"],
        "symbols": ["eth"],
        "tx_types": ["transfer"],
        "min_value_usd": 5000000.0,
    }

    # Connect to the WebSocket server
    async with websockets.connect(url) as ws:
        # Send the subscription message
        await ws.send(json.dumps(subscription_msg))

        # Wait for a response
        response = await ws.recv()
        resp_data = json.loads(response)

        # Print the response
        print(f"Connection response: {json.dumps(resp_data, indent=2)}")

        # Continue to handle incoming messages
        while True:
            try:
                # Wait for a new message
                message = await ws.recv()  
                
                # 预解析以进行客户端过滤
                data = json.loads(message)
                if data.get("type") == "alert":
                    amounts = data.get("amounts", [])
                    total_value_usd = sum(a.get("value_usd", 0) for a in amounts)
                    
                    # 客户端过滤：如果服务端未按预期过滤，则在此处拦截
                    if total_value_usd < subscription_msg.get("min_value_usd", 0):
                        # print(f"Filtered out alert with value {total_value_usd:,.2f} USD (below threshold)")
                        continue

                # 执行巨鲸建模逻辑
                modeled_data = analyze_and_model_whale(message)
                
                # 获取 LLM 分析所需的自然语言文本
                llm_report = protocol(message)
                
                if llm_report:
                    print("="*40)
                    print(llm_report)
                    print("="*40)
                elif modeled_data:
                    print("--- Whale Modeled (Internal) ---")
                    print(json.dumps(modeled_data, indent=2, ensure_ascii=False))
                else:
                    # 如果是 unknown wallet 或解析失败，则仅打印原始简略信息或跳过
                    print(f"Received alert (skipped modeling): {message[:100]}...")
                    
            except asyncio.TimeoutError:
                print('Timeout error, closing connection')
                break
            except websockets.ConnectionClosed:
                print('Connection closed')
                break

# Run the connect function until it completes
if __name__ == "__main__":
    # 启动前加载历史数据
    load_whale_data()
    asyncio.run(connect())
