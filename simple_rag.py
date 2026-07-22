import os
import json
import hashlib
from typing import List, Dict, Any, Optional

class SimpleRAG:
    def __init__(self, db_path: str = "/home/coinautomation/mem/institution_kb.json"):
        self.db_path = db_path
        self.kb = self._load_kb()

    def _load_kb(self) -> Dict[str, Any]:
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_kb(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(self.kb, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving RAG KB: {e}")

    def query(self, entity_name: str) -> Optional[str]:
        """检索机构定义"""
        key = entity_name.lower().strip()
        # 简单匹配：直接匹配或关键词匹配
        if key in self.kb:
            return self.kb[key]["definition"]
        
        # 模糊匹配逻辑（可选增强）
        for name, data in self.kb.items():
            if key in name or name in key:
                return data["definition"]
        return None

    def upsert(self, entity_name: str, definition: str, source: str = "web_search"):
        """更新或插入机构定义"""
        key = entity_name.lower().strip()
        self.kb[key] = {
            "entity_name": entity_name,
            "definition": definition,
            "source": source,
            "updated_at": os.popen("date +%Y-%m-%d").read().strip()
        }
        self._save_kb()

rag_instance = SimpleRAG()
