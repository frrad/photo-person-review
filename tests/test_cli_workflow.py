"""End-to-end clean-v2 CLI contract using synthetic images only."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from photo_person_review.analysis import AnalysisResult, CatalogAnalysisRepository, FaceObservation  # noqa: E402
from photo_person_review.cli import _packet_media, _reviewable_faces, app  # noqa: E402
from photo_person_review.db import Catalog  # noqa: E402

runner = CliRunner()


def _write_photo(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (40, 30), color).save(path, format="JPEG", quality=90)


def _json_result(result):
    assert result.exit_code == 0, result.stdout
    return json.loads(result.stdout)


def test_cli_person_identity_review_workflow_is_metadata_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    photo = source / "one.jpg"
    _write_photo(photo, (20, 40, 60))
    workspace = tmp_path / "workspace"
    workspace_arg = ["--workspace", str(workspace)]

    initialized = _json_result(runner.invoke(app, ["init", *workspace_arg]))
    assert initialized["schema_version"] == 2
    first = _json_result(runner.invoke(app, ["import", str(source), "--source-id", "album", *workspace_arg]))
    assert first["counts"]["new"] == 1
    second = _json_result(runner.invoke(app, ["import", str(source), "--source-id", "album", *workspace_arg]))
    assert second["counts"]["unchanged"] == 1
    photo_id = hashlib.sha256(photo.read_bytes()).hexdigest()

    person = _json_result(runner.invoke(app, ["person", "create", "chloe", "--label", "Chloe", *workspace_arg]))
    assert person == {"label": "Chloe", "person_id": "chloe"}
    assert _json_result(runner.invoke(app, ["person", "list", *workspace_arg]))[0]["person_id"] == "chloe"

    with Catalog(workspace / "catalog.sqlite3") as catalog:
        assert catalog.counts()["people"] == 1
        assert catalog.counts()["photos"] == 1
        columns = {row["name"] for row in catalog.connection.execute("PRAGMA table_info(photos)")}
        assert not columns.intersection({"image", "image_bytes", "photo_bytes", "blob"})
        run_id = catalog.create_analysis_run(backend="test", model="test")
        catalog.add_face(photo_id, run_id, face_id="face-1", x=0, y=0, width=10, height=10, quality=1.0)
        catalog.add_numeric_feature(photo_id, run_id, "face_embedding", [1.0, 0.0], subject_id="face-1")

    assigned = _json_result(
        runner.invoke(app, ["identity", "assign", "--person", "chloe", "--face", "face-1", *workspace_arg])
    )
    assert assigned["person_id"] == "chloe" and assigned["assertion_kind"] == "positive"
    excluded = _json_result(
        runner.invoke(app, ["identity", "exclude", "--person", "chloe", "--face", "face-1", *workspace_arg])
    )
    assert excluded["assertion_kind"] == "negative"
    active_assertions = _json_result(
        runner.invoke(app, ["identity", "assertions", "--person", "chloe", *workspace_arg])
    )
    assert {row["assertion_kind"] for row in active_assertions} == {"positive", "negative"}
    history_assertions = _json_result(
        runner.invoke(app, ["identity", "assertions", "--person", "chloe", "--history", *workspace_arg])
    )
    assert len(history_assertions) == 2
    with Catalog(workspace / "catalog.sqlite3") as catalog:
        catalog.connection.execute(
            """INSERT INTO identity_conflicts(
               conflict_id,face_id,person_id,conflict_kind,assertion_ids_json,details_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                "test-conflict",
                "face-1",
                "chloe",
                "positive_and_negative_same_person",
                json.dumps([assigned["assertion_id"], excluded["assertion_id"]]),
                "{}",
                "now",
            ),
        )
        catalog.connection.commit()
    conflicts = _json_result(runner.invoke(app, ["identity", "conflicts", *workspace_arg]))
    assert conflicts[0]["assertion_ids"] == [assigned["assertion_id"], excluded["assertion_id"]]
    assert "ppr identity retire ASSERTION_ID" in conflicts[0]["action"]
    rank_error = runner.invoke(app, ["rank", "--person", "chloe", "--batch", "missing", *workspace_arg])
    assert rank_error.exit_code != 0 and "unresolved identity conflict" in rank_error.output
    retired = _json_result(runner.invoke(app, ["identity", "retire", assigned["assertion_id"], *workspace_arg]))
    assert retired == {"assertion_id": assigned["assertion_id"], "event": "retired"}
    active_after_retire = _json_result(
        runner.invoke(app, ["identity", "assertions", "--person", "chloe", *workspace_arg])
    )
    assert len(active_after_retire) == 1 and active_after_retire[0]["assertion_kind"] == "negative"

    batch_id = _json_result(runner.invoke(app, ["batches", *workspace_arg]))[-1]["batch_id"]
    packet_dir = tmp_path / "packet"
    packet = _json_result(
        runner.invoke(
            app,
            [
                "review",
                "packet",
                "--person",
                "chloe",
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
    assert packet_path == packet_dir / "packet.json" and packet_path.is_file()
    assert Path(packet["contact_sheet"]).is_file()
    assert packet["packet"]["visible"][0]["source_path"] == str(photo.resolve())
    assert not (workspace / "thumbnails").exists()

    note = "That's Chloe turned away from the camera in the bottom-left corner, wearing her jacket."
    decision = _json_result(
        runner.invoke(
            app,
            [
                "decide",
                "--person",
                "chloe",
                "accept",
                photo_id,
                "--actor",
                "user",
                "--note",
                note,
                *workspace_arg,
            ],
        )
    )
    assert decision["person_id"] == "chloe" and "target_id" not in decision
    assert decision["note"] == note
    assert len(decision["decision_ids"]) == 1

    export_path = tmp_path / "export.json"
    exported = _json_result(
        runner.invoke(
            app, ["export", "--person", "chloe", "--output", str(export_path), "--format", "json", *workspace_arg]
        )
    )
    assert exported["row_count"] == 1
    rows = json.loads(export_path.read_text(encoding="utf-8"))
    assert rows[0]["person_id"] == "chloe" and "target_id" not in rows[0]
    assert rows[0]["decision"]["decision"] == "accept"
    assert rows[0]["decision"]["evidence"] == {"note": note}

    hardlink_dir = tmp_path / "chloe-export"
    hardlink_export = _json_result(
        runner.invoke(
            app,
            [
                "export",
                "--person",
                "chloe",
                "--output",
                str(hardlink_dir),
                "--filename-prefix",
                "PPR Chloe Export",
                *workspace_arg,
            ],
        )
    )
    assert hardlink_export["format"] == "hardlinks" and hardlink_export["person_id"] == "chloe"
    assert hardlink_export["created_count"] == 1
    links = list(hardlink_dir.glob(f"ppr_chloe_export_*_{photo_id}.jpg"))
    assert len(links) == 1 and os.path.samefile(links[0], photo)
    manifest = json.loads((hardlink_dir / ".ppr-symlink-export.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 4 and manifest["person_id"] == "chloe" and "target_id" not in manifest


def test_person_packet_selection_prefers_clear_few_face_photos(tmp_path: Path) -> None:
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source = catalog.create_source("folder")
        run = catalog.create_import_run(source)
        batch = catalog.create_batch(source)
        ids = [character * 64 for character in ("a", "b", "c")]
        for photo_id in ids:
            catalog.upsert_photo(photo_id, width=1000, height=1000)
            catalog.observe_source_file(source, photo_id, f"/source/{photo_id[0]}.jpg", import_run_id=run)
            catalog.observe_batch_photo(batch, photo_id, import_run_id=run)
        results = [
            AnalysisResult(ids[0], batch, faces=(FaceObservation(ids[0], "clear", (0, 0, 300, 300), 0.95),)),
            AnalysisResult(
                ids[1],
                batch,
                faces=tuple(FaceObservation(ids[1], f"crowd-{i}", (0, 0, 350, 350), 0.95) for i in range(10)),
            ),
            AnalysisResult(ids[2], batch),
        ]
        CatalogAnalysisRepository(catalog).save_results(results)
        catalog.create_person("person")
        selected = _packet_media(catalog, batch, "person", 3, "reference-seeding")
    assert [item.media_id for item in selected] == ids


def test_rank_face_area_filter_excludes_tiny_4k_detection_and_keeps_low_res_face() -> None:
    tiny = FaceObservation("4k", "tiny", (0, 0, 20, 20), embedding=(1.0, 0.0))
    low_res = FaceObservation("low-res", "large", (0, 0, 20, 20), embedding=(1.0, 0.0))
    assert _reviewable_faces([tiny], photo_width=4000, photo_height=3000, min_face_area_ratio=0.0005) == []
    assert _reviewable_faces([low_res], photo_width=40, photo_height=30, min_face_area_ratio=0.0005) == [low_res]
    assert _reviewable_faces([tiny], photo_width=4000, photo_height=3000, min_face_area_ratio=0.0) == [tiny]
    assert _reviewable_faces([tiny], photo_width=None, photo_height=None, min_face_area_ratio=0.0005) == [tiny]
