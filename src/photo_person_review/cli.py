"""Agent-friendly CLI: JSON is stdout, diagnostics are stderr."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated

import typer

from .analysis import (
    PINNED_MODELS,
    CatalogAnalysisRepository,
    FaceObservation,
    ModelManager,
    OpenCVAnalyzer,
    rank_candidates,
    score_candidate,
)
from .config import CatalogConfig
from .db import Catalog
from .exporters import catalog_rows, write_csv, write_json
from .importers import CatalogImportRepository, FolderImporter, VidigamiAdapter
from .review import ReviewMedia, ReviewStore, build_review_packet

app = typer.Typer(help="Local-first photo catalog and person-review state.", no_args_is_help=True)
target_app = typer.Typer(help="Manage persistent person targets.")
tag_app = typer.Typer(help="Manage metadata tag definitions.")
review_app = typer.Typer(help="Build disposable visual packets for conversational review.")
models_app = typer.Typer(help="Install and inspect pinned local analysis models.")
app.add_typer(target_app, name="target")
app.add_typer(tag_app, name="tag")
app.add_typer(review_app, name="review")
app.add_typer(models_app, name="models")


def _config(workspace: Path | None) -> CatalogConfig:
    config = CatalogConfig.from_workspace(workspace)
    config.ensure_workspace()
    return config


def _emit(payload: object) -> None:
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _catalog(workspace: Path | None) -> tuple[CatalogConfig, Catalog]:
    config = _config(workspace)
    return config, Catalog(config.database_path)


@app.command()
def init(workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None) -> None:
    """Create a private catalog workspace and apply schema migrations."""
    config = _config(workspace)
    with Catalog(config.database_path) as catalog:
        _emit(
            {
                "workspace": str(config.workspace),
                "database": str(config.database_path),
                "schema_version": 1,
                "counts": catalog.counts(),
            }
        )


@app.command()
def status(workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None) -> None:
    """Print catalog schema and append-only record counts as JSON."""
    config = _config(workspace)
    with Catalog(config.database_path) as catalog:
        version = int(catalog.connection.execute("SELECT version FROM schema_version").fetchone()[0])
        _emit(
            {
                "workspace": str(config.workspace),
                "database": str(config.database_path),
                "database_bytes": config.database_path.stat().st_size,
                "schema_version": version,
                "counts": catalog.counts(),
            }
        )


@target_app.command("create")
def target_create(
    target_id: str,
    label: Annotated[str | None, typer.Option("--label")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Create a reusable person target."""
    config = _config(workspace)
    with Catalog(config.database_path) as catalog:
        _emit({"target_id": catalog.create_target(target_id, label), "label": label})


@target_app.command("list")
def target_list(
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """List targets as JSON."""
    config = _config(workspace)
    with Catalog(config.database_path) as catalog:
        rows = catalog.connection.execute(
            "SELECT target_id,label,created_at FROM targets ORDER BY created_at"
        ).fetchall()
        _emit([dict(row) for row in rows])


@target_app.command("reference-add")
def target_reference_add(
    target_id: str,
    photo_id: str,
    face_id: Annotated[str, typer.Option("--face")],
    kind: Annotated[str, typer.Option("--kind")] = "positive",
    batch_id: Annotated[str | None, typer.Option("--batch")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Promote one analyzed face to a persistent positive or hard-negative reference."""

    _, catalog = _catalog(workspace)
    try:
        row = catalog.connection.execute(
            """SELECT vector_json FROM numeric_features
               WHERE photo_id=? AND subject_id=? AND feature_kind='face_embedding'
               ORDER BY feature_id DESC LIMIT 1""",
            (photo_id, face_id),
        ).fetchone()
        if row is None:
            raise typer.BadParameter("the requested analyzed face embedding does not exist")
        embedding = tuple(float(value) for value in json.loads(row["vector_json"]))
        photo_row = catalog.connection.execute(
            "SELECT capture_time FROM photos WHERE photo_id=?", (photo_id,)
        ).fetchone()
        with ReviewStore(catalog.connection) as review:
            reference_id = review.add_reference(
                target_id,
                media_id=photo_id,
                face_id=face_id,
                batch_id=batch_id,
                embedding=embedding,
                kind=kind,
                captured_at=str(photo_row["capture_time"])
                if photo_row is not None and photo_row["capture_time"]
                else None,
            )
        _emit(
            {
                "reference_id": reference_id,
                "target_id": target_id,
                "photo_id": photo_id,
                "face_id": face_id,
                "kind": kind,
            }
        )
    finally:
        catalog.close()


@target_app.command("references")
def target_references(
    target_id: str,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """List active persistent face references without exposing embeddings."""

    _, catalog = _catalog(workspace)
    try:
        with ReviewStore(catalog.connection) as review:
            rows = review.list_references(target_id, kind=kind)
        for row in rows:
            row.pop("embedding", None)
            row.pop("embedding_json", None)
        _emit(rows)
    finally:
        catalog.close()


@tag_app.command("define")
def tag_define(
    name: str,
    description: Annotated[str | None, typer.Option("--description")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Define a durable tag without assigning it to a photo."""
    config = _config(workspace)
    with Catalog(config.database_path) as catalog:
        tag_id = catalog.create_tag(name, description)
        _emit({"tag_id": tag_id, "name": name, "description": description})


@models_app.command("install")
def models_install(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Explicitly download and checksum pinned local face models."""

    config = _config(workspace)
    manager = ModelManager(config.models_dir)
    installed = manager.install_all(names)
    _emit({"models": {name: str(path.resolve()) for name, path in installed.items()}})


@models_app.command("status")
def models_status(
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Report pinned model installation state without making network calls."""

    config = _config(workspace)
    manager = ModelManager(config.models_dir)
    _emit(
        {
            model.name: {
                "installed": manager.is_installed(model.name),
                "path": str(manager.path(model.name)),
                "sha256": model.sha256,
                "license": model.license,
            }
            for model in manager.available()
        }
    )


@app.command("analyze")
def analyze(
    batch_id: Annotated[str, typer.Option("--batch")],
    new_only: Annotated[bool, typer.Option("--new/--all")] = True,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Run local YuNet/SFace analysis and append numeric evidence to the catalog."""

    config, catalog = _catalog(workspace)
    try:
        manager = ModelManager(config.models_dir)
        version = f"yunet:{PINNED_MODELS['yunet'].sha256[:12]}+sface:{PINNED_MODELS['sface'].sha256[:12]}+score:0.85"
        analyzer = OpenCVAnalyzer(
            face_model=manager.path("yunet"),
            recognition_model=manager.path("sface"),
            analyzer_version=version,
        )
        repository = CatalogAnalysisRepository(catalog)
        records = repository.batch_photo_records(
            batch_id,
            analyzed=False if new_only else None,
            analyzer_version=version if new_only else None,
        )
        results = []
        errors: list[dict[str, str]] = []
        for record in records:
            source_path = record.get("source_path")
            if not source_path:
                errors.append({"photo_id": str(record["photo_id"]), "error": "source missing"})
                continue
            try:
                results.append(
                    analyzer.analyze(
                        str(record["photo_id"]),
                        Path(str(source_path)),
                        batch_id=batch_id,
                    )
                )
            except Exception as exc:
                errors.append(
                    {
                        "photo_id": str(record["photo_id"]),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        run_id = repository.save_results(results, model=version) if results else None
        _emit(
            {
                "batch_id": batch_id,
                "analysis_run_id": run_id,
                "analyzer_version": version,
                "analyzed": len(results),
                "faces": sum(len(result.faces) for result in results),
                "errors": errors,
            }
        )
        if errors:
            raise typer.Exit(1)
    finally:
        catalog.close()


@app.command("rank")
def rank(
    target_id: Annotated[str, typer.Option("--target")],
    batch_id: Annotated[str, typer.Option("--batch")],
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Rank a batch from persistent approved face references; never create decisions."""

    _, catalog = _catalog(workspace)
    try:
        repository = CatalogAnalysisRepository(catalog)
        with ReviewStore(catalog.connection) as review:
            positive = [
                (str(row["reference_id"]), tuple(row["embedding"]))
                for row in review.list_references(target_id, kind="positive")
                if row.get("embedding")
            ]
            negative = [
                (str(row["reference_id"]), tuple(row["embedding"]))
                for row in review.list_references(target_id, kind="negative")
                if row.get("embedding")
            ]
            face_rows = repository.latest_face_observations(batch_id)
            faces: dict[str, list[FaceObservation]] = {}
            for row in face_rows:
                feature = catalog.connection.execute(
                    """SELECT vector_json FROM numeric_features
                       WHERE subject_id=? AND feature_kind='face_embedding'
                       ORDER BY feature_id DESC LIMIT 1""",
                    (row["face_id"],),
                ).fetchone()
                embedding = (
                    tuple(float(value) for value in json.loads(feature["vector_json"])) if feature is not None else None
                )
                faces.setdefault(str(row["photo_id"]), []).append(
                    FaceObservation(
                        media_id=str(row["photo_id"]),
                        face_id=str(row["face_id"]),
                        bbox=(
                            round(float(row["x"])),
                            round(float(row["y"])),
                            round(float(row["width"])),
                            round(float(row["height"])),
                        ),
                        quality=float(row["quality"] or 0.0),
                        embedding=embedding,
                        detector_version="persisted",
                    )
                )
            scores = rank_candidates(
                score_candidate(
                    media_id=str(record["photo_id"]),
                    batch_id=batch_id,
                    faces=faces.get(str(record["photo_id"]), ()),
                    positive_face_references=positive,
                    negative_face_references=negative,
                )
                for record in repository.batch_photo_records(batch_id)
            )
            run_id = review.save_scores(target_id, scores)
        _emit(
            {
                "run_id": run_id,
                "target_id": target_id,
                "batch_id": batch_id,
                "positive_references": len(positive),
                "negative_references": len(negative),
                "candidates": [score.as_dict() for score in scores],
            }
        )
    finally:
        catalog.close()


@app.command("import")
def import_photos(
    source: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    manifest: Annotated[Path | None, typer.Option("--manifest", exists=True, dir_okay=False)] = None,
    source_type: Annotated[str, typer.Option("--source-type")] = "folder",
    source_id: Annotated[str | None, typer.Option("--source-id")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Append one read-only folder or Vidigami archive observation run."""

    _, catalog = _catalog(workspace)
    try:
        repository = CatalogImportRepository(catalog)
        if source_type == "folder":
            result = FolderImporter(repository).import_folder(
                source,
                source_id=source_id,
                manifest=manifest,
            )
        elif source_type == "vidigami":
            if manifest is None:
                raise typer.BadParameter("--manifest is required for --source-type vidigami")
            result = VidigamiAdapter(repository).import_archive(
                source,
                manifest,
                source_id=source_id or "vidigami",
            )
        else:
            raise typer.BadParameter("--source-type must be folder or vidigami")
        _emit(result.to_dict())
    finally:
        catalog.close()


@app.command("batches")
def list_batches(
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """List immutable import batches and their photo counts."""

    _, catalog = _catalog(workspace)
    try:
        rows = catalog.connection.execute(
            """SELECT b.batch_id,b.label,b.capture_date,b.created_at,COUNT(bp.photo_id) photo_count
               FROM batches b LEFT JOIN batch_photos bp ON bp.batch_id=b.batch_id
               GROUP BY b.batch_id ORDER BY b.created_at,b.batch_id"""
        ).fetchall()
        _emit([dict(row) for row in rows])
    finally:
        catalog.close()


@app.command("decide")
def decide(
    target_id: str,
    decision: str,
    photo_ids: Annotated[list[str], typer.Argument()],
    actor: Annotated[str, typer.Option("--actor")] = "user",
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Append an accept, reject, or unsure event for one or more photos."""

    _, catalog = _catalog(workspace)
    try:
        tag_name = f"person:{target_id}:presence"
        tag_row = catalog.connection.execute("SELECT tag_id FROM tag_definitions WHERE name=?", (tag_name,)).fetchone()
        tag_id = (
            str(tag_row["tag_id"])
            if tag_row is not None
            else catalog.create_tag(
                tag_name,
                "Append-only person-presence review outcome; the latest assignment is current.",
            )
        )
        decision_ids: list[int] = []
        assignment_ids: list[int] = []
        for photo_id in photo_ids:
            decision_ids.append(catalog.record_decision(target_id, photo_id, decision, actor=actor))
            assignment_ids.append(
                catalog.assign_tag(
                    photo_id,
                    tag_id,
                    provenance=actor,
                    value=decision,
                    target_id=target_id,
                )
            )
        _emit(
            {
                "target_id": target_id,
                "decision": decision,
                "actor": actor,
                "photo_ids": photo_ids,
                "decision_ids": decision_ids,
                "tag_assignment_ids": assignment_ids,
            }
        )
    finally:
        catalog.close()


@app.command("export")
def export_catalog(
    output: Annotated[Path, typer.Option("--output")],
    format: Annotated[str, typer.Option("--format")] = "json",
    target_id: Annotated[str | None, typer.Option("--target")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Atomically export the current photo, metadata, tag, and decision view."""

    _, catalog = _catalog(workspace)
    try:
        rows = catalog_rows(catalog.connection, target_id=target_id)
        if format == "json":
            written = write_json(output, rows)
        elif format == "csv":
            written = write_csv(output, rows)
        else:
            raise typer.BadParameter("--format must be json or csv")
        _emit({"path": str(written.resolve()), "format": format, "row_count": len(rows)})
    finally:
        catalog.close()


def _packet_media(
    catalog: Catalog,
    batch_id: str,
    target_id: str,
    limit: int,
    strategy: str,
) -> list[ReviewMedia]:
    decision_filters = {
        "reference-seeding": "latest_decision IS NULL",
        "likely": "latest_decision IS NULL",
        "no-face": "latest_decision IS NULL",
        "cluster": "latest_decision IS NULL",
        "uncertain": "latest_decision = 'unsure'",
        "audit-positive": "latest_decision = 'accept'",
        "audit-negative": "latest_decision = 'reject'",
    }
    if strategy not in decision_filters:
        choices = ", ".join(sorted(decision_filters))
        raise typer.BadParameter(f"--strategy must be one of: {choices}")
    extra_filter = " AND COALESCE(face_count,0)=0" if strategy == "no-face" else ""
    order_by = {
        "reference-seeding": "CASE WHEN face_count BETWEEN 1 AND 4 THEN 0 ELSE 1 END,face_seed_score DESC",
        "likely": "COALESCE(latest_score,face_seed_score,0) DESC",
        "no-face": "capture_time",
        "cluster": "COALESCE(latest_score,face_seed_score,0) DESC",
        "uncertain": "ABS(COALESCE(latest_score,0.5)-0.5)",
        "audit-positive": "COALESCE(latest_score,0) ASC",
        "audit-negative": "COALESCE(latest_score,0) DESC",
    }[strategy]
    rows = catalog.connection.execute(
        f"""WITH candidates AS (
               SELECT p.photo_id,p.capture_time,sf.path,
                      (SELECT d.decision FROM decisions d
                       WHERE d.target_id=? AND d.photo_id=p.photo_id
                       ORDER BY d.decision_id DESC LIMIT 1) latest_decision,
                      (SELECT cs.score FROM candidate_scores cs
                       WHERE cs.target_id=? AND cs.photo_id=p.photo_id AND cs.batch_id=bp.batch_id
                       ORDER BY cs.score_id DESC LIMIT 1) latest_score,
                      (SELECT MAX(f.quality * f.width * f.height /
                                      MAX(1.0, p.width * p.height))
                       FROM faces f WHERE f.photo_id=p.photo_id AND f.analysis_run_id=(
                           SELECT ar.analysis_run_id FROM analysis_results ar
                           WHERE ar.photo_id=p.photo_id AND ar.batch_id=bp.batch_id
                           ORDER BY ar.result_id DESC LIMIT 1
                       )) face_seed_score,
                      (SELECT COUNT(*) FROM faces f WHERE f.photo_id=p.photo_id
                       AND f.analysis_run_id=(
                           SELECT ar.analysis_run_id FROM analysis_results ar
                           WHERE ar.photo_id=p.photo_id AND ar.batch_id=bp.batch_id
                           ORDER BY ar.result_id DESC LIMIT 1
                       )) face_count
               FROM batch_photos bp
               JOIN photos p ON p.photo_id=bp.photo_id
               JOIN source_files sf ON sf.photo_id=p.photo_id
               WHERE bp.batch_id=?
             AND sf.source_file_id=(SELECT MAX(sf2.source_file_id) FROM source_files sf2
                                      WHERE sf2.photo_id=p.photo_id)
             AND sf.observation_state IN ('present','replaced')
           ) SELECT photo_id,capture_time,path FROM candidates
             WHERE {decision_filters[strategy]}{extra_filter}
             ORDER BY {order_by},photo_id LIMIT ?""",
        (target_id, target_id, batch_id, limit),
    ).fetchall()
    from datetime import datetime

    return [
        ReviewMedia(
            media_id=str(row["photo_id"]),
            path=Path(str(row["path"])),
            capture_time=datetime.fromisoformat(row["capture_time"]) if row["capture_time"] else None,
        )
        for row in rows
    ]


@review_app.command("packet")
def review_packet(
    target_id: Annotated[str, typer.Option("--target")],
    batch_id: Annotated[str, typer.Option("--batch")],
    strategy: Annotated[str, typer.Option("--strategy")] = "reference-seeding",
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 12,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
) -> None:
    """Render a disposable contact sheet and JSON label map."""

    _, catalog = _catalog(workspace)
    try:
        media = _packet_media(catalog, batch_id, target_id, limit, strategy)
        repository = CatalogAnalysisRepository(catalog)
        face_rows = repository.latest_face_observations(
            batch_id,
            photo_ids=[item.media_id for item in media],
        )
        faces: dict[str, list[FaceObservation]] = {}
        for row in face_rows:
            faces.setdefault(str(row["photo_id"]), []).append(
                FaceObservation(
                    media_id=str(row["photo_id"]),
                    face_id=str(row["face_id"]),
                    bbox=(
                        round(float(row["x"])),
                        round(float(row["y"])),
                        round(float(row["width"])),
                        round(float(row["height"])),
                    ),
                    quality=float(row["quality"] or 0.0),
                    detector_version="persisted",
                )
            )
        destination = output or Path(tempfile.mkdtemp(prefix="ppr-review-"))
        packet_path = build_review_packet(
            media,
            output_dir=destination,
            faces=faces,
            strategy=strategy,
        )
        payload = json.loads(packet_path.read_text(encoding="utf-8"))
        _emit(
            {
                "packet_path": str(packet_path.resolve()),
                "contact_sheet": str((packet_path.parent / payload["contact_sheet"]).resolve()),
                "count": len(payload["visible"]),
                "packet": payload,
            }
        )
    finally:
        catalog.close()


if __name__ == "__main__":
    app()
