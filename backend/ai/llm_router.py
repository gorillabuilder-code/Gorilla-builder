"""
llm_router.py — gor://a Groq-Exclusive LLM Router

Purpose:
- Centralize ALL communication with Groq LLMs
- Allow planner, coder, debugger, deployer, tester to share routing
- Ensure correct model is used depending on task
- Retry on transient failures
- Consistent formatting and hygiene
"""

from __future__ import annotations

import os
import json
import httpx
import asyncio
from typing import Dict, Any, List, Optional


GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY must be defined to route LLM requests")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMRouter:

    MODEL_MAP = {
        "plan": os.getenv("GROQ_MODEL_PLAN", "mixtral-8x7b"),
        "code": os.getenv("GROQ_MODEL_CODE", "mixtral-8x7b"),
        "reason": os.getenv("GROQ_MODEL_REASONING", "mixtral-8x7b"),
        "test": os.getenv("GROQ_MODEL_TEST", "mixtral-8x7b"),
    }

    def __init__(self, max_retries: int = 3, timeout: float = 60.0):
        self.max_retries = max_retries
        self.timeout = timeout

    async def call(
        self,
        mode: str,
        system: str,
        user: str,
        temperature: float = 0.15,
    ) -> str:
        """
        Routes the call to the Groq model assigned for that mode.
        """

        if mode not in self.MODEL_MAP:
            raise ValueError(f"Unknown LLM mode: {mode}")

        model = self.MODEL_MAP[mode]

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        last_error = None

        for _ in range(self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(GROQ_URL, json=payload, headers=headers)
                    resp.raise_for_status()
                    content = resp.json()
                    return content["choices"][0]["message"]["content"]
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.75)

        raise RuntimeError(f"Groq LLM request failed after retries: {last_error}")

    async def structured(
        self,
        mode: str,
        system: str,
        user: str,
        fields: List[str],
    ) -> Dict[str, Any]:
        """
        Requests structured JSON.

        The LLM must reply JSON only with given fields.
        """

        field_str = ", ".join(fields)

        system_prompt = (
            f"{system}\n\n"
            f"Return only JSON with fields: {field_str}.\n"
            "NO commentary. NO code blocks. NO markdown."
        )

        raw = await self.call(mode, system_prompt, user)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            cleaned = raw.strip().removeprefix("```json").removesuffix("```").strip()
            parsed = json.loads(cleaned)

        return parsed
