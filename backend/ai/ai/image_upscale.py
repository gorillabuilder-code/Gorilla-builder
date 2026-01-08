"""
image_upscale.py — gor://a AI Image Upscaling (Real-ESRGAN Hybrid)

Purpose:
- Enhance resolution for small / noisy / compressed images
- Useful for:
    thumbnails → full preview
    product photos → e-commerce
    icon → hero image
- Runs cloud-only (no stable free local models)
- If missing API key → returns a usable fallback response
"""

from __future__ import annotations
import os
import base64
import requests
from typing import Dict, Any

REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")


class ImageUpscale:

    def __init__(self):
        self.replicate_key = REPLICATE_API_KEY
        self.model = os.getenv(
            "GOR_UPSCALE_MODEL",
            "nightmareai/real-esrgan"
        )

    @staticmethod
    def _to_base64(url: str):
        data = requests.get(url).content
        return base64.b64encode(data).decode("utf-8")

    def upscale(self, img_bytes: bytes) -> Dict[str, Any]:
        if not self.replicate_key:
            return {
                "success": False,
                "warning": "Replicate API key missing — upscaling unavailable.",
            }

        try:
            # Send image as base64 to the cloud model
            encoded = base64.b64encode(img_bytes).decode("utf-8")
            resp = requests.post(
                "https://api.replicate.com/v1/predictions",
                headers={
                    "Authorization": f"Token {self.replicate_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "version": self.model,
                    "input": {
                        "image": f"data:image/png;base64,{encoded}"
                    }
                }
            )
            data = resp.json()
            status = data.get("status")
            prediction_url = data.get("urls", {}).get("get")

            while status in ["starting", "processing"]:
                later = requests.get(
                    prediction_url,
                    headers={"Authorization": f"Token {self.replicate_key}"}
                ).json()
                status = later.get("status")
                data = later

            if status != "succeeded":
                return {"success": False, "error": f"Upscale failed: {status}"}

            out_url = data["output"][0]  # Real-ESRGAN return
            return {
                "success": True,
                "base64": self._to_base64(out_url),
            }

        except Exception as exc:
            return {
                "success": False,
                "error": f"real-esrgan-upscale-error: {exc}"
            }
