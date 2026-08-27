"""Review facade over the single metadata-only :class:`Catalog`.

Review state is deliberately stored in the core catalog. This facade keeps the
analysis/review APIs small while ensuring a workspace never grows a second,
conflicting people or decisions database.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from photo_person_review.analysis.models import AnalysisResult
from photo_person_review.analysis.scoring import CandidateScore
from photo_person_review.db import Catalog
from photo_person_review.schema import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _vector(values: Sequence[float]) -> str:
    return json.dumps([float(x) for x in values], separators=(",", ":"))


def _decode_vector(value: str | None) -> tuple[float, ...] | None:
    if value is None:
        return None
    return tuple(float(x) for x in json.loads(value))


class ReviewStore:
    """Review operations backed by the core catalog tables.

    The ``media_id`` argument is retained for API compatibility and maps to a
    catalog ``photo_id``. Real imported media IDs are SHA-256 photo IDs; no
    image bytes or generated-image paths are persisted here.
    """

    def __init__(self, database: str | Path | sqlite3.Connection):
        self._catalog: Catalog | None
        if isinstance(database, sqlite3.Connection):
            self._catalog = None
            self.connection = database
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            apply_migrations(self.connection)
        else:
            self._catalog = Catalog(database)
            self.connection = self._catalog.connection

    def close(self) -> None:
        if self._catalog is not None:
            self._catalog.close()

    def initialize(self) -> None:
        """Retained as an idempotent compatibility hook."""
        apply_migrations(self.connection)

    def __enter__(self) -> "ReviewStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def create_person(
        self, person_id: str | None = None, *, label: str | None = None, metadata: dict[str, Any] | None = None
    ) -> str:
        person_id = person_id or str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO people(person_id, label, created_at, metadata_json) VALUES (?, ?, ?, ?)
               ON CONFLICT(person_id) DO UPDATE SET
                 label=COALESCE(excluded.label, people.label),
                 metadata_json=CASE WHEN excluded.metadata_json='{}'
                   THEN people.metadata_json ELSE excluded.metadata_json END""",
            (person_id, label, _now(), json.dumps(metadata or {}, sort_keys=True)),
        )
        self.connection.commit()
        return person_id

    def _require_person(self, person_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM people WHERE person_id=?", (person_id,)).fetchone() is None:
            raise ValueError(f"unknown person: {person_id}")

    def list_people(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM people ORDER BY created_at, person_id")]

    def identity_conflicts(self, person_id: str | None = None) -> list[dict[str, Any]]:
        """Return conflicts that still involve currently active assertions."""
        rows = self.connection.execute("SELECT * FROM identity_conflicts ORDER BY conflict_id").fetchall()
        result: list[dict[str, Any]] = []
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
            if conflict_kind == "multiple_positive_identities":
                relevant = len({person for person, kind in kinds if kind == "positive"}) > 1
            elif conflict_kind == "positive_and_negative_same_person":
                relevant = any(kind == "positive" for _, kind in kinds) and any(kind == "negative" for _, kind in kinds)
            elif conflict_kind == "duplicate_exact_identity_evidence":
                relevant = len(active) > 1
            else:
                relevant = bool(active)
            if relevant:
                result.append(dict(row))
        return result

    def add_identity_assertion(
        self,
        person_id: str,
        *,
        media_id: str,
        face_id: str | None = None,
        batch_id: str | None = None,
        embedding: Sequence[float] | None = None,
        assertion_kind: str = "positive",
        assertion_id: str | None = None,
        captured_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if assertion_kind not in {"positive", "negative"}:
            raise ValueError("assertion kind must be positive or negative")
        self._require_person(person_id)
        assertion_id = assertion_id or str(uuid.uuid4())
        if face_id is not None:
            active = self.connection.execute(
                """SELECT a.assertion_id FROM face_identity_assertions a
                   WHERE a.face_id=? AND a.person_id=? AND a.assertion_kind=?
                     AND (SELECT e.event FROM face_identity_assertion_events e
                          WHERE e.assertion_id=a.assertion_id ORDER BY e.event_id DESC LIMIT 1)='active'
                   LIMIT 1""",
                (face_id, person_id, assertion_kind),
            ).fetchone()
            if active is not None:
                return str(active["assertion_id"])
            if assertion_kind == "positive":
                other = self.connection.execute(
                    """SELECT a.person_id FROM face_identity_assertions a
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
        self.connection.execute(
            """INSERT INTO face_identity_assertions
               (assertion_id,person_id,photo_id,face_id,assertion_kind,batch_id,captured_at,
                embedding_json,created_at,metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                assertion_id,
                person_id,
                media_id,
                face_id,
                assertion_kind,
                batch_id,
                captured_at,
                _vector(embedding) if embedding is not None else None,
                _now(),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        self.connection.execute(
            """INSERT INTO face_identity_assertion_events(
               assertion_id,event,created_at,metadata_json) VALUES (?, 'active', ?, '{}')""",
            (assertion_id, _now()),
        )
        self.connection.commit()
        return assertion_id

    def list_identity_assertions(
        self, person_id: str, *, assertion_kind: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        query = """SELECT a.*, a.assertion_id AS reference_id, a.photo_id AS media_id,
                          a.assertion_kind AS kind
                   FROM face_identity_assertions a WHERE a.person_id = ?"""
        params: list[Any] = [person_id]
        if assertion_kind is not None:
            query += " AND a.assertion_kind = ?"
            params.append(assertion_kind)
        if active_only:
            query += """ AND (SELECT e.event FROM face_identity_assertion_events e
                         WHERE e.assertion_id=a.assertion_id ORDER BY e.event_id DESC LIMIT 1) = 'active'"""
        query += " ORDER BY a.created_at, a.assertion_id"
        return [
            dict(row) | {"embedding": _decode_vector(row["embedding_json"])}
            for row in self.connection.execute(query, params)
        ]

    def retire_identity_assertion(self, assertion_id: str) -> None:
        self.connection.execute(
            """INSERT INTO face_identity_assertion_events(
               assertion_id,event,created_at,metadata_json) VALUES (?, 'retired', ?, '{}')""",
            (assertion_id, _now()),
        )
        self.connection.commit()

    def add_appearance_reference(
        self,
        person_id: str,
        *,
        media_id: str,
        batch_id: str,
        feature: Sequence[float],
        person_box_id: str | None = None,
        reference_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._require_person(person_id)
        reference_id = reference_id or str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO appearance_references
               (reference_id,person_id,photo_id,person_box_id,batch_id,feature_json,created_at,metadata_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                reference_id,
                person_id,
                media_id,
                person_box_id,
                batch_id,
                _vector(feature),
                _now(),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        self.connection.execute(
            "INSERT INTO appearance_reference_events(reference_id,event,created_at) VALUES (?, 'active', ?)",
            (reference_id, _now()),
        )
        self.connection.commit()
        return reference_id

    def list_appearance_references(self, person_id: str, *, batch_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT r.*, r.photo_id AS media_id,
                      r.person_box_id AS appearance_subject_id
               FROM appearance_references r
               WHERE r.person_id = ? AND r.batch_id = ?
                 AND (SELECT e.event FROM appearance_reference_events e
                      WHERE e.reference_id=r.reference_id ORDER BY e.event_id DESC LIMIT 1) = 'active'
               ORDER BY r.created_at""",
            (person_id, batch_id),
        )
        return [dict(row) | {"feature": tuple(json.loads(row["feature_json"]))} for row in rows]

    def save_analysis(self, result: AnalysisResult, *, analysis_run_id: str | None = None) -> None:
        self.connection.execute(
            """INSERT INTO analysis_results(photo_id,batch_id,analyzer_version,result_json,analysis_run_id,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                result.media_id,
                result.batch_id,
                result.analyzer_version,
                json.dumps(result.to_dict(), sort_keys=True),
                analysis_run_id,
                _now(),
            ),
        )
        self.connection.commit()

    def save_scores(self, person_id: str, scores: Sequence[CandidateScore], *, run_id: str | None = None) -> str:
        self._require_person(person_id)
        run_id = run_id or str(uuid.uuid4())
        now = _now()
        self.connection.executemany(
            """INSERT INTO candidate_scores(run_id,person_id,photo_id,batch_id,score,score_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    run_id,
                    person_id,
                    score.media_id,
                    score.batch_id,
                    score.score,
                    json.dumps(score.as_dict(), sort_keys=True),
                    now,
                )
                for score in scores
            ],
        )
        self.connection.commit()
        return run_id

    def add_decision(
        self,
        person_id: str,
        media_id: str,
        decision: str,
        *,
        batch_id: str | None = None,
        actor: str = "user",
        evidence: dict[str, Any] | None = None,
        analysis_run_id: str | None = None,
    ) -> str:
        if decision not in {"accept", "reject", "unsure"}:
            raise ValueError("decision must be accept, reject, or unsure")
        self._require_person(person_id)
        cursor = self.connection.execute(
            """INSERT INTO decisions(
                   person_id,photo_id,batch_id,decision,actor,evidence_json,analysis_run_id,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                person_id,
                media_id,
                batch_id,
                decision,
                actor,
                json.dumps(evidence or {}, sort_keys=True),
                analysis_run_id,
                _now(),
            ),
        )
        self.connection.commit()
        return str(cursor.lastrowid)

    def decision_history(self, person_id: str, media_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT d.*, d.photo_id AS media_id FROM decisions d WHERE d.person_id = ?"
        args: list[Any] = [person_id]
        if media_id is not None:
            query += " AND d.photo_id = ?"
            args.append(media_id)
        query += " ORDER BY d.decision_id"
        return [
            dict(row) | {"evidence": json.loads(row["evidence_json"] or "{}")}
            for row in self.connection.execute(query, args)
        ]

    def latest_decisions(self, person_id: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.decision_history(person_id):
            latest[row["media_id"]] = row
        return latest
