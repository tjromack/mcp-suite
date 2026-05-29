"""Compat shim — real code is in `core/cache.py`. See `clinical_trial_mcp/__init__.py`."""

from core.cache import *  # noqa: F401,F403
from core.cache import TTLCache  # noqa: F401 — re-export for explicit attribute access
