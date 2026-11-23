-- One SQLite file per stage. Sections are selected by the `-- @schema <name>` markers.

-- @schema staging
-- IDENTIFIABLE. Lives in the PHI location only.
CREATE TABLE IF NOT EXISTS documents (
    doc_pk INTEGER PRIMARY KEY,
    empi TEXT NOT NULL,
    source_table TEXT NOT NULL,
    report_number TEXT,
    report_datetime_raw TEXT,
    report_datetime TEXT,
    report_description TEXT,
    report_type TEXT,
    report_status TEXT,
    site TEXT,
    relevance_class TEXT,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    n_chars INTEGER,
    n_sources INTEGER
);
CREATE INDEX IF NOT EXISTS ix_documents_empi ON documents(empi, report_datetime);
CREATE TABLE IF NOT EXISTS document_sources (
    doc_pk INTEGER NOT NULL REFERENCES documents(doc_pk),
    source_table TEXT NOT NULL,
    report_number TEXT,
    report_status TEXT,
    report_datetime_raw TEXT,
    report_description TEXT,
    report_type TEXT
);
CREATE TABLE IF NOT EXISTS demographics (
    empi TEXT PRIMARY KEY,
    dob TEXT,
    dob_resolved_by TEXT,
    dob_age_mismatch INTEGER,
    raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS structured_rows (
    table_name TEXT NOT NULL,
    empi TEXT NOT NULL,
    row_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_manifest (key TEXT PRIMARY KEY, value TEXT);

-- @schema pilot_selection
CREATE TABLE IF NOT EXISTS pilot_candidates (
    empi TEXT PRIMARY KEY,
    n_documents INTEGER,
    tokens_before INTEGER,
    tokens_after INTEGER,
    n_neuro_mri INTEGER,
    counts_by_class TEXT,
    counts_by_table TEXT,
    keyword_flags TEXT
);

-- @schema deid
-- DE-IDENTIFIED. No EMPI, MRN, names, source report numbers or real dates.
CREATE TABLE IF NOT EXISTS patients (
    study_id TEXT PRIMARY KEY,
    sex_at_birth TEXT,
    gender_legal_sex TEXT,
    race1 TEXT,
    race_group TEXT,
    ethnic_group TEXT,
    language_group TEXT,
    vital_status TEXT,
    date_of_death TEXT,
    age_at_export TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES patients(study_id),
    source_table TEXT NOT NULL,
    report_date TEXT,
    report_description TEXT,
    report_type TEXT,
    report_status TEXT,
    site TEXT,
    relevance_class TEXT,
    n_sources INTEGER,
    header_pattern TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deid_documents_study ON documents(study_id, report_date);
CREATE TABLE IF NOT EXISTS structured_rows (
    table_name TEXT NOT NULL,
    study_id TEXT NOT NULL,
    row_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS span_counts (
    doc_id TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    n INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
    doc_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS manifest (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS signoff (
    doc_id TEXT NOT NULL,
    reviewer TEXT,
    reviewed INTEGER,
    misses TEXT,
    signed_at TEXT
);

-- @schema extract
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT,
    config_json TEXT,
    config_hash TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    study_id TEXT NOT NULL,
    field_group TEXT NOT NULL,
    seq INTEGER NOT NULL,
    view_json TEXT NOT NULL,
    n_chars INTEGER,
    est_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS paragraph_repeats (
    doc_id TEXT NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    first_doc_id TEXT NOT NULL,
    first_char_start INTEGER,
    first_char_end INTEGER
);
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    chunk_id TEXT,
    purpose TEXT,
    record_json TEXT,
    prompt_sha TEXT,
    output_json TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS done (
    run_config_hash TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    field_group TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (run_config_hash, doc_id, field_group)
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    chunk_id TEXT,
    study_id TEXT NOT NULL,
    field_group TEXT NOT NULL,
    item_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    event_date TEXT,
    estimate_code TEXT,
    quote TEXT,
    char_start INTEGER,
    char_end INTEGER,
    certainty TEXT,
    negated INTEGER,
    hypothetical INTEGER,
    verified INTEGER,
    verify_score REAL,
    doc_date TEXT
);
CREATE INDEX IF NOT EXISTS ix_evidence_study ON evidence(study_id, field_group);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    instance_key TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    estimate_code TEXT,
    evidence_ids TEXT,
    conflict_flag INTEGER DEFAULT 0,
    conflict_note TEXT,
    run_id TEXT NOT NULL,
    anchor_date TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_study ON proposals(study_id, instrument, instance_key);
CREATE TABLE IF NOT EXISTS reconcile_llm (
    call_id TEXT PRIMARY KEY,
    study_id TEXT,
    prompt TEXT,
    output TEXT,
    created_at TEXT
);

-- @schema review
CREATE TABLE IF NOT EXISTS reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer TEXT NOT NULL,
    at TEXT NOT NULL,
    study_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    instance_key TEXT NOT NULL,
    field TEXT,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    estimate_code TEXT,
    reason TEXT,
    evidence_doc_id TEXT,
    evidence_start INTEGER,
    evidence_end INTEGER
);
CREATE TRIGGER IF NOT EXISTS reviews_no_update BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END;
CREATE TRIGGER IF NOT EXISTS reviews_no_delete BEFORE DELETE ON reviews
BEGIN SELECT RAISE(ABORT, 'reviews is append-only'); END;
CREATE TABLE IF NOT EXISTS reviewer_sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer TEXT NOT NULL,
    study_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
