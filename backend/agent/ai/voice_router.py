"""
voice_router.py — gor://a Voice AI (Groq Whisper + Optional Local TTS)

Purpose:
- Speech-to-Text (STT) via Groq Whisper (whisper-large-v3)
- Optional Text-to-Speech (TTS) via local Piper / Coqui if installed
- No local LLM or Whisper model loading (low RAM, no GPU)
"""

from __future__ import annotations
import io
import os
from typing import Dict, Any, Optional

# -----------------------------
# Groq Whisper (STT)
# -----------------------------
try:
    from groq import Groq
    GROQ_AVAILABLE = True
except Exception:
    Groq = None
    GROQ_AVAILABLE = False

# -----------------------------
# Optional local TTS (Coqui / Piper)
# -----------------------------
try:
    from TTS.api import TTS  # Coqui TTS
    TTS_AVAILABLE = True
except Exception:
    TTS = None
    TTS_AVAILABLE = False


class VoiceRouter:
    """
    VoiceRouter coordinates:
    - STT: audio bytes → text (Groq Whisper)
    - TTS: text → audio bytes (optional local)
    """

    def __init__(self):
        self._groq_client = None
        self._tts_model = None

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

    def _ensure_tts(self):
        if not TTS_AVAILABLE:
            return None
        if self._tts_model is None:
            try:
                # Lightweight, CPU-friendly TTS
                self._tts_model = TTS(
                    model_name="tts_models/en/ljspeech/tacotron2-DDC",
                    progress_bar=False,
                )
            except Exception:
                self._tts_model = None
        return self._tts_model

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
                "error": "Groq Whisper is not available. Set GROQ_API_KEY to enable speech recognition."
            }

        try:
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "audio.wav"  # Groq SDK expects a filename

            response = client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-large-v3",
                language=language,
            )

            return {
                "success": True,
                "text": response.text.strip()
            }

        except Exception as exc:
            return {
                "success": False,
                "error": f"groq-whisper-failed: {exc}"
            }

    # =========================================================
    # TTS — Text to Speech (Optional, Local)
    # =========================================================
    def text_to_speech(
        self,
        text: str,
        speaker: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Convert text to speech using local Coqui TTS where available.
        Returns raw WAV bytes.
        """
        tts = self._ensure_tts()

        if tts is None:
            return {
                "success": False,
                "error": "Local TTS not available. Install `TTS` (Coqui) or add a TTS backend."
            }

        try:
            buf = io.BytesIO()
            tts.tts_to_file(
                text=text,
                file_path=buf,
                speaker=speaker if speaker else None,
            )
            return {
                "success": True,
                "audio_bytes": buf.getvalue(),
                "format": "wav"
            }

        except Exception as exc:
            return {
                "success": False,
                "error": f"tts-failed: {exc}"
            }
