from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Iterable


class WorkspaceSandbox:
    """Per-run copy of a repository; writes are path-contained and rollback is local."""

    def __init__(
        self,
        source: str | Path,
        root: str | Path,
        *,
        allowed_prefixes: Iterable[str] = ("reviews", "src/url_shortener", "tests", "docs"),
    ) -> None:
        self.source = Path(source).resolve()
        self.root = Path(root).resolve()
        self.allowed_prefixes = tuple(Path(prefix) for prefix in allowed_prefixes)
        self._snapshot_root = self.root.parent / "checkpoints" / self.root.name
        self._latest_checkpoint: Path | None = None
        if not self.source.is_dir():
            raise ValueError("sandbox source must be an existing directory")
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if self.root.exists():
            raise FileExistsError(f"sandbox already exists: {self.root}")
        ignored = shutil.ignore_patterns(
            ".git",
            ".agentic_se",
            ".venv",
            "venv",
            "__pycache__",
            "*.pyc",
            "node_modules",
        )
        shutil.copytree(self.source, self.root, ignore=ignored, symlinks=True)
        self.checkpoint("base")

    def resolve_write_path(self, relative_path: str | Path) -> Path:
        requested = Path(relative_path)
        if requested.is_absolute() or ".." in requested.parts:
            raise PermissionError("workspace paths must be relative and cannot traverse parents")
        if not any(requested.parts[: len(prefix.parts)] == prefix.parts for prefix in self.allowed_prefixes):
            raise PermissionError("workspace write is outside the approved path scope")
        current = self.root
        for part in requested.parts:
            current = current / part
            if current.is_symlink():
                raise PermissionError("workspace writes cannot traverse symbolic links")
        destination = (self.root / requested).resolve()
        if not destination.is_relative_to(self.root):
            raise PermissionError("workspace path escapes the run directory")
        return destination

    def write_text(self, relative_path: str | Path, content: str) -> Path:
        destination = self.resolve_write_path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(destination)
        return destination

    def list_files(self, limit: int = 200) -> tuple[str, ...]:
        files = []
        for path in self.root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            files.append(path.relative_to(self.root).as_posix())
            if len(files) >= limit:
                break
        return tuple(sorted(files))

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            digest.update(path.relative_to(self.root).as_posix().encode("utf-8"))
            with path.open("rb") as source_file:
                for chunk in iter(lambda: source_file.read(64 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    def checkpoint(self, label: str) -> Path:
        if not label or not all(character.isalnum() or character in "-_" for character in label):
            raise ValueError("checkpoint label contains unsupported characters")
        self._snapshot_root.mkdir(parents=True, exist_ok=True)
        snapshot = self._snapshot_root / f"{len(list(self._snapshot_root.iterdir())):03d}-{label}"
        shutil.copytree(self.root, snapshot, symlinks=True)
        self._latest_checkpoint = snapshot
        return snapshot

    def rollback(self) -> None:
        if self._latest_checkpoint is None or not self._latest_checkpoint.is_dir():
            raise RuntimeError("no workspace checkpoint is available")
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise PermissionError("run workspace root changed unexpectedly")
        snapshot_root = self._snapshot_root.resolve()
        if not self._latest_checkpoint.resolve().is_relative_to(snapshot_root):
            raise PermissionError("checkpoint is outside the managed snapshot directory")
        for item in self.root.iterdir():
            if item.is_symlink() or item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item)
        shutil.copytree(self._latest_checkpoint, self.root, dirs_exist_ok=True, symlinks=True)