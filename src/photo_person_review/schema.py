"""SQLite migrations for the local photo catalog.

The schema is deliberately metadata-only: there is no image/blob column.  A
numeric embedding is represented as JSON text so migrations remain portable;
future vector stores can be added without changing photo identity records.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

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
    """,
    2: """
    -- v2 gives named identities their own canonical vocabulary.  Legacy names
    -- are used only while copying v1 rows below; they are not exposed after
    -- migration.
    PRAGMA foreign_keys = OFF;
    BEGIN;

    CREATE TABLE people (
        person_id TEXT PRIMARY KEY,
        label TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO people(person_id,label,created_at,metadata_json)
        SELECT target_id,label,created_at,metadata_json FROM targets;
    DROP TABLE targets;

    CREATE TABLE face_identity_assertions (
        assertion_id TEXT PRIMARY KEY,
        person_id TEXT NOT NULL REFERENCES people(person_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        face_id TEXT,
        assertion_kind TEXT NOT NULL CHECK (assertion_kind IN ('positive', 'negative')),
        batch_id TEXT REFERENCES batches(batch_id),
        captured_at TEXT,
        embedding_json TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO face_identity_assertions(
        assertion_id,person_id,photo_id,face_id,assertion_kind,batch_id,captured_at,
        embedding_json,created_at,metadata_json)
        SELECT reference_id,target_id,photo_id,face_id,kind,batch_id,captured_at,
               embedding_json,created_at,metadata_json FROM target_references;
    DROP TABLE target_references;
    CREATE INDEX idx_face_identity_assertions_person
        ON face_identity_assertions(person_id, assertion_kind, created_at);

    CREATE TABLE face_identity_assertion_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        assertion_id TEXT NOT NULL REFERENCES face_identity_assertions(assertion_id),
        event TEXT NOT NULL CHECK (event IN ('active', 'retired')),
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO face_identity_assertion_events(assertion_id,event,created_at,metadata_json)
        SELECT reference_id,event,created_at,metadata_json FROM target_reference_events
        ORDER BY event_id;
    DROP TABLE target_reference_events;
    CREATE INDEX idx_face_identity_assertion_events_latest
        ON face_identity_assertion_events(assertion_id, event_id);

    CREATE TABLE tag_assignments_v2 (
        assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        tag_id TEXT NOT NULL REFERENCES tag_definitions(tag_id),
        value TEXT NOT NULL DEFAULT 'true',
        provenance TEXT NOT NULL,
        confidence REAL,
        person_id TEXT REFERENCES people(person_id),
        analysis_run_id TEXT,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO tag_assignments_v2(
        assignment_id,photo_id,tag_id,value,provenance,confidence,person_id,analysis_run_id,
        created_at,metadata_json)
        SELECT assignment_id,photo_id,tag_id,value,provenance,confidence,target_id,analysis_run_id,
               created_at,metadata_json FROM tag_assignments;
    DROP TABLE tag_assignments;
    ALTER TABLE tag_assignments_v2 RENAME TO tag_assignments;
    CREATE INDEX idx_tag_assignments_photo ON tag_assignments(photo_id, created_at);

    CREATE TABLE appearance_references_v2 (
        reference_id TEXT PRIMARY KEY,
        person_id TEXT NOT NULL REFERENCES people(person_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        person_box_id TEXT,
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        feature_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO appearance_references_v2(
        reference_id,person_id,photo_id,person_box_id,batch_id,feature_json,created_at,metadata_json)
        SELECT reference_id,target_id,photo_id,person_id,batch_id,feature_json,created_at,metadata_json
        FROM appearance_references;
    DROP TABLE appearance_references;
    ALTER TABLE appearance_references_v2 RENAME TO appearance_references;
    CREATE INDEX idx_appearance_references_batch
        ON appearance_references(person_id, batch_id, created_at);

    CREATE TABLE candidate_scores_v2 (
        score_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        person_id TEXT NOT NULL REFERENCES people(person_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        score REAL NOT NULL,
        score_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    INSERT INTO candidate_scores_v2(
        score_id,run_id,person_id,photo_id,batch_id,score,score_json,created_at)
        SELECT score_id,run_id,target_id,photo_id,batch_id,score,score_json,created_at
        FROM candidate_scores;
    DROP TABLE candidate_scores;
    ALTER TABLE candidate_scores_v2 RENAME TO candidate_scores;
    CREATE INDEX idx_candidate_scores_person
        ON candidate_scores(person_id, batch_id, run_id, created_at);

    CREATE TABLE analysis_runs_v2 (
        analysis_run_id TEXT PRIMARY KEY,
        batch_id TEXT REFERENCES batches(batch_id),
        person_id TEXT REFERENCES people(person_id),
        backend TEXT NOT NULL,
        model TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'failed')),
        parameters_json TEXT NOT NULL DEFAULT '{}',
        summary_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO analysis_runs_v2(
        analysis_run_id,batch_id,person_id,backend,model,started_at,finished_at,status,
        parameters_json,summary_json)
        SELECT analysis_run_id,batch_id,target_id,backend,model,started_at,finished_at,status,
               parameters_json,summary_json FROM analysis_runs;
    DROP TABLE analysis_runs;
    ALTER TABLE analysis_runs_v2 RENAME TO analysis_runs;

    CREATE TABLE decisions_v2 (
        decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id TEXT NOT NULL REFERENCES people(person_id),
        photo_id TEXT NOT NULL REFERENCES photos(photo_id),
        batch_id TEXT REFERENCES batches(batch_id),
        decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'unsure')),
        actor TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '{}',
        analysis_run_id TEXT REFERENCES analysis_runs(analysis_run_id),
        created_at TEXT NOT NULL
    );
    INSERT INTO decisions_v2(
        decision_id,person_id,photo_id,batch_id,decision,actor,evidence_json,analysis_run_id,created_at)
        SELECT decision_id,target_id,photo_id,batch_id,decision,actor,evidence_json,analysis_run_id,created_at
        FROM decisions;
    DROP TABLE decisions;
    ALTER TABLE decisions_v2 RENAME TO decisions;
    CREATE INDEX idx_decisions_latest ON decisions(person_id, photo_id, decision_id);

    CREATE TABLE artifact_manifests_v2 (
        manifest_id TEXT PRIMARY KEY,
        person_id TEXT REFERENCES people(person_id),
        batch_id TEXT REFERENCES batches(batch_id),
        created_at TEXT NOT NULL,
        expires_at TEXT,
        root_path TEXT NOT NULL,
        artifacts_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );
    INSERT INTO artifact_manifests_v2(
        manifest_id,person_id,batch_id,created_at,expires_at,root_path,artifacts_json,metadata_json)
        SELECT manifest_id,target_id,batch_id,created_at,expires_at,root_path,artifacts_json,metadata_json
        FROM artifact_manifests;
    DROP TABLE artifact_manifests;
    ALTER TABLE artifact_manifests_v2 RENAME TO artifact_manifests;

    CREATE TABLE identity_conflicts (
        conflict_id TEXT PRIMARY KEY,
        face_id TEXT,
        person_id TEXT,
        conflict_kind TEXT NOT NULL,
        assertion_ids_json TEXT NOT NULL,
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    -- No assertion is discarded when identities conflict.  These rows make
    -- migration ambiguity queryable and permit explicit later reconciliation.
    INSERT INTO identity_conflicts(
        conflict_id,face_id,person_id,conflict_kind,assertion_ids_json,details_json,created_at)
        SELECT 'multiple-positive:' || a.face_id, a.face_id, NULL, 'multiple_positive_identities',
               json_group_array(a.assertion_id), '{}', datetime('now')
        FROM face_identity_assertions a
        JOIN face_identity_assertion_events ev ON ev.assertion_id=a.assertion_id
            AND ev.event_id=(SELECT MAX(e2.event_id) FROM face_identity_assertion_events e2
                             WHERE e2.assertion_id=a.assertion_id)
            AND ev.event='active'
        WHERE a.face_id IS NOT NULL AND a.assertion_kind='positive'
        GROUP BY a.face_id HAVING COUNT(DISTINCT a.person_id) > 1;
    INSERT INTO identity_conflicts(
        conflict_id,face_id,person_id,conflict_kind,assertion_ids_json,details_json,created_at)
        SELECT 'positive-negative:' || a.face_id || ':' || a.person_id, a.face_id, a.person_id,
               'positive_and_negative_same_person', json_group_array(a.assertion_id), '{}', datetime('now')
        FROM face_identity_assertions a
        JOIN face_identity_assertion_events ev ON ev.assertion_id=a.assertion_id
            AND ev.event_id=(SELECT MAX(e2.event_id) FROM face_identity_assertion_events e2
                             WHERE e2.assertion_id=a.assertion_id)
            AND ev.event='active'
        WHERE a.face_id IS NOT NULL
        GROUP BY a.face_id, a.person_id
        HAVING SUM(a.assertion_kind='positive') > 0 AND SUM(a.assertion_kind='negative') > 0;
    INSERT INTO identity_conflicts(
        conflict_id,face_id,person_id,conflict_kind,assertion_ids_json,details_json,created_at)
        SELECT 'duplicate:' || a.face_id || ':' || a.person_id || ':' || a.assertion_kind
               || ':' || hex(a.embedding_json),
               a.face_id, a.person_id, 'duplicate_exact_identity_evidence',
               json_group_array(a.assertion_id), '{}', datetime('now')
        FROM face_identity_assertions a
        JOIN face_identity_assertion_events ev ON ev.assertion_id=a.assertion_id
            AND ev.event_id=(SELECT MAX(e2.event_id) FROM face_identity_assertion_events e2
                             WHERE e2.assertion_id=a.assertion_id)
            AND ev.event='active'
        WHERE a.face_id IS NOT NULL
        GROUP BY a.face_id, a.person_id, a.assertion_kind, a.embedding_json
        HAVING COUNT(*) > 1;
    INSERT INTO identity_conflicts(
        conflict_id,face_id,person_id,conflict_kind,assertion_ids_json,details_json,created_at)
        SELECT 'missing-face:' || a.assertion_id, a.face_id, a.person_id, 'missing_face_observation',
               json_array(a.assertion_id), '{}', datetime('now')
        FROM face_identity_assertions a
        JOIN face_identity_assertion_events ev ON ev.assertion_id=a.assertion_id
            AND ev.event_id=(SELECT MAX(e2.event_id) FROM face_identity_assertion_events e2
                             WHERE e2.assertion_id=a.assertion_id)
            AND ev.event='active'
        WHERE a.face_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.face_id=a.face_id);

    -- Preserve legacy negatives, but retire those now derivable from another
    -- named person's active positive assertion.
    INSERT INTO face_identity_assertion_events(
        assertion_id,event,created_at,metadata_json)
        SELECT n.assertion_id, 'retired', datetime('now'),
               '{"reason":"migrated_redundant_derived_negative"}'
        FROM face_identity_assertions n
        JOIN face_identity_assertion_events ne ON ne.assertion_id=n.assertion_id
            AND ne.event_id=(SELECT MAX(e2.event_id) FROM face_identity_assertion_events e2
                             WHERE e2.assertion_id=n.assertion_id)
            AND ne.event='active'
        WHERE n.assertion_kind='negative' AND n.face_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM face_identity_assertions p
              JOIN face_identity_assertion_events pe ON pe.assertion_id=p.assertion_id
                  AND pe.event_id=(SELECT MAX(e3.event_id) FROM face_identity_assertion_events e3
                                   WHERE e3.assertion_id=p.assertion_id)
                  AND pe.event='active'
              WHERE p.assertion_kind='positive' AND p.face_id=n.face_id
                AND p.person_id<>n.person_id
          );

    """,
}


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Apply all migrations and return the resulting schema version."""

    connection.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = connection.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    current = int(row[0]) if row else 0
    if current > SCHEMA_VERSION:
        raise RuntimeError(f"catalog schema {current} is newer than supported {SCHEMA_VERSION}")
    for version in range(current + 1, SCHEMA_VERSION + 1):
        # v2's script leaves its transaction open until this function verifies
        # all foreign keys and records the version marker in that same commit.
        try:
            connection.executescript(MIGRATIONS[version])
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"catalog foreign-key violations after migration {version}: {violations!r}")
            connection.execute("DELETE FROM schema_version")
            connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
        except Exception:
            # A failed executescript can leave its explicit BEGIN open and v2
            # temporarily disables FK enforcement. Always restore both before
            # propagating the error so callers can inspect/retry safely.
            connection.rollback()
            connection.execute("PRAGMA foreign_keys = ON")
            raise
    return SCHEMA_VERSION
