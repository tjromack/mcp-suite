"""Compat shim — real code is in `core/config.py`. See `clinical_trial_mcp/__init__.py`."""

from core.config import *  # noqa: F401,F403
from core.config import Settings, settings  # noqa: F401 — re-export for explicit access
