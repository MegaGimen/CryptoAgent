import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from env_config import load_project_env
load_project_env()
import requests
import os
import json
import time
import hashlib
import gc
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime
import trafilatura
import cloudscraper
from filelock import FileLock, Timeout

# Add project root to path for importing run_first_gate
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from run_first_gate import run_news_gate
from run_second_gate import run_news_second_gate
from tools import filter_sensitive_items
# 从 .env 文件中获取 API Key (优先尝试 CRYPTONEWS_API_TOKEN，兼容 CRYPTO_API_KEY)
API_KEY = os.getenv('CRYPTONEWS_API_TOKEN') or os.getenv('CRYPTO_API_KEY')
JSON_FILE = 'news_data.json'
BEIJING_TZ = timezone(timedelta(hours=8))
DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

# 复用同一个 Session，避免大量短连接造成句柄/内存放大
HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update(DEFAULT_HEADERS)

# 检查是否成功加载了 API Key
if not API_KEY:
    raise ValueError("API Key not found. Please ensure the .env file contains your key.")

def convert_to_beijing_time(date_str):
    """
    把类似 "Mon, 20 Apr 2026 00:03:35 -0400" 的时间转换为北京时间字符串。
    返回格式保持 RFC2822 风格: "Mon, 20 Apr 2026 12:03:35 +0800"
    """
    if not date_str or not isinstance(date_str, str):
        return date_str

    try:
        dt = parsedate_to_datetime(date_str)
        if dt is None:
            return date_str
        # 若源字符串缺少时区信息，则按 UTC 处理后再转换为北京时间
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BEIJING_TZ).strftime("%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        return date_str

def normalize_news_time_fields(news_item):
    """
    统一将新闻时间字段转换为北京时间，保留原始值用于审计。
    """
    if not isinstance(news_item, dict):
        return

    # 常见时间字段名：优先处理主字段 date
    time_keys = ["date", "published_at", "published", "pubDate"]
    for key in time_keys:
        raw = news_item.get(key)
        if raw and isinstance(raw, str):
            converted = convert_to_beijing_time(raw)
            if converted != raw:
                news_item[f"{key}_original"] = raw
                news_item[key] = converted

def fetch_article_content(url):
    """
    使用 trafilatura 获取新闻详情页面的内容，加入 requests 和 cloudscraper fallback 机制以应对反爬虫
    """
    try:
        # 尝试使用 trafilatura 默认的方法获取
        downloaded = trafilatura.fetch_url(url)
        
        # 如果获取失败，尝试使用带 User-Agent 的 requests
        if not downloaded:
            with HTTP_SESSION.get(url, timeout=10) as response:
                if response.status_code == 200:
                    downloaded = response.text
                elif response.status_code in [403, 401, 503]:
                    # 遇到 403 (Forbidden) 或 503 (Service Unavailable) 通常是 Cloudflare
                    # 使用 cloudscraper 进行终极尝试，且显式关闭连接，避免句柄泄漏
                    with cloudscraper.create_scraper() as scraper:
                        with scraper.get(url, timeout=15) as cf_response:
                            if cf_response.status_code == 200:
                                downloaded = cf_response.text
                            else:
                                print(f"Warning: Access denied ({cf_response.status_code}) for {url} even with cloudscraper.")
                                return ""
                
        if downloaded:
            # 提取主要正文内容
            result = trafilatura.extract(downloaded)
            
            # 如果网站返回了 Cloudflare 等安全质询页面，trafilatura 有时会提取出这部分文字
            if result and "security service to protect itself" in result.lower():
                print(f"Warning: Hit security/anti-bot challenge for {url}.")
                return ""
                
            if result:
                return result
    except Exception as e:
        print(f"Error fetching content from {url}: {e}")
    return ""

def load_local_news():
    """
    加载本地存储的新闻数据 (带文件锁)
    """
    if not os.path.exists(JSON_FILE):
        return []
        
    lock_path = f"{JSON_FILE}.lock"
    lock = FileLock(lock_path, timeout=10)
    try:
        with lock:
            with open(JSON_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Timeout:
        print(f"Lock timeout reading {JSON_FILE}")
        return []
    except json.JSONDecodeError:
        return []
    except Exception as e:
        print(f"Error loading {JSON_FILE}: {e}")
        return []

def save_local_news(news_list):
    """
    保存新闻数据到本地 JSON 文件 (使用原子写入 + 文件锁)
    """
    lock_path = f"{JSON_FILE}.lock"
    lock = FileLock(lock_path, timeout=10)
    temp_file = f"{JSON_FILE}.tmp"
    
    try:
        with lock:
            try:
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(news_list, f, ensure_ascii=False, indent=4)
                os.replace(temp_file, JSON_FILE)
            except Exception as e:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                raise e
    except Timeout:
        print(f"Lock timeout saving {JSON_FILE}")
    except Exception as e:
        print(f"Error saving news to {JSON_FILE}: {e}")

def run_task():
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Fetching news...")
    
    # Check if we should run SecondGate (every 3 hours)
    # We use a simple marker file to track the last run
    last_run_file = os.path.join(PROJECT_ROOT, "news", "secondgate", "last_run.txt")
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
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Running News SecondGate cleanup...")
            run_news_second_gate()
            with open(last_run_file, "w") as f:
                f.write(str(now))
        except Exception as e:
            print(f"Error running News SecondGate: {e}")

    # CryptoNews API URL
    url = "https://cryptonews-api.com/api/v1"

    # 设置请求参数
    params = {
        'tickers': 'ETH',
        'items': 100,
        'sortBy': 'rank',
        'token': API_KEY,
        'fallback': False
    }

    try:
        with HTTP_SESSION.get(url, params=params, timeout=15) as response:
            if response.status_code == 200:
                api_data = response.json()
            else:
                print(f"Failed to fetch data. HTTP Status Code: {response.status_code}")
                try:
                    print(response.json())
                except Exception:
                    print(response.text[:500])
                return

            if 'data' in api_data:
                local_news = load_local_news()
                # 使用 news_url 作为唯一标识符进行去重
                existing_news_map = {item['news_url']: item for item in local_news}
                
                new_entries_added = 0
                total_fetched = len(api_data['data'])
                
                # 直接使用 API 返回的数据，不再与本地数据合并（实现原地替换）
                new_news_list = []
                for index, news_item in enumerate(api_data['data'], 1):
                    news_url = news_item.get('news_url')
                    if not news_url:
                        continue

                    # 统一把新闻时间转换为北京时间，避免模型上下文时区错位
                    normalize_news_time_fields(news_item)
                    
                    # 为 API 返回的每一条新闻生成 ID 和 Rank
                    news_item['rank'] = index
                    news_item['id'] = hashlib.md5(news_url.encode()).hexdigest()[:12]
                    
                    # 仅对“首次出现”的新闻抓取正文；已有 URL 不再重试正文抓取
                    existing_item = existing_news_map.get(news_url)
                    if existing_item is not None:
                        news_item['full_content'] = existing_item.get('full_content', '')
                    else:
                        print(f"Fetching full content for: {news_item.get('title')} (Rank: {index})")
                        news_item['full_content'] = fetch_article_content(news_url)
                        new_entries_added += 1
                    
                    new_news_list.append(news_item)
                
                # 在保存之前，全局剔除包含敏感词的条目
                new_news_list = filter_sensitive_items(new_news_list)
                
                if new_news_list:
                    save_local_news(new_news_list)
                    print(f"Replaced news_data.json with {len(new_news_list)} current articles. (Fetched {new_entries_added} new contents)")
                    
                    # Trigger FirstGate
                    try:
                        run_news_gate()
                    except Exception as e:
                        print(f"Error triggering News FirstGate: {e}")
            else:
                print("API response format unexpected: 'data' key not found.")
    except Exception as e:
        print(f"An error occurred during task execution: {e}")
    finally:
        # 长跑进程显式回收，有助于降低峰值内存滞留
        gc.collect()
        if os.path.isdir("/proc/self/fd"):
            try:
                fd_count = len(os.listdir("/proc/self/fd"))
                print(f"Open file descriptors: {fd_count}")
            except Exception:
                pass

if __name__ == "__main__":
    while True:
        run_task()
        print("Waiting for 3 minutes...")
        time.sleep(600)  # 等待 10 分钟 (600 秒)

