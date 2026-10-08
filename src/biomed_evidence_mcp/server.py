"""Re-export of the server entry points under the distribution's own name.

The real server lives in `core/server.py`; this module re-exports its public
entry points so `python -m biomed_evidence_mcp.server` and `python -m
core.server` are the same thing.
"""

from __future__ import annotations

from core.server import main, mcp

__all__ = ["main", "mcp"]


if __name__ == "__main__":
    main()
