"""Pinned, verifiable downloads for local OpenCV models.

Model installation is explicit. Analysis never downloads anything and never
transmits a photograph; the installer only contacts the fixed HTTPS URLs
listed below and records checksums/license metadata beside the model files.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, cast


@dataclass(frozen=True)
class PinnedModel:
    name: str
    filename: str
    url: str
    sha256: str
    license: str
    license_url: str
    description: str


PINNED_MODELS: dict[str, PinnedModel] = {
    "yunet": PinnedModel(
        name="yunet",
        filename="face_detection_yunet_2023mar.onnx",
        url=(
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
            "face_detection_yunet_2023mar.onnx"
        ),
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        license="MIT",
        license_url="https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE",
        description="YuNet face detector (fixed-shape OpenCV 4.x model)",
    ),
    "sface": PinnedModel(
        name="sface",
        filename="face_recognition_sface_2021dec.onnx",
        url=(
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
            "face_recognition_sface_2021dec.onnx"
        ),
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        license="Apache-2.0",
        license_url="https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE",
        description="SFace face recognizer (five-landmark alignment)",
    ),
}

Download = Callable[[str, BinaryIO], None]


class ModelInstallError(RuntimeError):
    """Raised when a pinned model cannot be downloaded or verified."""


def _download(url: str, destination: BinaryIO) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "photo-person-review/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - URL is pinned below
        while chunk := response.read(1024 * 1024):
            destination.write(chunk)


class ModelManager:
    """Install and inspect pinned models beneath a caller-provided directory."""

    def __init__(self, models_dir: str | Path, *, models: dict[str, PinnedModel] | None = None):
        self.models_dir = Path(models_dir).expanduser()
        self.models = models or PINNED_MODELS
        self.metadata_path = self.models_dir / "models.json"

    def available(self) -> list[PinnedModel]:
        return list(self.models.values())

    def path(self, name: str) -> Path:
        model = self._model(name)
        return self.models_dir / model.filename

    def is_installed(self, name: str) -> bool:
        path = self.path(name)
        return path.is_file() and self._sha256(path) == self._model(name).sha256

    def install(self, name: str, *, downloader: Download | None = None) -> Path:
        model = self._model(name)
        if not model.url.startswith("https://"):
            raise ModelInstallError(f"Refusing non-HTTPS model URL for {name}")
        if self.is_installed(name):
            return self.path(name)
        self.models_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b", prefix=f".{model.filename}.", suffix=".part", dir=self.models_dir, delete=False
            ) as handle:
                temp_path = Path(handle.name)
                (downloader or _download)(model.url, cast(BinaryIO, handle))
                handle.flush()
                os.fsync(handle.fileno())
            actual = self._sha256(temp_path)
            if actual != model.sha256:
                raise ModelInstallError(f"Checksum mismatch for {name}: expected {model.sha256}, got {actual}")
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path(name))
            temp_path = None
            self._write_metadata(model)
            return self.path(name)
        except ModelInstallError:
            raise
        except Exception as exc:
            raise ModelInstallError(f"Could not install {name}: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def install_all(self, names: Iterable[str] | None = None, *, downloader: Download | None = None) -> dict[str, Path]:
        selected = list(names) if names is not None else list(self.models)
        return {name: self.install(name, downloader=downloader) for name in selected}

    def _model(self, name: str) -> PinnedModel:
        try:
            return self.models[name]
        except KeyError as exc:
            raise ModelInstallError(f"Unknown model {name!r}; choose from {sorted(self.models)}") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_metadata(self, model: PinnedModel) -> None:
        records: dict[str, object] = {}
        if self.metadata_path.is_file():
            try:
                records = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                records = {}
        records[model.name] = {**asdict(model), "installed_sha256": model.sha256}
        temp = self.metadata_path.with_suffix(".json.part")
        temp.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.metadata_path)
