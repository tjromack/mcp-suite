"""get_trial_details tool — live structured lookup from ClinicalTrials.gov.

This is the only tool that hits the live API. Goes through ctgov.py so the
Akamai/proxy handling is shared with the ingest script.
"""

from __future__ import annotations

import logging
import re

from mcp.types import TextContent

from clinical_trial_mcp.ctgov import fetch_study

logger = logging.getLogger("clinical_trial_mcp.get_trial_details")

_NCT_RE = re.compile(r"^NCT\d{8}$")


def _safe(obj: dict | None, *path: str):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _format_study(study: dict) -> str:
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    status_mod = proto.get("statusModule") or {}
    design_mod = proto.get("designModule") or {}
    cond_mod = proto.get("conditionsModule") or {}
    arms_mod = proto.get("armsInterventionsModule") or {}
    desc_mod = proto.get("descriptionModule") or {}

    phases = design_mod.get("phases") or []
    conditions = cond_mod.get("conditions") or []
    interventions = [
        i.get("name")
        for i in (arms_mod.get("interventions") or [])
        if isinstance(i, dict) and i.get("name")
    ]
    summary = (desc_mod.get("briefSummary") or "").strip() or "—"

    return "\n".join(
        [
            f"NCT ID:        {ident.get('nctId', '—')}",
            f"Title:         {ident.get('briefTitle', '—')}",
            f"Official:      {ident.get('officialTitle', '—')}",
            f"Status:        {status_mod.get('overallStatus', '—')}",
            f"Phase:         {phases[0] if phases else '—'}",
            f"Start date:    {_safe(status_mod, 'startDateStruct', 'date') or '—'}",
            f"Conditions:    {', '.join(conditions) or '—'}",
            f"Interventions: {', '.join(interventions) or '—'}",
            "",
            "Brief summary:",
            summary,
        ]
    )


async def get_trial_details(nct_id: str) -> list[TextContent]:
    """Fetch full structured details for one trial by NCT ID.

    Errors (bad ID, not found, network) are returned as TextContent, never
    raised (MCP tool contract).
    """
    try:
        nct_id = (nct_id or "").strip().upper()
        if not _NCT_RE.match(nct_id):
            return [
                TextContent(
                    type="text",
                    text=f"Error: '{nct_id}' is not a valid NCT ID (expected NCT + 8 digits).",
                )
            ]

        study = await fetch_study(nct_id)
        if study is None:
            return [TextContent(type="text", text=f"No trial found with NCT ID {nct_id}.")]

        return [TextContent(type="text", text=_format_study(study))]
    except Exception as exc:  # noqa: BLE001 — tool must not raise out
        logger.exception("get_trial_details failed")
        return [
            TextContent(
                type="text",
                text=f"Error running get_trial_details: {type(exc).__name__}: {exc}",
            )
        ]
