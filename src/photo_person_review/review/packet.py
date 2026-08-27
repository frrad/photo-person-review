"""Temporary visual review artifacts for the conversational interface.

Artifacts are written only beneath the directory supplied by the caller. No
path is stored in the database, and this module never copies bytes to a
persistent application cache.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from photo_person_review.analysis.models import FaceObservation, PersonObservation
from photo_person_review.analysis.scoring import CandidateScore


@dataclass(frozen=True)
class ReviewMedia:
    media_id: str
    path: Path
    capture_time: datetime | None = None


PacketStrategy = Literal[
    "reference-seeding",
    "likely",
    "uncertain",
    "no-face",
    "cluster",
    "audit-positive",
    "audit-negative",
]


def select_packet_media(
    media: Iterable[ReviewMedia],
    *,
    strategy: PacketStrategy = "likely",
    limit: int = 12,
    scores: Mapping[str, CandidateScore | Mapping[str, Any]] | None = None,
    decisions: Mapping[str, str] | None = None,
    faces: Mapping[str, Sequence[FaceObservation]] | None = None,
) -> list[ReviewMedia]:
    """Select a deterministic, strategy-specific packet input list.

    Selection is metadata-only. It never opens a source photo and therefore
    can run before the caller allocates a temporary artifact directory.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    items = list(media)
    scores = scores or {}
    decisions = decisions or {}
    faces = faces or {}

    def value(item: ReviewMedia) -> float:
        score = scores.get(item.media_id)
        if isinstance(score, CandidateScore):
            return score.score
        raw = score.get("score") if score else 0.0
        return float(raw) if isinstance(raw, (int, float)) else 0.0

    if strategy in {"likely", "reference-seeding"}:
        # A no-score reference seed preserves importer order; scored seeds use
        # strongest evidence first, which is useful after a prior batch.
        ordered = sorted(items, key=lambda item: (-value(item), item.media_id))
    elif strategy == "uncertain":
        ordered = sorted(items, key=lambda item: (abs(value(item) - 0.5), item.media_id))
    elif strategy == "no-face":
        ordered = sorted(
            (item for item in items if not faces.get(item.media_id)),
            key=lambda item: (-value(item), item.media_id),
        )
    elif strategy == "audit-positive":
        ordered = sorted(
            (item for item in items if decisions.get(item.media_id) == "accept"),
            key=lambda item: (value(item), item.media_id),
        )
    elif strategy == "audit-negative":
        ordered = sorted(
            (item for item in items if decisions.get(item.media_id) == "reject"),
            key=lambda item: (-value(item), item.media_id),
        )
    else:  # cluster
        ordered = sorted(items, key=lambda item: (-value(item), item.media_id))
        seen_groups: set[str] = set()
        deduplicated: list[ReviewMedia] = []
        for item in ordered:
            score = scores.get(item.media_id)
            metadata = score.metadata if isinstance(score, CandidateScore) else score or {}
            group = metadata.get("duplicate_group")
            if isinstance(group, str):
                if group in seen_groups:
                    continue
                seen_groups.add(group)
            deduplicated.append(item)
        ordered = deduplicated

    if strategy not in {"audit-positive", "audit-negative"}:
        ordered = [item for item in ordered if item.media_id not in decisions]
    return ordered[:limit]


def _require_pillow() -> Any:
    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as exc:
        raise RuntimeError("Review packets require Pillow; install the image dependencies.") from exc
    return Image, ImageDraw, ImageOps


def _safe_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _save_private(image: Any, path: Path) -> None:
    # Explicit JPEG options avoid carrying source metadata into a remote VLM
    # packet and avoid preserving arbitrary EXIF/GPS information.
    image.save(path, format="JPEG", quality=90, optimize=True, exif=b"")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _scaled_box(box: tuple[int, int, int, int], sx: float, sy: float) -> tuple[int, int, int, int]:
    x, y, w, h = box
    return round(x * sx), round(y * sy), round((x + w) * sx), round((y + h) * sy)


def _annotate(
    image: Any,
    faces: Sequence[FaceObservation],
    people: Sequence[PersonObservation],
    label: str,
    draw_cls: Any,
) -> Any:
    canvas = image.copy()
    draw = draw_cls.Draw(canvas)
    sx = canvas.width / image.width
    sy = canvas.height / image.height
    # The image is copied at its EXIF-corrected dimensions, so observations
    # from the canonical importer can be drawn directly after scaling.
    for person_index, person in enumerate(people, 1):
        box = _scaled_box(person.bbox, sx, sy)
        draw.rectangle(box, outline=(52, 150, 255), width=max(2, canvas.width // 500))
        draw.text(
            (box[0] + 3, box[1] + 3),
            f"P{person_index}",
            fill=(52, 150, 255),
        )
    for face_index, face in enumerate(faces, 1):
        box = _scaled_box(face.bbox, sx, sy)
        draw.rectangle(box, outline=(255, 75, 75), width=max(2, canvas.width // 500))
        x1, y1, _, _ = box
        draw.text((x1 + 3, y1 + 3), f"F{face_index}", fill=(255, 75, 75))
    draw.rectangle((0, 0, min(canvas.width, 180), 30), fill=(0, 0, 0))
    draw.text((6, 6), label, fill=(255, 255, 255))
    return canvas


def _crop(image: Any, box: tuple[int, int, int, int], *, pad: float = 0.18) -> Any:
    x, y, w, h = box
    px, py = round(w * pad), round(h * pad)
    return image.crop(
        (
            max(0, x - px),
            max(0, y - py),
            min(image.width, x + w + px),
            min(image.height, y + h + py),
        )
    )


def build_review_packet(
    media: Iterable[ReviewMedia],
    *,
    output_dir: str | Path,
    faces: Mapping[str, Sequence[FaceObservation]] | None = None,
    people: Mapping[str, Sequence[PersonObservation]] | None = None,
    scores: Mapping[str, CandidateScore | Mapping[str, Any]] | None = None,
    packet_id: str | None = None,
    strategy: str = "likely",
) -> Path:
    """Render a contact sheet and per-photo evidence into ``output_dir``.

    Returns the path to ``packet.json``. Paths in that JSON are relative to the
    packet directory so it can safely be passed between CLI commands and the
    chat interface. The packet itself is disposable and contains no source
    image bytes beyond the derived JPEGs requested by the caller.
    """
    Image, ImageDraw, ImageOps = _require_pillow()
    items = list(media)
    out = Path(output_dir)
    _safe_mkdir(out)
    _safe_mkdir(out / "media")
    _safe_mkdir(out / "faces")
    _safe_mkdir(out / "people")
    faces = faces or {}
    people = people or {}
    scores = scores or {}
    packet_id = packet_id or out.name
    visible: list[dict[str, Any]] = []
    tiles: list[Any] = []
    tile_size = (360, 280)

    for index, item in enumerate(items, 1):
        label = f"{index:02d}"
        with Image.open(item.path) as source:
            corrected = ImageOps.exif_transpose(source).convert("RGB")
        item_faces = list(faces.get(item.media_id, ()))
        item_people = list(people.get(item.media_id, ()))
        annotated = _annotate(corrected, item_faces, item_people, label, ImageDraw)
        annotated.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
        annotated_path = out / "media" / f"{label}.jpg"
        _save_private(annotated, annotated_path)

        face_entries: list[dict[str, Any]] = []
        for face_index, face in enumerate(item_faces, 1):
            face_crop = _crop(corrected, face.bbox)
            face_crop.thumbnail((600, 600), Image.Resampling.LANCZOS)
            face_path = out / "faces" / f"{label}-face-{face_index:02d}.jpg"
            _save_private(face_crop, face_path)
            face_entries.append(
                {
                    "face_id": face.face_id,
                    "path": str(face_path.relative_to(out)),
                    "bbox": list(face.bbox),
                }
            )

        person_entries: list[dict[str, Any]] = []
        for person_index, person in enumerate(item_people, 1):
            person_crop = _crop(corrected, person.bbox)
            person_crop.thumbnail((800, 800), Image.Resampling.LANCZOS)
            person_path = out / "people" / f"{label}-person-{person_index:02d}.jpg"
            _save_private(person_crop, person_path)
            person_entries.append(
                {
                    "person_id": person.person_id,
                    "path": str(person_path.relative_to(out)),
                    "bbox": list(person.bbox),
                }
            )

        tile = annotated.copy()
        tile.thumbnail(tile_size, Image.Resampling.LANCZOS)
        tile_canvas = Image.new("RGB", tile_size, (35, 35, 35))
        tile_canvas.paste(
            tile,
            (
                (tile_size[0] - tile.width) // 2,
                28 + (tile_size[1] - 28 - tile.height) // 2,
            ),
        )
        draw = ImageDraw.Draw(tile_canvas)
        draw.rectangle((0, 0, tile_size[0], 28), fill=(0, 0, 0))
        score = scores.get(item.media_id)
        score_value = score.score if isinstance(score, CandidateScore) else score.get("score") if score else None
        suffix = f"  score={score_value:.3f}" if isinstance(score_value, (int, float)) else ""
        short_id = item.media_id[:12]
        draw.text((6, 7), f"{label}  {short_id}{suffix}", fill=(255, 255, 255))
        tiles.append(tile_canvas)

        entry: dict[str, Any] = {
            "label": label,
            "media_id": item.media_id,
            "source_path": str(item.path),
            "capture_time": item.capture_time.isoformat() if item.capture_time else None,
            "annotated_path": str(annotated_path.relative_to(out)),
            "faces": face_entries,
            "people": person_entries,
        }
        if score is not None:
            entry["score"] = score.as_dict() if isinstance(score, CandidateScore) else dict(score)
        visible.append(entry)

    columns = 3
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile_size[0], max(1, rows) * tile_size[1]), (25, 25, 25))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % columns) * tile_size[0], (i // columns) * tile_size[1]))
    sheet_path = out / "contact-sheet.jpg"
    _save_private(sheet, sheet_path)
    packet = {
        "packet_id": packet_id,
        "strategy": strategy,
        "created_at": datetime.now().astimezone().isoformat(),
        "contact_sheet": str(sheet_path.relative_to(out)),
        "visible": visible,
    }
    packet_path = out / "packet.json"
    packet_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(packet_path, 0o600)
    except OSError:
        pass
    return packet_path
