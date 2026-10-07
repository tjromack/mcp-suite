"""AACT monthly pipe-delimited archive → `staging.aact_*`.

CTTI's AACT project republishes ClinicalTrials.gov as a relational database and
archives a permanent pipe-delimited copy on the first of each month. That
archive is the counterpart to this repo's `clinical` document corpus: the same
registry, but as joinable tables instead of one embedded row per trial.

Two things shape this loader.

**Only five of the 49 members are read.** The archive is 2.5 GB; `studies`,
`sponsors`, `facilities`, `conditions` and `interventions` are 330 MB of it.
`staging.remote_zip` fetches those byte ranges and leaves the rest on the
server, so a load transfers about an eighth of the archive.

**The export is unquoted.** A pipe inside an official title or an intervention
description produces a row with more fields than the header declares, and
there is no escaping to undo it. Those rows go to `staging.load_reject` with
their line numbers rather than being force-fitted or dropped.

Upstream: https://aact.ctti-clinicaltrials.org/download
"""

from __future__ import annotations

import datetime as _dt
import re

from staging.dates import parse_bool, parse_float, parse_int, parse_iso_date, parse_timestamp
from staging.loader import RowError, TableSpec

SOURCE = "aact"
DELIMITER = "|"

ARCHIVE_URL = (
    "https://aact.ctti-clinicaltrials.org/static/exported_files/monthly/"
    "{stamp}_pipe-delimited-export.zip"
)

_SNAPSHOT_RE = re.compile(r"^(20\d{2})-(\d{2})-(\d{2})$")


def parse_snapshot(text: str) -> _dt.date:
    """Validate a snapshot date. AACT archives are cut on the 1st of a month."""
    raw = text.strip()
    if not _SNAPSHOT_RE.match(raw):
        raise ValueError(f"{raw!r} is not a snapshot date (expected e.g. 2026-09-01)")
    snapshot = _dt.date.fromisoformat(raw)
    if snapshot.day != 1:
        raise ValueError(
            f"{raw} is not the first of a month; AACT's permanent monthly "
            "archives are cut on the 1st"
        )
    return snapshot


def archive_url(snapshot: _dt.date) -> str:
    return ARCHIVE_URL.format(stamp=snapshot.strftime("%Y%m%d"))


def _require(values: dict[str, str | None], column: str) -> str:
    value = values.get(column)
    if not value:
        raise RowError(f"{column} is empty")
    return value


def _require_id(values: dict[str, str | None], column: str) -> int:
    parsed = parse_int(values.get(column))
    if parsed is None:
        raise RowError(f"{column}={values.get(column)!r} is not an integer")
    return parsed


# --- row builders ----------------------------------------------------------


def _studies_row(v: dict[str, str | None], snapshot: str) -> tuple:
    return (
        _dt.date.fromisoformat(snapshot),
        _require(v, "nct_id"),
        v.get("study_type"),
        v.get("overall_status"),
        v.get("last_known_status"),
        v.get("phase"),
        parse_int(v.get("enrollment")),
        v.get("enrollment_type"),
        v.get("source"),
        v.get("source_class"),
        v.get("acronym"),
        v.get("brief_title"),
        v.get("official_title"),
        v.get("why_stopped"),
        parse_int(v.get("number_of_arms")),
        parse_int(v.get("number_of_groups")),
        parse_bool(v.get("has_dmc")),
        parse_bool(v.get("is_fda_regulated_drug")),
        parse_bool(v.get("is_fda_regulated_device")),
        parse_iso_date(v.get("start_date")),
        v.get("start_date_type"),
        parse_iso_date(v.get("completion_date")),
        v.get("completion_date_type"),
        parse_iso_date(v.get("primary_completion_date")),
        v.get("primary_completion_date_type"),
        parse_iso_date(v.get("study_first_submitted_date")),
        parse_iso_date(v.get("study_first_posted_date")),
        parse_iso_date(v.get("last_update_submitted_date")),
        parse_iso_date(v.get("last_update_posted_date")),
        parse_iso_date(v.get("results_first_posted_date")),
        parse_timestamp(v.get("updated_at")),
    )


def _sponsors_row(v: dict[str, str | None], snapshot: str) -> tuple:
    return (
        _dt.date.fromisoformat(snapshot),
        _require_id(v, "id"),
        v.get("nct_id"),
        v.get("agency_class"),
        v.get("lead_or_collaborator"),
        v.get("name"),
    )


def _conditions_row(v: dict[str, str | None], snapshot: str) -> tuple:
    return (
        _dt.date.fromisoformat(snapshot),
        _require_id(v, "id"),
        v.get("nct_id"),
        v.get("name"),
        v.get("downcase_name"),
    )


def _interventions_row(v: dict[str, str | None], snapshot: str) -> tuple:
    return (
        _dt.date.fromisoformat(snapshot),
        _require_id(v, "id"),
        v.get("nct_id"),
        v.get("intervention_type"),
        v.get("name"),
        v.get("description"),
    )


def _facilities_row(v: dict[str, str | None], snapshot: str) -> tuple:
    return (
        _dt.date.fromisoformat(snapshot),
        _require_id(v, "id"),
        v.get("nct_id"),
        v.get("status"),
        v.get("name"),
        v.get("city"),
        v.get("state"),
        v.get("zip"),
        v.get("country"),
        parse_float(v.get("latitude")),
        parse_float(v.get("longitude")),
    )


# The 31 of `studies.txt`'s 71 columns this layer keeps. The rest are
# result-reporting and IPD-sharing metadata that nothing here joins on;
# docs/STAGING.md lists what was left behind so the scope is on the record.
STUDIES_COLUMNS = (
    "snapshot",
    "nct_id",
    "study_type",
    "overall_status",
    "last_known_status",
    "phase",
    "enrollment",
    "enrollment_type",
    "source",
    "source_class",
    "acronym",
    "brief_title",
    "official_title",
    "why_stopped",
    "number_of_arms",
    "number_of_groups",
    "has_dmc",
    "is_fda_regulated_drug",
    "is_fda_regulated_device",
    "start_date",
    "start_date_type",
    "completion_date",
    "completion_date_type",
    "primary_completion_date",
    "primary_completion_date_type",
    "study_first_submitted_date",
    "study_first_posted_date",
    "last_update_submitted_date",
    "last_update_posted_date",
    "results_first_posted_date",
    "aact_updated_at",
)


def table_specs() -> list[TableSpec]:
    """The five members loaded from the monthly archive, parent first."""
    return [
        TableSpec(
            table="aact_studies",
            members=("studies.txt",),
            columns=STUDIES_COLUMNS,
            build=_studies_row,
            delimiter=DELIMITER,
            partition_column="snapshot",
        ),
        TableSpec(
            table="aact_sponsors",
            members=("sponsors.txt",),
            columns=("snapshot", "id", "nct_id", "agency_class", "lead_or_collaborator", "name"),
            build=_sponsors_row,
            delimiter=DELIMITER,
            partition_column="snapshot",
        ),
        TableSpec(
            table="aact_conditions",
            members=("conditions.txt",),
            columns=("snapshot", "id", "nct_id", "name", "downcase_name"),
            build=_conditions_row,
            delimiter=DELIMITER,
            partition_column="snapshot",
        ),
        TableSpec(
            table="aact_interventions",
            members=("interventions.txt",),
            columns=("snapshot", "id", "nct_id", "intervention_type", "name", "description"),
            build=_interventions_row,
            delimiter=DELIMITER,
            partition_column="snapshot",
        ),
        TableSpec(
            table="aact_facilities",
            members=("facilities.txt",),
            columns=(
                "snapshot",
                "id",
                "nct_id",
                "status",
                "name",
                "city",
                "state",
                "zip",
                "country",
                "latitude",
                "longitude",
            ),
            build=_facilities_row,
            delimiter=DELIMITER,
            partition_column="snapshot",
        ),
    ]


# Child tables, for the cross-table integrity gate.
CHILD_TABLES = (
    "aact_sponsors",
    "aact_conditions",
    "aact_interventions",
    "aact_facilities",
)
