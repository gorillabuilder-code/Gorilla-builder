"""
vision_router.py — gor://a Vision AI (Captioning / Object Recognition)

Purpose:
- Caption images (describe scenes)
- Assist OCR pipeline
- Provide context for agents writing UI or accessibility
- Local lightweight fallback if no API key (returns stub only)

Primary: Replicate (BLIP or LLaVA)
Fallback: returns generic caption stating unavailable
"""

from __future__ import annotations
import os
import base64
import io
from typing import Dict, Any
import requests
from PIL import Image

REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")

class VisionRouter:

    def __init__(self):
        self.replicate_api_key = REPLICATE_API_KEY
        self.replicate_url = "https://api.replicate.com/v1/predictions"
        self.model = os.getenv("GOR_VISION_MODEL", "salesforce/blip")

    @staticmethod
    def _to_base64(bytes_data: bytes):
        return base64.b64encode(bytes_data).decode("utf-8")

    def caption(self, img_bytes: bytes) -> Dict[str, Any]:
        if not self.replicate_api_key:
            return {
                "success": False,
                "caption": "(vision unavailable — no replicate key configured)"
            }

        try:
            resp = requests.post(
                self.replicate_url,
                headers={
                    "Authorization": f"Token {self.replicate_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "version": self.model,
                    "input": {"image": f"data:image/png;base64,{self._to_base64(img_bytes)}"}
                }
            )
            data = resp.json()
            caption = data.get("output") or data.get("prediction") or "No caption returned"
            return {"success": True, "caption": caption}
        except Exception as exc:
            return {
                "success": False,
                "error": f"vision-inference-failed: {exc}"
            }
