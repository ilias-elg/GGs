"""Gemini provider — Google AI Studio's OpenAI-compatible endpoint (what Jarvis used)."""
import logging

from openai import AsyncOpenAI

import config
from .openai_provider import OpenAIProvider

logger = logging.getLogger("discord")

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class GeminiProvider(OpenAIProvider):
    def __init__(self) -> None:
        self.client = AsyncOpenAI(
            base_url=_GEMINI_BASE_URL,
            api_key=config.GOOGLE_API_KEY,
            max_retries=0,
        )
        self.model = config.get_model()
        logger.info(f"GeminiProvider initialized with model: {self.model}")
