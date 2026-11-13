"""Secure handling of uploaded files.

* Extension allow-list and content sniffing (magic bytes / ZIP structure) — the extension alone is never trusted.
* Size limits, ZIP-bomb protection (entry count, total uncompressed size, compression ratio).
* Macro-enabled Office content (``vbaProject.bin``) and path-traversal entries are rejected.
* Files are stored under random names in a per-session directory and deleted on removal / session end.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from services.security import safe_filename

logger = logging.getLogger(__name__)

ALLOWED_SOURCE_TYPES = {"docx", "pdf", "txt", "xlsx", "xls", "csv"}
ALLOWED_TEMPLATE_TYPES = {"docx"}

MAX_ZIP_ENTRIES = 5_000
MAX_ZIP_UNCOMPRESSED = 300 * 1024 * 1024
MAX_ZIP_RATIO = 150


class UploadRejected(ValueError):
    pass


@dataclass
class StoredFile:
    file_id: str
    original_name: str
    file_type: str
    path: Path
    size_bytes: int
    sha256: str


def _check_zip(data: bytes, required: str, kind: str) -> None:
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadRejected(
            f"The file is not a valid {kind} document.") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise UploadRejected(
                "The document contains too many internal parts.")
        total = 0
        names = set()
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise UploadRejected(
                    "The document contains unsafe internal paths.")
            total += info.file_size
            if (
                info.compress_size
                and info.file_size / max(info.compress_size, 1) > MAX_ZIP_RATIO
                and info.file_size > 10 * 1024 * 1024
            ):
                raise UploadRejected(
                    "The document is suspiciously compressed (possible ZIP bomb)."
                )
            names.add(name.lower())
        if total > MAX_ZIP_UNCOMPRESSED:
            raise UploadRejected(
                "The document is too large when uncompressed.")
        if "[content_types].xml" not in names or required not in names:
            raise UploadRejected(f"The file is not a valid {kind} document.")
        if any(n.endswith("vbaproject.bin") for n in names):
            raise UploadRejected("Macro-enabled documents are not accepted.")
        ct = zf.read("[Content_Types].xml").lower()
        if b"macroenabled" in ct:
            raise UploadRejected("Macro-enabled documents are not accepted.")


def sniff_and_validate(
    name: str, data: bytes, allowed: set[str], max_bytes: int
) -> str:
    """Validate an upload and return its normalized type. Raises UploadRejected."""
    if not data:
        raise UploadRejected("The file is empty.")
    if len(data) > max_bytes:
        raise UploadRejected(
            f"The file exceeds the {max_bytes // (1024 * 1024)} MB limit."
        )
    ext = Path(name).suffix.lower().lstrip(".")
    if ext not in allowed:
        raise UploadRejected(
            f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(allowed))}."
        )
    if ext == "docx":
        _check_zip(data, "word/document.xml", "Word")
    elif ext == "xlsx":
        _check_zip(data, "xl/workbook.xml", "Excel")
    elif ext == "pdf":
        if not data[:1024].lstrip().startswith(b"%PDF-"):
            raise UploadRejected("The file is not a valid PDF document.")
    elif ext == "xls":
        if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise UploadRejected(
                "The file is not a valid legacy Excel (.xls) document."
            )
    elif ext in ("txt", "csv"):
        sample = data[:65536]
        if b"\x00" in sample:
            raise UploadRejected("Text files must not contain binary data.")
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            try:
                sample.decode("cp1252")
            except UnicodeDecodeError as exc:
                raise UploadRejected(
                    "The text encoding is not supported (use UTF-8)."
                ) from exc
    return ext


class FileService:
    def __init__(
        self, base_dir: Path, max_upload_mb: int, retention_hours: int = 24
    ) -> None:
        self.session_dir = base_dir / uuid.uuid4().hex
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_upload_mb * 1024 * 1024
        self._cleanup_stale(base_dir, retention_hours)

    @staticmethod
    def _cleanup_stale(base_dir: Path, retention_hours: int) -> None:
        cutoff = time.time() - retention_hours * 3600
        for child in base_dir.iterdir() if base_dir.exists() else []:
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    def store(self, name: str, data: bytes, allowed: set[str]) -> StoredFile:
        file_type = sniff_and_validate(name, data, allowed, self.max_bytes)
        file_id = uuid.uuid4().hex[:12]
        path = self.session_dir / f"{file_id}.{file_type}"
        path.write_bytes(data)
        path.chmod(0o600)
        return StoredFile(
            file_id=file_id,
            original_name=safe_filename(name),
            file_type=file_type,
            path=path,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def delete(self, path: Path | str) -> None:
        path = Path(path)
        try:
            if path.resolve().is_relative_to(self.session_dir.resolve()):
                path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not delete upload", exc_info=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.session_dir, ignore_errors=True)
