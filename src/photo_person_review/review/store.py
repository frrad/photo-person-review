"""Compatibility facade over the single metadata-only :class:`Catalog`.

Review state is deliberately stored in the core catalog. This facade keeps the
analysis/review APIs small while ensuring a workspace never grows a second,
conflicting targets or decisions database.
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

    def create_target(self, target_id: str | None = None, *, label: str | None = None) -> str:
        target_id = target_id or str(uuid.uuid4())
        self.connection.execute(
            "INSERT OR IGNORE INTO targets(target_id, label, created_at, metadata_json) VALUES (?, ?, ?, '{}')",
            (target_id, label, _now()),
        )
        self.connection.commit()
        return target_id

    def add_reference(
        self,
        target_id: str,
        *,
        media_id: str,
        face_id: str | None = None,
        batch_id: str | None = None,
        embedding: Sequence[float] | None = None,
        kind: str = "positive",
        reference_id: str | None = None,
        captured_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if kind not in {"positive", "negative"}:
            raise ValueError("reference kind must be positive or negative")
        self.create_target(target_id)
        reference_id = reference_id or str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO target_references
               (reference_id,target_id,photo_id,face_id,kind,batch_id,captured_at,embedding_json,created_at,metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                reference_id,
                target_id,
                media_id,
                face_id,
                kind,
                batch_id,
                captured_at,
                _vector(embedding) if embedding is not None else None,
                _now(),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        self.connection.execute(
            "INSERT INTO target_reference_events(reference_id,event,created_at) VALUES (?, 'active', ?)",
            (reference_id, _now()),
        )
        self.connection.commit()
        return reference_id

    def list_references(
        self, target_id: str, *, kind: str | None = None, active_only: bool = True
    ) -> list[dict[str, Any]]:
        query = """SELECT r.*, r.photo_id AS media_id FROM target_references r
                   WHERE r.target_id = ?"""
        params: list[Any] = [target_id]
        if kind is not None:
            query += " AND r.kind = ?"
            params.append(kind)
        if active_only:
            query += """ AND (SELECT e.event FROM target_reference_events e
                         WHERE e.reference_id=r.reference_id ORDER BY e.event_id DESC LIMIT 1) = 'active'"""
        query += " ORDER BY r.created_at, r.reference_id"
        return [
            dict(row) | {"embedding": _decode_vector(row["embedding_json"])}
            for row in self.connection.execute(query, params)
        ]

    def retire_reference(self, reference_id: str) -> None:
        self.connection.execute(
            "INSERT INTO target_reference_events(reference_id,event,created_at) VALUES (?, 'retired', ?)",
            (reference_id, _now()),
        )
        self.connection.commit()

    def add_appearance_reference(
        self,
        target_id: str,
        *,
        media_id: str,
        batch_id: str,
        feature: Sequence[float],
        person_id: str | None = None,
        reference_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self.create_target(target_id)
        reference_id = reference_id or str(uuid.uuid4())
        self.connection.execute(
            """INSERT INTO appearance_references
               (reference_id,target_id,photo_id,person_id,batch_id,feature_json,created_at,metadata_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                reference_id,
                target_id,
                media_id,
                person_id,
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

    def list_appearance_references(self, target_id: str, *, batch_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT r.*, r.photo_id AS media_id FROM appearance_references r
               WHERE r.target_id = ? AND r.batch_id = ?
                 AND (SELECT e.event FROM appearance_reference_events e
                      WHERE e.reference_id=r.reference_id ORDER BY e.event_id DESC LIMIT 1) = 'active'
               ORDER BY r.created_at""",
            (target_id, batch_id),
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

    def save_scores(self, target_id: str, scores: Sequence[CandidateScore], *, run_id: str | None = None) -> str:
        self.create_target(target_id)
        run_id = run_id or str(uuid.uuid4())
        now = _now()
        self.connection.executemany(
            """INSERT INTO candidate_scores(run_id,target_id,photo_id,batch_id,score,score_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    run_id,
                    target_id,
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
        target_id: str,
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
        self.create_target(target_id)
        cursor = self.connection.execute(
            """INSERT INTO decisions(
                   target_id,photo_id,batch_id,decision,actor,evidence_json,analysis_run_id,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                target_id,
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

    def decision_history(self, target_id: str, media_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT d.*, d.photo_id AS media_id FROM decisions d WHERE d.target_id = ?"
        args: list[Any] = [target_id]
        if media_id is not None:
            query += " AND d.photo_id = ?"
            args.append(media_id)
        query += " ORDER BY d.decision_id"
        return [
            dict(row) | {"evidence": json.loads(row["evidence_json"] or "{}")}
            for row in self.connection.execute(query, args)
        ]

    def latest_decisions(self, target_id: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.decision_history(target_id):
            latest[row["media_id"]] = row
        return latest
