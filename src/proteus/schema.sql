-- =============================================================================
-- PROTEUS Automation Pipeline — Sample SQLite Schema
-- Version: 0.1 (brainstorming draft)
--
-- Notes:
--   * SQLite stores timestamps as TEXT in ISO-8601 form. They sort correctly
--     and are human-readable.
--   * Primary keys are TEXT (hash-derived) where idempotent re-imports matter,
--     INTEGER autoincrement otherwise.
--   * Foreign keys are off by default in SQLite; PRAGMA foreign_keys = ON;
--     must be set per connection.
--   * CHECK constraints enforce enum-like fields without a separate lookup.
-- =============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- 1. Provenance: where did this data come from?
-- ----------------------------------------------------------------------------

CREATE TABLE raw_imports (
    import_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path        TEXT NOT NULL,
    file_hash        TEXT NOT NULL UNIQUE,           -- SHA-256 of the file
    file_format      TEXT NOT NULL CHECK (file_format IN ('datashop_tab', 'runestone_useinfo_csv')),
    imported_at      TEXT NOT NULL DEFAULT (datetime('now')),
    row_count        INTEGER,
    notes            TEXT
);

CREATE TABLE pipeline_runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    status           TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'partial')),
    prompt_version   TEXT NOT NULL,                  -- e.g. 'stage2-v1', 'stage3-v0.3'
    model_id         TEXT NOT NULL,                  -- e.g. 'claude-sonnet-4-6'
    vocab_version    INTEGER NOT NULL,
    codebook_version INTEGER NOT NULL,
    notes            TEXT,
    FOREIGN KEY (vocab_version)    REFERENCES vocabulary_versions(version_id),
    FOREIGN KEY (codebook_version) REFERENCES codebook_versions(version_id)
);

-- ----------------------------------------------------------------------------
-- 2. Responses: one row per student submission
-- ----------------------------------------------------------------------------

CREATE TABLE responses (
    response_id      TEXT PRIMARY KEY,               -- hash(student_id + question_id + timestamp)
    import_id        INTEGER NOT NULL,
    student_id       TEXT NOT NULL,
    question_id      TEXT NOT NULL,
    response_text    TEXT NOT NULL,
    submitted_at     TEXT,                           -- timestamp from log, may be null
    course_id        TEXT,
    UNIQUE (student_id, question_id, submitted_at),
    FOREIGN KEY (import_id) REFERENCES raw_imports(import_id)
);

CREATE INDEX idx_responses_question ON responses(question_id);
CREATE INDEX idx_responses_student  ON responses(student_id);

-- ----------------------------------------------------------------------------
-- 3. Pipeline outputs: per-run artifacts for each response
-- ----------------------------------------------------------------------------

CREATE TABLE screenings (
    screening_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id      TEXT NOT NULL,
    run_id           INTEGER NOT NULL,
    label            TEXT NOT NULL CHECK (label IN ('INCLUDE', 'EXCLUDE', 'FLAG')),
    confidence       REAL CHECK (confidence BETWEEN 0 AND 1),
    rationale        TEXT,
    UNIQUE (response_id, run_id),
    FOREIGN KEY (response_id) REFERENCES responses(response_id),
    FOREIGN KEY (run_id)      REFERENCES pipeline_runs(run_id)
);

CREATE TABLE chains (
    chain_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id      TEXT NOT NULL,
    run_id           INTEGER NOT NULL,
    extractor_notes  TEXT,
    UNIQUE (response_id, run_id),
    FOREIGN KEY (response_id) REFERENCES responses(response_id),
    FOREIGN KEY (run_id)      REFERENCES pipeline_runs(run_id)
);

CREATE TABLE chain_steps (
    step_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id         INTEGER NOT NULL,
    step_index       INTEGER NOT NULL,               -- 1-based ordering
    premise          TEXT,                           -- canonical-form proposition; nullable
    conclusion       TEXT NOT NULL,
    oov_flag         INTEGER NOT NULL DEFAULT 0,     -- 1 if any term is out-of-vocabulary
    UNIQUE (chain_id, step_index),
    FOREIGN KEY (chain_id) REFERENCES chains(chain_id) ON DELETE CASCADE
);

CREATE TABLE matches (
    match_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id               INTEGER NOT NULL,
    code_id               TEXT NOT NULL,
    codebook_version_id   INTEGER NOT NULL,         -- needed for composite FK
    rationale             TEXT,
    FOREIGN KEY (step_id)  REFERENCES chain_steps(step_id) ON DELETE CASCADE,
    FOREIGN KEY (code_id, codebook_version_id) REFERENCES codebook(code_id, version_id)
);

CREATE TABLE dispositions (
    disposition_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id      TEXT NOT NULL,
    run_id           INTEGER NOT NULL,
    disposition      TEXT NOT NULL CHECK (disposition IN (
                          'auto_accept', 'standard_review',
                          'problem_type_flag', 'terminology_flag',
                          'no_match', 'escalate'
                     )),
    trigger_note     TEXT,
    UNIQUE (response_id, run_id),
    FOREIGN KEY (response_id) REFERENCES responses(response_id),
    FOREIGN KEY (run_id)      REFERENCES pipeline_runs(run_id)
);

-- ----------------------------------------------------------------------------
-- 4. Human review: who looked at what, and what they did
-- ----------------------------------------------------------------------------

CREATE TABLE reviewers (
    reviewer_id      TEXT PRIMARY KEY,               -- short handle e.g. 'geoff'
    display_name     TEXT NOT NULL,
    is_active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE reviews (
    review_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id      TEXT NOT NULL,
    reviewer_id      TEXT NOT NULL,
    run_id           INTEGER NOT NULL,               -- which pipeline run was reviewed
    opened_at        TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,                           -- null while in progress
    lock_expires_at  TEXT,                           -- soft-lock for concurrency
    status           TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'abandoned')),
    notes            TEXT,
    FOREIGN KEY (response_id) REFERENCES responses(response_id),
    FOREIGN KEY (reviewer_id) REFERENCES reviewers(reviewer_id),
    FOREIGN KEY (run_id)      REFERENCES pipeline_runs(run_id)
);

CREATE INDEX idx_reviews_status ON reviews(status);
CREATE INDEX idx_reviews_response ON reviews(response_id);

CREATE TABLE review_codes (
    review_id             INTEGER NOT NULL,
    code_id               TEXT NOT NULL,
    codebook_version_id   INTEGER NOT NULL,
    PRIMARY KEY (review_id, code_id),
    FOREIGN KEY (review_id) REFERENCES reviews(review_id) ON DELETE CASCADE,
    FOREIGN KEY (code_id, codebook_version_id) REFERENCES codebook(code_id, version_id)
);

CREATE TABLE override_log (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id        INTEGER NOT NULL,
    occurred_at      TEXT NOT NULL DEFAULT (datetime('now')),
    edit_kind        TEXT NOT NULL CHECK (edit_kind IN ('confirm', 'add', 'remove', 'replace', 'reject_all')),
    tool_codes       TEXT NOT NULL,                  -- JSON array of code_ids before edit
    reviewer_codes   TEXT NOT NULL,                  -- JSON array of code_ids after edit
    rationale_note   TEXT,
    FOREIGN KEY (review_id) REFERENCES reviews(review_id)
);

-- ----------------------------------------------------------------------------
-- 5. Codebook and controlled vocabulary (versioned)
-- ----------------------------------------------------------------------------

CREATE TABLE codebook_versions (
    version_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL UNIQUE,           -- e.g. 'codebook2-april24'
    activated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    notes            TEXT
);

CREATE TABLE codebook (
    code_id          TEXT NOT NULL,                  -- e.g. 'CS7'
    version_id       INTEGER NOT NULL,
    name             TEXT NOT NULL,                  -- e.g. 'Free variables'
    description      TEXT NOT NULL,
    PRIMARY KEY (code_id, version_id),
    FOREIGN KEY (version_id) REFERENCES codebook_versions(version_id)
);

CREATE TABLE codebook_patterns (
    pattern_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id          TEXT NOT NULL,
    version_id       INTEGER NOT NULL,
    pattern_text     TEXT NOT NULL,                  -- DSL or human-readable rule
    pattern_kind     TEXT NOT NULL CHECK (pattern_kind IN ('keyword', 'structural', 'composite')),
    notes            TEXT,
    FOREIGN KEY (code_id, version_id) REFERENCES codebook(code_id, version_id)
);

CREATE TABLE vocabulary_versions (
    version_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL UNIQUE,
    activated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    notes            TEXT
);

CREATE TABLE vocabulary (
    term_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id       INTEGER NOT NULL,
    canonical_form   TEXT NOT NULL,                  -- e.g. '∃ free variable'
    description      TEXT,
    UNIQUE (version_id, canonical_form),
    FOREIGN KEY (version_id) REFERENCES vocabulary_versions(version_id)
);

-- ----------------------------------------------------------------------------
-- 6. Question previewer: textbook catalog + extracted-question cache
--
-- These tables back the question-previewer feature (see
-- proteus_ui/proteus_ui_guide.md, "Question previewer" section). The
-- relationship from responses.question_id to questions.question_id is
-- deliberately logical, not a foreign key: responses can be ingested
-- before the textbook is synced, and `questions` is a derived cache that
-- may lag.
-- ----------------------------------------------------------------------------

CREATE TABLE textbooks (
    textbook_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    git_repo_url     TEXT NOT NULL,
    git_ref          TEXT NOT NULL,                  -- SHA or tag
    book_xml_id      TEXT NOT NULL,                  -- e.g. 'ula-proteus'
    source_dir       TEXT,                           -- local clone path (nullable until importer exists)
    build_dir        TEXT NOT NULL,                  -- local path to built HTML
    added_at         TEXT NOT NULL DEFAULT (datetime('now')),
    notes            TEXT,
    UNIQUE (git_repo_url, git_ref)
);

CREATE TABLE questions (
    question_id      TEXT PRIMARY KEY,               -- matches responses.question_id
    textbook_id      INTEGER NOT NULL,
    xml_id           TEXT NOT NULL,                  -- 'ula-proteus-pivots-reading-q1-part-a'
    parent_xml_id    TEXT,                           -- enclosing <exercise> xml:id, if a task
    section_id       TEXT,                           -- 'sec-pivots'
    section_title    TEXT,
    runestone_url    TEXT,                           -- canonical Runestone URL with fragment
    preview_path     TEXT,                           -- relative path to standalone HTML file
    metadata_json    TEXT NOT NULL,                  -- exercise_type, n_tasks, has_hints, ...
    source           TEXT NOT NULL CHECK (source IN ('auto_extracted','manual','placeholder')),
    source_hash      TEXT,                           -- SHA-256 of render input chunk
    extracted_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (textbook_id) REFERENCES textbooks(textbook_id)
);

CREATE INDEX idx_questions_textbook ON questions(textbook_id);
CREATE INDEX idx_questions_section  ON questions(section_id);

-- Seed: one textbooks row for the analyzed book. The book_xml_id 'ula-proteus'
-- is established in the design doc; the remaining fields are placeholders.
-- TODO(maintainer): confirm git_repo_url, git_ref, and build_dir for the
-- analyzed textbook, or replace this seed row with the real registration
-- before sync_questions runs against this database.
INSERT INTO textbooks (git_repo_url, git_ref, book_xml_id, build_dir, notes)
    VALUES (
        'TODO(maintainer):git_repo_url',
        'TODO(maintainer):git_ref',
        'ula-proteus',
        'TODO(maintainer):build_dir',
        'Placeholder seed row for the analyzed book; values need maintainer confirmation.'
    );

-- ----------------------------------------------------------------------------
-- 7. Schema migrations
-- ----------------------------------------------------------------------------

CREATE TABLE schema_version (
    version          INTEGER PRIMARY KEY,
    applied_at       TEXT NOT NULL DEFAULT (datetime('now')),
    description      TEXT
);

INSERT INTO schema_version (version, description)
    VALUES (1, 'Initial schema');
INSERT INTO schema_version (version, description)
    VALUES (2, 'Add textbooks and questions tables for question previewer');

-- =============================================================================
-- End of schema.
-- =============================================================================
