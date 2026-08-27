"""Person-aware ranking service for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .models import AnalysisResult
from .scoring import CandidateScore, ScoreWeights, rank_candidates, score_candidate


class ReferenceStore(Protocol):
    def list_identity_assertions(
        self, person_id: str, *, assertion_kind: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    def list_people(self) -> list[Mapping[str, Any]]: ...

    def identity_conflicts(self, person_id: str | None = None) -> list[Mapping[str, Any]]: ...

    def list_appearance_references(self, person_id: str, *, batch_id: str) -> list[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class CandidateEvidence:
    """Non-image evidence supplied by importers or a future context analyzer."""

    duplicate: float = 0.0
    temporal: float = 0.0
    context: float = 0.0
    provider_hint: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _rows(store: ReferenceStore, person_id: str, kind: str) -> list[Mapping[str, Any]]:
    """Read canonical person identity assertions."""
    return list(store.list_identity_assertions(person_id, assertion_kind=kind))


def _other_positive_rows(store: ReferenceStore, person_id: str) -> list[Mapping[str, Any]]:
    """Return other people's positives as derived negatives for this search."""
    rows: list[Mapping[str, Any]] = []
    for person in store.list_people():
        other_id = str(person["person_id"])
        if other_id and other_id != person_id:
            rows.extend(_rows(store, other_id, "positive"))
    return rows


def rank_for_person(
    person_id: str,
    results: Sequence[AnalysisResult],
    store: ReferenceStore,
    *,
    evidence: Mapping[str, CandidateEvidence] | None = None,
    weights: ScoreWeights | None = None,
) -> list[CandidateScore]:
    """Rank results using persistent face refs and current-batch appearance refs.

    Identity assertions intentionally have no batch filter: they are the person's
    long-lived identity evidence. Appearance references are fetched per result
    batch, preventing an outfit from leaking into future or unrelated batches.
    """
    conflicts = store.identity_conflicts(person_id)
    if conflicts:
        conflict_ids = ", ".join(str(row["conflict_id"]) for row in conflicts)
        raise ValueError(f"cannot rank person {person_id}: identity conflicts require reconciliation ({conflict_ids})")
    positive_rows = _rows(store, person_id, "positive")
    explicit_negative_rows = _rows(store, person_id, "negative")
    derived_negative_rows = _other_positive_rows(store, person_id)
    positives = [
        (str(row.get("assertion_id", row.get("reference_id"))), row["embedding"])
        for row in positive_rows
        if row.get("embedding") is not None
    ]
    negatives = [
        (str(row.get("assertion_id", row.get("reference_id"))), row["embedding"])
        for row in [*explicit_negative_rows, *derived_negative_rows]
        if row.get("embedding") is not None
    ]
    appearance_by_batch: dict[str, list[Sequence[float]]] = {}
    for result in results:
        if result.batch_id not in appearance_by_batch:
            appearance_by_batch[result.batch_id] = [
                row["feature"] for row in store.list_appearance_references(person_id, batch_id=result.batch_id)
            ]
    ranked: list[CandidateScore] = []
    for result in results:
        extra = (evidence or {}).get(result.media_id, CandidateEvidence())
        ranked.append(
            score_candidate(
                media_id=result.media_id,
                batch_id=result.batch_id,
                faces=result.faces,
                appearances=result.appearances,
                positive_face_references=positives,
                negative_face_references=negatives,
                appearance_references=appearance_by_batch[result.batch_id],
                duplicate=extra.duplicate,
                temporal=extra.temporal,
                context=extra.context,
                provider_hint=extra.provider_hint,
                weights=weights,
                metadata=extra.metadata,
            )
        )
    return rank_candidates(ranked)
