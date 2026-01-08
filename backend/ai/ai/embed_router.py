"""
embed_router.py — gor://a Embeddings (Hybrid Free)

Purpose:
- Convert text into vector embeddings for:
    - Semantic search
    - Document Q&A
    - Project memory
    - Code referencing
- Primary: Local SentenceTransformers
- Fallback: Voyage AI (if API key exists & chosen)
"""

from __future__ import annotations
import os
from typing import List, Dict, Any

import numpy as np

try:
    from sentence_transformers import SentenceTransformer
    LOCAL_MODEL_AVAILABLE = True
except Exception:
    LOCAL_MODEL_AVAILABLE = False

import requests

VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY")

class EmbedRouter:

    def __init__(self):
        self.local_model_name = os.getenv("GOR_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        self.voyage_key = VOYAGE_API_KEY

        self.local_model = None
        if LOCAL_MODEL_AVAILABLE:
            try:
                self.local_model = SentenceTransformer(self.local_model_name)
            except Exception:
                self.local_model = None

    # -------------------------------
    # Helpers
    # -------------------------------
    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(v)
        return v / norm if norm != 0 else v

    # -------------------------------
    # LOCAL MODE — free
    # -------------------------------
    def local_embed(self, text_list: List[str]) -> List[List[float]]:
        vectors = self.local_model.encode(text_list).astype("float32")
        return [self._normalize(v).tolist() for v in vectors]

    # -------------------------------
    # CLOUD FALLBACK — Voyage (if key exists)
    # -------------------------------
    def voyage_embed(self, text_list: List[str]) -> List[List[float]]:
        url = "https://api.voyageai.com/v1/embeddings"
        headers = {"Authorization": f"Bearer {self.voyage_key}"}
        resp = requests.post(url, headers=headers, json={"texts": text_list})
        data = resp.json()
        return [self._normalize(np.array(vec)).tolist() for vec in data["embeddings"]]

    # -------------------------------
    # Unified Entry Point
    # -------------------------------
    def embed(self, text_list: List[str]) -> Dict[str, Any]:

        if self.local_model:
            try:
                vecs = self.local_embed(text_list)
                return {"success": True, "vectors": vecs, "mode": "local"}
            except Exception as exc:
                pass

        # fallback cloud
        if self.voyage_key:
            try:
                vecs = self.voyage_embed(text_list)
                return {"success": True, "vectors": vecs, "mode": "voyage-cloud"}
            except Exception as exc:
                return {"success": False, "error": f"voyage-error: {exc}"}

        return {
            "success": False,
            "error": "No local embed model & no cloud key — embeddings unavailable"
        }
