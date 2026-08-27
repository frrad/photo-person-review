"""Adapter from the import repository protocol to the built-in SQLite catalog."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from photo_person_review.db import Catalog
from photo_person_review.media.metadata import ImageMetadata

from .folder import ImportRepository, PreviousObservation


class CatalogImportRepository(ImportRepository):
    """Persist imports through :class:`~photo_person_review.db.Catalog`.

    This small adapter keeps source-specific traversal out of the catalog and
    makes the append-only import semantics explicit.  It stores no image
    payloads; ``source_files.path`` is the only source-media reference.
    """

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def _ensure_source(self, source_id: str, root_path: str) -> None:
        row = self.catalog.connection.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None:
            self.catalog.create_source("folder", label=source_id, root_path=root_path, source_id=source_id)

    def _provider_tag(self, provider: str, kind: str) -> str:
        """Return a stable catalog tag used for non-authoritative hints."""

        name = f"provider:{provider}:{kind}"
        row = self.catalog.connection.execute("SELECT tag_id FROM tag_definitions WHERE name=?", (name,)).fetchone()
        if row is not None:
            return str(row["tag_id"])
        return self.catalog.create_tag(name, description="Imported provider hint; not a review decision")

    def begin_import_run(self, *, source_id: str, root_path: str, started_at: str) -> str:
        self._ensure_source(source_id, root_path)
        return self.catalog.create_import_run(source_id)

    def observations_for_source(self, *, source_id: str) -> Iterable[PreviousObservation]:
        rows = self.catalog.connection.execute(
            """SELECT sf.relative_path, sf.photo_id, p.sha256, sf.file_size, sf.mtime_ns,
                      sf.observation_state
               FROM source_files sf JOIN photos p ON p.photo_id=sf.photo_id
               WHERE sf.source_id=? AND sf.relative_path IS NOT NULL
                 AND sf.source_file_id=(SELECT MAX(sf2.source_file_id)
                                        FROM source_files sf2
                                        WHERE sf2.source_id=sf.source_id
                                          AND sf2.relative_path=sf.relative_path)""",
            (source_id,),
        )
        return [
            PreviousObservation(
                relative_path=str(row["relative_path"]),
                content_hash=str(row["sha256"]),
                byte_size=row["file_size"],
                modified_ns=row["mtime_ns"],
                photo_id=str(row["photo_id"]),
                observation_state=str(row["observation_state"]),
            )
            for row in rows
        ]

    def upsert_photo(
        self,
        *,
        metadata: ImageMetadata,
        external_refs: Mapping[str, Any],
        provider_hints: Mapping[str, Any],
    ) -> str:
        all_metadata: dict[str, Any] = dict(metadata.metadata)
        all_metadata.update(
            {
                "orientation": metadata.orientation,
                "display_width": metadata.display_width,
                "display_height": metadata.display_height,
                "mode": metadata.mode,
                "captured_at_source": metadata.captured_at_source,
                "external_refs": dict(external_refs),
                "provider_hints": dict(provider_hints),
            }
        )
        return self.catalog.upsert_photo(
            metadata.content_hash,
            width=metadata.width,
            height=metadata.height,
            mime_type=metadata.format,
            capture_time=metadata.captured_at,
            metadata=all_metadata,
        )

    def record_observation(
        self,
        *,
        run_id: str,
        source_id: str,
        photo_id: str,
        relative_path: str,
        status: str,
        metadata: ImageMetadata,
        batch_id: str,
        external_refs: Mapping[str, Any],
        provider_hints: Mapping[str, Any],
    ) -> None:
        self.catalog.observe_source_file(
            source_id,
            photo_id,
            metadata.path,
            relative_path=relative_path,
            file_size=metadata.byte_size,
            mtime_ns=metadata.modified_ns,
            import_run_id=run_id,
            observation_state="replaced" if status == "replaced" else "present",
        )
        self.catalog.observe_batch_photo(
            batch_id,
            photo_id,
            {
                "status": status,
                "relative_path": relative_path,
                "sha256": metadata.content_hash,
            },
            import_run_id=run_id,
            observation_state="replaced" if status == "replaced" else "present",
        )
        # Metadata history is intentionally append-only, including unchanged
        # files.  This gives each run a durable photo -> metadata trail.
        values = {
            "path": metadata.path,
            "relative_path": relative_path,
            "sha256": metadata.content_hash,
            "byte_size": metadata.byte_size,
            "modified_ns": metadata.modified_ns,
            "width": metadata.width,
            "height": metadata.height,
            "display_width": metadata.display_width,
            "display_height": metadata.display_height,
            "format": metadata.format,
            "mode": metadata.mode,
            "orientation": metadata.orientation,
            "captured_at": metadata.captured_at,
            "captured_at_source": metadata.captured_at_source,
            "import_status": status,
            **metadata.metadata,
        }
        for key, value in values.items():
            self.catalog.add_metadata(photo_id, key, value, "folder-import", run_id)

        for key, value in external_refs.items():
            self.catalog.add_metadata(photo_id, f"external_ref.{key}", value, "manifest-reference", run_id)
        provider = str(provider_hints.get("source", "manifest"))
        for key, value in provider_hints.items():
            self.catalog.add_metadata(photo_id, f"provider_hint.{key}", value, f"{provider}-hint", run_id)
        self._assign_provider_tags(
            photo_id=photo_id,
            run_id=run_id,
            provider=provider,
            provider_hints=provider_hints,
        )

    def _assign_provider_tags(
        self,
        *,
        photo_id: str,
        run_id: str,
        provider: str,
        provider_hints: Mapping[str, Any],
    ) -> None:
        """Record provider hints as tags while keeping them non-authoritative."""

        nested = provider_hints.get("manifest_fields", {})
        fields = nested if isinstance(nested, Mapping) else {}
        face_tags = provider_hints.get("face_tags", fields.get("face_tags", []))
        containers = provider_hints.get("containers", fields.get("containers", []))
        container_ids = provider_hints.get("container_ids", fields.get("container_ids", []))
        if isinstance(face_tags, list):
            tag_id = self._provider_tag(provider, "face-tag")
            for item in face_tags:
                self.catalog.assign_tag(
                    photo_id,
                    tag_id,
                    provenance="provider-hint",
                    value="true",
                    evidence={"provider": provider, "hint": item},
                    analysis_run_id=run_id,
                )
        if isinstance(containers, list) or isinstance(container_ids, list):
            tag_id = self._provider_tag(provider, "container")
            values = containers if isinstance(containers, list) and containers else container_ids
            for item in values:
                self.catalog.assign_tag(
                    photo_id,
                    tag_id,
                    provenance="provider-hint",
                    value="true",
                    evidence={"provider": provider, "hint": item},
                    analysis_run_id=run_id,
                )

    def record_missing(
        self,
        *,
        run_id: str,
        source_id: str,
        relative_path: str,
        previous: PreviousObservation,
    ) -> None:
        if previous.photo_id is None:
            return
        # A tombstone is represented by another source observation with the
        # old photo identity and null file facts; catalog rows remain intact.
        self.catalog.observe_source_file(
            source_id,
            previous.photo_id,
            "",
            relative_path=relative_path,
            file_size=None,
            mtime_ns=None,
            import_run_id=run_id,
            observation_state="missing",
        )
        self.catalog.add_metadata(previous.photo_id, "import_status", "missing", "folder-import", run_id)

    def create_batch(self, *, run_id: str, source_id: str, batch_key: str) -> str:
        return self.catalog.create_batch(
            source_id=source_id,
            label=f"{batch_key} ({run_id[:8]})",
            capture_date=None if batch_key == "undated" else batch_key,
        )

    def finish_import_run(self, *, run_id: str, summary: Mapping[str, Any]) -> None:
        self.catalog.finish_import_run(run_id, "complete", summary)
