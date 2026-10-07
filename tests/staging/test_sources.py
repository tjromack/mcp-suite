"""Per-source mapping: quarter/snapshot handling, member names, row shapes.

The row builders are where a column can quietly land in the wrong slot — the
tuples are positional and FAERS DEMO has 36 of them. Each builder is checked
against the real header from the upstream file, in upstream order, so a
column inserted later cannot shift the rest unnoticed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from staging import aact, faers
from staging.loader import RowError

# The real header lines, copied from the 2025Q1 archive and the 2026-09-01
# export. Building rows from these rather than from hand-picked dicts means a
# typo in a column name shows up as a None in the wrong place.
DEMO_HEADER = (
    "primaryid$caseid$caseversion$i_f_code$event_dt$mfr_dt$init_fda_dt$fda_dt$rept_cod$"
    "auth_num$mfr_num$mfr_sndr$lit_ref$age$age_cod$age_grp$sex$e_sub$wt$wt_cod$rept_dt$"
    "to_mfr$occp_cod$reporter_country$occr_country"
).split("$")
DEMO_ROW = (
    "100294532$10029453$2$F$$20250319$20140322$20250326$EXP$$PHHY2014IT031524$NOVARTIS$"
    "Bianchi G V. TUMORI. 2013;99(6):269-72$53$YR$$F$Y$$$20250326$$HP$IT$IT"
).split("$")


def _values(header: list[str], row: list[str]) -> dict[str, str | None]:
    return {k: (v.strip() or None) for k, v in zip(header, row)}


# --- FAERS -----------------------------------------------------------------


def test_quarter_parsing_and_spellings() -> None:
    quarter = faers.parse_quarter("2025Q1")
    assert quarter.label == "2025Q1"
    assert quarter.file_suffix == "25Q1"
    assert quarter.url.endswith("faers_ascii_2025q1.zip")


@pytest.mark.parametrize("bad", ["2025", "25Q1", "2025Q5", "Q1 2025", ""])
def test_bad_quarters_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="not a FAERS quarter"):
        faers.parse_quarter(bad)


def test_member_names_cover_the_archive_layout() -> None:
    specs = {s.table: s for s in faers.table_specs(faers.parse_quarter("2025Q1"))}
    assert "ASCII/DEMO25Q1.txt" in specs["faers_demo"].members
    assert "Deleted/DELETE25Q1.txt" in specs["faers_deleted_case"].members


def test_every_faers_spec_builds_a_tuple_matching_its_columns() -> None:
    """Positional tuples and 36-column tables: this is the arity guard."""
    headers = {
        "faers_demo": (DEMO_HEADER, DEMO_ROW),
        "faers_drug": (
            "primaryid$caseid$drug_seq$role_cod$drugname$prod_ai$val_vbm$route$dose_vbm$"
            "cum_dose_chr$cum_dose_unit$dechal$rechal$lot_num$exp_dt$nda_num$dose_amt$"
            "dose_unit$dose_form$dose_freq".split("$"),
            "100294532$10029453$1$PS$LETROZOLE$LETROZOLE$1$Unknown$UNK$$$U$$$$20726$$$$".split("$"),
        ),
        "faers_reac": (
            "primaryid$caseid$pt$drug_rec_act".split("$"),
            "100294532$10029453$Asthenia$".split("$"),
        ),
        "faers_outc": (
            "primaryid$caseid$outc_cod".split("$"),
            "100294532$10029453$OT".split("$"),
        ),
        "faers_rpsr": (
            "primaryid$caseid$rpsr_cod".split("$"),
            "247995591$24799559$CSM".split("$"),
        ),
        "faers_ther": (
            "primaryid$caseid$dsg_drug_seq$start_dt$end_dt$dur$dur_cod".split("$"),
            "100294532$10029453$2$200906$$$".split("$"),
        ),
        "faers_indi": (
            "primaryid$caseid$indi_drug_seq$indi_pt".split("$"),
            "100294532$10029453$1$Breast cancer metastatic".split("$"),
        ),
        "faers_deleted_case": (["caseid"], ["10243933"]),
    }
    for spec in faers.table_specs(faers.parse_quarter("2025Q1")):
        header, row = headers[spec.table]
        built = spec.build(_values(header, row), "2025Q1")
        assert len(built) == len(spec.columns), f"{spec.table} arity"
        assert built[0] == "2025Q1", f"{spec.table} partition column"


def test_demo_row_lands_values_in_the_right_columns() -> None:
    columns = {s.table: s.columns for s in faers.table_specs(faers.parse_quarter("2025Q1"))}
    built = faers._demo_row(_values(DEMO_HEADER, DEMO_ROW), "2025Q1")
    row = dict(zip(columns["faers_demo"], built))
    assert row["primaryid"] == 100294532
    assert row["caseid"] == 10029453
    assert row["caseversion"] == 2
    assert row["i_f_code"] == "F"
    assert row["mfr_sndr"] == "NOVARTIS"
    assert row["age"] == "53"
    assert row["age_cod"] == "YR"
    assert row["sex"] == "F"
    assert row["occr_country"] == "IT"
    # An absent event date stays absent — no precision, no invented DATE.
    assert (row["event_dt_raw"], row["event_dt_prec"], row["event_dt"]) == (None, None, None)
    assert row["fda_dt"] == dt.date(2025, 3, 26)
    assert row["fda_dt_prec"] == "day"


def test_a_month_precision_therapy_date_keeps_its_raw_value() -> None:
    header = "primaryid$caseid$dsg_drug_seq$start_dt$end_dt$dur$dur_cod".split("$")
    row = "100294532$10029453$2$200906$$$".split("$")
    built = faers._ther_row(_values(header, row), "2025Q1")
    specs = faers.table_specs(faers.parse_quarter("2025Q1"))
    columns = next(s.columns for s in specs if s.table == "faers_ther")
    values = dict(zip(columns, built))
    assert values["start_dt_raw"] == "200906"
    assert values["start_dt_prec"] == "month"
    assert values["start_dt"] is None


def test_an_unusable_primaryid_rejects_the_row() -> None:
    with pytest.raises(RowError, match="primaryid"):
        faers._reac_row({"primaryid": "n/a", "caseid": "1", "pt": "Rash"}, "2025Q1")


def test_an_unusable_sequence_number_does_not_reject_the_row() -> None:
    """A null drug_seq is a gap in one column, not a reason to lose the drug."""
    built = faers._drug_row(
        {"primaryid": "1", "caseid": "2", "drug_seq": "", "drugname": "LETROZOLE"}, "2025Q1"
    )
    assert built[3] is None
    assert built[5] == "LETROZOLE"


# --- AACT ------------------------------------------------------------------


def test_snapshot_must_be_the_first_of_a_month() -> None:
    assert aact.parse_snapshot("2026-09-01") == dt.date(2026, 9, 1)
    with pytest.raises(ValueError, match="first of a month"):
        aact.parse_snapshot("2026-09-15")
    with pytest.raises(ValueError, match="not a snapshot date"):
        aact.parse_snapshot("September 2026")


def test_archive_url_uses_the_compact_stamp() -> None:
    assert aact.archive_url(dt.date(2026, 9, 1)).endswith("20260901_pipe-delimited-export.zip")


def test_every_aact_spec_builds_a_tuple_matching_its_columns() -> None:
    headers = {
        "aact_sponsors": (
            "id|nct_id|agency_class|lead_or_collaborator|name".split("|"),
            "379712590|NCT01131910|OTHER|collaborator|Karolinska Institutet".split("|"),
        ),
        "aact_conditions": (
            "id|nct_id|name|downcase_name".split("|"),
            "407173672|NCT06548750|Pes Planus|pes planus".split("|"),
        ),
        "aact_interventions": (
            "id|nct_id|intervention_type|name|description".split("|"),
            "389903800|NCT03786874|OTHER|Identify work interruptions|estimate baseline".split("|"),
        ),
        "aact_facilities": (
            "id|nct_id|status|name|city|state|zip|country|latitude|longitude".split("|"),
            "1400461980|NCT06256731||MS Integrated Center|Phoenix|Arizona|85018|"
            "United States|33.448380|-112.074040".split("|"),
        ),
    }
    for spec in aact.table_specs():
        if spec.table == "aact_studies":
            continue  # covered separately, it has 71 upstream columns
        header, row = headers[spec.table]
        built = spec.build(_values(header, row), "2026-09-01")
        assert len(built) == len(spec.columns), f"{spec.table} arity"
        assert built[0] == dt.date(2026, 9, 1)


def test_studies_row_keeps_the_declared_subset_in_order() -> None:
    values = {
        "nct_id": "NCT04361240",
        "study_type": "OBSERVATIONAL",
        "overall_status": "COMPLETED",
        "phase": None,
        "enrollment": "1200",
        "enrollment_type": "ACTUAL",
        "source": "University of Pennsylvania",
        "brief_title": "Cardiotoxicity in Breast Cancer Patients",
        "start_date": "2020-08-01",
        "completion_date": "2025-05-29",
        "has_dmc": "t",
        "is_fda_regulated_drug": "f",
        "updated_at": "2026-09-01 04:12:33.918241",
    }
    built = aact._studies_row(values, "2026-09-01")
    row = dict(zip(aact.STUDIES_COLUMNS, built))
    assert row["nct_id"] == "NCT04361240"
    assert row["enrollment"] == 1200
    assert row["start_date"] == dt.date(2020, 8, 1)
    assert row["completion_date"] == dt.date(2025, 5, 29)
    assert row["has_dmc"] is True
    assert row["is_fda_regulated_drug"] is False
    assert row["aact_updated_at"] is not None
    assert row["phase"] is None


def test_a_study_without_an_nct_id_is_rejected() -> None:
    with pytest.raises(RowError, match="nct_id"):
        aact._studies_row({"nct_id": None, "study_type": "INTERVENTIONAL"}, "2026-09-01")


def test_facilities_coordinates_parse_as_floats() -> None:
    built = aact._facilities_row(
        {"id": "1", "nct_id": "NCT1", "latitude": "33.448380", "longitude": "-112.074040"},
        "2026-09-01",
    )
    assert built[-2] == pytest.approx(33.44838)
    assert built[-1] == pytest.approx(-112.07404)
