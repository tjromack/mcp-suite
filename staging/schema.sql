-- biomed-evidence-mcp — relational staging layer (add-on A4).
--
-- This sits BESIDE `documents`, not instead of it. The canonical document
-- table keeps each source's detail in a JSONB payload, which is the right
-- shape for retrieval (one row per thing you might cite) and the wrong shape
-- for analysis (no joins, no grain, no per-quarter integrity). The staging
-- schema is the analysis half: the upstream relational files, loaded as
-- tables, with their keys, their partitions and their defects intact.
--
-- Three rules hold across every table here:
--   1. Partition column first. FAERS tables carry `quarter`, AACT tables
--      carry `snapshot`. A load is always scoped to one partition, so a
--      re-load replaces that partition and nothing else.
--   2. Nothing is dropped. A line that will not parse lands in
--      `load_reject` with its line number and the raw text.
--   3. Nothing is guessed. FAERS partial dates keep raw + precision, and a
--      real DATE only where the upstream actually gave a day.
--
-- Safe to re-run: every statement is idempotent.

CREATE SCHEMA IF NOT EXISTS staging;

-- ---------------------------------------------------------------------------
-- Load bookkeeping
-- ---------------------------------------------------------------------------

-- One row per (source, partition, table) load. `rows_read` must always equal
-- `rows_loaded + rows_rejected`; the conservation gate checks exactly that,
-- which is what makes "nothing is dropped" a test rather than a claim.
CREATE TABLE IF NOT EXISTS staging.load_run (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT        NOT NULL,          -- 'faers' | 'aact'
    partition     TEXT        NOT NULL,          -- '2025Q1' | '2026-09-01'
    table_name    TEXT        NOT NULL,          -- target table, unqualified
    rows_read     BIGINT      NOT NULL DEFAULT 0,
    rows_loaded   BIGINT      NOT NULL DEFAULT 0,
    rows_rejected BIGINT      NOT NULL DEFAULT 0,
    bytes_fetched BIGINT      NOT NULL DEFAULT 0,  -- HTTP bytes, for the range-read saving
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    status        TEXT        NOT NULL DEFAULT 'running',  -- running | success | error
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS load_run_source_partition_idx
    ON staging.load_run (source, partition, table_name);

-- Every line that did not parse. Kept verbatim and truncated to 2k chars:
-- enough to see what the upstream actually sent, bounded enough that one
-- pathological row cannot bloat the table.
CREATE TABLE IF NOT EXISTS staging.load_reject (
    id         BIGSERIAL PRIMARY KEY,
    load_id    BIGINT      NOT NULL REFERENCES staging.load_run (id) ON DELETE CASCADE,
    source     TEXT        NOT NULL,
    partition  TEXT        NOT NULL,
    table_name TEXT        NOT NULL,
    line_no    BIGINT      NOT NULL,
    reason     TEXT        NOT NULL,
    raw_line   TEXT        NOT NULL,
    -- The row's identifying value, when it survived the parse failure. A row
    -- is usually rejected because a free-text field contains a delimiter, so
    -- the leading key column is normally intact. The integrity gate joins on
    -- this to tell "orphaned because its parent was rejected" apart from
    -- "orphaned because the upstream is inconsistent".
    key_hint   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE staging.load_reject ADD COLUMN IF NOT EXISTS key_hint TEXT;

CREATE INDEX IF NOT EXISTS load_reject_load_idx ON staging.load_reject (load_id);
CREATE INDEX IF NOT EXISTS load_reject_scope_idx
    ON staging.load_reject (source, partition, table_name);
CREATE INDEX IF NOT EXISTS load_reject_key_idx
    ON staging.load_reject (table_name, partition, key_hint);

-- ---------------------------------------------------------------------------
-- FAERS — seven quarterly tables, '$'-delimited ASCII extracts
--
-- Declared grain (proved by `python -m staging gates`, not assumed here):
--   demo  one row per (quarter, primaryid)
--   drug  one row per (quarter, primaryid, drug_seq)
--   ther  one row per (quarter, primaryid, dsg_drug_seq)
--   indi  one row per (quarter, primaryid, indi_drug_seq)
--   reac  one row per (quarter, primaryid, pt)
--   outc  one row per (quarter, primaryid, outc_cod)
--   rpsr  one row per (quarter, primaryid, rpsr_cod)
--
-- Those are declared as plain indexes rather than unique constraints on
-- purpose: a 10M-row load must not abort because one quarter violates a key.
-- The gate reports the violation with example rows instead, which is more
-- useful than a failed COPY and keeps the evidence in the database.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS staging.faers_demo (
    quarter           TEXT   NOT NULL,
    primaryid         BIGINT NOT NULL,
    caseid            BIGINT,
    caseversion       INTEGER,
    i_f_code          TEXT,            -- I = initial, F = follow-up
    -- Partial dates: raw + precision + exact. See staging/dates.py.
    event_dt_raw      TEXT,
    event_dt_prec     TEXT,
    event_dt          DATE,
    mfr_dt_raw        TEXT,
    mfr_dt_prec       TEXT,
    mfr_dt            DATE,
    init_fda_dt_raw   TEXT,
    init_fda_dt_prec  TEXT,
    init_fda_dt       DATE,
    fda_dt_raw        TEXT,
    fda_dt_prec       TEXT,
    fda_dt            DATE,
    rept_dt_raw       TEXT,
    rept_dt_prec      TEXT,
    rept_dt           DATE,
    rept_cod          TEXT,
    auth_num          TEXT,
    mfr_num           TEXT,
    mfr_sndr          TEXT,
    lit_ref           TEXT,
    age               TEXT,            -- kept as text: the unit lives in age_cod
    age_cod           TEXT,
    age_grp           TEXT,
    sex               TEXT,
    e_sub             TEXT,
    wt                TEXT,
    wt_cod            TEXT,
    to_mfr            TEXT,
    occp_cod          TEXT,
    reporter_country  TEXT,
    occr_country      TEXT
);

CREATE INDEX IF NOT EXISTS faers_demo_grain_idx
    ON staging.faers_demo (quarter, primaryid);
CREATE INDEX IF NOT EXISTS faers_demo_caseid_idx
    ON staging.faers_demo (caseid);

CREATE TABLE IF NOT EXISTS staging.faers_drug (
    quarter       TEXT   NOT NULL,
    primaryid     BIGINT NOT NULL,
    caseid        BIGINT,
    drug_seq      INTEGER,
    role_cod      TEXT,              -- PS primary suspect, SS secondary, C concomitant, I interacting
    drugname      TEXT,
    prod_ai       TEXT,              -- active ingredient
    val_vbm       TEXT,
    route         TEXT,
    dose_vbm      TEXT,
    cum_dose_chr  TEXT,
    cum_dose_unit TEXT,
    dechal        TEXT,
    rechal        TEXT,
    lot_num       TEXT,
    exp_dt_raw    TEXT,
    exp_dt_prec   TEXT,
    exp_dt        DATE,
    nda_num       TEXT,
    dose_amt      TEXT,
    dose_unit     TEXT,
    dose_form     TEXT,
    dose_freq     TEXT
);

CREATE INDEX IF NOT EXISTS faers_drug_grain_idx
    ON staging.faers_drug (quarter, primaryid, drug_seq);
CREATE INDEX IF NOT EXISTS faers_drug_ai_idx
    ON staging.faers_drug (lower(prod_ai));

CREATE TABLE IF NOT EXISTS staging.faers_reac (
    quarter      TEXT   NOT NULL,
    primaryid    BIGINT NOT NULL,
    caseid       BIGINT,
    pt           TEXT,               -- MedDRA preferred term
    drug_rec_act TEXT
);

CREATE INDEX IF NOT EXISTS faers_reac_grain_idx
    ON staging.faers_reac (quarter, primaryid, pt);
CREATE INDEX IF NOT EXISTS faers_reac_pt_idx ON staging.faers_reac (lower(pt));

CREATE TABLE IF NOT EXISTS staging.faers_outc (
    quarter   TEXT   NOT NULL,
    primaryid BIGINT NOT NULL,
    caseid    BIGINT,
    outc_cod  TEXT                   -- DE death, LT life-threatening, HO hospitalisation, …
);

CREATE INDEX IF NOT EXISTS faers_outc_grain_idx
    ON staging.faers_outc (quarter, primaryid, outc_cod);

CREATE TABLE IF NOT EXISTS staging.faers_rpsr (
    quarter   TEXT   NOT NULL,
    primaryid BIGINT NOT NULL,
    caseid    BIGINT,
    rpsr_cod  TEXT                   -- report source: FGN, SDY, LIT, CSM, HP, UF, CR, DT, OTH
);

CREATE INDEX IF NOT EXISTS faers_rpsr_grain_idx
    ON staging.faers_rpsr (quarter, primaryid, rpsr_cod);

CREATE TABLE IF NOT EXISTS staging.faers_ther (
    quarter      TEXT   NOT NULL,
    primaryid    BIGINT NOT NULL,
    caseid       BIGINT,
    dsg_drug_seq INTEGER,            -- joins faers_drug.drug_seq
    start_dt_raw TEXT,
    start_dt_prec TEXT,
    start_dt     DATE,
    end_dt_raw   TEXT,
    end_dt_prec  TEXT,
    end_dt       DATE,
    dur          TEXT,
    dur_cod      TEXT
);

CREATE INDEX IF NOT EXISTS faers_ther_grain_idx
    ON staging.faers_ther (quarter, primaryid, dsg_drug_seq);

CREATE TABLE IF NOT EXISTS staging.faers_indi (
    quarter       TEXT   NOT NULL,
    primaryid     BIGINT NOT NULL,
    caseid        BIGINT,
    indi_drug_seq INTEGER,           -- joins faers_drug.drug_seq
    indi_pt       TEXT               -- MedDRA term for the indication
);

CREATE INDEX IF NOT EXISTS faers_indi_grain_idx
    ON staging.faers_indi (quarter, primaryid, indi_drug_seq);

-- The `Deleted/DELETE*.txt` member of each quarterly archive: case ids the
-- FDA has withdrawn. Headerless — one case id per line. Easy to miss, and
-- missing it means reporting counts that include cases the FDA has retracted.
CREATE TABLE IF NOT EXISTS staging.faers_deleted_case (
    quarter TEXT   NOT NULL,
    caseid  BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS faers_deleted_case_idx
    ON staging.faers_deleted_case (caseid);

-- Case-version resolution, materialised by `python -m staging dedup`.
--
-- A FAERS case is reported once and then amended; each amendment is a new row
-- with a higher `caseversion`, and the same case can recur across quarters.
-- Counting rows therefore counts amendments, not events — the single most
-- common way FAERS analyses are wrong. The resolution rule is the FDA's own:
-- keep the latest `fda_dt`, and break ties on the highest `primaryid`.
--
-- One row per caseid. `n_versions` and `superseded` make the dedup auditable:
-- kept rows + superseded rows must equal the demo row count exactly.
CREATE TABLE IF NOT EXISTS staging.faers_case_current (
    caseid       BIGINT PRIMARY KEY,
    primaryid    BIGINT  NOT NULL,     -- the surviving version
    quarter      TEXT    NOT NULL,     -- which quarter it survived from
    caseversion  INTEGER,
    fda_dt_raw   TEXT,
    fda_dt       DATE,
    n_versions   INTEGER NOT NULL,     -- demo rows for this caseid across loaded quarters
    superseded   INTEGER NOT NULL,     -- n_versions - 1
    is_deleted   BOOLEAN NOT NULL DEFAULT false,  -- caseid appears in any DELETE file
    resolved_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS faers_case_current_primaryid_idx
    ON staging.faers_case_current (primaryid);

-- ---------------------------------------------------------------------------
-- AACT — five tables from the pinned monthly pipe-delimited archive
--
-- Declared grain:
--   studies        one row per (snapshot, nct_id)
--   sponsors       one row per (snapshot, id)
--   conditions     one row per (snapshot, id)
--   interventions  one row per (snapshot, id)
--   facilities     one row per (snapshot, id)
--
-- `studies` keeps a named 31-column subset of the upstream 71. The dropped
-- columns are result-reporting and IPD-sharing metadata that nothing in this
-- repo reads; the subset is listed in docs/STAGING.md so the omission is a
-- documented scope decision rather than a silent one.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS staging.aact_studies (
    snapshot                        DATE NOT NULL,
    nct_id                          TEXT NOT NULL,
    study_type                      TEXT,
    overall_status                  TEXT,
    last_known_status               TEXT,
    phase                           TEXT,
    enrollment                      INTEGER,
    enrollment_type                 TEXT,
    source                          TEXT,
    source_class                    TEXT,
    acronym                         TEXT,
    brief_title                     TEXT,
    official_title                  TEXT,
    why_stopped                     TEXT,
    number_of_arms                  INTEGER,
    number_of_groups                INTEGER,
    has_dmc                         BOOLEAN,
    is_fda_regulated_drug           BOOLEAN,
    is_fda_regulated_device         BOOLEAN,
    start_date                      DATE,
    start_date_type                 TEXT,
    completion_date                 DATE,
    completion_date_type            TEXT,
    primary_completion_date         DATE,
    primary_completion_date_type    TEXT,
    study_first_submitted_date      DATE,
    study_first_posted_date         DATE,
    last_update_submitted_date      DATE,
    last_update_posted_date         DATE,
    results_first_posted_date       DATE,
    aact_updated_at                 TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS aact_studies_grain_idx
    ON staging.aact_studies (snapshot, nct_id);
CREATE INDEX IF NOT EXISTS aact_studies_status_idx
    ON staging.aact_studies (overall_status);

CREATE TABLE IF NOT EXISTS staging.aact_sponsors (
    snapshot           DATE   NOT NULL,
    id                 BIGINT NOT NULL,
    nct_id             TEXT,
    agency_class       TEXT,
    lead_or_collaborator TEXT,
    name               TEXT
);

CREATE INDEX IF NOT EXISTS aact_sponsors_grain_idx ON staging.aact_sponsors (snapshot, id);
CREATE INDEX IF NOT EXISTS aact_sponsors_nct_idx ON staging.aact_sponsors (nct_id);

CREATE TABLE IF NOT EXISTS staging.aact_conditions (
    snapshot      DATE   NOT NULL,
    id            BIGINT NOT NULL,
    nct_id        TEXT,
    name          TEXT,
    downcase_name TEXT
);

CREATE INDEX IF NOT EXISTS aact_conditions_grain_idx ON staging.aact_conditions (snapshot, id);
CREATE INDEX IF NOT EXISTS aact_conditions_nct_idx ON staging.aact_conditions (nct_id);
CREATE INDEX IF NOT EXISTS aact_conditions_name_idx ON staging.aact_conditions (downcase_name);

CREATE TABLE IF NOT EXISTS staging.aact_interventions (
    snapshot          DATE   NOT NULL,
    id                BIGINT NOT NULL,
    nct_id            TEXT,
    intervention_type TEXT,
    name              TEXT,
    description       TEXT
);

CREATE INDEX IF NOT EXISTS aact_interventions_grain_idx
    ON staging.aact_interventions (snapshot, id);
CREATE INDEX IF NOT EXISTS aact_interventions_nct_idx
    ON staging.aact_interventions (nct_id);

CREATE TABLE IF NOT EXISTS staging.aact_facilities (
    snapshot  DATE   NOT NULL,
    id        BIGINT NOT NULL,
    nct_id    TEXT,
    status    TEXT,
    name      TEXT,
    city      TEXT,
    state     TEXT,
    zip       TEXT,
    country   TEXT,
    latitude  DOUBLE PRECISION,
    longitude DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS aact_facilities_grain_idx ON staging.aact_facilities (snapshot, id);
CREATE INDEX IF NOT EXISTS aact_facilities_nct_idx ON staging.aact_facilities (nct_id);
CREATE INDEX IF NOT EXISTS aact_facilities_country_idx ON staging.aact_facilities (country);
