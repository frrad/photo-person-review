"""Generic JSON media manifest parsing.

Manifest parsing is intentionally provider-neutral.  Provider adapters can
preserve opaque identifiers and hints here without making them authoritative
review decisions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, cast


@dataclass(frozen=True)
class ManifestEntry:
    """One manifest row, with no image bytes attached."""

    path: str | None = None
    external_id: str | None = None
    capture_time: str | None = None
    external_refs: dict[str, Any] = field(default_factory=dict)
    provider_hints: dict[str, Any] = field(default_factory=dict)


def _rows(payload: Any) -> list[Mapping[str, Any]]:
    rows: Any
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("media", payload.get("items", payload.get("photos")))
        if rows is None:
            raise ValueError("manifest object must contain a media, items, or photos array")
    else:
        raise ValueError("manifest must be a JSON array or object containing an array")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("manifest rows must be JSON objects")
    return cast(list[Mapping[str, Any]], rows)


def _entry(row: Mapping[str, Any]) -> ManifestEntry:
    path = row.get("path", row.get("local_path", row.get("filename")))
    external_id = row.get("external_id", row.get("media_id", row.get("id")))
    capture_time = row.get("capture_time", row.get("captured_at"))
    refs = row.get("external_refs", {})
    hints = row.get("provider_hints", row.get("hints", {}))
    if not isinstance(refs, Mapping) or not isinstance(hints, Mapping):
        raise ValueError("external_refs and provider_hints must be JSON objects")
    # Preserve provider-specific fields under hints.  This makes the generic
    # format forward-compatible without putting those fields into the core
    # data model.
    known = {
        "path",
        "local_path",
        "filename",
        "external_id",
        "media_id",
        "id",
        "capture_time",
        "captured_at",
        "external_refs",
        "provider_hints",
        "hints",
    }
    extra = {str(k): v for k, v in row.items() if k not in known}
    merged_hints = {str(k): v for k, v in hints.items()}
    if extra:
        merged_hints.setdefault("manifest_fields", extra)
    return ManifestEntry(
        path=str(path) if path is not None else None,
        external_id=str(external_id) if external_id is not None else None,
        capture_time=str(capture_time) if capture_time is not None else None,
        external_refs={str(k): v for k, v in refs.items()},
        provider_hints=merged_hints,
    )


def load_manifest(path: str | Path) -> list[ManifestEntry]:
    """Load a JSON manifest and return normalized rows.

    This function only reads the manifest; it does not resolve or open paths.
    """

    manifest_path = Path(path).expanduser().resolve(strict=True)
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return [_entry(row) for row in _rows(payload)]
