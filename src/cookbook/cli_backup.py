"""Pure, side-effect-free helpers for the backup contract.

Implements the deterministic building blocks described in
docs/superpowers/specs/2026-11-09-backup-restore-design.md: UTC-based
artifact naming, streaming SHA-256, safe sorted media enumeration,
byte-for-byte deterministic tar.gz creation, and versioned manifest
serialization/validation. Only `cookbook.models` is imported, so these
helpers are testable with tmp_path fixtures alone (no database, no
subprocesses, no .env access).
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
import re
import stat as stat_module
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from cookbook.models import Base
from cookbook.config import cli_config

#: The supported schema's application tables (locked in against models.py so
#: the manifest contract cannot drift from the real inventory).
SUPPORTED_TABLES = frozenset(Base.metadata.tables)

#: Head of the alembic versions chain (v7). Kept in sync with the
#: EXPECTED_HEAD lock in tests/test_schema_inventory.py.
ALEMBIC_HEAD = "f18a6c4d0e72"

#: Every revision in the versions chain we can manage (v0 base through v7
#: head). Anything else is rejected so a mismatched backup is never
#: silently trusted.
_KNOWN_ALEMBIC_REVISIONS = frozenset(
    {
        ALEMBIC_HEAD,      # v7
        "e4c7a91d2f30",    # v6
        "b7a2f9c4d1e8",    # v5
        "3fcee3b79cca",    # v4
        "a2b53a9f0000",    # v3
        "52473474a186",    # v2
        "a901619780e2",    # v1
        "6d22befb6ec9",    # v0 (base)
    }
)

_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SQL_NAME_RE = re.compile(r"\Acookbook-(?P<date>\d{4}-\d{2}-\d{2})\.sql\.gz\Z")
_MEDIA_NAME_RE = re.compile(r"\Amedia-(?P<date>\d{4}-\d{2}-\d{2})\.tar\.gz\Z")
_MANIFEST_NAME_RE = re.compile(r"\Amanifest-(?P<date>\d{4}-\d{2}-\d{2})\.json\Z")

_MANIFEST_KEYS = frozenset(
    {"version", "artifacts", "alembic_revision", "table_row_counts", "media_file_count"}
)
_ARTIFACT_ENTRY_KEYS = frozenset({"name", "sha256"})


# ---------------------------------------------------------------------------
# UTC artifact naming
# ---------------------------------------------------------------------------

def utc_backup_date(instant: datetime.datetime) -> str:
    """Return the UTC calendar day ('YYYY-MM-DD') of an aware datetime.

    The date is derived from the instant itself (via UTC), never from local
    time, so a backup started at 23:59 UTC is never mis-dated as 'tomorrow'.
    Naive datetimes have no definite instant and are rejected.
    """
    if not isinstance(instant, datetime.datetime):
        raise ValueError("utc_backup_date requires a datetime, got "
                         f"{type(instant).__name__}")
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError("utc_backup_date requires a timezone-aware datetime")
    day = instant.astimezone(datetime.timezone.utc).date()
    # Format manually so years under 1000 stay zero-padded.
    return f"{day.year:04d}-{day.month:02d}-{day.day:02d}"


def _require_calendar_date(day: object) -> str:
    if not isinstance(day, str) or not _DATE_RE.fullmatch(day):
        raise ValueError(
            f"artifact date must be a UTC calendar day 'YYYY-MM-DD', got {day!r}"
        )
    try:
        datetime.date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError(f"artifact date is not a real calendar date: {day!r}") from exc
    return day


def sql_artifact_name(day: str) -> str:
    """Name the SQL dump artifact for a UTC backup day."""
    return f"cookbook-{_require_calendar_date(day)}.sql.gz"


def media_archive_name(day: str) -> str:
    """Name the media archive for a UTC backup day."""
    return f"media-{_require_calendar_date(day)}.tar.gz"


def manifest_name(day: str) -> str:
    """Name the manifest file for a UTC backup day."""
    return f"manifest-{_require_calendar_date(day)}.json"


# ---------------------------------------------------------------------------
# Streaming SHA-256
# ---------------------------------------------------------------------------

def file_sha256(file_path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    """Stream a file's SHA-256 hex digest in fixed-size binary chunks.

    Reading in binary mode keeps the digest correct for non-text media files;
    chunking keeps memory flat even for very large uploads.
    """
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size!r}")
    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Safe, sorted media enumeration
# ---------------------------------------------------------------------------

def list_media_files(media_dir: str | os.PathLike[str]) -> list[str]:
    """List every regular file under a media directory as sorted POSIX paths.

    Sorted order makes downstream archives deterministic; POSIX separators
    keep names portable across the backup/restore machines. Symlinks are
    rejected outright (not skipped, not followed) because they are an
    escape/ambiguity risk inside an archive. Traversal failures (e.g. an
    unreadable subdirectory raise PermissionError) escape the function so a
    partial backup is never produced silently.
    """
    if not isinstance(media_dir, (str, os.PathLike)):
        raise ValueError(f"media_dir must be a path, got {media_dir!r}")
    root = os.fspath(media_dir)
    if not os.path.isdir(root):
        raise ValueError(f"media directory does not exist or is not a directory: {media_dir!r}")

    rel_names: list[str] = []
    # followlinks=False is passed explicitly (not left to the default) so
    # the walk can never follow symlinked directories.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        # A symlink (to a file or a directory, valid or broken) is an
        # escape/ambiguity risk in a backup archive, so fail loudly rather
        # than skip or follow it. lstat judges the entry itself, never the
        # target it points to.
        for entry in dirnames + filenames:
            full = os.path.join(dirpath, entry)
            if stat_module.S_ISLNK(os.lstat(full).st_mode):
                raise ValueError(f"refusing to archive a symlink inside the media directory: {full!r}")
        # The walk's filenames list is not a guarantee of regular files:
        # every entry is stat'ed with the symlink NOT followed and accepted
        # only if S_ISREG. Directories and special files are rejected.
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            st = os.stat(full, follow_symlinks=False)
            if not stat_module.S_ISREG(st.st_mode):
                raise ValueError(f"refusing to archive a non-regular entry inside the media directory: {full!r}")
            rel = os.path.relpath(full, root)
            rel_names.append(rel.replace(os.sep, "/"))
    return sorted(rel_names)


def _raise_walk_error(error: OSError) -> None:
    """onerror callback for os.walk: re-raise instead of swallowing.

    os.walk normally calls onerror and continues on (or silently ignores
    by default), which would leave a backup silently incomplete. Re-raising
    makes enumeration fail loudly on unreadable directories.
    """
    raise error


# ---------------------------------------------------------------------------
# Deterministic tar.gz archive
# ---------------------------------------------------------------------------

def create_media_archive(media_dir: str | os.PathLike[str], archive_path: str | os.PathLike[str]) -> None:
    """Write a byte-deterministic .tar.gz of every file under media_dir.

    Determinism means two identical trees produce identical bytes: entries
    are sorted, and mtime/uid/gid/uname/gname are all pinned to neutral
    values, so archives are safe to diff, cache, and compare against a
    manifest digest without any host-specific metadata leaking in.
    """
    if not isinstance(media_dir, (str, os.PathLike)):
        raise ValueError(f"media_dir must be a path, got {media_dir!r}")
    root = os.fspath(media_dir)
    if not os.path.isdir(root):
        raise ValueError(f"media directory does not exist or is not a directory: {media_dir!r}")

    names = list_media_files(root)

    # Stream end-to-end: every media file is read in fixed-size chunks by
    # tarfile's copyfileobj and piped straight into a deterministic gzip
    # envelope (mtime=0, no filename) written to the archive file. Nothing
    # is ever buffered as a whole file or whole tree in memory.
    with open(archive_path, "wb") as archive_handle:
        gzip_stream = gzip.GzipFile(filename="", mode="wb", fileobj=archive_handle, mtime=0)
        try:
            with tarfile.open(fileobj=gzip_stream, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
                for rel_name in names:
                    with open(os.path.join(root, *rel_name.split("/")), "rb") as source:
                        info = tarfile.TarInfo(name=rel_name)
                        info.size = os.fstat(source.fileno()).st_size
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = 0o644
                        tar.addfile(info, source)
        finally:
            # gzip.GzipFile must be closed even if archiving fails so the
            # archive file is left in a valid (possibly short) gzip state
            # and the underlying handle is released.
            gzip_stream.close()


# ---------------------------------------------------------------------------
# Versioned manifest serialization / validation
# ---------------------------------------------------------------------------

def _require_nonnegative_int(value: object, field: str) -> int:
    # bool is a subclass of int, so it must be excluded explicitly: a
    # hand-edited manifest with true/false would otherwise pass as 1/0.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"manifest {field} must be a non-negative integer, got {value!r}")
    return value


def _parse_artifact_entry(name: object) -> tuple[str, str] | None:
    """Classify an artifact name as ('sql'|'media', date); None when invalid.

    Names a backup manifest may contain are exactly one of two shapes:
    ``cookbook-YYYY-MM-DD.sql.gz`` or ``media-YYYY-MM-DD.tar.gz`` with a real
    calendar date (a 13th month or 40th day is refused, since the regexes only
    assert the digit pattern). A manifest listing itself (``manifest-*.json``)
    is always invalid: the manifest file is never one of its own artifacts.
    """
    if not isinstance(name, str):
        return None
    if _MANIFEST_NAME_RE.fullmatch(name) is not None:
        return None
    match = _SQL_NAME_RE.fullmatch(name)
    kind = "sql"
    if match is None:
        match = _MEDIA_NAME_RE.fullmatch(name)
        if match is None:
            return None
        kind = "media"
    date = match["date"]
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return None
    return (kind, date)


def _is_valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def validate_manifest(manifest: object) -> dict[str, object]:
    """Validate a v1 manifest and return a deep copy (the input is left untouched).

    Every check is an explicit rejection: unknown table names, negative
    counts, unsorted or duplicate artifact entries, path-escaping names, and
    non-lowercase digests are all contract violations, not edge cases to
    paper over.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    if set(manifest) != _MANIFEST_KEYS:
        raise ValueError(
            f"manifest keys must be exactly {sorted(_MANIFEST_KEYS)}, got {sorted(manifest)}"
        )

    # Copy before validating so a partially-invalid input can never mutate
    # the caller's dict, and the returned value stays stable if the caller
    # later edits their own copy.
    validated = json.loads(json.dumps(manifest, sort_keys=True))

    # bool is a subclass of int, so True would compare equal to 1 and must
    # be excluded explicitly rather than papered over by ==.
    version = validated["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError(f"unsupported manifest version: {manifest['version']!r} (only 1)")

    revision = validated["alembic_revision"]
    if not isinstance(revision, str) or not revision or revision not in _KNOWN_ALEMBIC_REVISIONS:
        raise ValueError(
            f"manifest alembic_revision {revision!r} is not a known supported revision"
        )

    row_counts = validated["table_row_counts"]
    if not isinstance(row_counts, dict) or set(row_counts) != set(SUPPORTED_TABLES):
        raise ValueError(
            "manifest table_row_counts must cover exactly the supported tables "
            f"{sorted(SUPPORTED_TABLES)}"
        )
    for table in sorted(row_counts):
        _require_nonnegative_int(row_counts[table], f"table_row_counts[{table!r}]")

    _require_nonnegative_int(validated["media_file_count"], "media_file_count")

    artifacts = validated["artifacts"]
    if not isinstance(artifacts, list):
        raise ValueError("manifest artifacts must be a list")
    names: list[str] = []
    sql_date: str | None = None
    media_date: str | None = None
    for position, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict) or set(artifact) != _ARTIFACT_ENTRY_KEYS:
            raise ValueError(f"manifest artifacts[{position}] must have exactly the keys 'name' and 'sha256'")
        name = artifact["name"]
        digest = artifact["sha256"]
        if not _is_valid_sha256(digest):
            raise ValueError(f"manifest artifact {name!r} sha256 must be 64 lowercase hex chars, got {digest!r}")
        classified = _parse_artifact_entry(name)
        if classified is None:
            if isinstance(name, str) and _MANIFEST_NAME_RE.fullmatch(name) is not None:
                raise ValueError(f"manifest artifact {name!r} may not list the manifest itself as an artifact")
            raise ValueError(
                f"manifest artifact name {name!r} is not a cookbook SQL dump or media archive on a real calendar date"
            )
        kind, date = classified
        if kind == "sql":
            if sql_date is not None:
                raise ValueError(
                    f"manifest must contain exactly one cookbook-*.sql.gz artifact, "
                    f"but {name!r} repeats the SQL dump"
                )
            sql_date = date
        else:
            if media_date is not None:
                raise ValueError(
                    f"manifest must contain exactly one media-*.tar.gz artifact, "
                    f"but {name!r} repeats the media archive"
                )
            media_date = date
        names.append(name)

    if names != sorted(names):
        raise ValueError(f"manifest artifacts must be sorted by name, got {names}")
    if sql_date is None:
        raise ValueError("manifest must contain exactly one cookbook-YYYY-MM-DD.sql.gz artifact")
    if media_date is None:
        raise ValueError("manifest must contain exactly one media-YYYY-MM-DD.tar.gz artifact")
    if sql_date != media_date:
        raise ValueError(
            "manifest SQL and media artifacts must share the same UTC backup date, "
            f"got {sql_date} and {media_date}"
        )

    return validated


def serialize_manifest(manifest: object) -> str:
    """Validate a manifest and return its canonical, trailing-newline JSON text.

    Sorted keys + fixed indentation make the serialization a pure function of
    the manifest's value, so the same backup always yields the same bytes.
    """
    validated = validate_manifest(manifest)
    return json.dumps(validated, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_manifest(file_path: str | os.PathLike[str], manifest: object) -> None:
    """Validate then write the canonical manifest text to file_path (UTF-8)."""
    with open(file_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(serialize_manifest(manifest))


def load_manifest(file_path: str | os.PathLike[str]) -> dict[str, object]:
    """Read and validate a manifest; unvalidated bytes are never returned."""
    try:
        text = open(file_path, "r", encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"malformed manifest: cannot read {file_path!r}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed manifest: {file_path!r} is not valid JSON") from exc
    return validate_manifest(data)


def verify_backup_sources(
    sql_path: str | os.PathLike[str],
    media_archive_path: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
) -> dict[str, object]:
    """Validate a restore source set before inspecting or staging any data.

    The manifest is authoritative for artifact basenames and digests. The
    explicit input paths must name exactly those artifacts, and each digest
    is streamed from disk before the manifest is returned to the caller.
    """
    manifest = load_manifest(manifest_path)
    artifacts = {entry["name"]: entry["sha256"] for entry in manifest["artifacts"]}
    for label, source in (("SQL", sql_path), ("media archive", media_archive_path)):
        path = Path(source)
        if not path.is_file():
            raise ValueError(f"{label} source is not a regular file")
        if path.name not in artifacts:
            raise ValueError(f"{label} source basename does not match a manifest artifact")
        try:
            actual_digest = file_sha256(path)
        except OSError as exc:
            raise ValueError(f"{label} source cannot be read") from exc
        if actual_digest != artifacts[path.name]:
            raise ValueError(f"{label} source digest does not match the manifest")
    return manifest


def _normalized_archive_name(member: tarfile.TarInfo) -> str:
    """Return a safe, normalized POSIX relative path for one tar member."""
    name = member.name
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError(f"unsafe archive path: {name!r}")
    if name.startswith("/") or re.match(r"\A[A-Za-z]:", name):
        raise ValueError(f"unsafe absolute archive path: {name!r}")
    # Tar directories conventionally end with '/', which is not part of the
    # filesystem path. Strip only that suffix before validating components.
    raw = name[:-1] if member.isdir() and name.endswith("/") else name
    parts = raw.split("/")
    if not raw or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe archive path component: {name!r}")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized in ("", ".") or normalized.startswith("../"):
        raise ValueError(f"unsafe normalized archive path: {name!r}")
    return normalized


def _inspect_open_media_tar(archive: tarfile.TarFile) -> list[tuple[tarfile.TarInfo, str]]:
    """Validate every tar member and read all regular payloads to catch corruption."""
    inspected: list[tuple[tarfile.TarInfo, str]] = []
    seen: dict[str, bool] = {}
    try:
        members = archive.getmembers()
        for member in members:
            normalized = _normalized_archive_name(member)
            if member.isdir():
                is_dir = True
            elif member.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and member.isreg() and not getattr(member, "sparse", None):
                is_dir = False
            else:
                raise ValueError(f"media archive contains a link or non-regular special entry: {member.name!r}")

            if normalized in seen:
                raise ValueError(f"media archive has duplicate normalized path: {normalized!r}")
            # A regular file cannot be an ancestor of another entry, and a
            # file cannot replace a directory needed by a later entry.
            pieces = normalized.split("/")
            for index in range(1, len(pieces)):
                parent = "/".join(pieces[:index])
                if seen.get(parent) is False:
                    raise ValueError(f"media archive path conflicts with regular file: {parent!r}")
            if not is_dir and any(existing.startswith(normalized + "/") for existing in seen):
                raise ValueError(f"media archive regular file conflicts with child path: {normalized!r}")
            seen[normalized] = is_dir
            inspected.append((member, normalized))

        # getmembers() validates headers but may skip payload bytes. Read all
        # regular members now, before a staging directory is created, so a
        # truncated or checksum-invalid compressed archive fails preflight.
        for member, _ in inspected:
            if member.isdir():
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"media archive member has no readable payload: {member.name!r}")
            read_size = 0
            with source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    read_size += len(block)
            if read_size != member.size:
                raise ValueError(f"media archive member is truncated: {member.name!r}")
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("unsafe ", "media archive")):
            raise
        raise ValueError("media archive is corrupt or unreadable") from exc
    return inspected


def _verify_gzip_stream(archive_path: str | os.PathLike[str]) -> None:
    """Consume the entire gzip stream so its CRC and length trailer are checked."""
    import zlib

    try:
        with gzip.open(archive_path, mode="rb") as compressed:
            for _ in iter(lambda: compressed.read(1024 * 1024), b""):
                pass
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("media archive gzip stream is corrupt or unreadable") from exc


def inspect_media_archive(archive_path: str | os.PathLike[str]) -> list[str]:
    """Inspect and fully read a media tar.gz, returning regular-file paths.

    No filesystem writes occur. Only ordinary files and directories are
    accepted; path escapes, links, special files, conflicts and duplicates
    are rejected before extraction is attempted.
    """
    _verify_gzip_stream(archive_path)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            return [name for member, name in _inspect_open_media_tar(archive) if not member.isdir()]
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("media archive is corrupt or unreadable") from exc


def stage_media_archive(
    archive_path: str | os.PathLike[str],
    staging_parent: str | os.PathLike[str] | None = None,
) -> Path:
    """Validate then safely extract a media archive into a new private directory.

    The destination media directory is never accepted here: callers receive a
    fresh staging path which can be inspected before any restore mutation.
    """
    _verify_gzip_stream(archive_path)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            inspected = _inspect_open_media_tar(archive)
            stage_name = tempfile.mkdtemp(prefix="cookbook-restore-stage-", dir=staging_parent)
            stage = Path(stage_name)
            try:
                root = stage.resolve()
                for member, normalized in inspected:
                    target = stage.joinpath(*normalized.split("/"))
                    if not target.resolve().is_relative_to(root):
                        raise ValueError(f"unsafe extracted path: {normalized!r}")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"media archive member has no readable payload: {member.name!r}")
                    with source, open(target, "xb") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                    os.chmod(target, 0o644)
                return stage
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError("media archive is corrupt or unreadable") from exc


def validate_restore_targets(
    *,
    db_tables: set[str],
    unsupported_objects: list[str],
    media_dir: str | os.PathLike[str],
    replace: bool,
) -> dict[str, bool]:
    """Check target schema and media occupancy without modifying either target."""
    validate_supported_database_schema(db_tables, unsupported_objects)

    database_nonempty = bool(db_tables)
    if database_nonempty and not replace:
        raise ValueError("target database is non-empty; pass --replace to authorize replacement")

    target = Path(media_dir)
    if target.is_symlink():
        raise ValueError("media target directory must not be a symlink")
    if target.exists() and not target.is_dir():
        raise ValueError("media target must be a directory")
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("media target parent must be an existing ordinary directory")

    media_nonempty = False
    if target.exists():
        try:
            stack = [target]
            while stack:
                directory = stack.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        media_nonempty = True
                        if entry.is_symlink():
                            raise ValueError("media target contains a symlink")
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif not entry.is_file(follow_symlinks=False):
                            raise ValueError("media target contains a non-regular entry")
        except OSError as exc:
            raise ValueError("media target cannot be inspected") from exc
    if media_nonempty and not replace:
        raise ValueError("media target is non-empty; pass --replace to authorize replacement")
    return {"database_nonempty": database_nonempty, "media_nonempty": media_nonempty}


def validate_supported_database_schema(db_tables: set[str], unsupported_objects: list[str]) -> None:
    """Reject schema objects outside the application tables and Alembic state."""
    supported_tables = set(SUPPORTED_TABLES) | {"alembic_version"}
    unknown_tables = sorted(set(db_tables) - supported_tables)
    if unknown_tables:
        raise ValueError(f"target database contains unknown table(s): {', '.join(unknown_tables)}")
    if unsupported_objects:
        raise ValueError(
            "target database contains unsupported schema object(s): "
            + ", ".join(sorted(unsupported_objects))
        )


async def _read_target_schema_inventory(database_url, create_async_engine=None) -> tuple[set[str], list[str]]:
    """Read target tables and unsupported schema objects using a short-lived engine."""
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import create_async_engine as default_create_async_engine

    engine_factory = create_async_engine or default_create_async_engine

    async def _run() -> tuple[set[str], list[str]]:
        engine = engine_factory(database_url, echo=False)
        try:
            async with engine.connect() as conn:
                table_rows = (await conn.execute(sa_text(
                    "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE()"
                ))).all()
                trigger_rows = (await conn.execute(sa_text(
                    "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA = DATABASE()"
                ))).all()
                routine_rows = (await conn.execute(sa_text(
                    "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
                    "WHERE ROUTINE_SCHEMA = DATABASE()"
                ))).all()
                event_rows = (await conn.execute(sa_text(
                    "SELECT EVENT_NAME FROM information_schema.EVENTS "
                    "WHERE EVENT_SCHEMA = DATABASE()"
                ))).all()
            tables: set[str] = set()
            objects: list[str] = []
            for name, table_type in table_rows:
                if table_type == "BASE TABLE":
                    tables.add(name)
                else:
                    objects.append(f"{table_type}:{name}")
            objects.extend(f"TRIGGER:{row[0]}" for row in trigger_rows)
            objects.extend(f"{row[1]}:{row[0]}" for row in routine_rows)
            objects.extend(f"EVENT:{row[0]}" for row in event_rows)
            return tables, objects
        finally:
            await engine.dispose()

    return await _run()


def read_target_schema_inventory(database_url, create_async_engine=None) -> tuple[set[str], list[str]]:
    """Read schema inventory; engine creation, use, and disposal share one loop."""
    import asyncio

    return asyncio.run(_read_target_schema_inventory(database_url, create_async_engine))


def preflight_restore(args, *, inventory=None, staging_parent=None) -> dict[str, object]:
    """Validate all restore inputs and targets, then stage media without mutation."""
    manifest = verify_backup_sources(args.sql, args.media_archive, args.manifest)
    media_files = inspect_media_archive(args.media_archive)
    if len(media_files) != manifest["media_file_count"]:
        raise ValueError("media archive file count does not match the manifest")

    if inventory is None:
        try:
            db_tables, unsupported_objects = read_target_schema_inventory(args.db_url)
        except Exception as exc:
            # Do not expose a credential-bearing target URL or database error.
            raise ValueError("target database inventory failed") from exc
    else:
        db_tables, unsupported_objects = inventory
    occupancy = validate_restore_targets(
        db_tables=db_tables,
        unsupported_objects=unsupported_objects,
        media_dir=args.media_dir,
        replace=args.replace,
    )
    parent = staging_parent if staging_parent is not None else Path(args.media_dir).parent
    staged_media = stage_media_archive(args.media_archive, parent)
    return {
        "manifest": manifest,
        "media_stage": staged_media,
        "db_tables": set(db_tables),
        **occupancy,
    }


def _build_import_argv(defaults_path: str | os.PathLike[str], database: str) -> list[str]:
    """Build an explicit-database MariaDB import command with no credentials in argv."""
    return [
        "mariadb",
        f"--defaults-extra-file={os.fspath(defaults_path)}",
        database,
    ]


def run_mariadb_import(
    sql_path: str | os.PathLike[str],
    database_url: str,
    defaults_dir: str | os.PathLike[str],
    *,
    popen_factory=None,
) -> None:
    """Stream a gzip SQL dump into MariaDB, keeping connection secrets off argv."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    if url.get_backend_name() != "mysql" or not url.database or not url.username:
        raise ValueError("restore database URL must identify a MySQL-compatible database and user")
    defaults_path = Path(defaults_dir) / "cookbook-mariadb-restore.cnf"
    write_credentials_defaults_file(
        defaults_path,
        host=url.host or "localhost",
        port=url.port or 3306,
        user=url.username,
        password=url.password,
    )
    process = None
    try:
        factory = popen_factory or subprocess.Popen
        process = factory(
            _build_import_argv(defaults_path, url.database),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        if process.stdin is None:
            raise ValueError("SQL import failed")
        with gzip.open(sql_path, mode="rb") as source:
            shutil.copyfileobj(source, process.stdin, length=1024 * 1024)
        process.stdin.close()
        if process.wait() != 0:
            raise ValueError("SQL import failed")
    except Exception as exc:
        if process is not None:
            try:
                process.kill()
                process.wait()
            except Exception:
                pass
        raise ValueError("SQL import failed") from exc
    finally:
        try:
            defaults_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Best-effort cleanup; the enclosing staging directory is also
            # removed by run_restore_command on every exit path.
            pass


async def _clear_supported_database_async(database_url, existing_tables, create_async_engine=None) -> None:
    """Delete rows from known cookbook tables only, in reverse FK order."""
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import create_async_engine as default_create_async_engine

    engine_factory = create_async_engine or default_create_async_engine

    async def _run() -> None:
        engine = engine_factory(database_url, echo=False)
        try:
            async with engine.begin() as conn:
                for table in reversed(Base.metadata.sorted_tables):
                    if table.name in existing_tables:
                        await conn.execute(table.delete())
                if "alembic_version" in existing_tables:
                    await conn.execute(sa_text("DELETE FROM alembic_version"))
        finally:
            await engine.dispose()

    await _run()


def clear_supported_database(database_url, existing_tables, *, create_async_engine=None) -> None:
    """Clear known tables, creating/using/disposing the async engine in one loop."""
    import asyncio

    return asyncio.run(
        _clear_supported_database_async(database_url, existing_tables, create_async_engine)
    )


def _replace_media_target(staged_media: str | os.PathLike[str], media_target: str | os.PathLike[str]) -> None:
    """Move staged media into place, keeping the previous directory for rollback."""
    staged = Path(staged_media)
    target = Path(media_target)
    parent = target.parent
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("media target changed after preflight")

    previous: Path | None = None
    if target.exists():
        previous = Path(tempfile.mkdtemp(prefix=".cookbook-restore-previous-", dir=parent))
        previous.rmdir()
        os.replace(target, previous)
    try:
        os.replace(staged, target)
    except Exception:
        if previous is not None and previous.exists() and not target.exists():
            os.replace(previous, target)
        raise
    if previous is not None:
        shutil.rmtree(previous)


def _check_media_writable(media_dir: str | os.PathLike[str]) -> None:
    """Prove the restored media directory accepts a private create/delete."""
    fd = None
    path = None
    try:
        fd, name = tempfile.mkstemp(prefix=".cookbook-ready-", dir=media_dir)
        path = Path(name)
        os.close(fd)
        fd = None
        path.unlink()
    except OSError as exc:
        raise ValueError("media readiness check failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def run_restore_command(args) -> int:
    """Preflight, replace explicitly authorized targets, and verify the restore."""
    phase = "preflight"
    staged_media: Path | None = None
    try:
        result = preflight_restore(args)
        staged_media = result["media_stage"]

        phase = "database clear"
        if args.replace and result["database_nonempty"]:
            clear_supported_database(args.db_url, result["db_tables"])

        phase = "SQL import"
        run_mariadb_import(args.sql, args.db_url, staged_media)

        phase = "media replacement"
        _replace_media_target(staged_media, args.media_dir)

        phase = "verification"
        actual_counts, actual_revision = _async_db_snapshot(args.db_url, None)
        manifest = result["manifest"]
        if actual_revision != manifest["alembic_revision"] or actual_counts != manifest["table_row_counts"]:
            raise ValueError("database verification mismatch")
        if len(list_media_files(args.media_dir)) != manifest["media_file_count"]:
            raise ValueError("media verification mismatch")

        phase = "readiness"
        _check_media_writable(args.media_dir)
        print("restore complete")
        return 0
    except Exception:
        print(f"restore failed ({phase})", file=sys.stderr)
        return 1
    finally:
        if staged_media is not None and staged_media.exists():
            shutil.rmtree(staged_media, ignore_errors=True)


def artifact_pairs(
    *,
    sql_name: str,
    sql_sha256: str,
    media_name: str,
    media_sha256: str,
) -> list[dict[str, str]]:
    """Build the manifest's artifact list from one SQL dump + one media archive.

    Each name is checked against its own naming contract (not just 'a valid
    artifact name') so a crossed-wiring where the media slot holds the SQL
    file name is rejected rather than shipped. The two names must also name
    the same real UTC backup day: the digit-pattern regexes accept a 13th
    month or 40th day, so the dates are re-checked for calendar validity, and
    cross-dating the two artifacts would silently produce an internally
    inconsistent manifest.
    """
    sql_match = _SQL_NAME_RE.fullmatch(sql_name) if isinstance(sql_name, str) else None
    if sql_match is None:
        raise ValueError(f"sql_name {sql_name!r} does not match the SQL naming contract")
    media_match = _MEDIA_NAME_RE.fullmatch(media_name) if isinstance(media_name, str) else None
    if media_match is None:
        raise ValueError(f"media_name {media_name!r} does not match the media archive naming contract")
    # The digit-pattern regexes accept a 13th month or 40th day, so the
    # dates are re-checked for calendar validity before they are compared.
    for label, day in (("sql_name", sql_match["date"]), ("media_name", media_match["date"])):
        try:
            datetime.date.fromisoformat(day)
        except ValueError:
            raise ValueError(f"{label} date {day!r} is not a real calendar date") from None
    sql_date = sql_match["date"]
    media_date = media_match["date"]
    if not _is_valid_sha256(sql_sha256):
        raise ValueError(f"sql_sha256 must be 64 lowercase hex chars, got {sql_sha256!r}")
    if not _is_valid_sha256(media_sha256):
        raise ValueError(f"media_sha256 must be 64 lowercase hex chars, got {media_sha256!r}")
    if sql_date != media_date:
        raise ValueError(
            "sql_name and media_name must share the same UTC backup date, "
            f"got {sql_date} and {media_date}"
        )
    return [
        {"name": sql_name, "sha256": sql_sha256},
        {"name": media_name, "sha256": media_sha256},
    ]


# ---------------------------------------------------------------------------
# Known Alembic revisions
# ---------------------------------------------------------------------------

def is_supported_alembic_revision(revision: object) -> bool:
    """True only for revisions inside the supported versions chain (v0..v7)."""
    return isinstance(revision, str) and revision in _KNOWN_ALEMBIC_REVISIONS


# ---------------------------------------------------------------------------
# Backup orchestration (Task 2)
#
# `run_backup_command` is the only entry point the CLI dispatches to; every
# other function below is a focused, testable stage. Design constraints this
# section enforces (see 2026-11-09-backup-restore-design.md and the plan's
# Global Constraints):
#
#   * Credentials are written to a short-lived private defaults file that is
#     the FIRST option of the `mariadb-dump` argv. The password never appears
#     in argv, on the process list, or in any captured subprocess output.
#   * `mariadb-dump` runs with `--single-transaction` (InnoDB-consistent
#     snapshot), `--skip-lock-tables` (do not take table-level locks) and
#     `--skip-dump-date` (byte-deterministic header across runs). `--quick`
#     is used if the client supports it; we do NOT add unsupported boolean
#     syntax such as `--routines=false` (MariaDB 10.4+ clients omit
#     procedures/events by default, so no flag is needed).
#   * The database snapshot (row counts + alembic_version) is taken AFTER the
#     SQL dump has been written, so the manifest's counts describe what the
#     dump actually captured rather than a racy pre-dump count.
#   * Media is archived into a temporary file alongside the SQL dump. Only
#     after BOTH stages succeed do we publish the temp artifacts to their
#     final names with atomic no-clobber links and publish the manifest last. The
#     manifest is never published if either stage fails (tests assert this).
#   * Existing artifact paths are never overwritten (no --overwrite flag):
#     if any of the three final paths already exists, the run fails before
#     writing anything new.
#   * `out` must not be inside `MEDIA_DIR` — backing up into the very tree
#     you're tarring would create a self-referential archive and a
#     write-during-read race.
#   * Every error message is a fixed string that names only the failed stage
#     (never the database URL, host, user, password, or any path beyond the
#     `--out` directory name) and is additionally passed through a
#     redaction pass keyed off the configured credentials.
# ---------------------------------------------------------------------------

def _build_dump_argv(defaults_path: str | os.PathLike[str], out_sql: str | os.PathLike[str], database: str) -> list[str]:
    """Return the full `mariadb-dump` argv.

    The defaults file MUST be the first option (it is a file read by the
    child before any other argument is processed): the owner uses this file
    for host/port/user/password and nothing else. The two dump flags are
    pinned:
      * `--single-transaction`  -> InnoDB consistency without locking tables.
      * `--skip-lock-tables`    -> avoid FTWRL on the dump; combined with the
                                   above this is the documented recipe for
                                   non-blocking dumps of a busy InnoDB DB.
      * `--skip-dump-date`      -> the header line is the one variable that
                                   would otherwise change between runs;
                                   turning it off keeps the stream stable.
    """
    return [
        "mariadb-dump",
        f"--defaults-extra-file={os.fspath(defaults_path)}",
        "--single-transaction",
        "--skip-lock-tables",
        "--skip-dump-date",
        "--quick",
        f"--result-file={os.fspath(out_sql)}",
        database,
    ]


def _defaults_file_value(value: str) -> str:
    """Render one string value for a MariaDB options (defaults) file.

    The value is double-quoted so that option-file metacharacters are
    interpreted literally:
      * ``#`` and ``;`` would otherwise start a comment and truncate the
        value (a password ending in ``;admin`` would be silently clipped);
      * ``=`` and leading/trailing spaces are preserved;
      * an unescaped ``"`` would close the string early.
    Inside the quotes the only escape character is the backslash, so a
    backslash is doubled and an embedded double-quote is backslash-escaped.
    Because we control the line, the value must be a single line: NUL (an
    options file is byte-oriented), CR and LF are all rejected outright
    rather than written, since a newline would let an attacker append an
    extra ``key=value`` option to the file (e.g. ``password=abc\\nuser=evil``).
    """
    if "\x00" in value:
        raise ValueError("defaults-file value must not contain a NUL byte")
    if "\r" in value or "\n" in value:
        raise ValueError("defaults-file value must not contain a newline (CR or LF)")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_credentials_defaults_file(path: str | os.PathLike[str], host: str, port: int, user: str, password: str | None) -> None:
    """Write a MariaDB defaults file with owner-only permissions.

    The file's lifetime is bounded by the enclosing `run_backup_command`
    `finally` block, which unlinks it on every path (success or failure).
    Only `host`, `port`, `user` and (when configured) `password` are written
    — no `database` line, so the file cannot be used against an unintended
    schema. String values are quoted/escaped via `_defaults_file_value` so
    spaces, `#`, `;`, `=`, `"` and `\\` round-trip safely; NUL/CR/LF reject.
    """
    lines = [
        "[client]",
        f"host={_defaults_file_value(host)}",
        f"port={port}",
        f"user={_defaults_file_value(user)}",
    ]
    if password is not None:
        lines.append(f"password={_defaults_file_value(password)}")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise RuntimeError(f"Could not create defaults-extra-file at {path}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        # Best-effort hardening for filesystems where the mode in os.open is
        # not preserved (common on POSIX with umask=077): the file must be
        # owner-only before the child process can see it.
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    """Blank any configured credential substring out of a message.

    Credentials are replaced with a constant token so that a failure that
    echoes host/user/password (common in client error text) cannot leak it
    into stderr or logs. EVERY non-empty configured credential is redacted,
    including 1-3 character strings: a credential is a credential regardless
    of length, so an empty value (nothing configured) is the only one we
    skip. We deliberately do not special-case short values — leaving them
    un-redacted would leak exactly the kind of short, memorable secret
    (a 'db' user, a '1' password) that this exists to protect.
    """
    for secret in secrets:
        if not secret:
            continue
        if secret in text:
            text = text.replace(secret, "<redacted>")
    return text


def _async_db_snapshot(database_url, create_async_engine=None) -> tuple[dict[str, int], str | None]:
    """Count rows of every supported table and read the single alembic_version.

    The async engine is created, used and disposed all inside ONE asyncio
    event loop. SQLAlchemy's async engine is bound to the loop it is first
    used in, so creating one loop for the queries and a second for dispose()
    (the previous implementation) is invalid and can hang or leak. Here a
    single `asyncio.run` drives the whole lifecycle: build the engine, take
    the snapshot, then dispose it in the same finally. The alembic value is
    expected to be exactly one row; any other shape is treated as a failure
    (returning None) so the manifest never records an ambiguous head.
    """
    import asyncio
    from sqlalchemy import func, select, text as sa_text
    from sqlalchemy.ext.asyncio import create_async_engine as default_create_async_engine

    engine_factory = create_async_engine or default_create_async_engine

    async def _run() -> tuple[dict[str, int], str | None]:
        engine = engine_factory(database_url, echo=False)
        try:
            counts: dict[str, int] = {}
            async with engine.connect() as conn:
                table_rows = (await conn.execute(sa_text(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'"
                ))).all()
            existing_tables = {row[0] for row in table_rows}
            for table_name in sorted(Base.metadata.tables):
                if table_name not in existing_tables:
                    # A known older Alembic revision may not have introduced
                    # every table represented by the current model metadata.
                    # Its manifest still records the current supported table
                    # inventory, with absent-table counts represented as 0.
                    counts[table_name] = 0
                    continue
                table = Base.metadata.tables[table_name]
                async with engine.connect() as conn:
                    result = await conn.execute(select(func.count()).select_from(table))
                counts[table_name] = int(result.scalar_one())
            revision: str | None = None
            async with engine.connect() as conn:
                rows = (await conn.execute(sa_text("SELECT version_num FROM alembic_version"))).all()
            if len(rows) == 1 and isinstance(rows[0][0], str) and rows[0][0]:
                revision = rows[0][0]
            return counts, revision
        finally:
            # Dispose in the SAME loop the engine was used in.
            await engine.dispose()

    return asyncio.run(_run())


def run_backup_command(args) -> int:
    """Run the backup; return 0 on success, 1 on any failure.

    ``args.out`` is the output directory (required). DB/media configuration
    comes from the shared ``cli_config()``. Stages (each isolated so a
    mid-stage failure never publishes a partial set):

      1. Validate ``--out`` (exists, is a directory) and that it is not
         inside ``MEDIA_DIR`` (self-archive + write-during-read race).
      2. Check no-overwrite: any of the three final artifact paths may
         already exist — that is a refusal before we write anything new.
      3. Write a short-lived, owner-only (0600) MariaDB defaults file.
      4. Run ``mariadb-dump`` with the defaults file as the FIRST option
         and stream the raw SQL through a deterministic gzip (mtime=0) into
         the SQL artifact's temp location.
      5. Build the media archive temp via the deterministic media archiver.
      6. Take the DB snapshot (per-table row counts + alembic revision).
      7. Serialise the manifest to a temp location.
       8. Atomic-publish SQL, then media, then manifest (manifest last) via
          ``os.link`` (hard-link, no-clobber) into ``--out``. If the publish
          link fails (e.g. the path was created by another process after the
          precheck), only the files THIS run already published are unlinked,
          so ``--out`` is either the untouched original directory or a
          complete three-file set — never a partial, and a foreign file is
          never removed.
      9. Always (success, failure, or exception) remove the temp dir, all
         temp artifacts, and the private defaults file.
    """
    out_path: Path = Path(os.fspath(args.out))
    if not out_path.is_dir():
        print(f"backup failed (validation): --out is not a directory: {out_path}", file=sys.stderr)
        return 1

    cfg = cli_config()
    media_dir: Path = Path(os.fspath(cfg.media_dir))
    if not media_dir.is_dir():
        print(f"backup failed (validation): MEDIA_DIR is not a directory: {media_dir}", file=sys.stderr)
        return 1

    # Guard: --out must not be inside MEDIA_DIR. Compare on realpath so a
    # symlinked --out cannot sneak across the boundary.
    out_resolved = os.path.realpath(out_path)
    media_resolved = os.path.realpath(media_dir)
    if out_resolved == media_resolved or out_resolved.startswith(media_resolved + os.sep):
        print("backup failed (validation): --out must not be inside MEDIA_DIR", file=sys.stderr)
        return 1

    day = utc_backup_date(datetime.datetime.now(datetime.timezone.utc))
    sql_name = sql_artifact_name(day)
    media_name = media_archive_name(day)
    manifest_file = manifest_name(day)
    sql_final = out_path / sql_name
    media_final = out_path / media_name
    manifest_final = out_path / manifest_file

    # No-overwrite contract: any existing final path is a refusal.
    existing = [p for p in (sql_final, media_final, manifest_final) if p.exists()]
    if existing:
        for p in existing:
            print(f"backup failed (no-overwrite): existing file: {p}", file=sys.stderr)
        return 1

    # A database dump is imported verbatim at restore time. Refuse to create
    # one if the source contains user-defined tables or schema objects that
    # the restore contract cannot safely reconstruct.
    try:
        source_tables, source_objects = read_target_schema_inventory(cfg.database_url)
        validate_supported_database_schema(source_tables, source_objects)
    except Exception:
        print("backup failed (schema inventory): unsupported or unreadable source schema", file=sys.stderr)
        return 1

    # Staging area for all temp files (raw dump, artifacts, defaults file).
    # It MUST be on the same volume as --out, so a temporary directory is
    # created next to --out (not in the system temp dir) via a unique
    # tempfile.mkdtemp name. A unique name avoids a pre-existing/stale
    # stage-dir collision, and because os.link() is used to publish, that
    # link is only valid within a single filesystem -- same volume required.
    try:
        stage_dir_str = tempfile.mkdtemp(prefix=".cookbook-backup-", dir=os.fspath(out_path))
    except OSError:
        print("backup failed (staging): could not create temporary directory", file=sys.stderr)
        return 1
    stage_dir = Path(stage_dir_str)
    # mkdtemp already creates 0o700, but repeat to be explicit for platforms
    # where the mode is not preserved (mirrors the defaults-file hardening).
    try:
        os.chmod(stage_dir_str, 0o700)
    except OSError:
        pass

    raw_sql = stage_dir / sql_name.replace(".sql.gz", ".sql")   # e.g. cookbook-<day>.sql
    temp_sql = stage_dir / sql_name                               # e.g. cookbook-<day>.sql.gz
    temp_media = stage_dir / media_name
    defaults_path = stage_dir / "cookbook-mariadb-my.cnf"
    manifest_temp = stage_dir / manifest_file

    try:
        # -- Stage 3: private defaults file (0600, owner-only).
        try:
            write_credentials_defaults_file(
                defaults_path,
                host=cfg.db_host, port=cfg.db_port,
                user=cfg.db_user, password=cfg.db_password,
            )
        except (OSError, RuntimeError) as exc:
            print(f"backup failed (defaults-file): {type(exc).__name__}", file=sys.stderr)
            return 1

        # -- Stage 4: dump + deterministic gzip.
        argv = _build_dump_argv(defaults_path, raw_sql, cfg.db_name)
        try:
            proc = subprocess.run(
                argv, shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            print(f"backup failed (mariadb-dump): {type(exc).__name__}", file=sys.stderr)
            return 1
        if proc.returncode != 0:
            err = _redact(
                proc.stderr.decode("utf-8", "replace") if proc.stderr else "",
                (cfg.db_password or "", cfg.db_user, cfg.db_host, cfg.db_name),
            )
            print(f"backup failed (mariadb-dump): stage error code={proc.returncode}: {err}", file=sys.stderr)
            return 1
        if not raw_sql.is_file():
            print("backup failed (mariadb-dump): dump file not produced", file=sys.stderr)
            return 1
        try:
            gzip_file_to(raw_sql, temp_sql)
        except OSError as exc:
            print(f"backup failed (gzip): {type(exc).__name__}", file=sys.stderr)
            return 1

        # -- Stage 5: media archive.
        try:
            create_media_archive(media_dir, temp_media)
        except (OSError, ValueError) as exc:
            print(f"backup failed (media-archive): {type(exc).__name__}", file=sys.stderr)
            return 1
        if not temp_media.is_file():
            print("backup failed (media-archive): archive not produced", file=sys.stderr)
            return 1

        # -- Stage 6: DB snapshot (row counts + alembic revision). The async
        #    engine is created, used and disposed inside ONE asyncio event
        #    loop (previously the snapshot ran on one asyncio.run() and
        #    dispose() on a second, which is invalid for an async engine that
        #    holds loop-bound connection state).
        from sqlalchemy.ext.asyncio import create_async_engine
        row_counts, revision = _async_db_snapshot(cfg.database_url, create_async_engine)
        if not revision or not is_supported_alembic_revision(revision):
            print(
                "backup failed (snapshot): alembic_version is missing or not "
                "in the supported chain",
                file=sys.stderr,
            )
            return 1

        # -- Stage 7: serialise the manifest to a temp file (validated).
        media_count = len(list_media_files(media_dir))
        manifest_data = {
            "version": 1,
            "artifacts": artifact_pairs(
                sql_name=sql_name,
                sql_sha256=file_sha256(temp_sql),
                media_name=media_name,
                media_sha256=file_sha256(temp_media),
            ),
            "alembic_revision": revision,
            "table_row_counts": row_counts,
            "media_file_count": media_count,
        }
        try:
            with open(manifest_temp, "w", encoding="utf-8", newline="") as handle:
                handle.write(serialize_manifest(manifest_data))
        except (OSError, ValueError) as exc:
            print(f"backup failed (manifest): {type(exc).__name__}", file=sys.stderr)
            return 1

        # -- Stage 8: atomic, no-clobber publish (SQL, media, manifest --
        #    manifest last). os.link() fails with FileExistsError if the
        #    final path already exists, so we can never overwrite a path
        #    created by another process (including one raced in after the
        #    no-overwrite precheck). Only files this run successfully linked
        #    are unlinked on failure, so a foreign artifact is never removed.
        published: list[str] = []
        try:
            for stage_temp, stage_final in (
                (temp_sql, sql_final),
                (temp_media, media_final),
                (manifest_temp, manifest_final),
            ):
                os.link(str(stage_temp), str(stage_final))
                published.append(str(stage_final))
        except OSError as exc:
            # Roll back ONLY files this run published. A partial set of
            # SQL-only or media-only files is exactly the "apparently
            # complete" artifact the spec forbids, so we undo them.
            for p in reversed(published):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            print(f"backup failed (publish): {type(exc).__name__}", file=sys.stderr)
            return 1
        return 0
    finally:
        # -- Stage 9: always remove the private defaults file + temp artifacts.
        for p in (defaults_path, raw_sql, temp_sql, temp_media, manifest_temp):
            try:
                os.unlink(os.fspath(p))
            except OSError:
                pass
        try:
            shutil.rmtree(str(stage_dir), ignore_errors=True)
        except Exception:
            pass


def gzip_file_to(src: str | os.PathLike[str], dst: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> None:
    """Read ``src`` and write it as a deterministic gzip to ``dst``.

    The gzip header is pinned (mtime=0, empty filename) so byte-identical
    inputs yield byte-identical output; the caller uses this after a SQL dump
    to turn the raw ``.sql`` into the final ``.sql.gz`` artifact.

    The compressed stream is written directly to ``dst`` in fixed chunks: it
    is never buffered as one in-memory blob (a BytesIO holding the whole zip
    would peak at a full copy of the dump for large databases).
    """
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError(f"chunk_size must be a positive integer, got {chunk_size!r}")
    with open(src, "rb") as src_handle, open(dst, "wb") as dst_handle:
        # Wrap the open file handle directly so the compressed bytes stream
        # straight to disk; GzipFile handles the gzip trailer on close().
        with gzip.GzipFile(filename="", mode="wb", fileobj=dst_handle, mtime=0) as gz:
            for block in iter(lambda: src_handle.read(chunk_size), b""):
                gz.write(block)
