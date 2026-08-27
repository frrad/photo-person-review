"""Small typed-ish SQLite facade used by the CLI and future analysis modules."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .schema import apply_migrations


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_photo_id(sha256: str) -> str:
    """Use the content digest itself as the stable, provider-independent photo ID."""

    digest = sha256.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("sha256 must be a 64-character hexadecimal digest")
    return digest


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class Catalog:
    """A catalog connection.  Mutating methods commit one logical event at a time."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        apply_migrations(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _id(self) -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def create_source(
        self,
        kind: str,
        label: str | None = None,
        root_path: str | None = None,
        metadata: Mapping[str, object] | None = None,
        source_id: str | None = None,
    ) -> str:
        source_id = source_id or self._id()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, kind, label, root_path, self._json(metadata or {}), utc_now()),
            )
        return source_id

    def create_import_run(self, source_id: str, status: str = "running", import_run_id: str | None = None) -> str:
        import_run_id = import_run_id or self._id()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO import_runs VALUES (?, ?, ?, NULL, ?, '{}')",
                (import_run_id, source_id, utc_now(), status),
            )
        return import_run_id

    def finish_import_run(self, import_run_id: str, status: str, summary: Mapping[str, object] | None = None) -> None:
        if status not in {"complete", "failed"}:
            raise ValueError("finished import status must be complete or failed")
        with self.transaction() as db:
            db.execute(
                "UPDATE import_runs SET finished_at=?, status=?, summary_json=? WHERE import_run_id=?",
                (utc_now(), status, self._json(summary or {}), import_run_id),
            )

    def upsert_photo(
        self,
        sha256: str,
        *,
        width: int | None = None,
        height: int | None = None,
        mime_type: str | None = None,
        capture_time: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        photo_id = stable_photo_id(sha256)
        with self.transaction() as db:
            db.execute(
                """INSERT INTO photos(photo_id, sha256, first_seen_at, width, height, mime_type,
                                              capture_time, metadata_json)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT(sha256) DO UPDATE SET
                           width=COALESCE(excluded.width, photos.width),
                           height=COALESCE(excluded.height, photos.height),
                           mime_type=COALESCE(excluded.mime_type, photos.mime_type),
                           capture_time=COALESCE(excluded.capture_time, photos.capture_time),
                           metadata_json=CASE WHEN excluded.metadata_json='{}' THEN photos.metadata_json
                                              ELSE excluded.metadata_json END""",
                (
                    photo_id,
                    sha256.lower(),
                    utc_now(),
                    width,
                    height,
                    mime_type,
                    capture_time,
                    self._json(metadata or {}),
                ),
            )
        return photo_id

    def observe_source_file(
        self,
        source_id: str,
        photo_id: str,
        path: str,
        *,
        relative_path: str | None = None,
        file_size: int | None = None,
        mtime_ns: int | None = None,
        import_run_id: str | None = None,
        observation_state: str = "present",
    ) -> int:
        """Append one observation, including when the same file was imported before.

        This is intentionally not an upsert: an import run is an audit trail and
        must grow even when the content hash deduplicates the photo record.
        """
        if observation_state not in {"present", "missing", "replaced"}:
            raise ValueError("observation_state must be present, missing, or replaced")
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT INTO source_files(source_id, photo_id, path, relative_path,
                         file_size, mtime_ns, observed_at, import_run_id, observation_state)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id,
                    photo_id,
                    path,
                    relative_path,
                    file_size,
                    mtime_ns,
                    utc_now(),
                    import_run_id,
                    observation_state,
                ),
            )
            return int(cursor.lastrowid or 0)

    def create_batch(
        self,
        source_id: str | None = None,
        label: str | None = None,
        capture_date: str | None = None,
        metadata: Mapping[str, object] | None = None,
        batch_id: str | None = None,
    ) -> str:
        batch_id = batch_id or self._id()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, source_id, label, capture_date, utc_now(), self._json(metadata or {})),
            )
        return batch_id

    def create_analysis_run(
        self,
        *,
        backend: str,
        model: str | None = None,
        batch_id: str | None = None,
        person_id: str | None = None,
        parameters: Mapping[str, object] | None = None,
        analysis_run_id: str | None = None,
    ) -> str:
        """Record a reproducible analysis invocation before it produces rows."""
        analysis_run_id = analysis_run_id or self._id()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO analysis_runs(analysis_run_id,batch_id,person_id,backend,model,
                         started_at,status,parameters_json) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)""",
                (
                    analysis_run_id,
                    batch_id,
                    person_id,
                    backend,
                    model,
                    utc_now(),
                    self._json(parameters or {}),
                ),
            )
        return analysis_run_id

    def finish_analysis_run(
        self, analysis_run_id: str, status: str, summary: Mapping[str, object] | None = None
    ) -> None:
        if status not in {"complete", "failed"}:
            raise ValueError("finished analysis status must be complete or failed")
        with self.transaction() as db:
            db.execute(
                """UPDATE analysis_runs SET finished_at=?, status=?, summary_json=?
                         WHERE analysis_run_id=?""",
                (utc_now(), status, self._json(summary or {}), analysis_run_id),
            )

    def add_face(
        self,
        photo_id: str,
        analysis_run_id: str,
        *,
        face_id: str | None = None,
        x: float,
        y: float,
        width: float,
        height: float,
        quality: float | None = None,
        landmarks: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        face_id = face_id or self._id()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO faces(face_id,photo_id,analysis_run_id,x,y,width,height,quality,
                         landmarks_json,metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    face_id,
                    photo_id,
                    analysis_run_id,
                    x,
                    y,
                    width,
                    height,
                    quality,
                    self._json(landmarks) if landmarks is not None else None,
                    self._json(metadata or {}),
                ),
            )
        return face_id

    def add_person_box(
        self,
        photo_id: str,
        analysis_run_id: str,
        *,
        person_box_id: str | None = None,
        x: float,
        y: float,
        width: float,
        height: float,
        face_id: str | None = None,
        confidence: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        person_box_id = person_box_id or self._id()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO person_boxes(person_box_id,photo_id,analysis_run_id,x,y,width,height,
                         face_id,confidence,metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    person_box_id,
                    photo_id,
                    analysis_run_id,
                    x,
                    y,
                    width,
                    height,
                    face_id,
                    confidence,
                    self._json(metadata or {}),
                ),
            )
        return person_box_id

    def add_numeric_feature(
        self,
        photo_id: str,
        analysis_run_id: str,
        feature_kind: str,
        vector: list[float] | tuple[float, ...],
        *,
        subject_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> int:
        values = [float(value) for value in vector]
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT INTO numeric_features(photo_id,analysis_run_id,subject_id,
                         feature_kind,vector_json,dimensions,metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    photo_id,
                    analysis_run_id,
                    subject_id,
                    feature_kind,
                    self._json(values),
                    len(values),
                    self._json(metadata or {}),
                ),
            )
            return int(cursor.lastrowid or 0)

    def add_artifact_manifest(
        self,
        root_path: str,
        artifacts: Mapping[str, str | list[str]],
        *,
        person_id: str | None = None,
        batch_id: str | None = None,
        expires_at: str | None = None,
        metadata: Mapping[str, object] | None = None,
        manifest_id: str | None = None,
    ) -> str:
        """Record disposable artifact paths; callers own cleanup and source bytes."""
        manifest_id = manifest_id or self._id()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO artifact_manifests(manifest_id,person_id,batch_id,created_at,expires_at,
                         root_path,artifacts_json,metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    manifest_id,
                    person_id,
                    batch_id,
                    utc_now(),
                    expires_at,
                    root_path,
                    self._json(artifacts),
                    self._json(metadata or {}),
                ),
            )
        return manifest_id

    def observe_batch_photo(
        self,
        batch_id: str,
        photo_id: str,
        observation: Mapping[str, object] | None = None,
        *,
        import_run_id: str | None = None,
        observation_state: str = "present",
    ) -> int:
        """Keep stable membership plus an append-only observation event."""
        if observation_state not in {"present", "missing", "replaced"}:
            raise ValueError("observation_state must be present, missing, or replaced")
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO batch_photos(batch_id,photo_id,first_seen_at,last_seen_at,observation_json)
                         VALUES (?, ?, ?, ?, ?)
                         ON CONFLICT(batch_id,photo_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (batch_id, photo_id, now, now, self._json(observation or {})),
            )
            cursor = db.execute(
                """INSERT INTO batch_photo_observations(batch_id,photo_id,import_run_id,
                         observed_at,observation_state,observation_json) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    photo_id,
                    import_run_id,
                    now,
                    observation_state,
                    self._json(observation or {}),
                ),
            )
            return int(cursor.lastrowid or 0)

    def add_metadata(
        self,
        photo_id: str,
        key: str,
        value: object,
        provenance: str,
        import_run_id: str | None = None,
    ) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT INTO photo_metadata(photo_id,key,value_json,provenance,observed_at,import_run_id)
                                  VALUES (?, ?, ?, ?, ?, ?)""",
                (photo_id, key, self._json(value), provenance, utc_now(), import_run_id),
            )
            return int(cursor.lastrowid or 0)

    def create_person(
        self, person_id: str, label: str | None = None, metadata: Mapping[str, object] | None = None
    ) -> str:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO people VALUES (?, ?, ?, ?)
                   ON CONFLICT(person_id) DO UPDATE SET
                     label=COALESCE(excluded.label,people.label),
                     metadata_json=CASE WHEN excluded.metadata_json='{}'
                       THEN people.metadata_json ELSE excluded.metadata_json END""",
                (person_id, label, utc_now(), self._json(metadata or {})),
            )
        return person_id

    def _require_person(self, person_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM people WHERE person_id=?", (person_id,)).fetchone() is None:
            raise ValueError(f"unknown person: {person_id}")

    def add_identity_assertion(
        self,
        person_id: str,
        photo_id: str,
        *,
        face_id: str | None = None,
        kind: str = "positive",
        batch_id: str | None = None,
        captured_at: str | None = None,
        embedding: list[float] | tuple[float, ...] | None = None,
        metadata: Mapping[str, object] | None = None,
        assertion_id: str | None = None,
    ) -> str:
        if kind not in {"positive", "negative"}:
            raise ValueError("assertion kind must be positive or negative")
        assertion_id = assertion_id or self._id()
        self._require_person(person_id)
        if face_id is not None:
            active = self.connection.execute(
                """SELECT a.assertion_id,a.person_id,a.assertion_kind
                   FROM face_identity_assertions a
                   WHERE a.face_id=? AND a.person_id=? AND a.assertion_kind=?
                     AND (SELECT e.event FROM face_identity_assertion_events e
                          WHERE e.assertion_id=a.assertion_id ORDER BY e.event_id DESC LIMIT 1)='active'
                   LIMIT 1""",
                (face_id, person_id, kind),
            ).fetchone()
            if active is not None:
                return str(active["assertion_id"])
            if kind == "positive":
                other = self.connection.execute(
                    """SELECT a.assertion_id,a.person_id FROM face_identity_assertions a
                       WHERE a.face_id=? AND a.person_id<>? AND a.assertion_kind='positive'
                         AND (SELECT e.event FROM face_identity_assertion_events e
                              WHERE e.assertion_id=a.assertion_id ORDER BY e.event_id DESC LIMIT 1)='active'
                       LIMIT 1""",
                    (face_id, person_id),
                ).fetchone()
                if other is not None:
                    raise ValueError(
                        f"identity conflict: face {face_id} is actively assigned to person {other['person_id']}"
                    )
        with self.transaction() as db:
            db.execute(
                """INSERT INTO face_identity_assertions
                (assertion_id,person_id,photo_id,face_id,assertion_kind,batch_id,captured_at,
                 embedding_json,created_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    assertion_id,
                    person_id,
                    photo_id,
                    face_id,
                    kind,
                    batch_id,
                    captured_at,
                    self._json(embedding) if embedding is not None else None,
                    utc_now(),
                    self._json(metadata or {}),
                ),
            )
            db.execute(
                """INSERT INTO face_identity_assertion_events(
                   assertion_id,event,created_at,metadata_json)
                   VALUES (?, 'active', ?, '{}')""",
                (assertion_id, utc_now()),
            )
        return assertion_id

    def record_decision(
        self,
        person_id: str,
        photo_id: str,
        decision: str,
        *,
        actor: str = "user",
        evidence: Mapping[str, object] | None = None,
        analysis_run_id: str | None = None,
    ) -> int:
        """Record a person-specific append-only decision."""
        if decision not in {"accept", "reject", "unsure"}:
            raise ValueError("decision must be accept, reject, or unsure")
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT INTO decisions(person_id,photo_id,decision,actor,evidence_json,
                         analysis_run_id,created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (person_id, photo_id, decision, actor, self._json(evidence or {}), analysis_run_id, utc_now()),
            )
            return int(cursor.lastrowid or 0)

    def retire_identity_assertion(self, assertion_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO face_identity_assertion_events(
                   assertion_id,event,created_at,metadata_json)
                   VALUES (?, 'retired', ?, '{}')""",
                (assertion_id, utc_now()),
            )

    def list_identity_assertions(
        self, person_id: str, *, assertion_kind: str | None = None, active_only: bool = True
    ) -> list[sqlite3.Row]:
        query = "SELECT a.* FROM face_identity_assertions a WHERE a.person_id=?"
        args: list[object] = [person_id]
        if assertion_kind is not None:
            query += " AND a.assertion_kind=?"
            args.append(assertion_kind)
        if active_only:
            query += """ AND (SELECT e.event FROM face_identity_assertion_events e
                WHERE e.assertion_id=a.assertion_id ORDER BY e.event_id DESC LIMIT 1)='active'"""
        query += " ORDER BY a.created_at,a.assertion_id"
        return list(self.connection.execute(query, args))

    def list_people(self) -> list[sqlite3.Row]:
        return list(self.connection.execute("SELECT * FROM people ORDER BY created_at, person_id"))

    def identity_conflicts(self, person_id: str | None = None) -> list[sqlite3.Row]:
        """Return conflicts that still involve currently active assertions."""
        rows = self.connection.execute("SELECT * FROM identity_conflicts ORDER BY conflict_id").fetchall()
        result: list[sqlite3.Row] = []
        for row in rows:
            try:
                assertion_ids = json.loads(row["assertion_ids_json"])
            except (TypeError, json.JSONDecodeError):
                assertion_ids = []
            if not assertion_ids:
                continue
            placeholders = ",".join("?" for _ in assertion_ids)
            active = self.connection.execute(
                f"""SELECT assertion_id,person_id,assertion_kind,face_id
                    FROM face_identity_assertions
                    WHERE assertion_id IN ({placeholders})
                      AND (SELECT e.event FROM face_identity_assertion_events e
                           WHERE e.assertion_id=face_identity_assertions.assertion_id
                           ORDER BY e.event_id DESC LIMIT 1)='active'""",
                assertion_ids,
            ).fetchall()
            kinds = {(item["person_id"], item["assertion_kind"]) for item in active}
            conflict_kind = row["conflict_kind"]
            if person_id is not None and not any(item["person_id"] == person_id for item in active):
                continue
            relevant = (
                len({person for person, kind in kinds if kind == "positive"}) > 1
                if conflict_kind == "multiple_positive_identities"
                else (
                    any(kind == "positive" for _, kind in kinds) and any(kind == "negative" for _, kind in kinds)
                    if conflict_kind == "positive_and_negative_same_person"
                    else len(active) > 1
                    if conflict_kind == "duplicate_exact_identity_evidence"
                    else bool(active)
                )
            )
            if relevant:
                result.append(row)
        return result

    def create_tag(self, name: str, description: str | None = None, tag_id: str | None = None) -> str:
        tag_id = tag_id or self._id()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO tag_definitions VALUES (?, ?, ?, ?, '{}')",
                (tag_id, name, description, utc_now()),
            )
        return tag_id

    def assign_tag(
        self,
        photo_id: str,
        tag_id: str,
        *,
        provenance: str,
        value: str = "true",
        confidence: float | None = None,
        person_id: str | None = None,
        analysis_run_id: str | None = None,
        evidence: Mapping[str, object] | None = None,
    ) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """INSERT INTO tag_assignments(photo_id,tag_id,value,provenance,confidence,person_id,
                         analysis_run_id,created_at,metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    photo_id,
                    tag_id,
                    value,
                    provenance,
                    confidence,
                    person_id,
                    analysis_run_id,
                    utc_now(),
                    self._json(evidence or {}),
                ),
            )
            return int(cursor.lastrowid or 0)

    def counts(self) -> dict[str, int]:
        tables = (
            "photos",
            "source_files",
            "batches",
            "batch_photos",
            "batch_photo_observations",
            "photo_metadata",
            "tag_assignments",
            "people",
            "face_identity_assertions",
            "face_identity_assertion_events",
            "appearance_references",
            "analysis_runs",
            "analysis_results",
            "candidate_scores",
            "faces",
            "person_boxes",
            "numeric_features",
            "decisions",
            "artifact_manifests",
        )
        result = {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables
        }
        return result

    def latest_decisions(self, person_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """SELECT d.* FROM decisions d
            WHERE d.person_id=? AND d.decision_id=(SELECT MAX(d2.decision_id) FROM decisions d2
            WHERE d2.person_id=d.person_id AND d2.photo_id=d.photo_id) ORDER BY d.photo_id""",
                (person_id,),
            )
        )
