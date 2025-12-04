"""
document_ai.py — gor://a Document Intelligence (Free Hybrid)

Purpose:
- Extract text from PDFs
- Detect page blocks
- Send OCR pipeline for scanned PDFs
- Returns structured output for:
    - Document Q&A
    - Summaries
    - Data extraction (tables/fields)
"""

from __future__ import annotations
import io
from typing import Dict, Any, List
import pdfplumber
from PIL import Image
import pytesseract
import base64


class DocumentAI:

    def __init__(self):
        pass

    @staticmethod
    def _to_image(bytes_data: bytes) -> Image.Image:
        return Image.open(io.BytesIO(bytes_data)).convert("RGB")

    # ---------------------------------------------------
    # PDF Extraction — native
    # ---------------------------------------------------
    def extract_pdf_text(self, pdf_bytes: bytes) -> Dict[str, Any]:
        try:
            text_pages = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text_pages.append(page.extract_text() or "")

            joined = "\n\n".join(text_pages)
            return {
                "success": True,
                "pages": text_pages,
                "full_text": joined,
            }

        except Exception as exc:
            return {"success": False, "error": f"pdf-extract-error: {exc}"}

    # ---------------------------------------------------
    # OCR for scanned PDFs or images
    # ---------------------------------------------------
    def ocr_image(self, img_bytes: bytes) -> Dict[str, Any]:
        try:
            img = self._to_image(img_bytes)
            text = pytesseract.image_to_string(img)
            return {"success": True, "text": text}
        except Exception as exc:
            return {"success": False, "error": f"ocr-failed: {exc}"}

    # ---------------------------------------------------
    # Unified Entry
    # ---------------------------------------------------
    def process(self, file_bytes: bytes, file_type: str) -> Dict[str, Any]:
        """
        file_type: pdf or image
        """

        if file_type.lower() == "pdf":
            return self.extract_pdf_text(file_bytes)

        if file_type.lower() in ["png", "jpg", "jpeg", "webp"]:
            return self.ocr_image(file_bytes)

        return {
            "success": False,
            "error": f"Unsupported document type: {file_type}"
        }
