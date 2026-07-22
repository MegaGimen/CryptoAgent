import os
import json
import logging
from datetime import datetime
from strategy import LLM_API_KEY, LLM_MODEL_ID, LLM_BASE_URL
from langchain_openai import ChatOpenAI
from tools import safe_json_dump, filter_sensitive_items
from search_tool import web_search
from simple_rag import rag_instance
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage, AIMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROJECT_ROOT = "/home/coinautomation"
NEWS_DATA_PATH = os.path.join(PROJECT_ROOT, "news", "news_data.json")
POLYMARKET_DATA_PATH = os.path.join(PROJECT_ROOT, "polymarket", "monitor.json")

NEWS_GATED_PATH = os.path.join(PROJECT_ROOT, "news", "gated.json")
POLY_GATED_PATH = os.path.join(PROJECT_ROOT, "polymarket", "gated.json")

NEWS_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "news_firstgate_prompt.md")
POLY_PROMPT_PATH = os.path.join(PROJECT_ROOT, "prompts", "polymarket_firstgate_prompt.md")

BATCH_SIZE = 50

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
    Invoke the LLM using langchain_openai ChatOpenAI with tool calling support.
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
        
        # Define tools
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_institution_background",
                    "description": "Get fixed background, definition, and profile information about a crypto institution, project, or entity. DO NOT use this for news, current events, or recent price action.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entity_name": {
                                "type": "string",
                                "description": "The name of the institution or project to look up (e.g., 'Jump Crypto', 'Lido Finance')."
                            }
                        },
                        "required": ["entity_name"]
                    }
                }
            }
        ]
        
        model_with_tools = model.bind_tools(tools)
        
        # Prepare messages
        messages = [
            SystemMessage(content=prompt_template),
            HumanMessage(content=user_data or "")
        ]
        
        # Tool execution loop
        max_iterations = 5
        iteration = 0
        all_messages = list(messages)
        
        while iteration < max_iterations:
            response = model_with_tools.invoke(all_messages)
            all_messages.append(response)
            
            if not response.tool_calls:
                break
                
            for tool_call in response.tool_calls:
                if tool_call["name"] == "get_institution_background":
                    entity_name = tool_call["args"].get("entity_name")
                    logging.info(f"LLM requesting background for: {entity_name}")
                    
                    # 1. Try RAG first
                    rag_result = rag_instance.query(entity_name)
                    if rag_result:
                        logging.info(f"RAG Hit for: {entity_name}")
                        result = f"[RAG KNOWLEDGE BASE]: {rag_result}"
                    else:
                        # 2. Fallback to web search
                        logging.info(f"RAG Miss. Falling back to search for: {entity_name}")
                        result = web_search(entity_name)
                        
                    all_messages.append(ToolMessage(content=result, tool_call_id=tool_call["id"]))
            
            iteration += 1

        parsed_json = None
        if not response.content:
            logging.error("Model returned empty content.")
        else:
            # Clean up potential markdown formatting
            content = response.content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            try:
                parsed_json = json.loads(content)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse JSON. Model raw output: {content[:500]}")
        
        if log_file_path:
            # Convert message objects to a serializable format
            serialized_history = []
            for msg in all_messages:
                msg_data = {
                    "role": msg.type,
                    "content": msg.content
                }
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    msg_data["tool_calls"] = msg.tool_calls
                if hasattr(msg, "tool_call_id"):
                    msg_data["tool_call_id"] = msg.tool_call_id
                serialized_history.append(msg_data)

            log_data = {
                "input_system": prompt_template,
                "input_user": user_data,
                "raw_output": response.content,
                "parsed_output": parsed_json,
                "messages_history": serialized_history
            }
            save_json(log_file_path, log_data)
            
        return parsed_json
            
    except Exception as e:
        logging.error(f"LLM Invocation Error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return None

def run_news_gate():
    """Process incremental news through FirstGate."""
    raw_news = load_json(NEWS_DATA_PATH, [])
    gated_news = load_json(NEWS_GATED_PATH, [])
    
    history_path = os.path.join(PROJECT_ROOT, "news", "firstgate", "evaluated_ids.json")
    evaluated_news_ids = set(load_json(history_path, []))
    
    new_news = []
    for item in raw_news:
        nid = str(item.get("id") or item.get("news_url") or item.get("title"))
        if nid and nid not in evaluated_news_ids:
            item["id"] = nid
            new_news.append(item)
    
    if new_news:
        # 0. Pre-filter sensitive items
        filtered_new_news = filter_sensitive_items(new_news)
        
        # If items were filtered out, mark them as evaluated so we don't keep retrying them
        for item in new_news:
            if item not in filtered_new_news:
                evaluated_news_ids.add(item["id"])
                
        if not filtered_new_news:
            logging.info("All new news items were blocked by censorship filter.")
            save_json(history_path, list(evaluated_news_ids))
            return
            
        logging.info(f"Found {len(filtered_new_news)} new news items. Running News FirstGate...")
        if not os.path.exists(NEWS_PROMPT_PATH):
            logging.error(f"Prompt file missing: {NEWS_PROMPT_PATH}")
            return
            
        with open(NEWS_PROMPT_PATH, "r", encoding='utf-8') as f:
            news_prompt = f.read()
        
        all_keep_results = []
        has_error = False
        
        # Process in batches to avoid token limit
        log_dir = os.path.join(PROJECT_ROOT, "news", "firstgatelog")
        os.makedirs(log_dir, exist_ok=True)
        
        for i in range(0, len(filtered_new_news), BATCH_SIZE):
            batch = filtered_new_news[i:i + BATCH_SIZE]
            batch_idx = i // BATCH_SIZE + 1
            logging.info(f"Processing News FirstGate batch {batch_idx} ({len(batch)} items)...")
            
            log_file = os.path.join(log_dir, f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_idx}.json")
            keep_results = invoke_model(news_prompt, json.dumps(batch, ensure_ascii=False, indent=2), log_file_path=log_file)
            
            if keep_results is None:
                logging.error(f"News FirstGate batch {i//BATCH_SIZE + 1} aborted due to model invocation failure.")
                has_error = True
                break
                
            all_keep_results.extend(keep_results)
            
        if has_error:
            logging.error("News FirstGate aborted due to partial batch failure. IDs will not be marked as evaluated to allow retry.")
            return
            
        keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
        
        for item in filtered_new_news:
            nid = item["id"]
            evaluated_news_ids.add(nid)
            if nid in keep_ids:
                result_item = next((r for r in all_keep_results if str(r.get("id")) == nid), {})
                item["gate_reason"] = result_item.get("reason", "")
                # Ensure importance_score is an integer
                try:
                    item["importance_score"] = int(result_item.get("importance_score", 5))
                except (ValueError, TypeError):
                    item["importance_score"] = 5
                item["gated_at"] = datetime.now().isoformat()
                gated_news.append(item)
        
        # Sort gated news by importance_score (descending)
        gated_news.sort(key=lambda x: x.get("importance_score", 0), reverse=True)
                
        save_json(NEWS_GATED_PATH, gated_news)
        save_json(history_path, list(evaluated_news_ids))
        
        # Save log
        log_file = os.path.join(PROJECT_ROOT, "news", "firstgate", f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        save_json(log_file, {"incremental": filtered_new_news, "keep_results": all_keep_results})
        logging.info(f"News FirstGate completed. {len(keep_ids)} items added to gated.json")

def run_poly_gate():
    """Process incremental Polymarket data through FirstGate."""
    raw_poly_dict = load_json(POLYMARKET_DATA_PATH, {})
    # Convert grouped data to list if necessary
    raw_poly = []
    if isinstance(raw_poly_dict, dict):
        for event_id, info in raw_poly_dict.items():
            info["event_id"] = event_id
            raw_poly.append(info)
            
    gated_poly = load_json(POLY_GATED_PATH, [])
    
    poly_history_path = os.path.join(PROJECT_ROOT, "polymarket", "firstgate", "evaluated_ids.json")
    evaluated_poly_ids = set(load_json(poly_history_path, []))
    
    new_poly = []
    for item in raw_poly:
        pid = str(item.get("event_id") or item.get("question"))
        if pid and pid not in evaluated_poly_ids:
            item["id"] = pid
            new_poly.append(item)
            
    if new_poly:
        # 0. Pre-filter sensitive items
        filtered_new_poly = filter_sensitive_items(new_poly)
        
        # If items were filtered out, mark them as evaluated so we don't keep retrying them
        for item in new_poly:
            if item not in filtered_new_poly:
                evaluated_poly_ids.add(item["id"])
                
        if not filtered_new_poly:
            logging.info("All new Polymarket items were blocked by censorship filter.")
            save_json(poly_history_path, list(evaluated_poly_ids))
            return
            
        logging.info(f"Found {len(filtered_new_poly)} new polymarket items. Running Poly FirstGate...")
        if not os.path.exists(POLY_PROMPT_PATH):
            logging.error(f"Prompt file missing: {POLY_PROMPT_PATH}")
            return

        with open(POLY_PROMPT_PATH, "r", encoding='utf-8') as f:
            poly_prompt = f.read()
            
        all_keep_results = []
        has_error = False
        
        # Process in batches to avoid token limit
        log_dir = os.path.join(PROJECT_ROOT, "polymarket", "firstgatelog")
        os.makedirs(log_dir, exist_ok=True)
        
        for i in range(0, len(filtered_new_poly), BATCH_SIZE):
            batch = filtered_new_poly[i:i + BATCH_SIZE]
            batch_idx = i // BATCH_SIZE + 1
            logging.info(f"Processing Poly FirstGate batch {batch_idx} ({len(batch)} items)...")
            
            log_file = os.path.join(log_dir, f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{batch_idx}.json")
            keep_results = invoke_model(poly_prompt, json.dumps(batch, ensure_ascii=False, indent=2), log_file_path=log_file)
            
            if keep_results is None:
                logging.error(f"Poly FirstGate batch {i//BATCH_SIZE + 1} aborted due to model invocation failure.")
                has_error = True
                break
                
            all_keep_results.extend(keep_results)
            
        if has_error:
            logging.error("Poly FirstGate aborted due to partial batch failure. IDs will not be marked as evaluated to allow retry.")
            return
            
        keep_ids = {str(res.get("id")) for res in all_keep_results if res.get("id")}
        
        for item in filtered_new_poly:
            pid = item["id"]
            evaluated_poly_ids.add(pid)
            if pid in keep_ids:
                result_item = next((r for r in all_keep_results if str(r.get("id")) == pid), {})
                item["gate_reason"] = result_item.get("reason", "")
                # Ensure importance_score is an integer
                try:
                    item["importance_score"] = int(result_item.get("importance_score", 5))
                except (ValueError, TypeError):
                    item["importance_score"] = 5
                item["gated_at"] = datetime.now().isoformat()
                gated_poly.append(item)
        
        # Sort gated poly by importance_score (descending)
        gated_poly.sort(key=lambda x: x.get("importance_score", 0), reverse=True)
                
        save_json(POLY_GATED_PATH, gated_poly)
        save_json(poly_history_path, list(evaluated_poly_ids))
        
        log_file = os.path.join(PROJECT_ROOT, "polymarket", "firstgate", f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        save_json(log_file, {"incremental": filtered_new_poly, "keep_results": all_keep_results})
        logging.info(f"Poly FirstGate completed. {len(keep_ids)} items added to gated.json")

def main():
    run_news_gate()
    run_poly_gate()

if __name__ == "__main__":
    main()