"""Credential storage.

Two backends are layered:

* :class:`MemoryCredentialStore` keeps session-only secrets in process memory.
* :class:`EncryptedFileCredentialStore` persists secrets encrypted with Fernet (AES-128-CBC + HMAC).
  The key is taken from ``DOCSGEN_MASTER_KEY`` if set, otherwise generated once and stored in
  ``master.key`` with owner-only permissions. Keep that file (or the environment variable) private.

Secret values are never logged and never written to the plain configuration file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import ClassVar, Protocol

from cryptography.fernet import Fernet, InvalidToken

from services.security import Redactor

logger = logging.getLogger(__name__)


class CredentialStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def keys(self, prefix: str = "") -> list[str]: ...


class MemoryCredentialStore:
    """Session-only secrets. Lost when the browser session ends."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        Redactor.register(value)
        with self._lock:
            self._data[key] = value

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return sorted(k for k in self._data if k.startswith(prefix))

    def __repr__(self) -> str:  # never show values
        return f"MemoryCredentialStore(keys={len(self._data)})"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_or_create_master_key(key_file: Path) -> bytes:
    env_key = os.environ.get("DOCSGEN_MASTER_KEY")
    if env_key:
        key = env_key.strip().encode()
        Fernet(key)  # validates format
        return key
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    _write_private(key_file, key)
    logger.info("Generated a new master key for encrypted credential storage.")
    return key


class EncryptedFileCredentialStore:
    """Persistent, encrypted credential storage shared by all sessions of this installation."""

    _locks: ClassVar[dict[str, threading.Lock]] = {}

    def __init__(self, path: Path, key_file: Path) -> None:
        self.path = path
        self._fernet = Fernet(load_or_create_master_key(key_file))
        self._lock = self._locks.setdefault(str(path.resolve()), threading.Lock())

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = self._fernet.decrypt(self.path.read_bytes())
            data = json.loads(payload.decode("utf-8"))
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError(
                "Stored credentials could not be decrypted. The master key may have"
                " changed; clear saved credentials from Application Settings."
            ) from exc
        for value in data.values():
            Redactor.register(value)
        return data

    def _write(self, data: dict[str, str]) -> None:
        token = self._fernet.encrypt(json.dumps(data, sort_keys=True).encode("utf-8"))
        _write_private(self.path, token)

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._read().get(key)

    def set(self, key: str, value: str) -> None:
        Redactor.register(value)
        with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._read()
            if key in data:
                data.pop(key)
                self._write(data)

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return sorted(k for k in self._read() if k.startswith(prefix))

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()

    def __repr__(self) -> str:
        return f"EncryptedFileCredentialStore(path={self.path.name})"


class LayeredCredentialStore:
    """Session values override persistent ones; writes go to the requested layer."""

    def __init__(
        self,
        session: MemoryCredentialStore,
        persistent: EncryptedFileCredentialStore | None,
    ) -> None:
        self.session = session
        self.persistent = persistent

    def get(self, key: str) -> str | None:
        value = self.session.get(key)
        if value is None and self.persistent is not None:
            value = self.persistent.get(key)
        return value

    def source(self, key: str) -> str | None:
        if self.session.get(key) is not None:
            return "session"
        if self.persistent is not None and self.persistent.get(key) is not None:
            return "persistent"
        return None

    def set(self, key: str, value: str, persist: bool) -> None:
        if persist:
            if self.persistent is None:
                raise RuntimeError("Persistent credential storage is not available.")
            self.persistent.set(key, value)
            self.session.delete(key)
        else:
            self.session.set(key, value)

    def delete(self, key: str) -> None:
        self.session.delete(key)
        if self.persistent is not None:
            self.persistent.delete(key)

    def delete_prefix(self, prefix: str) -> int:
        removed = 0
        for key in self.keys(prefix):
            self.delete(key)
            removed += 1
        return removed

    def keys(self, prefix: str = "") -> list[str]:
        names = set(self.session.keys(prefix))
        if self.persistent is not None:
            names.update(self.persistent.keys(prefix))
        return sorted(names)
