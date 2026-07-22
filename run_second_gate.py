import os
import json
import logging
from datetime import datetime
from strategy import LLM_API_KEY, LLM_MODEL_ID, LLM_BASE_URL
from langchain_openai import ChatOpenAI
from tools import safe_json_dump, filter_sensitive_items

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROJECT_ROOT = "/home/coinautomation"
NEWS_GATED_PATH = os.path.join(PROJECT_ROOT, "news", "gated.json")
POLY_GATED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "gated.json")

NEWS_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "news_secondgate_prompt.md")
POLY_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "polymarket_secondgate_prompt.md")

BATCH_SIZE = 15

def load_json(path, default=None):
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Error loading {path}: {e}")
        return default

def save_json(path, data):
    safe_json_dump(data, path, use_lock=True)

def invoke_model(prompt_template, user_data=None, log_file_path=None):
    """
    Invoke the LLM using langchain_openai ChatOpenAI.
    Optionally logs the exact input and output to log_file_path.
    """
    try:
        # Construct kwargs
        auth_key = LLM_API_KEY.replace("Bearer ", "", 1) if LLM_API_KEY.startswith("Bearer ") else LLM_API_KEY
        kwargs = {
            "api_key": auth_key,
            "model": LLM_MODEL_ID,
            "temperature": 0.2
        }
        if LLM_BASE_URL:
            kwargs["base_url"] = LLM_BASE_URL
            
        model = ChatOpenAI(**kwargs)
        
        # Prepare messages: separate system prompt and user data
        messages = [
            {"role": "system", "content": prompt_template},
            {"role": "user", "content": user_data}
        ]
            
        response = model.invoke(messages)
        
        parsed_json = None
        if not response.content:
            logging.error("Model returned empty JSON block.")
        else:
            try:
                parsed_json = json.loads(response.content)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse JSON. Model raw output: {response.content[:500]}")
        
        if log_file_path:
            log_data = {
                "input_system": prompt_template,
                "input_user": user_data,
                "raw_output": response.content,
                "parsed_output": parsed_json
            }
            save_json(log_file_path, log_data)
            
        return parsed_json
            
    except Exception as e:
        logging.error(f"LLM Invocation Error: {e}")
        return None

def run_news_second_gate():
    """Clean up News Gated pool (SecondGate)."""
    gated_news = load_json(NEWS_GATED_PATH, [])
    if not gated_news:
        return

    # Optional: Hard-filter any sensitive items that might have snuck in
    gated_news = filter_sensitive_items(gated_news)
    
    if not gated_news:
        save_json(NEWS_GATED_PATH, [])
        return

    logging.info("Running News SecondGate (Timeliness Cleanup)...")
    if not os.path.exists(NEWS_PROMPT_PATH):
        logging.error(f"Prompt missing: {NEWS_PROMPT_PATH}")
        return

    with open(NEWS_PROMPT_PATH, "r", encoding='utf-8') as f:
        news_prompt = f.read()
        
    all_keep_results = []
    
    # Process in batches to avoid token limit
    log_dir = os.path.join(PROJECT_ROOT, "news", "secondgatelog")
    os.makedirs(log_dir, exist_ok=True)
    
    for i in range(0, len(gated_news), BATCH_SIZE):
        batch = gated_news[i:i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        logging.info(f"Processing News SecondGate batch {batch_idx} ({len(batch)} items)...")
        
        log_file = os.path.join(log_dir, f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_idx}.json")
        keep_results = invoke_model(news_prompt, json.dumps(batch, ensure_ascii=False, indent=2), log_file_path=log_file)
        
        if keep_results is None:
            logging.error(f"News SecondGate batch {i//BATCH_SIZE + 1} aborted due to model invocation failure. Remaining batches will not be processed. No cleanup will be performed.")
            return
            
        all_keep_results.extend(keep_results)
        
    keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
    
    new_gated_news = []
    for item in gated_news:
        nid = item.get("id")
        if nid in keep_ids:
            reason = next((r.get("reason") for r in all_keep_results if str(r.get("id")) == nid), item.get("gate_reason", ""))
            item["gate_reason"] = reason
            new_gated_news.append(item)
            
    save_json(NEWS_GATED_PATH, new_gated_news)
    
    log_file = os.path.join(PROJECT_ROOT, "news", "secondgate", f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    save_json(log_file, {"before": len(gated_news), "after": len(new_gated_news), "keep_results": all_keep_results})
    logging.info(f"News SecondGate completed. {len(new_gated_news)}/{len(gated_news)} items kept.")

def run_poly_second_gate():
    """Clean up Polymarket Gated pool (SecondGate)."""
    gated_poly = load_json(POLY_GATED_PATH, [])
    if not gated_poly:
        return

    # Optional: Hard-filter any sensitive items that might have snuck in
    gated_poly = filter_sensitive_items(gated_poly)
    
    if not gated_poly:
        save_json(POLY_GATED_PATH, [])
        return

    logging.info("Running Polymarket SecondGate (Timeliness Cleanup)...")
    if not os.path.exists(POLY_PROMPT_PATH):
        logging.error(f"Prompt missing: {POLY_PROMPT_PATH}")
        return

    with open(POLY_PROMPT_PATH, "r", encoding='utf-8') as f:
        poly_prompt = f.read()
        
    all_keep_results = []
    
    # Process in batches to avoid token limit
    log_dir = os.path.join(PROJECT_ROOT, "polymarket", "secondgatelog")
    os.makedirs(log_dir, exist_ok=True)
    
    for i in range(0, len(gated_poly), BATCH_SIZE):
        batch = gated_poly[i:i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        logging.info(f"Processing Polymarket SecondGate batch {batch_idx} ({len(batch)} items)...")
        
        log_file = os.path.join(log_dir, f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_idx}.json")
        keep_results = invoke_model(poly_prompt, json.dumps(batch, ensure_ascii=False, indent=2), log_file_path=log_file)
        
        if keep_results is None:
            logging.error(f"Polymarket SecondGate batch {i//BATCH_SIZE + 1} aborted due to model invocation failure. Remaining batches will not be processed. No cleanup will be performed.")
            return
            
        all_keep_results.extend(keep_results)
        
    keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
    
    new_gated_poly = []
    for item in gated_poly:
        pid = item.get("id")
        if pid in keep_ids:
            reason = next((r.get("reason") for r in all_keep_results if str(r.get("id")) == pid), item.get("gate_reason", ""))
            item["gate_reason"] = reason
            new_gated_poly.append(item)
            
    save_json(POLY_GATED_PATH, new_gated_poly)
    
    log_file = os.path.join(PROJECT_ROOT, "polymarket", "secondgate", f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    save_json(log_file, {"before": len(gated_poly), "after": len(new_gated_poly), "keep_results": all_keep_results})
    logging.info(f"Polymarket SecondGate completed. {len(new_gated_poly)}/{len(gated_poly)} items kept.")

if __name__ == "__main__":
    # For testing purposes
    run_news_second_gate()
    run_poly_second_gate()