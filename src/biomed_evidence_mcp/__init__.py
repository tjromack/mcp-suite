"""Thin alias package — `python -m biomed_evidence_mcp[.server]` runs the server.

The real code lives under `core/` and `servers/<source_id>/`. This package
exists only to give the distribution an importable module matching its name,
so `python -m biomed_evidence_mcp.server` works alongside `python -m
core.server`.

It began as a compatibility shim for the pre-refactor `clinical_trial_mcp.*`
namespace. That namespace was retired in the rename to `biomed-evidence-mcp`,
so the shim no longer shims anything: nothing outside this repo imports it, and
it can be deleted whenever `core.server` is the only documented entry point.
"""

# Importing core triggers its __init__ — which registers truststore for the
# whole process. Doing it here too means `import biomed_evidence_mcp` alone
# (without any submodule) also wires SSL trust correctly.
import core  # noqa: F401
