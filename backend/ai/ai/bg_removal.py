"""
bg_removal.py — gor://a Background Removal using Rembg (Free/Open-Source)

Purpose:
- Perform background removal locally via ONNX runtime where possible
- Must run CPU-only OK for small images (thumbnails, icons, product photos)
- No API keys required for local processing
- Supports fallback remote call if future cloud is configured
"""

from __future__ import annotations
import base64
import io
from typing import Dict, Any
from PIL import Image
from rembg import remove


class BackgroundRemoval:

    def __init__(self):
        pass  # No configuration needed for local rembg

    @staticmethod
    def _to_image(bytes_data: bytes) -> Image.Image:
        return Image.open(io.BytesIO(bytes_data)).convert("RGBA")

    @staticmethod
    def _to_bytes(image: Image.Image, format="PNG") -> bytes:
        buf = io.BytesIO()
        image.save(buf, format=format)
        return buf.getvalue()

    @staticmethod
    def _to_base64(bytes_data: bytes) -> str:
        return base64.b64encode(bytes_data).decode("utf-8")

    def remove_bg(self, img_bytes: bytes) -> Dict[str, Any]:
        """
        Local-only background removal for now.
        """
        try:
            result = remove(img_bytes)
        except Exception as exc:
            return {
                "success": False,
                "error": f"background-removal-failed: {exc}"
            }

        return {
            "success": True,
            "base64": self._to_base64(result),
        }
