"""
image_generation.py — gor://a Stable Diffusion (Free Hybrid)

Purpose:
- Generate images from text prompts
- Default: Cloud via Replicate (free-tier available)
- If no Replicate API Key: returns simulated response so frontend UI remains functional

Behavior:
- User prompt → SD model
- Optional params: resolution, steps, inference speed
"""

from __future__ import annotations
import os
import requests
import base64
from typing import Dict, Any

REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")

class ImageGeneration:

    def __init__(self):
        self.replicate_key = REPLICATE_API_KEY
        self.replicate_model = os.getenv(
            "GOR_SD_MODEL",
            # SDXL lite / free community
            "stability-ai/sdxl-lite"
        )

    @staticmethod
    def _to_base64_url(url: str) -> str:
        data = requests.get(url).content
        return base64.b64encode(data).decode("utf-8")

    def generate(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """
        Generate an image using SD via Replicate.
        """
        if not self.replicate_key:
            return {
                "success": False,
                "base64": None,
                "warning": "Replicate API key missing — returning mock placeholder",
                "prompt_received": prompt
            }

        try:
            resp = requests.post(
                "https://api.replicate.com/v1/predictions",
                headers={
                    "Authorization": f"Token {self.replicate_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "version": self.replicate_model,
                    "input": {
                        "prompt": prompt,
                        "num_inference_steps": kwargs.get("steps", 20),
                        "guidance_scale": kwargs.get("cfg", 7)
                    }
                }
            )
            data = resp.json()

            # Poll the result
            status = data.get("status")
            prediction_url = data.get("urls", {}).get("get")

            while status in ["starting", "processing"]:
                later = requests.get(prediction_url, headers={"Authorization": f"Token {self.replicate_key}"}).json()
                status = later.get("status")
                data = later

            if status != "succeeded":
                return {"success": False, "error": f"Image generation failed: {status}"}

            image_url = data["output"][0]
            return {
                "success": True,
                "base64": self._to_base64_url(image_url)
            }

        except Exception as exc:
            return {
                "success": False,
                "error": f"sdxl-generation-failed: {exc}"
            }
