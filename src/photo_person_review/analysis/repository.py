"""Persistence adapter for local analyzer measurements.

This module is the boundary between an analyzer and the append-only catalog.
Only IDs, boxes, scalar measurements, and numeric vectors cross the boundary;
the analyzer's source path is intentionally not accepted here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from photo_person_review.db import Catalog, utc_now

from .models import AnalysisResult


class CatalogAnalysisRepository:
    """Save and query local-analysis observations in one :class:`Catalog`."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def save_analysis(self, result: AnalysisResult) -> str:
        """Persist one result as a complete analysis run.

        The method implements ``AnalysisRepository`` and is therefore usable
        with :func:`analysis.pipeline.analyze_media`. Each result gets its own
        immutable run, while :meth:`save_results` can group a batch into one
        run when the caller wants a single progress record.
        """
        run_id = self.catalog.create_analysis_run(
            backend="local",
            model=result.analyzer_version,
            batch_id=result.batch_id,
            parameters={"analyzer_version": result.analyzer_version},
        )
        try:
            self._persist_result(result, run_id)
            self.catalog.finish_analysis_run(run_id, "complete", self._summary(result))
        except Exception as exc:
            self.catalog.finish_analysis_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        return run_id

    def save_results(
        self,
        results: Iterable[AnalysisResult],
        *,
        backend: str = "local",
        model: str | None = None,
        parameters: Mapping[str, object] | None = None,
    ) -> str:
        """Persist a set of results as one append-only run."""
        materialized = list(results)
        if not materialized:
            raise ValueError("at least one analysis result is required")
        batch_ids = {result.batch_id for result in materialized}
        if len(batch_ids) != 1:
            raise ValueError("all results in one analysis run must belong to one batch")
        run_id = self.catalog.create_analysis_run(
            backend=backend,
            model=model or materialized[0].analyzer_version,
            batch_id=materialized[0].batch_id,
            parameters=parameters or {"analyzer_versions": sorted({r.analyzer_version for r in materialized})},
        )
        try:
            for result in materialized:
                self._persist_result(result, run_id)
            self.catalog.finish_analysis_run(
                run_id,
                "complete",
                {"results": len(materialized), "photos": [result.media_id for result in materialized]},
            )
        except Exception as exc:
            self.catalog.finish_analysis_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        return run_id

    def _persist_result(self, result: AnalysisResult, run_id: str) -> None:
        self.catalog.connection.execute(
            """INSERT INTO analysis_results
               (photo_id,batch_id,analyzer_version,result_json,analysis_run_id,created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                result.media_id,
                result.batch_id,
                result.analyzer_version,
                json.dumps(result.to_dict(), sort_keys=True),
                run_id,
                utc_now(),
            ),
        )
        self.catalog.connection.commit()
        for face in result.faces:
            stored_face_id = f"{run_id}:{face.face_id}"
            self.catalog.add_face(
                result.media_id,
                run_id,
                face_id=stored_face_id,
                x=face.bbox[0],
                y=face.bbox[1],
                width=face.bbox[2],
                height=face.bbox[3],
                quality=face.quality,
                landmarks=face.landmarks,
                metadata={
                    "detector_version": face.detector_version,
                    "analyzer_face_id": face.face_id,
                },
            )
            if face.embedding is not None:
                self.catalog.add_numeric_feature(
                    result.media_id,
                    run_id,
                    "face_embedding",
                    face.embedding,
                    subject_id=stored_face_id,
                    metadata={"detector_version": face.detector_version},
                )
        for person in result.people:
            self.catalog.add_person_box(
                result.media_id,
                run_id,
                person_box_id=f"{run_id}:{person.person_id}",
                x=person.bbox[0],
                y=person.bbox[1],
                width=person.bbox[2],
                height=person.bbox[3],
                face_id=f"{run_id}:{person.face_id}" if person.face_id else None,
                confidence=person.confidence,
                metadata={
                    "detector_version": person.detector_version,
                    "analyzer_person_id": person.person_id,
                },
            )
        for appearance in result.appearances:
            self.catalog.add_numeric_feature(
                result.media_id,
                run_id,
                "appearance",
                appearance.feature,
                subject_id=appearance.person_id,
                metadata={
                    "extractor_version": appearance.extractor_version,
                    "batch_id": appearance.batch_id,
                    "analyzer_person_id": appearance.person_id,
                },
            )

    @staticmethod
    def _summary(result: AnalysisResult) -> dict[str, object]:
        return {
            "photos": 1,
            "faces": len(result.faces),
            "people": len(result.people),
            "appearances": len(result.appearances),
        }

    def unanalysed_batch_photos(self, batch_id: str, *, analyzer_version: str | None = None) -> list[str]:
        """Return stable IDs in a batch lacking a successful matching result."""
        return [
            str(row["photo_id"])
            for row in self.batch_photo_records(batch_id, analyzed=False, analyzer_version=analyzer_version)
        ]

    # American spelling is useful to CLI and API callers.
    unanalyzed_batch_photos = unanalysed_batch_photos

    def analyzed_batch_photos(self, batch_id: str, *, analyzer_version: str | None = None) -> list[str]:
        return [
            str(row["photo_id"])
            for row in self.batch_photo_records(batch_id, analyzed=True, analyzer_version=analyzer_version)
        ]

    def batch_photo_records(
        self,
        batch_id: str,
        *,
        analyzed: bool | None = None,
        analyzer_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return batch media metadata and the latest existing source ref.

        ``source_path`` comes only from the import observation; this adapter
        never creates or persists a derivative path for a review packet.
        """
        clauses = ["bp.batch_id = ?"]
        params: list[Any] = [batch_id]
        existence = ""
        if analyzed is not None:
            existence = "EXISTS" if analyzed else "NOT EXISTS"
            version_clause = ""
            if analyzer_version is not None:
                version_clause = " AND ar.analyzer_version = ?"
            clauses.append(
                f"{existence} (SELECT 1 FROM analysis_results ar WHERE ar.batch_id=bp.batch_id "
                f"AND ar.photo_id=bp.photo_id{version_clause})"
            )
            if analyzer_version is not None:
                params.append(analyzer_version)
        rows = self.catalog.connection.execute(
            f"""SELECT bp.photo_id, p.sha256, p.capture_time, p.width, p.height, sf.path AS source_path
                FROM batch_photos bp JOIN photos p ON p.photo_id=bp.photo_id
                LEFT JOIN source_files sf ON sf.source_file_id=(
                    SELECT MAX(sf2.source_file_id) FROM source_files sf2
                    WHERE sf2.photo_id=bp.photo_id
                      AND sf2.observation_state IN ('present','replaced')
                ) WHERE {" AND ".join(clauses)} ORDER BY bp.photo_id""",
            params,
        )
        return [dict(row) for row in rows]

    def latest_face_observations(
        self, batch_id: str, *, photo_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return the newest persisted face rows per photo in a batch."""
        params: list[Any] = [batch_id]
        filter_clause = ""
        ids = list(photo_ids or ())
        if ids:
            placeholders = ",".join("?" for _ in ids)
            filter_clause = f" AND f.photo_id IN ({placeholders})"
            params.extend(ids)
        rows = self.catalog.connection.execute(
            f"""WITH latest AS (
                    SELECT ar.photo_id, ar.analysis_run_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY ar.photo_id ORDER BY ar.result_id DESC
                           ) AS row_number
                    FROM analysis_results ar
                    WHERE ar.batch_id=?
                )
                SELECT f.* FROM faces f
                JOIN latest l ON l.analysis_run_id=f.analysis_run_id
                    AND l.photo_id=f.photo_id AND l.row_number=1
                WHERE 1=1{filter_clause}
                ORDER BY f.photo_id, f.face_id""",
            [batch_id, *ids],
        )
        return [dict(row) for row in rows]
