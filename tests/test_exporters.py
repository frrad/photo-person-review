import csv
import json
import os
from pathlib import Path

from photo_person_review.db import Catalog
from photo_person_review.exporters import catalog_rows, write_csv, write_json, write_symlinks
from photo_person_review.review import ReviewStore


def test_export_contains_current_metadata_tags_and_decision_without_photo_bytes(
    tmp_path: Path,
) -> None:
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source = catalog.create_source("folder")
        run = catalog.create_import_run(source)
        photo_id = catalog.upsert_photo("a" * 64, width=20, height=10)
        catalog.observe_source_file(source, photo_id, "/source/a.jpg", import_run_id=run)
        catalog.add_metadata(photo_id, "camera", "old", "test", run)
        catalog.add_metadata(photo_id, "camera", "new", "test", run)
        target_id = catalog.create_target("person")
        tag_id = catalog.create_tag("contains-person")
        catalog.assign_tag(photo_id, tag_id, provenance="user", target_id=target_id)
        catalog.record_decision(target_id, photo_id, "accept")
        rows = catalog_rows(catalog.connection, target_id=target_id)

    assert rows[0]["metadata"]["camera"]["value"] == "new"
    assert rows[0]["tags"][0]["name"] == "contains-person"
    assert rows[0]["decision"]["decision"] == "accept"
    assert "bytes" not in json.dumps(rows[0]).lower()

    json_path = write_json(tmp_path / "out.json", rows)
    csv_path = write_csv(tmp_path / "out.csv", rows)
    assert json.loads(json_path.read_text(encoding="utf-8"))[0]["photo_id"] == photo_id
    with csv_path.open(encoding="utf-8", newline="") as stream:
        assert next(csv.DictReader(stream))["photo_id"] == photo_id


def test_symlink_export_unions_current_positive_references_and_accepts(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    first_source = source_dir / "first.jpg"
    accepted_source = source_dir / "accepted.PNG"
    rejected_source = source_dir / "rejected.jpg"
    for source in (first_source, accepted_source, rejected_source):
        source.write_bytes(b"placeholder")

    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        first_id = catalog.upsert_photo("a" * 64)
        accepted_id = catalog.upsert_photo("b" * 64)
        rejected_id = catalog.upsert_photo("c" * 64)
        for photo_id, path in (
            (first_id, first_source),
            (accepted_id, accepted_source),
            (rejected_id, rejected_source),
        ):
            catalog.observe_source_file(source_id, photo_id, str(path), import_run_id=run_id)
        target_id = catalog.create_target("chloe")
        with ReviewStore(catalog.connection) as review:
            review.add_reference(target_id, media_id=first_id, kind="positive")
            review.add_reference(target_id, media_id=rejected_id, kind="positive")
        catalog.record_decision(target_id, accepted_id, "accept")
        catalog.record_decision(target_id, rejected_id, "reject")
        destination = tmp_path / "export"

        result = write_symlinks(catalog.connection, target_id, destination)

    assert result["row_count"] == 2
    assert result["created_count"] == 2
    assert (destination / f"{first_id}.jpg").resolve() == first_source.resolve()
    assert (destination / f"{accepted_id}.png").resolve() == accepted_source.resolve()
    assert not (destination / f"{rejected_id}.jpg").exists()
    manifest = json.loads((destination / ".ppr-symlink-export.json").read_text(encoding="utf-8"))
    assert manifest["target_id"] == "chloe"
    assert sorted(manifest["managed_names"]) == sorted((f"{first_id}.jpg", f"{accepted_id}.png"))


def test_symlink_export_reconciles_only_managed_links_and_preserves_collisions(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"placeholder")
    png_source = tmp_path / "source.png"
    png_source.write_bytes(b"placeholder")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = catalog.upsert_photo("d" * 64)
        stale_id = catalog.upsert_photo("e" * 64)
        collision_id = catalog.upsert_photo("a" * 64)
        catalog.observe_source_file(source_id, photo_id, str(source), import_run_id=run_id)
        catalog.observe_source_file(source_id, stale_id, str(source), import_run_id=run_id)
        catalog.observe_source_file(source_id, collision_id, str(png_source), import_run_id=run_id)
        target_id = catalog.create_target("chloe")
        catalog.record_decision(target_id, photo_id, "accept")
        catalog.record_decision(target_id, stale_id, "accept")
        catalog.record_decision(target_id, collision_id, "accept")
        destination = tmp_path / "export"
        destination.mkdir()
        arbitrary_link = destination / "keep-link"
        os.symlink(source, arbitrary_link)
        unknown_hash_link = destination / f"{'f' * 64}.jpg"
        os.symlink(source, unknown_hash_link)
        unmanaged_desired_link = destination / f"{photo_id}.jpg"
        unmanaged_source = tmp_path / "unmanaged.jpg"
        unmanaged_source.write_bytes(b"unmanaged")
        os.symlink(unmanaged_source, unmanaged_desired_link)
        regular_collision = destination / f"{collision_id}.png"
        regular_collision.write_bytes(b"keep")

        write_symlinks(catalog.connection, target_id, destination)
        catalog.record_decision(target_id, stale_id, "reject")
        result = write_symlinks(catalog.connection, target_id, destination)

        assert not (destination / f"{stale_id}.jpg").exists()
        assert arbitrary_link.is_symlink()
        assert unknown_hash_link.is_symlink()
        assert unmanaged_desired_link.resolve() == unmanaged_source.resolve()
        assert regular_collision.read_bytes() == b"keep"
        assert result["removed_count"] == 1
        assert result["conflict_count"] == 2
        assert {item["reason"] for item in result["conflicts"]} == {
            "regular_file_collision",
            "unmanaged_symlink_collision",
        }
