"""Synthetic tests for metadata-only incremental imports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from photo_person_review.db import Catalog  # noqa: E402
from photo_person_review.importers.catalog import CatalogImportRepository  # noqa: E402
from photo_person_review.importers.folder import FolderImporter, PreviousObservation  # noqa: E402
from photo_person_review.importers.vidigami import VidigamiAdapter  # noqa: E402
from photo_person_review.media.metadata import extract_metadata  # noqa: E402


class MemoryRepository:
    """Small protocol fake that makes append-only behavior observable."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.photos: dict[str, str] = {}
        self.observations: list[dict[str, Any]] = []
        self.batches: list[dict[str, Any]] = []
        self.batch_links: list[dict[str, Any]] = []
        self.missing: list[dict[str, Any]] = []

    def begin_import_run(self, *, source_id: str, root_path: str, started_at: str) -> str:
        run_id = f"run-{len(self.runs) + 1}"
        self.runs.append({"id": run_id, "source_id": source_id, "root": root_path, "started": started_at})
        return run_id

    def observations_for_source(self, *, source_id: str):
        latest: dict[str, dict[str, Any]] = {}
        for item in self.observations:
            if item["source_id"] == source_id:
                latest[item["relative_path"]] = item
        return [
            PreviousObservation(
                relative_path=path,
                content_hash=item["hash"],
                byte_size=item["size"],
                modified_ns=item["mtime"],
                photo_id=item["photo_id"],
                observation_state=item.get("observation_state", "present"),
            )
            for path, item in latest.items()
        ]

    def upsert_photo(self, *, metadata, external_refs, provider_hints) -> str:
        photo_id = self.photos.setdefault(metadata.content_hash, f"photo-{len(self.photos) + 1}")
        return photo_id

    def record_observation(
        self,
        *,
        run_id,
        source_id,
        photo_id,
        relative_path,
        status,
        metadata,
        batch_id,
        external_refs,
        provider_hints,
    ):
        self.observations.append(
            {
                "run_id": run_id,
                "source_id": source_id,
                "photo_id": photo_id,
                "relative_path": relative_path,
                "status": status,
                "hash": metadata.content_hash,
                "size": metadata.byte_size,
                "mtime": metadata.modified_ns,
                "batch_id": batch_id,
                "external_refs": dict(external_refs),
                "provider_hints": dict(provider_hints),
                "observation_state": "replaced" if status == "replaced" else "present",
            }
        )
        self.batch_links.append({"run_id": run_id, "batch_id": batch_id, "photo_id": photo_id})

    def record_missing(self, *, run_id, source_id, relative_path, previous):
        self.missing.append({"run_id": run_id, "source_id": source_id, "relative_path": relative_path})

    def create_batch(self, *, run_id, source_id, batch_key):
        batch_id = f"batch-{len(self.batches) + 1}"
        self.batches.append({"id": batch_id, "run_id": run_id, "source_id": source_id, "key": batch_key})
        return batch_id

    def finish_import_run(self, *, run_id, summary):
        next(item for item in self.runs if item["id"] == run_id)["summary"] = dict(summary)


def write_photo(path: Path, color: tuple[int, int, int], *, exif: bool = False) -> None:
    image = Image.new("RGB", (32, 20), color)
    if exif:
        image.getexif()[274] = 6
        image.getexif()[36867] = "2026:08:27 11:22:33"
    image.save(path, format="JPEG", quality=90, exif=image.getexif())


def test_extract_metadata_hash_dimensions_orientation_and_capture(tmp_path):
    path = tmp_path / "photo.jpg"
    write_photo(path, (20, 40, 60), exif=True)
    metadata = extract_metadata(path)
    assert metadata.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert metadata.byte_size == path.stat().st_size
    assert (metadata.width, metadata.height) == (32, 20)
    assert (metadata.display_width, metadata.display_height) == (20, 32)
    assert metadata.orientation == 6
    assert metadata.captured_at == "2026-08-27T11:22:33"
    assert metadata.captured_at_source == "DateTimeOriginal"


def test_incremental_import_deduplicates_photo_but_appends_history(tmp_path):
    write_photo(tmp_path / "one.jpg", (1, 2, 3), exif=True)
    repo = MemoryRepository()
    importer = FolderImporter(repo)
    first = importer.import_folder(tmp_path, source_id="album")
    second = importer.import_folder(tmp_path, source_id="album")
    assert first.counts["new"] == 1
    assert second.counts["unchanged"] == 1
    assert len(repo.photos) == 1
    assert len(repo.runs) == 2
    assert len(repo.observations) == 2
    assert len(repo.batch_links) == 2
    assert len(repo.batches) == 2


def test_incremental_import_detects_replacement_and_missing_tombstone(tmp_path):
    path = tmp_path / "one.jpg"
    write_photo(path, (1, 2, 3))
    repo = MemoryRepository()
    importer = FolderImporter(repo)
    importer.import_folder(tmp_path, source_id="album")
    write_photo(path, (200, 100, 50))
    replaced = importer.import_folder(tmp_path, source_id="album")
    assert replaced.counts["replaced"] == 1
    path.unlink()
    missing = importer.import_folder(tmp_path, source_id="album")
    assert missing.missing == ["one.jpg"]
    assert len(repo.photos) == 2
    assert len(repo.missing) == 1


def test_folder_import_skips_symlinks_and_non_images(tmp_path):
    write_photo(tmp_path / "real.jpg", (1, 2, 3))
    (tmp_path / "notes.txt").write_text("not a photo", encoding="utf-8")
    link = tmp_path / "linked.jpg"
    try:
        link.symlink_to(tmp_path / "real.jpg")
    except OSError:
        pytest.skip("symlinks unavailable")
    repo = MemoryRepository()
    result = FolderImporter(repo).import_folder(tmp_path, source_id="album")
    assert result.counts["new"] == 1
    assert [item["relative_path"] for item in repo.observations] == ["real.jpg"]


def test_vidigami_maps_report_ids_to_hash_named_archive_and_preserves_hints(tmp_path):
    media_id = "media|123"
    stem = hashlib.sha256(media_id.encode()).hexdigest()
    write_photo(tmp_path / f"{stem}.jpg", (9, 8, 7))
    report = tmp_path / "media.json"
    report.write_text(
        json.dumps(
            [
                {
                    "media_id": media_id,
                    "container_ids": ["container|1"],
                    "containers": [{"container_id": "container|1", "container_type": "EVENT"}],
                    "face_tags": [{"tag_id": "tag|1", "tagged_user_id": None}],
                    "matched_page_ids": ["page|1"],
                }
            ]
        ),
        encoding="utf-8",
    )
    repo = MemoryRepository()
    result = VidigamiAdapter(repo).import_archive(tmp_path, report, source_id="vidigami")
    assert result.counts["new"] == 1
    assert len(repo.photos) == 1
    # The fake stores only IDs, so inspect the importer mapping contract by
    # ensuring the report was accepted and linked to the hash-named file.
    assert repo.observations[0]["relative_path"] == f"{stem}.jpg"
    assert repo.observations[0]["external_refs"]["external_id"] == media_id
    assert repo.observations[0]["provider_hints"]["source"] == "vidigami"


def test_catalog_adapter_appends_external_and_provider_metadata_and_tags(tmp_path):
    media_id = "media|catalog"
    stem = hashlib.sha256(media_id.encode()).hexdigest()
    write_photo(tmp_path / f"{stem}.jpg", (9, 8, 7))
    report = tmp_path / "media.json"
    report.write_text(
        json.dumps(
            [
                {
                    "media_id": media_id,
                    "container_ids": ["container|1"],
                    "face_tags": [{"tag_id": "tag|1", "tagged_user_id": None}],
                }
            ]
        ),
        encoding="utf-8",
    )
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        result = VidigamiAdapter(CatalogImportRepository(catalog)).import_archive(
            tmp_path, report, source_id="vidigami"
        )
        assert result.counts["new"] == 1
        metadata_keys = {row["key"] for row in catalog.connection.execute("SELECT key FROM photo_metadata")}
        assert "external_ref.external_id" in metadata_keys
        assert "provider_hint.manifest_fields" in metadata_keys
        assert catalog.connection.execute("SELECT COUNT(*) FROM tag_assignments").fetchone()[0] == 2
        assignments = list(catalog.connection.execute("SELECT provenance FROM tag_assignments"))
        assert all(row["provenance"] == "provider-hint" for row in assignments)
