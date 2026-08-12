"""Fail-closed validation for independently published ready score tables."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import duckdb

from .metrics import METHODOLOGY_VERSION


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp(path: Path) -> tuple[str, int, int, int]:
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


@lru_cache(maxsize=64)
def _validate_cached(
    artifact_stamp: tuple[str, int, int, int],
    manifest_stamp: tuple[str, int, int, int],
) -> bool:
    artifact_path = Path(artifact_stamp[0])
    manifest_path = Path(manifest_stamp[0])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifact = manifest.get("artifact") or {}
        con = duckdb.connect()
        try:
            cursor = con.execute(
                f"SELECT * FROM read_parquet('{artifact_path.as_posix()}') LIMIT 0"
            )
            columns = [entry[0] for entry in cursor.description]
            row_count = con.execute(
                f"SELECT count(*) FROM read_parquet('{artifact_path.as_posix()}')"
            ).fetchone()[0]
        finally:
            con.close()
        return bool(
            manifest.get("schema_version") == "1.0"
            and manifest.get("methodology_version") == METHODOLOGY_VERSION
            and artifact.get("path") == artifact_path.name
            and artifact.get("sha256") == _sha256(artifact_path)
            and artifact.get("size_bytes") == artifact_path.stat().st_size
            and artifact.get("row_count") == row_count
            and artifact.get("columns") == columns
            and row_count > 0
        )
    except (OSError, ValueError, TypeError, KeyError, duckdb.Error, json.JSONDecodeError):
        return False


def ready_publication_valid(
    ready_root: Path,
    score_relpath: str,
    manifest_relpath: str | None,
) -> bool:
    """Unmanaged legacy tables pass; manifest-declared tables must verify."""
    artifact_path = ready_root / score_relpath
    if manifest_relpath is None:
        return artifact_path.exists()
    manifest_path = ready_root / manifest_relpath
    try:
        return _validate_cached(_stamp(artifact_path), _stamp(manifest_path))
    except OSError:
        return False
