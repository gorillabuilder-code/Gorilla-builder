"""
voice_router.py — gor://a Voice AI (Groq Whisper + PlayAI TTS)

Purpose:
- Speech-to-Text (STT) via Groq Whisper (whisper-large-v3)
- Text-to-Speech (TTS) via PlayAI (served through Groq)
- No local models, no GPU, low RAM
"""

from __future__ import annotations
import io
import os
from typing import Dict, Any, Optional

# -----------------------------
# Groq Client
# -----------------------------
try:
    from groq import Groq
    GROQ_AVAILABLE = True
except Exception:
    Groq = None
    GROQ_AVAILABLE = False


class VoiceRouter:
    """
    VoiceRouter coordinates:
    - STT: audio bytes → text (Groq Whisper)
    - TTS: text → audio bytes (PlayAI via Groq)
    """

    def __init__(self):
        self._groq_client = None

    # =========================================================
    # Internal helpers
    # =========================================================

    def _ensure_groq(self):
        if not GROQ_AVAILABLE:
            return None
        if self._groq_client is None:
            api_key = os.getenv("GROQ_API_KEY")
            if not api_key:
                return None
            self._groq_client = Groq(api_key=api_key)
        return self._groq_client

    # =========================================================
    # STT — Speech to Text (Groq Whisper)
    # =========================================================
    def speech_to_text(
        self,
        audio_bytes: bytes,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Convert audio to text using Groq Whisper (whisper-large-v3).
        audio_bytes should be raw file bytes (wav, mp3, m4a).
        """
        client = self._ensure_groq()

        if client is None:
            return {
                "success": False,
                "error": "Groq client unavailable. Set GROQ_API_KEY."
            }

        try:
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "audio.wav"  # filename required by SDK

            response = client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-large-v3",
                language=language,
            )

            return {
                "success": True,
                "text": response.text.strip(),
            }

        except Exception as exc:
            return {
                "success": False,
                "error": f"groq-whisper-failed: {exc}",
            }

    # =========================================================
    # TTS — Text to Speech (PlayAI via Groq)
    # =========================================================
    def text_to_speech(
        self,
        text: str,
        voice: str = "default",
    ) -> Dict[str, Any]:
        """
        Convert text to speech using PlayAI TTS served via Groq.
        Returns raw WAV bytes.
        """
        client = self._ensure_groq()

        if client is None:
            return {
                "success": False,
                "error": "Groq client unavailable. Set GROQ_API_KEY."
            }

        try:
            response = client.audio.speech.create(
                model="playai-tts",
                voice=voice,
                input=text,
                format="wav",
            )

            return {
                "success": True,
                "audio_bytes": response.audio,
                "format": "wav",
            }

        except Exception as exc:
            return {
                "success": False,
                "error": f"groq-playai-tts-failed: {exc}",
            }
