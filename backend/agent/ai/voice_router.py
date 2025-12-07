"""
voice_router.py — gor://a Voice AI (Free Hybrid: Whisper + Piper/Coqui)

Purpose:
- Speech-to-Text (STT) using Whisper (local or small model)
- Text-to-Speech (TTS) using Piper/Coqui (local where available)
- Keep everything as free as possible for testing and dev

Design:
- Try local libraries first
- If not installed, return clear error but don't crash platform
"""

from __future__ import annotations
import io
from typing import Dict, Any, Optional

# -----------------------------
# Try to import Whisper (STT)
# -----------------------------
try:
    import whisper  # openai/whisper
    WHISPER_AVAILABLE = True
except Exception:
    whisper = None
    WHISPER_AVAILABLE = False

# -----------------------------
# Try to import Coqui TTS / Piper
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
    - STT: audio bytes → text
    - TTS: text → audio bytes
    """

    def __init__(self):
        # Load Whisper model lazily
        self._whisper_model = None
        self._tts_model = None

    # =========================================================
    # Internal helpers
    # =========================================================

    def _ensure_whisper(self):
        if not WHISPER_AVAILABLE:
            return None
        if self._whisper_model is None:
            # Use small or tiny for CPU-friendly local runs
            try:
                self._whisper_model = whisper.load_model("small")
            except Exception:
                self._whisper_model = None
        return self._whisper_model

    def _ensure_tts(self):
        if not TTS_AVAILABLE:
            return None
        if self._tts_model is None:
            try:
                # Auto-select lightweight TTS model
                self._tts_model = TTS(model_name="tts_models/en/ljspeech/tacotron2-DDC", progress_bar=False)
            except Exception:
                self._tts_model = None
        return self._tts_model

    # =========================================================
    # STT — Speech to Text
    # =========================================================
    def speech_to_text(self, audio_bytes: bytes, language: Optional[str] = None) -> Dict[str, Any]:
        """
        Convert audio to text using Whisper (local).
        audio_bytes should be raw file bytes (e.g. WAV, MP3, M4A)
        """
        model = self._ensure_whisper()

        if model is None:
            return {
                "success": False,
                "error": "Whisper STT is not available. Install `whisper` and FFmpeg for local speech recognition."
            }

        try:
            audio_buf = io.BytesIO(audio_bytes)
            # Whisper expects filename or numpy array; easiest to write to temp if needed.
            # For simplicity in this skeleton, we rely on load_audio accepting BytesIO with monkey-patched frontends.
            result = model.transcribe(audio_buf, language=language)
            return {
                "success": True,
                "text": result.get("text", "").strip()
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"whisper-stt-failed: {exc}"
            }

    # =========================================================
    # TTS — Text to Speech
    # =========================================================
    def text_to_speech(self, text: str, speaker: Optional[str] = None) -> Dict[str, Any]:
        """
        Convert text to speech using Coqui TTS where available.
        Returns raw WAV bytes.
        """
        tts = self._ensure_tts()

        if tts is None:
            return {
                "success": False,
                "error": "Coqui TTS is not available. Install `TTS` library or configure a TTS backend."
            }

        try:
            buf = io.BytesIO()
            # Generate directly to memory buffer
            tts.tts_to_file(
                text=text,
                file_path=buf,
                speaker=speaker if speaker else None,
            )
            wav_bytes = buf.getvalue()
            return {
                "success": True,
                "audio_bytes": wav_bytes,
                "format": "wav"
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"tts-failed: {exc}"
            }
