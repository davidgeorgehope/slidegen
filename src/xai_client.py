"""xAI (Grok) client: OpenAI SDK pointed at https://api.x.ai/v1 with XAI_API_KEY."""
from __future__ import annotations

import os

XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_TEXT_MODEL = "grok-4.7"
DEFAULT_IMAGE_MODEL = "grok-imagine-image-2.0"


def xai_client():
    from openai import OpenAI

    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise RuntimeError("XAI_API_KEY is required")
    return OpenAI(api_key=key, base_url=XAI_BASE_URL)
