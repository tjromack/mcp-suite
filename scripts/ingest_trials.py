"""Thin shim — delegates to the generic ingest runner with --source clinical.

The old CLI (with --query / --max flags clinical-specific to CT.gov search
terms) is retired. Connector configuration now lives in
``servers/clinical/server.toml`` under the ``[connector]`` table — edit
queries there, then re-run this shim or call the generic runner directly:

    python -m core.ingest --source clinical [--since YYYY-MM-DD]

This shim exists only so existing muscle-memory commands keep working through
Phase A; it will be removed in a future release.
"""

from __future__ import annotations

import sys

from core.ingest import main


def _shim_entry() -> None:
    # Strip any of the legacy clinical-specific flags so they don't error
    # out of argparse — they're no longer meaningful here.
    legacy = {"--query", "--max"}
    argv: list[str] = ["--source", "clinical"]
    it = iter(sys.argv[1:])
    for tok in it:
        if tok in legacy:
            next(it, None)  # consume the value
            continue
        if any(tok.startswith(f"{lf}=") for lf in legacy):
            continue
        argv.append(tok)
    main(argv)


if __name__ == "__main__":
    _shim_entry()
