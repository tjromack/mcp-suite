"""Compat shim — real code is in `core/embeddings.py`. See `clinical_trial_mcp/__init__.py`."""

from core.embeddings import *  # noqa: F401,F403
from core.embeddings import (  # noqa: F401 — re-export for explicit access
    InputType,
    embed_text,
    embed_texts,
    to_vector_literal,
)
