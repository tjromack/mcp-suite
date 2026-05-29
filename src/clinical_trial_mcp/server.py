"""Compat re-export — preserves `python -m clinical_trial_mcp.server` for
existing `claude_desktop_config.json` entries through Phase A.

The real server lives in `core/server.py`; this module re-exports its
public entry points so prior commands keep working unchanged.
"""

from __future__ import annotations

from core.server import main, mcp

__all__ = ["main", "mcp"]


if __name__ == "__main__":
    main()
