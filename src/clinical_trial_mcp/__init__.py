"""Compat shim — preserves the `clinical_trial_mcp.*` import namespace through
Phase A so existing `claude_desktop_config.json` entries (`python -m
clinical_trial_mcp.server`) and any external code keep working unchanged.

The real code lives under `core/` and `servers/clinical/` now. This package
will be removed (or renamed) once a publishable `pip install mcp-suite-clinical`
story exists — see the plan's "Notes on what NOT to do".
"""

# Importing core triggers its __init__ — which registers truststore for the
# whole process. Doing it here too means `import clinical_trial_mcp` alone
# (without any submodule) also wires SSL trust correctly.
import core  # noqa: F401
