"""SQLite migrations for the local photo catalog.

The schema is deliberately metadata-only: there is no image/blob column.  A
numeric embedding is represented as JSON text so migrations remain portable;
future vector stores can be added without changing photo identity records.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        label TEXT,
        root_path TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS import_runs (
        import_run_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
        summary_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS batches (
        batch_id TEXT PRIMARY KEY,
        source_id TEXT REFERENCES sources(source_id),
        label TEXT,
        capture_date TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS photos (
        photo_id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        first_seen_at TEXT NOT NULL,
        width INTEGER,
        height INTEGER,
        mime_type TEXT,
        capture_time TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS source_files (
        source_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id TEXT NOT NULL REFERENCES sources(source_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        path TEXT NOT NULL,
        relative_path TEXT,
        file_size INTEGER,
        mtime_ns INTEGER,
        observed_at TEXT NOT NULL,
        import_run_id TEXT REFERENCES import_runs(import_run_id),
        observation_state TEXT NOT NULL DEFAULT 'present'
            CHECK (observation_state IN ('present', 'missing', 'replaced'))
    );
    CREATE INDEX IF NOT EXISTS idx_source_files_photo ON source_files(photo_id);
    CREATE INDEX IF NOT EXISTS idx_source_files_run ON source_files(import_run_id);

    CREATE TABLE IF NOT EXISTS batch_photos (
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        observation_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (batch_id, photo_id)
    );
    CREATE INDEX IF NOT EXISTS idx_batch_photos_photo ON batch_photos(photo_id);
    CREATE TABLE IF NOT EXISTS batch_photo_observations (
        observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        import_run_id TEXT REFERENCES import_runs(import_run_id),
        observed_at TEXT NOT NULL,
        observation_state TEXT NOT NULL DEFAULT 'present'
            CHECK (observation_state IN ('present', 'missing', 'replaced')),
        observation_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_batch_photo_observations_lookup
        ON batch_photo_observations(batch_id, photo_id, observation_id);

    -- Append-only observations.  The latest value can be selected by observed_at/id.
    CREATE TABLE IF NOT EXISTS photo_metadata (
        metadata_id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        provenance TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        import_run_id TEXT REFERENCES import_runs(import_run_id)
    );
    CREATE INDEX IF NOT EXISTS idx_photo_metadata_latest ON photo_metadata(photo_id, key, metadata_id);

    CREATE TABLE IF NOT EXISTS tag_definitions (
        tag_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS tag_assignments (
        assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        tag_id TEXT NOT NULL REFERENCES tag_definitions(tag_id),
        value TEXT NOT NULL DEFAULT 'true',
        provenance TEXT NOT NULL,
        confidence REAL,
        target_id TEXT,
        analysis_run_id TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_tag_assignments_photo ON tag_assignments(photo_id, created_at);

    CREATE TABLE IF NOT EXISTS targets (
        target_id TEXT PRIMARY KEY,
        label TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS target_references (
        reference_id TEXT PRIMARY KEY,
        target_id TEXT NOT NULL REFERENCES targets(target_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        face_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('positive', 'negative')),
        batch_id TEXT REFERENCES batches(batch_id),
        captured_at TEXT,
        embedding_json TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_target_references_target
        ON target_references(target_id, kind, created_at);
    CREATE TABLE IF NOT EXISTS target_reference_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference_id TEXT NOT NULL REFERENCES target_references(reference_id),
        event TEXT NOT NULL CHECK (event IN ('active', 'retired')),
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_target_reference_events_latest
        ON target_reference_events(reference_id, event_id);
    CREATE TABLE IF NOT EXISTS appearance_references (
        reference_id TEXT PRIMARY KEY,
        target_id TEXT NOT NULL REFERENCES targets(target_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        person_id TEXT,
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        feature_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_appearance_references_batch
        ON appearance_references(target_id, batch_id, created_at);
    CREATE TABLE IF NOT EXISTS appearance_reference_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference_id TEXT NOT NULL REFERENCES appearance_references(reference_id),
        event TEXT NOT NULL CHECK (event IN ('active', 'retired')),
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS analysis_results (
        result_id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        analyzer_version TEXT NOT NULL,
        result_json TEXT NOT NULL,
        analysis_run_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_analysis_results_photo
        ON analysis_results(photo_id, batch_id, created_at);
    CREATE TABLE IF NOT EXISTS candidate_scores (
        score_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        target_id TEXT NOT NULL REFERENCES targets(target_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        score REAL NOT NULL,
        score_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_candidate_scores_target
        ON candidate_scores(target_id, batch_id, run_id, created_at);
    CREATE TABLE IF NOT EXISTS analysis_runs (
        analysis_run_id TEXT PRIMARY KEY,
        batch_id TEXT REFERENCES batches(batch_id),
        target_id TEXT REFERENCES targets(target_id),
        backend TEXT NOT NULL,
        model TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
        parameters_json TEXT NOT NULL DEFAULT '{}',
        summary_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS faces (
        face_id TEXT PRIMARY KEY,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id),
        x REAL NOT NULL, y REAL NOT NULL, width REAL NOT NULL, height REAL NOT NULL,
        quality REAL,
        landmarks_json TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS person_boxes (
        person_box_id TEXT PRIMARY KEY,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id),
        x REAL NOT NULL, y REAL NOT NULL, width REAL NOT NULL, height REAL NOT NULL,
        face_id TEXT REFERENCES faces(face_id),
        confidence REAL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS numeric_features (
        feature_id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(analysis_run_id),
        subject_id TEXT,
        feature_kind TEXT NOT NULL,
        vector_json TEXT NOT NULL,
        dimensions INTEGER NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_numeric_features_subject ON numeric_features(photo_id, subject_id, feature_kind);

    -- User/model decisions are events; do not update or delete an old decision.
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id TEXT NOT NULL REFERENCES targets(target_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        batch_id TEXT REFERENCES batches(batch_id),
        decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'unsure')),
        actor TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        analysis_run_id TEXT REFERENCES analysis_runs(analysis_run_id),
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_decisions_latest ON decisions(target_id, photo_id, decision_id);

    -- This contains only references to disposable artifacts, never image bytes.
    CREATE TABLE IF NOT EXISTS artifact_manifests (
        manifest_id TEXT PRIMARY KEY,
        target_id TEXT REFERENCES targets(target_id),
        batch_id TEXT REFERENCES batches(batch_id),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        root_path TEXT NOT NULL,
        artifacts_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    """
}


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Apply all migrations and return the resulting schema version."""

    connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    current = int(row[0]) if row else 0
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"catalog schema {current} is newer than supported {SCHEMA_VERSION}")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        with connection:
            connection.executescript(MIGRATIONS[version])
            connection.execute("DELETE FROM schema_version")
            connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    return SCHEMA_VERSION
