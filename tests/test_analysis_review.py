import json
from pathlib import Path

from PIL import Image

from photo_person_review.analysis import FaceObservation, PersonObservation
from photo_person_review.review import ReviewMedia, build_review_packet


def test_packet_maps_labels_and_renders_exif_corrected_derivatives(tmp_path: Path):
    source = tmp_path / "source.jpg"
    image = Image.new("RGB", (100, 60), (255, 255, 255))
    image.save(source, exif=Image.Exif())
    out = tmp_path / "packet"
    packet_path = build_review_packet(
        [ReviewMedia("stable-media", source)],
        output_dir=out,
        faces={"stable-media": [FaceObservation("stable-media", "face-1", (10, 10, 20, 20))]},
        people={"stable-media": [PersonObservation("stable-media", "person-1", (5, 5, 40, 45), face_id="face-1")]},
    )
    packet = json.loads(packet_path.read_text())
    visible = packet["visible"][0]
    assert visible["label"] == "01"
    assert visible["media_id"] == "stable-media"
    assert visible["faces"][0]["face_id"] == "face-1"
    assert (out / packet["contact_sheet"]).is_file()
    assert (out / packet["face_sheet"]).is_file()
    assert (out / visible["annotated_path"]).is_file()
    assert (out / visible["faces"][0]["path"]).is_file()
    assert (out / visible["people"][0]["path"]).is_file()
    assert all("source" not in path.name for path in (out / "media").iterdir())
