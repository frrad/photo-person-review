import csv
import errno
import json
import os
from pathlib import Path

from photo_person_review.db import Catalog
from photo_person_review.exporters import catalog_rows, write_csv, write_hardlinks, write_json, write_symlinks
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
        first_id = catalog.upsert_photo("a" * 64, capture_time="2026-08-26T09:23:28")
        accepted_id = catalog.upsert_photo("b" * 64, capture_time="not-a-time")
        rejected_id = catalog.upsert_photo("c" * 64)
        for photo_id, path in (
            (first_id, first_source),
            (accepted_id, accepted_source),
            (rejected_id, rejected_source),
        ):
            catalog.observe_source_file(source_id, photo_id, str(path), import_run_id=run_id)
        target_id = catalog.create_target("chloe", label="Chloé Vidigami")
        with ReviewStore(catalog.connection) as review:
            review.add_reference(target_id, media_id=first_id, kind="positive")
            review.add_reference(target_id, media_id=rejected_id, kind="positive")
        catalog.record_decision(target_id, accepted_id, "accept")
        catalog.record_decision(target_id, rejected_id, "reject")
        destination = tmp_path / "export"

        result = write_symlinks(catalog.connection, target_id, destination)

    assert result["row_count"] == 2
    assert result["created_count"] == 2
    first_name = f"chlo_vidigami_2026-08-26_092328_{first_id}.jpg"
    accepted_name = f"chlo_vidigami_undated_{accepted_id}.png"
    assert (destination / first_name).resolve() == first_source.resolve()
    assert (destination / accepted_name).resolve() == accepted_source.resolve()
    assert not any(path.name.startswith(rejected_id) for path in destination.iterdir())
    manifest = json.loads((destination / ".ppr-symlink-export.json").read_text(encoding="utf-8"))
    assert manifest["target_id"] == "chloe"
    assert manifest["version"] == 2
    assert manifest["filename_prefix"] == "chlo_vidigami"
    assert sorted(manifest["managed_names"]) == sorted((first_name, accepted_name))


def test_symlink_export_migrates_legacy_hash_links_and_honors_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"placeholder")
    photo_id = "a" * 64
    old_name = f"{photo_id}.jpg"
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        catalog.upsert_photo(photo_id, capture_time="2026-08-26T09:23:28")
        catalog.observe_source_file(source_id, photo_id, str(source), import_run_id=run_id)
        target_id = catalog.create_target("chloe", label="Chloe")
        catalog.record_decision(target_id, photo_id, "accept")
        destination = tmp_path / "export"
        destination.mkdir()
        os.symlink(source, destination / old_name)
        (destination / ".ppr-symlink-export.json").write_text(
            json.dumps({"version": 1, "target_id": target_id, "managed_names": [old_name]}) + "\n",
            encoding="utf-8",
        )
        unknown = destination / ("f" * 64 + ".jpg")
        os.symlink(source, unknown)

        result = write_symlinks(catalog.connection, target_id, destination, filename_prefix="PPR Chloe Vidigami")

    new_name = f"ppr_chloe_vidigami_2026-08-26_092328_{photo_id}.jpg"
    assert not (destination / old_name).exists()
    assert (destination / new_name).resolve() == source.resolve()
    assert unknown.is_symlink()
    assert result["removed_count"] == 1
    assert result["filename_prefix"] == "ppr_chloe_vidigami"
    manifest = json.loads((destination / ".ppr-symlink-export.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert manifest["filename_prefix"] == "ppr_chloe_vidigami"
    assert manifest["managed_names"] == [new_name]


def test_symlink_export_reconciles_only_managed_links_and_preserves_collisions(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"placeholder")
    png_source = tmp_path / "source.png"
    png_source.write_bytes(b"placeholder")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = catalog.upsert_photo("d" * 64, capture_time="2026-08-26T09:23:28")
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
        managed_photo_name = f"chloe_2026-08-26_092328_{photo_id}.jpg"
        managed_stale_name = f"chloe_undated_{stale_id}.jpg"
        managed_collision_name = f"chloe_undated_{collision_id}.png"
        unmanaged_desired_link = destination / managed_photo_name
        unmanaged_source = tmp_path / "unmanaged.jpg"
        unmanaged_source.write_bytes(b"unmanaged")
        os.symlink(unmanaged_source, unmanaged_desired_link)
        regular_collision = destination / managed_collision_name
        regular_collision.write_bytes(b"keep")

        write_symlinks(catalog.connection, target_id, destination)
        catalog.record_decision(target_id, stale_id, "reject")
        result = write_symlinks(catalog.connection, target_id, destination)

        assert not (destination / managed_stale_name).exists()
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


def test_hardlink_export_is_idempotent_and_records_inode_ownership(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo bytes")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = catalog.upsert_photo("a" * 64, capture_time="2026-08-26T09:23:28")
        catalog.observe_source_file(source_id, photo_id, str(source), import_run_id=run_id)
        target_id = catalog.create_target("chloe", label="Chloe")
        catalog.record_decision(target_id, photo_id, "accept")
        destination = tmp_path / "export"

        first = write_hardlinks(catalog.connection, target_id, destination)
        second = write_hardlinks(catalog.connection, target_id, destination)

    name = f"chloe_2026-08-26_092328_{photo_id}.jpg"
    exported = destination / name
    assert exported.is_file() and not exported.is_symlink()
    assert os.path.samefile(exported, source)
    assert first["created_count"] == 1
    assert second["unchanged_count"] == 1
    manifest = json.loads((destination / ".ppr-symlink-export.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 3
    assert manifest["managed"][name]["kind"] == "hardlink"
    assert manifest["managed"][name]["source_identity"]["ino"] == source.stat().st_ino


def test_hardlink_export_migrates_manifest_symlink_and_preserves_stored_prefix(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo bytes")
    photo_id = "b" * 64
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        catalog.upsert_photo(photo_id, capture_time="2026-08-26T09:23:28")
        catalog.observe_source_file(source_id, photo_id, str(source), import_run_id=run_id)
        target_id = catalog.create_target("chloe", label="Chloe")
        catalog.record_decision(target_id, photo_id, "accept")
        destination = tmp_path / "export"
        destination.mkdir()
        old_name = f"ppr_custom_2026-08-26_092328_{photo_id}.jpg"
        os.symlink(source, destination / old_name)
        (destination / ".ppr-symlink-export.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "target_id": target_id,
                    "filename_prefix": "ppr_custom",
                    "managed_names": [old_name],
                }
            ),
            encoding="utf-8",
        )

        result = write_hardlinks(catalog.connection, target_id, destination)

    exported = destination / old_name
    assert result["filename_prefix"] == "ppr_custom"
    assert result["updated_count"] == 1
    assert exported.is_file() and not exported.is_symlink()
    assert os.path.samefile(exported, source)


def test_hardlink_stale_removal_does_not_delete_replaced_regular_file(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo bytes")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = catalog.upsert_photo("c" * 64, capture_time="2026-08-26T09:23:28")
        catalog.observe_source_file(source_id, photo_id, str(source), import_run_id=run_id)
        target_id = catalog.create_target("chloe")
        catalog.record_decision(target_id, photo_id, "accept")
        destination = tmp_path / "export"
        write_hardlinks(catalog.connection, target_id, destination)
        name = f"chloe_2026-08-26_092328_{photo_id}.jpg"
        exported = destination / name
        exported.unlink()
        exported.write_bytes(b"user-owned")
        catalog.record_decision(target_id, photo_id, "reject")

        result = write_hardlinks(catalog.connection, target_id, destination)

    assert result["removed_count"] == 0
    assert result["conflict_count"] == 0
    assert exported.read_bytes() == b"user-owned"


def test_hardlink_export_reports_cross_device_without_copy(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"photo bytes")
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        source_id = catalog.create_source("folder")
        run_id = catalog.create_import_run(source_id)
        photo_id = catalog.upsert_photo("d" * 64)
        catalog.observe_source_file(source_id, photo_id, str(source), import_run_id=run_id)
        target_id = catalog.create_target("chloe")
        catalog.record_decision(target_id, photo_id, "accept")

        def fail_link(*args, **kwargs):
            raise OSError(errno.EXDEV, "cross-device link")

        monkeypatch.setattr(os, "link", fail_link)
        result = write_hardlinks(catalog.connection, target_id, tmp_path / "export")

    assert result["created_count"] == 0
    assert result["skipped"][0]["reason"] == "cross_device"
    assert not any(path.is_file() for path in (tmp_path / "export").iterdir() if not path.name.startswith("."))
