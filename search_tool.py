import os
import json
import time
import hashlib
import requests
from typing import List, Dict, Any, Optional
from simple_rag import rag_instance

class SearchTool:
    def __init__(self, api_key: Optional[str] = None, cache_file: str = "/home/coinautomation/mem/search_cache.json", ttl: int = 2592000):
        """
        Initialize the SearchTool with Tavily API.
        :param api_key: Tavily API Key.
        :param cache_file: Path to the cache file.
        :param ttl: Time to live for cache in seconds (default 30 days for definitions).
        """
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        self.cache_file = cache_file
        self.ttl = ttl
        self.cache = self._load_cache()

    def _load_cache(self) -> Dict[str, Any]:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving search cache: {e}")

    def _get_cache_key(self, query: str) -> str:
        # Normalize query to focus on "definition/background" for better hit rate
        normalized = query.lower().strip()
        if not any(word in normalized for word in ["who is", "what is", "background", "definition"]):
            normalized = f"definition background of {normalized}"
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def search(self, query: str, search_depth: str = "basic") -> str:
        """
        Perform a search focusing on entity definitions and background.
        """
        # Ensure API key is loaded if it was initialized as None
        if not self.api_key:
            self.api_key = os.getenv("TAVILY_API_KEY")

        if not self.api_key:
            return "Error: TAVILY_API_KEY not found."

        # Force query to be background-oriented
        search_query = query
        if not any(word in query.lower() for word in ["background", "who is", "what is", "profile"]):
            search_query = f"Institutional background and profile of {query}"

        cache_key = self._get_cache_key(search_query)
        now = time.time()

        # Check cache
        if cache_key in self.cache:
            entry = self.cache[cache_key]
            if now - entry["timestamp"] < self.ttl:
                return entry["result"]

        # Perform actual search
        try:
            url = "https://api.tavily.com/search"
            payload = {
                "api_key": self.api_key,
                "query": query,
                "search_depth": "advanced" if search_depth == "advanced" else "basic",
                "include_answer": True,
                "max_results": 5
            }
            response = requests.post(url, json=payload, timeout=20)
            response.raise_for_status()
            data = response.json()

            # Format result
            results = data.get("results", [])
            answer = data.get("answer")
            
            formatted_results = []
            if answer:
                formatted_results.append(f"Summary: {answer}\n")
            
            for res in results:
                formatted_results.append(f"- {res.get('title')} ({res.get('url')}): {res.get('content')}")
            
            result_str = "\n".join(formatted_results)
            
            # Sync to RAG KB
            try:
                # We use the original query (entity name) to store in RAG
                entity_name = query.replace("Institutional background and profile of ", "")
                rag_instance.upsert(entity_name, result_str, source="tavily_search")
            except Exception as e:
                print(f"Error syncing to RAG: {e}")

            # Update cache
            self.cache[cache_key] = {
                "query": query,
                "result": result_str,
                "timestamp": now
            }
            self._save_cache()
            
            return result_str

        except Exception as e:
            return f"Error performing search for '{query}': {str(e)}"

# Singleton instance for easy import
search_tool_instance = SearchTool()

def web_search(query: str) -> str:
    """
    Search the web for information about institutions, events, or projects.
    Use this to get background info on entities you're not familiar with.
    """
    return search_tool_instance.search(query)
