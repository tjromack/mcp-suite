"""Compat shim — real code lives in `core/store/connection.py`.

See `clinical_trial_mcp/__init__.py` for the Phase-A namespace policy.
"""

from core.store.connection import *  # noqa: F401,F403
from core.store.connection import (  # noqa: F401 — re-export for explicit access
    close_pool,
    get_pool,
    init_pool,
)
