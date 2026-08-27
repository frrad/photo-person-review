"""End-to-end CLI contract using synthetic images only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from photo_person_review.analysis import (  # noqa: E402
    AnalysisResult,
    CatalogAnalysisRepository,
    FaceObservation,
)
from photo_person_review.cli import (
    _packet_media,  # noqa: E402
    app,  # noqa: E402
)
from photo_person_review.db import Catalog  # noqa: E402

runner = CliRunner()


def _write_photo(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (40, 30), color).save(path, format="JPEG", quality=90)


def _json_result(result):
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def test_cli_incremental_review_workflow_is_metadata_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    photo = source / "one.jpg"
    _write_photo(photo, (20, 40, 60))
    workspace = tmp_path / "workspace"
    workspace_arg = ["--workspace", str(workspace)]

    initialized = _json_result(runner.invoke(app, ["init", *workspace_arg]))
    assert initialized["counts"]["photos"] == 0
    first = _json_result(runner.invoke(app, ["import", str(source), "--source-id", "album", *workspace_arg]))
    assert first["counts"] == {
        "errors": 0,
        "missing": 0,
        "new": 1,
        "replaced": 0,
        "unchanged": 0,
    }
    second = _json_result(runner.invoke(app, ["import", str(source), "--source-id", "album", *workspace_arg]))
    assert second["counts"]["unchanged"] == 1

    photo_id = hashlib.sha256(photo.read_bytes()).hexdigest()
    with Catalog(workspace / "catalog.sqlite3") as catalog:
        counts = catalog.counts()
        assert counts["photos"] == 1
        assert counts["source_files"] == 2
        assert counts["photo_metadata"] > 1
        columns = {row["name"] for row in catalog.connection.execute("PRAGMA table_info(photos)")}
        assert not columns.intersection({"image", "image_bytes", "photo_bytes", "blob"})

    target = _json_result(
        runner.invoke(
            app,
            ["target", "create", "child", "--label", "Child", *workspace_arg],
        )
    )
    assert target == {"label": "Child", "target_id": "child"}
    tag = _json_result(
        runner.invoke(
            app,
            ["tag", "define", "manual-note", "--description", "test", *workspace_arg],
        )
    )
    assert tag["name"] == "manual-note"

    batches = _json_result(runner.invoke(app, ["batches", *workspace_arg]))
    assert len(batches) == 2
    batch_id = batches[-1]["batch_id"]
    packet_dir = tmp_path / "packet"
    packet = _json_result(
        runner.invoke(
            app,
            [
                "review",
                "packet",
                "--target",
                "child",
                "--batch",
                batch_id,
                "--strategy",
                "likely",
                "--output",
                str(packet_dir),
                *workspace_arg,
            ],
        )
    )
    packet_path = Path(packet["packet_path"])
    contact_sheet = Path(packet["contact_sheet"])
    assert packet_path == packet_dir / "packet.json"
    assert packet_path.is_file()
    assert contact_sheet == packet_dir / "contact-sheet.jpg"
    assert contact_sheet.is_file()
    packet_payload = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet_payload["visible"][0]["source_path"] == str(photo.resolve())
    assert packet_payload["visible"][0]["annotated_path"] == "media/01.jpg"
    assert not (workspace / "thumbnails").exists()

    decision = _json_result(
        runner.invoke(
            app,
            ["decide", "child", "accept", photo_id, "--actor", "user", *workspace_arg],
        )
    )
    assert decision["photo_ids"] == [photo_id]
    assert len(decision["decision_ids"]) == 1
    assert len(decision["tag_assignment_ids"]) == 1

    export_path = tmp_path / "export.json"
    exported = _json_result(
        runner.invoke(
            app,
            [
                "export",
                "--output",
                str(export_path),
                "--format",
                "json",
                "--target",
                "child",
                *workspace_arg,
            ],
        )
    )
    assert exported["row_count"] == 1
    rows = json.loads(export_path.read_text(encoding="utf-8"))
    assert rows[0]["photo_id"] == photo_id
    assert rows[0]["decision"]["decision"] == "accept"
    assert any(tag_row["value"] == "accept" for tag_row in rows[0]["tags"])

    with Catalog(workspace / "catalog.sqlite3") as catalog:
        assert catalog.counts()["photos"] == 1
        assert catalog.counts()["decisions"] == 1
        assert catalog.counts()["tag_assignments"] == 1


def test_reference_seeding_prefers_clear_few_face_photos(tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source = catalog.create_source("folder")
        run = catalog.create_import_run(source)
        batch = catalog.create_batch(source)
        ids = [character * 64 for character in ("a", "b", "c")]
        for photo_id in ids:
            catalog.upsert_photo(photo_id, width=1000, height=1000)
            catalog.observe_source_file(
                source,
                photo_id,
                f"/source/{photo_id[0]}.jpg",
                import_run_id=run,
            )
            catalog.observe_batch_photo(batch, photo_id, import_run_id=run)
        results = [
            AnalysisResult(
                ids[0],
                batch,
                faces=(FaceObservation(ids[0], "clear", (0, 0, 300, 300), 0.95),),
            ),
            AnalysisResult(
                ids[1],
                batch,
                faces=tuple(FaceObservation(ids[1], f"crowd-{index}", (0, 0, 350, 350), 0.95) for index in range(10)),
            ),
            AnalysisResult(ids[2], batch),
        ]
        CatalogAnalysisRepository(catalog).save_results(results)
        catalog.create_target("target")

        selected = _packet_media(catalog, batch, "target", 3, "reference-seeding")

    assert [item.media_id for item in selected] == ids
