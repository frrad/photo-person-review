"""Read stable, useful metadata from an image without retaining its bytes."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image

_ORIENTATION_TAG = 274
_CAPTURE_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime
_SUPPORTED_ORIENTATIONS = frozenset(range(1, 9))
_EXIF_FIELDS = {
    271: "camera_make",
    272: "camera_model",
    305: "software",
    42016: "image_unique_id",
    33434: "exposure_time",
    33437: "f_number",
    34855: "iso",
    37378: "exposure_bias",
    37386: "focal_length",
    37500: "maker_note",
    42036: "lens_model",
}


@dataclass(frozen=True)
class ImageMetadata:
    """Metadata associated with one source path.

    ``content_hash`` is the durable identity of the photo.  ``path`` is only
    an observation of where that identity was found and may change between
    imports.  No image bytes are represented by this object.
    """

    path: str
    content_hash: str
    byte_size: int
    modified_ns: int
    width: int
    height: int
    display_width: int
    display_height: int
    format: str | None
    mode: str | None
    orientation: int
    captured_at: str | None
    captured_at_source: str | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def capture_date(self) -> str | None:
        """Return an ISO calendar date useful for batch grouping."""

        return self.captured_at[:10] if self.captured_at else None

    @property
    def sha256(self) -> str:
        """Compatibility spelling for callers that use the field name."""

        return self.content_hash

    @property
    def size_bytes(self) -> int:
        return self.byte_size

    @property
    def capture_timestamp(self) -> str | None:
        return self.captured_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        # Do not retain binary MakerNotes or other opaque blocks in the
        # normalized metadata table.
        return None
    text = str(value).strip()
    return text or None


def _json_value(value: Any) -> Any:
    """Convert Pillow's EXIF rationals/tuples into JSON-safe values."""

    if isinstance(value, bytes):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    try:
        numerator = value.numerator
        denominator = value.denominator
    except AttributeError:
        return _text(value)
    if denominator == 0:
        return None
    result = numerator / denominator
    return int(result) if result.is_integer() else result


def _parse_exif_time(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    # EXIF uses ``YYYY:MM:DD HH:MM:SS`` and sometimes appends a timezone.
    # Preserve a timezone when present, otherwise make the explicitly-naive
    # local capture time clear by omitting a fabricated timezone.
    normalized = text.replace(".", ":", 2)
    for pattern in (
        "%Y:%m:%d %H:%M:%S%z",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(normalized, pattern).isoformat()
        except ValueError:
            pass
    return text


def _capture_time(exif: Any) -> tuple[str | None, str | None]:
    for tag, source in (
        (36867, "DateTimeOriginal"),
        (36868, "DateTimeDigitized"),
        (306, "DateTime"),
    ):
        parsed = _parse_exif_time(exif.get(tag))
        if parsed:
            return parsed, source
    return None, None


def _fallback_mtime(stat_result: os.stat_result) -> tuple[str, str]:
    return datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc).isoformat(), "file_mtime"


def extract_metadata(path: str | os.PathLike[str], *, hash_chunk_size: int = 1024 * 1024) -> ImageMetadata:
    """Inspect *path*, returning metadata and a SHA-256 content identity.

    Pillow reads image headers and EXIF from the source; it does not write or
    retain a decoded image.  ``UnidentifiedImageError`` and normal filesystem
    errors intentionally propagate so an importer can report a per-file
    failure while continuing with the rest of a folder.
    """

    source = Path(path).expanduser().resolve(strict=True)
    stat_result = source.stat()
    digest = _hash_file(source, hash_chunk_size)

    with Image.open(source) as image:
        exif = image.getexif()
        orientation_value = exif.get(_ORIENTATION_TAG, 1)
        try:
            orientation = int(orientation_value)
        except (TypeError, ValueError):
            orientation = 1
        if orientation not in _SUPPORTED_ORIENTATIONS:
            orientation = 1
        width, height = image.size
        if orientation in {5, 6, 7, 8}:
            display_width, display_height = height, width
        else:
            display_width, display_height = width, height
        captured_at, capture_source = _capture_time(exif)
        if captured_at is None:
            captured_at, capture_source = _fallback_mtime(stat_result)

        normalized: dict[str, Any] = {}
        for tag, name in _EXIF_FIELDS.items():
            value = _json_value(exif.get(tag))
            if value is not None:
                normalized[name] = value
        # Keep all useful non-sensitive scalar EXIF values, including tags
        # added by newer cameras, while intentionally omitting GPS and binary
        # blobs from the normalized record.
        for tag, value in exif.items():
            tag_name = ExifTags.TAGS.get(tag)
            if not tag_name or tag_name in normalized or tag_name.startswith("GPS") or tag in {37500}:
                continue
            safe_value = _json_value(value)
            if safe_value is not None and isinstance(safe_value, (str, int, float, bool, list)):
                normalized[tag_name] = safe_value

        return ImageMetadata(
            path=str(source),
            content_hash=digest,
            byte_size=stat_result.st_size,
            modified_ns=stat_result.st_mtime_ns,
            width=width,
            height=height,
            display_width=display_width,
            display_height=display_height,
            format=image.format,
            mode=image.mode,
            orientation=orientation,
            captured_at=captured_at,
            captured_at_source=capture_source,
            metadata=normalized,
        )
