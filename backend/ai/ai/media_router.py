"""
media_router.py — gor://a Hybrid Media AI Engine

Purpose:
- Handle image transformation tasks (resize, crop, rotate, blur, grayscale)
- Perform lightweight local operations with PIL/Pillow
- Act as the dispatch center for heavy tasks:
    - image generation (Stable Diffusion via Replicate)
    - image upscaling (Real-ESRGAN)
    - vision captioning (BLIP/LLaVA)
"""

from __future__ import annotations
import os
from typing import Dict, Any, Optional
from PIL import Image, ImageFilter, ImageOps
import io
import base64
import requests


REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")  # Optional fallback


class MediaRouter:

    def __init__(self):
        self.replicate_api_key = REPLICATE_API_KEY

    # ------------------------
    # Image helpers
    # ------------------------
    @staticmethod
    def _load_image_bytes(data: bytes) -> Image.Image:
        return Image.open(io.BytesIO(data)).convert("RGBA")

    @staticmethod
    def _to_bytes(image: Image.Image, format="PNG") -> bytes:
        buf = io.BytesIO()
        image.save(buf, format=format)
        return buf.getvalue()

    @staticmethod
    def _to_base64(data: bytes) -> str:
        return base64.b64encode(data).decode("utf-8")

    # ------------------------
    # LOCAL PROCESSING
    # ------------------------
    def local_edit(self, img_bytes: bytes, edits: Dict[str, Any]) -> Dict[str, Any]:
        """
        Supported:
        - grayscale
        - rotate (degrees)
        - blur (1-10)
        - mirror
        - flip
        - resize: {width, height}
        """
        img = self._load_image_bytes(img_bytes)

        if edits.get("grayscale"):
            img = ImageOps.grayscale(img).convert("RGBA")

        if edits.get("rotate"):
            img = img.rotate(edits["rotate"], expand=True)

        if edits.get("blur"):
            b = min(max(int(edits["blur"]), 1), 10)
            img = img.filter(ImageFilter.GaussianBlur(b))

        if edits.get("mirror"):
            img = ImageOps.mirror(img)

        if edits.get("flip"):
            img = ImageOps.flip(img)

        if edits.get("resize"):
            width = edits["resize"].get("width")
            height = edits["resize"].get("height")
            if width and height:
                img = img.resize((int(width), int(height)))

        img_bytes = self._to_bytes(img)
        return {
            "success": True,
            "base64": self._to_base64(img_bytes),
            "size": img.size,
        }

    # ------------------------
    # CLOUD IMAGE GEN / UPSCALE / CAPTION
    # Delegated to sub-modules
    # ------------------------
    def generate(self, prompt: str, **kwargs):
        """
        Forward to Stable Diffusion router
        """
        from .image_generation import ImageGeneration
        gen = ImageGeneration()
        return gen.generate(prompt, **kwargs)

    def upscale(self, img_bytes: bytes):
        """
        Forward to ESRGAN router
        """
        from .image_upscale import ImageUpscale
        up = ImageUpscale()
        return up.upscale(img_bytes)

    def caption(self, img_bytes: bytes):
        """
        Forward to Vision Caption router
        """
        from .vision_router import VisionRouter
        vr = VisionRouter()
        return vr.caption(img_bytes)
