"""Unit tests for the pure backup helpers (Task 20, Task 2 slice).

Covers the deterministic, side-effect-free building blocks of the backup
contract in docs/superpowers/specs/2026-11-09-backup-restore-design.md:
UTC artifact naming, streaming SHA-256, safe sorted media enumeration,
deterministic tar creation, and versioned manifest serialization/validation.

No database, no subprocesses, no .env access — the helpers under test are
pure filesystem/string work, provable with tmp_path fixtures only.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import tarfile

import pytest

from cookbook import cli_backup
from cookbook.models import Base

# Locked in against models.py so the manifest contract cannot silently drift
# from the real supported schema inventory (the 14 application tables).
SUPPORTED_TABLES = frozenset(Base.metadata.tables)


def _hexdigest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


def _valid_manifest(**overrides) -> dict[str, object]:
    """A structurally valid v1 manifest; tests mutate via overrides."""
    manifest: dict[str, object] = {
        "version": 1,
        "artifacts": [
            {"name": "cookbook-2026-11-09.sql.gz", "sha256": _hexdigest(b"sql-bytes")},
            {"name": "media-2026-11-09.tar.gz", "sha256": _hexdigest(b"media-bytes")},
        ],
        "alembic_revision": "6d22befb6ec9",
        "table_row_counts": {**{table: 0 for table in sorted(SUPPORTED_TABLES)}, "recipes": 3, "users": 1},
        "media_file_count": 2,
    }
    manifest.update(overrides)
    return manifest


# ---------------------------------------------------------------------------
# UTC artifact names
# ---------------------------------------------------------------------------

def test_utc_date_uses_the_instant_not_local_time() -> None:
    # UTC-evening instants are not "tomorrow"; boundaries use the instant.
    assert cli_backup.utc_backup_date(_utc(2026, 11, 9, 19)) == "2026-11-09"
    assert cli_backup.utc_backup_date(_utc(2026, 11, 9, 23, 59)) == "2026-11-09"
    assert cli_backup.utc_backup_date(_utc(2026, 11, 10, 0, 0)) == "2026-11-10"
    assert cli_backup.utc_backup_date(_utc(2026, 12, 31, 23)) == "2026-12-31"
    assert cli_backup.utc_backup_date(_utc(2027, 1, 1, 0)) == "2027-01-01"


def test_utc_date_converts_other_timezones() -> None:
    plus8 = datetime.timezone(datetime.timedelta(hours=8))
    instant = datetime.datetime(2026, 11, 9, 12, tzinfo=plus8)  # 04:00 UTC the same day
    assert cli_backup.utc_backup_date(instant) == "2026-11-09"
    # An instant that is "next day" in UTC but not locally must follow UTC.
    minus5 = datetime.timezone(datetime.timedelta(hours=-5))
    rollover = datetime.datetime(2026, 11, 10, 2, tzinfo=minus5)  # 07:00 UTC Nov 10
    assert cli_backup.utc_backup_date(rollover) == "2026-11-10"


def test_artifact_names_follow_the_naming_contract() -> None:
    day = cli_backup.utc_backup_date(_utc(2026, 11, 9))
    assert cli_backup.sql_artifact_name(day) == "cookbook-2026-11-09.sql.gz"
    assert cli_backup.media_archive_name(day) == "media-2026-11-09.tar.gz"
    assert cli_backup.manifest_name(day) == "manifest-2026-11-09.json"


def test_artifact_name_helpers_reject_bad_dates() -> None:
    with pytest.raises(ValueError):
        cli_backup.sql_artifact_name("2026-11-09 ")
    with pytest.raises(ValueError):
        cli_backup.media_archive_name("11-09-2026")
    with pytest.raises(ValueError):
        cli_backup.manifest_name("junk")
    with pytest.raises(ValueError):  # not a real calendar date
        cli_backup.sql_artifact_name("2026-13-40")


def test_utc_backup_date_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        cli_backup.utc_backup_date(datetime.datetime(2026, 11, 9, 12))


# ---------------------------------------------------------------------------
# Streaming SHA-256
# ---------------------------------------------------------------------------

def test_streaming_sha256_matches_hashlib(tmp_path) -> None:
    payload = os.urandom(700_000)  # spans many reads, not a single block
    target = tmp_path / "payload.bin"
    target.write_bytes(payload)
    assert cli_backup.file_sha256(target) == _hexdigest(payload)


def test_streaming_sha256_is_independent_of_chunk_size(tmp_path) -> None:
    payload = b"a" * 32 + b"b" * 4096 + b"c" * 5  # odd, non-multiple size
    target = tmp_path / "chunk.bin"
    target.write_bytes(payload)
    expected = _hexdigest(payload)
    for chunk in (1, 33, 4096, 1_048_576):
        assert cli_backup.file_sha256(target, chunk_size=chunk) == expected


def test_streaming_sha256_streams_binary_bytes_untouched(tmp_path) -> None:
    # Non-UTF-8 bytes must digest identically — a text-mode reader would
    # corrupt the digest of binary media files.
    payload = bytes(range(256))
    target = tmp_path / "raw.bin"
    target.write_bytes(payload)
    assert cli_backup.file_sha256(target) == _hexdigest(payload)


# ---------------------------------------------------------------------------
# Sorted, safe media enumeration
# ---------------------------------------------------------------------------

def test_list_media_files_returns_sorted_relative_paths(tmp_path) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "zeta" / "z2.jpg").write_bytes(b"z2")
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "one.jpg").write_bytes(b"one")
    (tmp_path / "alpha" / "deep").mkdir(parents=True)
    (tmp_path / "alpha" / "deep" / "three.jpg").write_bytes(b"three")
    (tmp_path / "top.png").write_bytes(b"top")
    paths = cli_backup.list_media_files(tmp_path)
    assert paths == [
        "alpha/deep/three.jpg",
        "alpha/one.jpg",
        "top.png",
        "zeta/z2.jpg",
    ]
    assert paths == sorted(paths)


def test_list_media_files_uses_forward_slashes(tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.jpg").write_bytes(b"x")
    paths = cli_backup.list_media_files(tmp_path)
    assert paths == ["sub/file.jpg"]
    assert all("\\" not in p for p in paths)


def test_list_media_files_skips_directories_but_keeps_data_files(tmp_path) -> None:
    (tmp_path / "empty-dir").mkdir()  # never an archive entry
    (tmp_path / "a.jpg").write_bytes(b"a")
    (tmp_path / "note.txt").write_text("hi")  # data files in MEDIA_DIR count
    assert cli_backup.list_media_files(tmp_path) == ["a.jpg", "note.txt"]


def test_list_media_files_rejects_symlinks(tmp_path) -> None:
    # A symlink (valid or broken) is an escape/ambiguity risk in a backup
    # archive, so enumeration must fail loudly rather than skip or follow.
    link = tmp_path / "link.jpg"
    try:
        os.symlink(tmp_path / "target.jpg", link)
    except OSError:
        pytest.skip("this host does not permit creating symlinks")
    try:
        with pytest.raises(ValueError, match="symlink"):
            cli_backup.list_media_files(tmp_path)
    finally:
        os.remove(link)
    (tmp_path / "broken").symlink_to(tmp_path / "no-such-target")
    with pytest.raises(ValueError, match="symlink"):
        cli_backup.list_media_files(tmp_path)


def test_list_media_files_rejects_directories_reported_as_files(tmp_path, monkeypatch):
    # A directory in the filenames list must never be archived as a file.
    # Enumeration therefore cannot trust the walk's dir/file split: every
    # filename entry is stat'd and refused unless it is a regular file.
    stray_dir = tmp_path / "actually-a-directory"
    stray_dir.mkdir()

    def fake_walk(top, topdown=True, onerror=None, followlinks=False):
        # Lock the walk contract: never follow links, never swallow errors.
        assert followlinks is False, "backup enumeration must not follow symlinks"
        assert onerror is not None, "backup enumeration must propagate traversal failures"
        yield (os.fspath(top), [], [stray_dir.name])

    monkeypatch.setattr(cli_backup.os, "walk", fake_walk)
    with pytest.raises(ValueError, match="non-regular"):
        cli_backup.list_media_files(tmp_path)


def test_list_media_files_propagates_walk_traversal_errors(tmp_path, monkeypatch):
    # An unreadable subdirectory means the backup is incomplete, and a
    # partial backup must never be produced silently: the PermissionError
    # os.walk reports through onerror must escape list_media_files.
    def fake_walk(top, topdown=True, onerror=None, followlinks=False):
        onerror(PermissionError(f"simulated unreadable directory inside {top!r}"))
        return iter(())

    monkeypatch.setattr(cli_backup.os, "walk", fake_walk)
    with pytest.raises(PermissionError, match="simulated unreadable"):
        cli_backup.list_media_files(tmp_path)


def test_empty_media_dir_yields_no_entries(tmp_path) -> None:
    assert cli_backup.list_media_files(tmp_path) == []


def test_list_media_files_requires_an_existing_directory(tmp_path) -> None:
    with pytest.raises(ValueError):
        cli_backup.list_media_files(tmp_path / "not-there")


# ---------------------------------------------------------------------------
# Deterministic tar archive
# ---------------------------------------------------------------------------

def _archive_names(archive_path) -> list[str]:
    with tarfile.open(archive_path, "r:gz") as archive:
        return [entry.name for entry in archive]


def test_create_media_archive_is_deterministic_and_round_trips(tmp_path) -> None:
    media = tmp_path / "media"
    (media / "beta").mkdir(parents=True)
    (media / "beta" / "b.jpg").write_bytes(b"beta-pixels")
    (media / "alpha").mkdir()
    (media / "alpha" / "a.jpg").write_bytes(b"alpha-pixels")

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    cli_backup.create_media_archive(media, first)
    # Re-running on the identical tree must yield an identical byte stream:
    # fixed mtime/uid/gid/owner plus a gzip header with mtime=0.
    cli_backup.create_media_archive(media, second)
    assert first.read_bytes() == second.read_bytes()

    assert _archive_names(first) == ["alpha/a.jpg", "beta/b.jpg"]
    with tarfile.open(first, "r:gz") as archive:
        assert archive.extractfile("alpha/a.jpg").read() == b"alpha-pixels"
        assert archive.extractfile("beta/b.jpg").read() == b"beta-pixels"
        for entry in archive.getmembers():
            # Safe, portable, owner-neutral entries only.
            assert entry.isreg()
            assert entry.uid == 0 and entry.gid == 0
            assert entry.uname == "" and entry.gname == ""


def test_create_media_archive_handles_an_empty_tree(tmp_path) -> None:
    media = tmp_path / "empty-media"
    media.mkdir()
    archive = tmp_path / "empty.tar.gz"
    cli_backup.create_media_archive(media, archive)
    assert _archive_names(archive) == []


def test_create_media_archive_refuses_missing_source(tmp_path) -> None:
    with pytest.raises(ValueError):
        cli_backup.create_media_archive(tmp_path / "missing", tmp_path / "out.tar.gz")


def test_create_media_archive_streams_each_file_in_fixed_chunks(tmp_path, monkeypatch) -> None:
    # Regression guard: the writer must stream media through the tar
    # stream in fixed-size reads; it may never slurp a whole file into
    # memory. A monkeypatched module-level open() hands out handles for
    # media source files whose read() refuses whole-file calls (the
    # default size, 0, or negative), while chunked reads pass straight
    # through to the real file.
    media = tmp_path / "media"
    (media / "alpha").mkdir(parents=True)
    small_bytes = b"alpha-pixels"
    (media / "alpha" / "a.jpg").write_bytes(small_bytes)
    (media / "beta").mkdir()
    large_bytes = os.urandom(300_000)  # spans many chunks, no single read
    (media / "beta" / "b.jpg").write_bytes(large_bytes)

    real_open = open
    media_root = os.fspath(media)

    class WholeFileReadGuard:
        """Wraps a real media file handle and rejects whole-file reads."""

        def __init__(self, real_handle):
            self._real_handle = real_handle

        def read(self, size=-1):
            if size is None or not isinstance(size, int) or size <= 0:
                raise AssertionError(
                    f"whole-file read() with size={size!r}: streaming "
                    "archives must read media in fixed positive chunks"
                )
            return self._real_handle.read(size)

        def close(self):
            return self._real_handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self._real_handle.__exit__(*exc_info)

        def __getattr__(self, name):
            return getattr(self._real_handle, name)

    def guarded_open(path, mode="r", *args, **kwargs):
        # Wrap media source reads only; the archive output (and anything
        # else) keeps using the real open.
        if (
            "r" in mode
            and "w" not in mode
            and os.fspath(path).startswith(media_root + os.sep)
        ):
            return WholeFileReadGuard(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    # raising=False: the module currently resolves `open` via builtins, so
    # no module-level `open` attribute exists yet — we create one (and
    # monkeypatch removes it on test teardown).
    monkeypatch.setattr(cli_backup, "open", guarded_open, raising=False)

    archive = tmp_path / "streamed.tar.gz"
    cli_backup.create_media_archive(media, archive)

    with tarfile.open(archive, "r:gz") as tar:
        assert tar.extractfile("alpha/a.jpg").read() == small_bytes
        assert tar.extractfile("beta/b.jpg").read() == large_bytes


# ---------------------------------------------------------------------------
# Versioned manifest serialization / validation
# ---------------------------------------------------------------------------

def test_manifest_round_trip_preserves_every_field(tmp_path) -> None:
    manifest = _valid_manifest()
    path = tmp_path / "manifest.json"
    cli_backup.write_manifest(path, manifest)
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert cli_backup.load_manifest(path) == manifest


def test_manifest_serialization_is_deterministic_json() -> None:
    manifest = _valid_manifest()
    once = cli_backup.serialize_manifest(manifest)
    twice = cli_backup.serialize_manifest(json.loads(once))
    assert once == twice
    assert once.endswith("\n")
    assert json.loads(once) == manifest


def test_load_manifest_validates_before_trusting(tmp_path) -> None:
    path = tmp_path / "m.json"
    path.write_text(cli_backup.serialize_manifest(_valid_manifest()), encoding="utf-8")
    assert cli_backup.load_manifest(path)["version"] == 1

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed manifest"):
        cli_backup.load_manifest(path)


def test_load_manifest_rejects_non_object_json(tmp_path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        cli_backup.load_manifest(path)


@pytest.mark.parametrize(
    "override",
    [
        {"version": 2},
        {"version": "1"},
        {"version": True},
        {"alembic_revision": ""},
        {"alembic_revision": 1234},
        {"table_row_counts": {"recipes": "3"}},
        {"table_row_counts": {"recipes": 3}},          # not the full table set
        {"table_row_counts": {"recipes": -1}},
        {"table_row_counts": {"recipes": 3, "rogue_table": 1}},
        {"table_row_counts": "not a dict"},
        {"media_file_count": -1},
        {"media_file_count": "2"},
        {"media_file_count": True},
        {"artifacts": [{"name": "cookbook-2026-11-09.sql.gz"}]},  # missing sha256
        {
            "artifacts": [
                {"name": "media-2026-11-09.tar.gz", "sha256": "ab" * 32},
                {"name": "cookbook-2026-11-09.sql.gz", "sha256": "cd" * 32},
            ]
        },  # entries must be sorted by name
        {
            "artifacts": [
                {"name": "cookbook-2026-11-09.sql.gz", "sha256": "ab" * 32},
                {"name": "cookbook-2026-11-09.sql.gz", "sha256": "cd" * 32},
            ]
        },  # duplicate names
        {"artifacts": [{"name": "../escaped", "sha256": "ab" * 32}]},
        {"artifacts": [{"name": "c:\\abs", "sha256": "ab" * 32}]},
        {"artifacts": [{"name": "cookbook-2026-11-09.sql.gz", "sha256": "AB" * 32}]},
        {"artifacts": [{"name": "cookbook-2026-11-09.sql.gz", "sha256": "ab" * 31}]},
    ],
)
def test_manifest_validation_rejects_bad_shapes(override) -> None:
    manifest = _valid_manifest()
    manifest.update(override)
    with pytest.raises(ValueError):
        cli_backup.validate_manifest(manifest)


def test_valid_manifest_passes_validation_unchanged() -> None:
    manifest = _valid_manifest()
    validated = cli_backup.validate_manifest(manifest)
    assert validated == manifest
    # validate must not mutate the caller's copy
    manifest["version"] = 99
    assert validated["version"] == 1


def test_manifest_rejects_sql_only_artifact_set() -> None:
    # A manifest without its media archive is incomplete: restoring it would
    # lose uploaded media, so a SQL-only artifact set must be rejected.
    manifest = _valid_manifest(
        artifacts=[{"name": "cookbook-2026-11-09.sql.gz", "sha256": _hexdigest(b"sql-bytes")}],
    )
    with pytest.raises(ValueError, match="exactly one media"):
        cli_backup.validate_manifest(manifest)


def test_manifest_rejects_mismatched_artifact_dates() -> None:
    # The SQL dump and media archive must come from the same UTC backup day.
    # Two different dates means an artifact leaked in from another run, so the
    # manifest is internally inconsistent and must not be trusted.
    manifest = _valid_manifest(
        artifacts=[
            {"name": "cookbook-2026-11-09.sql.gz", "sha256": _hexdigest(b"sql-bytes")},
            {"name": "media-2026-11-10.tar.gz", "sha256": _hexdigest(b"media-bytes")},
        ],
    )
    with pytest.raises(ValueError, match="same UTC backup date"):
        cli_backup.validate_manifest(manifest)


def test_manifest_rejects_manifest_self_entry_in_artifacts() -> None:
    # A manifest can never list itself as one of its own artifacts — that
    # would be a self-referential cycle no legitimate backup producer emits.
    manifest = _valid_manifest(
        artifacts=[
            {"name": "cookbook-2026-11-09.sql.gz", "sha256": _hexdigest(b"sql-bytes")},
            {"name": "manifest-2026-11-09.json", "sha256": _hexdigest(b"manifest-bytes")},
        ],
    )
    with pytest.raises(ValueError, match="manifest itself"):
        cli_backup.validate_manifest(manifest)


def test_manifest_rejects_media_only_artifact_set() -> None:
    # A manifest without its SQL dump has no durable database state; a
    # media-only artifact set is a partial backup and must be rejected.
    manifest = _valid_manifest(
        artifacts=[{"name": "media-2026-11-09.tar.gz", "sha256": _hexdigest(b"media-bytes")}],
    )
    with pytest.raises(ValueError, match="exactly one cookbook"):
        cli_backup.validate_manifest(manifest)


def test_artifact_pairs_requires_conforming_names_and_digests() -> None:
    sql_name = cli_backup.sql_artifact_name("2026-11-09")
    media_name = cli_backup.media_archive_name("2026-11-09")
    sql_digest = _hexdigest(b"sql")
    media_digest = _hexdigest(b"media")
    pairs = cli_backup.artifact_pairs(
        sql_name=sql_name, sql_sha256=sql_digest,
        media_name=media_name, media_sha256=media_digest,
    )
    assert pairs == [
        {"name": sql_name, "sha256": sql_digest},
        {"name": media_name, "sha256": media_digest},
    ]
    with pytest.raises(ValueError):
        cli_backup.artifact_pairs(
            sql_name="not-a-name", sql_sha256=sql_digest,
            media_name=media_name, media_sha256=media_digest,
        )
    with pytest.raises(ValueError):
        cli_backup.artifact_pairs(
            sql_name=sql_name, sql_sha256=sql_digest,
            media_name=cli_backup.sql_artifact_name("2026-11-09"),
            media_sha256=media_digest,
        )


def test_artifact_pairs_rejects_mismatched_dates() -> None:
    # artifact_pairs builds the artifacts for a single UTC backup day, so the
    # SQL and media names must carry the same date. Cross-dating silently
    # produces an internally-inconsistent manifest that restore cannot trust.
    sql_digest = _hexdigest(b"sql")
    media_digest = _hexdigest(b"media")
    sql_name = cli_backup.sql_artifact_name("2026-11-09")
    media_name = cli_backup.media_archive_name("2026-11-10")
    with pytest.raises(ValueError, match="same UTC backup date"):
        cli_backup.artifact_pairs(
            sql_name=sql_name, sql_sha256=sql_digest,
            media_name=media_name, media_sha256=media_digest,
        )


def test_artifact_pairs_rejects_invalid_calendar_dates() -> None:
    # The date-embedding regexes accept any \d{4}-\d{2}-\d{2} string, so a
    # non-real date such as a 13th month or 40th day matches them —
    # artifact_pairs must reject those separately rather than ship a name that
    # no real backup day can produce.
    sql_digest = _hexdigest(b"sql")
    media_digest = _hexdigest(b"media")
    with pytest.raises(ValueError, match="real calendar date"):
        cli_backup.artifact_pairs(
            sql_name="cookbook-2026-13-40.sql.gz", sql_sha256=sql_digest,
            media_name="media-2026-13-40.tar.gz", media_sha256=media_digest,
        )
    with pytest.raises(ValueError, match="real calendar date"):
        cli_backup.artifact_pairs(
            sql_name="cookbook-2026-02-30.sql.gz", sql_sha256=sql_digest,
            media_name="media-2026-02-30.tar.gz", media_sha256=media_digest,
        )


# ---------------------------------------------------------------------------
# Known Alembic revisions (pure chain logic over the versions files)
# ---------------------------------------------------------------------------

def test_alembic_head_is_the_v7_revision() -> None:
    # The versions chain terminates at v7 (confirmed by
    # tests/test_schema_inventory.py's EXPECTED_HEAD lock).
    assert cli_backup.ALEMBIC_HEAD == "f18a6c4d0e72"


def test_known_ancestors_are_supported() -> None:
    for revision in (
        "f18a6c4d0e72",  # v7 head
        "e4c7a91d2f30",  # v6
        "b7a2f9c4d1e8",  # v5
        "52473474a186",  # v2
        "a901619780e2",  # v1
        "6d22befb6ec9",  # v0 base
    ):
        assert cli_backup.is_supported_alembic_revision(revision)


@pytest.mark.parametrize(
    "revision_id",
    [
        "6d22befb6ec9",
        "a901619780e2",
        "52473474a186",
        "a2b53a9f0000",
        "3fcee3b79cca",
        "b7a2f9c4d1e8",
        "e4c7a91d2f30",
        "f18a6c4d0e72",
    ],
)
def test_every_revision_in_the_supported_chain_is_supported(revision_id) -> None:
    assert cli_backup.is_supported_alembic_revision(revision_id)


def test_unknown_or_base_revisions_are_not_supported() -> None:
    assert not cli_backup.is_supported_alembic_revision("base")
    assert not cli_backup.is_supported_alembic_revision("deadbeef")
    assert not cli_backup.is_supported_alembic_revision(None)
    assert not cli_backup.is_supported_alembic_revision(1234)


# ---------------------------------------------------------------------------
# run_backup_command orchestration (Task 20, Task 2)
#
# These tests drive the full backup orchestration with every external side
# effect faked: the mariadb-dump subprocess, the async DB engine/snapshot,
# and the UTC "now". They lock the naming contract, manifest correctness,
# no-overwrite refusal, temp-artifact + defaults-file cleanup, subprocess
# argv shape (defaults file first, no shell, no password in argv), error
# redaction, and the "--out must not be inside MEDIA_DIR" guard. No real
# database, no real mariadb-dump, and no .env values are used.
# ---------------------------------------------------------------------------

import gzip as _gzip
import stat as _stat
import types as _types
from pathlib import Path as _Path

_BACKUP_DAY = "2026-11-09"
_BACKUP_NAMES = (
    f"cookbook-{_BACKUP_DAY}.sql.gz",
    f"media-{_BACKUP_DAY}.tar.gz",
    f"manifest-{_BACKUP_DAY}.json",
)


def _ns(**kw):
    return _types.SimpleNamespace(**kw)


def _cfg(media_dir, password="p@ss 4rd"):
    """A minimal stand-in for cli_config()'s CLISettings (every attribute the
    backup reads). Kept as a plain namespace so these tests never touch .env."""
    return _types.SimpleNamespace(
        db_host="db.internal",
        db_port=3306,
        db_name="cookbook_db",
        db_user="cookbook_user",
        db_password=password,
        media_dir=_Path(media_dir),
        database_url="mysql+asyncmy://cookbook_user:x@db.internal:3306/cookbook_db",
    )


def _fake_utc_now(monkeypatch):
    """Pin cli_backup.datetime.now() to a known instant so artifact names are
    identical in the run and in the test assertions (no midnight-flake).
    Backup orchestration tests also get a known-supported schema inventory,
    so they never contact a database.
    """
    real = datetime

    class FakeDateTime(real.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                tz = real.timezone.utc
            return cls(2026, 11, 9, 23, 59, 59, tzinfo=tz)

    # Copy every real attribute (date, time, timezone, MAXYEAR, ...) so the
    # name helpers that call datetime.date/... keep working; override only the
    # datetime class with our now()-pinned subclass.
    attrs = {name: getattr(real, name) for name in dir(real) if not name.startswith("__")}
    attrs["datetime"] = FakeDateTime
    monkeypatch.setattr(cli_backup, "datetime", _types.SimpleNamespace(**attrs))
    monkeypatch.setattr(
        cli_backup,
        "read_target_schema_inventory",
        lambda database_url: (set(SUPPORTED_TABLES) | {"alembic_version"}, []),
    )


def _sample_counts():
    return {table: 0 for table in SUPPORTED_TABLES} | {"recipes": 3, "users": 1}


def _fake_dump_ok(captured):
    def _run(argv, **kwargs):
        captured.update(argv=argv, kw=kwargs)
        for arg in argv:
            if arg.startswith("--result-file="):
                with open(arg.split("=", 1)[1], "wb") as handle:
                    handle.write(b"-- SQL DUMP BODY\n")
        return _types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"OK")

    return _run


def test_backup_publishes_named_artifacts_and_valid_manifest(tmp_path, monkeypatch):
    media = tmp_path / "media"
    (media / "sub").mkdir(parents=True)
    (media / "a.jpg").write_bytes(b"AA")
    (media / "sub" / "b.jpg").write_bytes(b"BB")
    out = tmp_path / "out"
    out.mkdir()

    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    captured = {}
    monkeypatch.setattr("subprocess.run", _fake_dump_ok(captured))
    counts = _sample_counts()
    monkeypatch.setattr(cli_backup, "_async_db_snapshot", lambda *args: (counts, "6d22befb6ec9"))

    assert cli_backup.run_backup_command(_ns(out=out)) == 0

    sql_final = out / _BACKUP_NAMES[0]
    media_final = out / _BACKUP_NAMES[1]
    manifest_final = out / _BACKUP_NAMES[2]
    assert sql_final.is_file() and media_final.is_file() and manifest_final.is_file()

    manifest = json.loads(manifest_final.read_text(encoding="utf-8"))
    assert manifest == {
        "version": 1,
        "artifacts": [
            {"name": sql_final.name, "sha256": _hexdigest(sql_final.read_bytes())},
            {"name": media_final.name, "sha256": _hexdigest(media_final.read_bytes())},
        ],
        "alembic_revision": "6d22befb6ec9",
        "table_row_counts": counts,
        "media_file_count": 2,
    }
    # SQL artifact is a real gzip carrying the dumped bytes; the media
    # archive round-trips the exact tree in sorted order.
    assert b"-- SQL DUMP BODY" in _gzip.open(sql_final, "rb").read()
    assert _archive_names(media_final) == ["a.jpg", "sub/b.jpg"]
    # No staging leftovers behind.
    assert not list(out.glob(".cookbook-backup-*"))


def test_backup_manifest_is_published_last(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    monkeypatch.setattr("subprocess.run", _fake_dump_ok({}))
    monkeypatch.setattr(cli_backup, "_async_db_snapshot", lambda *args: (_sample_counts(), "6d22befb6ec9"))

    order = []
    real_link = os.link

    def _recording_link(src, dst):
        order.append(os.path.basename(os.fspath(dst)))
        return real_link(src, dst)

    monkeypatch.setattr(cli_backup.os, "link", _recording_link)

    assert cli_backup.run_backup_command(_ns(out=out)) == 0
    # SQL, then media, then manifest -- the manifest link is last, so an
    # observer of --out never sees a partial set.
    assert order == list(_BACKUP_NAMES)


def test_publish_never_clobbers_a_path_created_after_the_precheck(tmp_path, monkeypatch, capsys):
    # No-overwrite is not just a precheck: a target may appear in the window
    # BETWEEN the precheck and the publish link (another process writing).
    # os.link() is atomic/no-clobber -- it raises FileExistsError instead of
    # overwriting -- so the foreign file must survive and the run must fail.
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    monkeypatch.setattr("subprocess.run", _fake_dump_ok({}))
    monkeypatch.setattr(cli_backup, "_async_db_snapshot", lambda *args: (_sample_counts(), "6d22befb6ec9"))

    sql_final = out / _BACKUP_NAMES[0]
    real_link = os.link

    def _racing_link(src, dst):
        # The SQL target is created by a foreign process right at publish time.
        if os.path.basename(os.fspath(dst)) == _BACKUP_NAMES[0]:
            with open(dst, "wb") as handle:
                handle.write(b"FOREIGN-OTHER-OWNER")
            raise FileExistsError(f"[Errno 17] File exists: {dst!r}")
        return real_link(src, dst)

    monkeypatch.setattr(cli_backup.os, "link", _racing_link)

    assert cli_backup.run_backup_command(_ns(out=out)) == 1
    # The foreign file is preserved UNTOUCHED (not clobbered, not deleted):
    # cleanup only removes files THIS run successfully published, and this
    # run published nothing (the very first link failed).
    assert sql_final.read_bytes() == b"FOREIGN-OTHER-OWNER"
    assert not (out / _BACKUP_NAMES[1]).exists()   # media never published
    assert not (out / _BACKUP_NAMES[2]).exists()   # manifest never published
    assert "publish" in capsys.readouterr().err
    # No temp/staging leftovers.
    assert not list(out.glob(".cookbook-backup-*"))


def test_gzip_file_to_is_deterministic_and_round_trips(tmp_path):
    # gzip_file_to is the SQL-dump compressor: it must be byte-deterministic
    # (pinned header) and lossless, and it streams straight to the output
    # file (a whole-dump BytesIO was explicitly out of contract).
    payload = b"-- SQL DUMP BODY\n" + os.urandom(300_000)  # spans many chunks
    src = tmp_path / "raw.sql"
    src.write_bytes(payload)
    first = tmp_path / "first.sql.gz"
    second = tmp_path / "second.sql.gz"
    cli_backup.gzip_file_to(src, first)
    cli_backup.gzip_file_to(src, second)
    assert first.read_bytes() == second.read_bytes()      # deterministic
    assert _gzip.open(first, "rb").read() == payload      # lossless
    # A fixed, small chunk must produce an identical byte stream, proving
    # the writer chunk-handles without changing output bytes.
    third = tmp_path / "third.sql.gz"
    cli_backup.gzip_file_to(src, third, chunk_size=128)
    assert third.read_bytes() == first.read_bytes()


def test_async_db_snapshot_uses_and_disposes_engine_in_one_loop():
    # Issue 5: SQLAlchemy's async engine is bound to the event loop it is
    # first used in. The previous code created the snapshot on one
    # asyncio.run() and dispose() on a second -- invalid and a hang/leak.
    # This test runs the REAL _async_db_snapshot against a fake engine that
    # records, on a per-loop basis, which loop create/connect/dispose ran on:
    # they must all be the SAME loop.
    import asyncio

    from cookbook import models as _models

    events: list[tuple[str, "asyncio.AbstractEventLoop"]] = []
    loop_ids: set = set()

    # Fake SQLAlchemy select result so .scalar_one() and .all() work without DB.
    class _FakeCountResult:
        def scalar_one(self):
            return 0

    class _FakeRevisionResult:
        def all(self):
            return [( "6d22befb6ec9", )]

    class _FakeTablesResult:
        def all(self):
            return [(name,) for name in _models.Base.metadata.tables]

    class _FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement):
            events.append(("execute", asyncio.get_running_loop()))
            loop_ids.add(id(asyncio.get_running_loop()))
            query = str(statement)
            # Distinguish the supported-table inventory, row counts, and
            # alembic_version lookup.
            if "information_schema.TABLES" in query:
                return _FakeTablesResult()
            if "version_num" in query:
                return _FakeRevisionResult()
            return _FakeCountResult()

    class _FakeEngine:
        def connect(self):
            events.append(("connect", asyncio.get_running_loop()))
            loop_ids.add(id(asyncio.get_running_loop()))
            return _FakeConn()

        async def dispose(self):
            events.append(("dispose", asyncio.get_running_loop()))
            loop_ids.add(id(asyncio.get_running_loop()))
            return None

    def _fake_create(url, **kwargs):
        events.append(("create", asyncio.get_running_loop()))
        return _FakeEngine()

    counts, revision = cli_backup._async_db_snapshot("mysql+asyncmy://u:p@h/db", _fake_create)

    # The engine ran (create + per-table connects + a revision exec) and the
    # revision round-tripped from the (single) loop it ran on.
    assert revision == "6d22befb6ec9"
    assert counts == {table: 0 for table in _models.Base.metadata.tables}
    # Every engine lifecycle event (create, connect, dispose) ran on ONE loop.
    assert len(loop_ids) == 1, (
        "engine create/connect/dispose must all run in the same asyncio loop, "
        f"but observed {len(loop_ids)} distinct loops"
    )
    # dispose must have been called (the finally block), not skipped.
    assert ("dispose", events[-1][1]) in events


def test_backup_no_overwrite_refuses_and_writes_nothing(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    out = tmp_path / "out"
    out.mkdir()
    (out / _BACKUP_NAMES[1]).write_bytes(b"ORIGINAL-KEEP")
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess must not be reached on no-overwrite")

    monkeypatch.setattr("subprocess.run", _boom)
    assert cli_backup.run_backup_command(_ns(out=out)) == 1
    # The pre-existing file is untouched and no new artifact or temp appears.
    assert (out / _BACKUP_NAMES[1]).read_bytes() == b"ORIGINAL-KEEP"
    assert not (out / _BACKUP_NAMES[0]).exists()
    assert not (out / _BACKUP_NAMES[2]).exists()
    assert not list(out.glob(".cookbook-backup-*"))


def test_backup_refuses_unknown_source_schema_before_dump(tmp_path, monkeypatch, capsys):
    media = tmp_path / "media"
    media.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    monkeypatch.setattr(
        cli_backup,
        "read_target_schema_inventory",
        lambda url: ({"recipes", "custom_table"}, []),
    )
    calls = []

    def dump_run(*args, **kwargs):
        calls.append(args)
        return _types.SimpleNamespace(returncode=1, stderr=b"nope", stdout=b"")

    monkeypatch.setattr("subprocess.run", dump_run)

    assert cli_backup.run_backup_command(_ns(out=out)) == 1
    assert not calls
    assert "schema" in capsys.readouterr().err.lower()
    assert not list(out.glob(".cookbook-backup-*"))


def test_backup_cleans_tmp_and_redacts_on_dump_failure(tmp_path, monkeypatch, capsys):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    out = tmp_path / "out"
    out.mkdir()
    password = "p@ss 4rd"
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media, password=password))
    _fake_utc_now(monkeypatch)
    captured = {}

    def _run(argv, **kwargs):
        captured.update(argv=argv, kw=kwargs)
        return _types.SimpleNamespace(
            returncode=12, stdout=b"",
            stderr=f"ERROR 1045 Access denied (password={password})".encode("utf-8"),
        )

    monkeypatch.setattr("subprocess.run", _run)
    assert cli_backup.run_backup_command(_ns(out=out)) == 1

    # argv contract: mariadb-dump, defaults file FIRST, no shell, no password
    # in any argument (credentials travel only via the defaults file).
    assert captured["argv"][0] == "mariadb-dump"
    assert captured["argv"][1].startswith("--defaults-extra-file=")
    assert captured["kw"].get("shell") is False
    assert all(password not in argument for argument in captured["argv"])

    # Error text is passed through the redactor: no credential, no raw stderr.
    err = capsys.readouterr().err
    assert password not in err
    assert "access denied" not in err.lower() or "<redacted>" in err
    assert "<redacted>" in err

    # No artifacts published; temp artifacts and stage dir removed.
    for name in _BACKUP_NAMES:
        assert not (out / name).exists()
    assert not list(out.glob(".cookbook-backup-*"))


def test_backup_no_published_artifacts_on_media_archive_failure(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    monkeypatch.setattr("subprocess.run", _fake_dump_ok({}))

    def _raise(media_dir, archive_path):
        raise ValueError("simulated media failure")

    monkeypatch.setattr(cli_backup, "create_media_archive", _raise)
    monkeypatch.setattr(cli_backup, "_async_db_snapshot", lambda *args: (_sample_counts(), "6d22befb6ec9"))

    assert cli_backup.run_backup_command(_ns(out=out)) == 1
    for name in _BACKUP_NAMES:
        assert not (out / name).exists()
    assert not list(out.glob(".cookbook-backup-*"))


def test_backup_refuses_unsupported_revision_and_cleans(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    monkeypatch.setattr("subprocess.run", _fake_dump_ok({}))
    monkeypatch.setattr(
        cli_backup, "_async_db_snapshot",
        lambda *args: (dict.fromkeys(SUPPORTED_TABLES, 0), "deadbeef"),
    )

    assert cli_backup.run_backup_command(_ns(out=out)) == 1
    for name in _BACKUP_NAMES:
        assert not (out / name).exists()
    assert not list(out.glob(".cookbook-backup-*"))


def test_backup_refuses_out_inside_media_dir(tmp_path, monkeypatch, capsys):
    media = tmp_path / "media"
    media.mkdir()
    out = media / "backups"
    out.mkdir()
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    assert cli_backup.run_backup_command(_ns(out=out)) == 1
    assert "MEDIA_DIR" in capsys.readouterr().err
    for name in _BACKUP_NAMES:
        assert not (out / name).exists()
    assert not list(out.glob(".cookbook-backup-*"))


def test_backup_refuses_out_equal_to_media_dir(tmp_path, monkeypatch, capsys):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media))
    _fake_utc_now(monkeypatch)
    assert cli_backup.run_backup_command(_ns(out=media)) == 1
    assert "MEDIA_DIR" in capsys.readouterr().err


def test_defaults_file_content_and_overwrite_refusal(tmp_path):
    path = tmp_path / "my.cnf"
    cli_backup.write_credentials_defaults_file(path, host="h1", port=3307, user="u1", password="pw 1")
    text = path.read_text(encoding="utf-8")
    assert "[client]" in text
    # String values are double-quoted so option-file metacharacters
    # (spaces, #, ;, =, quotes) survive a real option-file parse.
    assert 'host="h1"' in text and "port=3307" in text and 'user="u1"' in text
    assert 'password="pw 1"' in text
    assert "database=" not in text
    if os.name != "nt":
        assert _stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # O_EXCL: an existing defaults file is a refusal, never a silent clobber.
    with pytest.raises((OSError, RuntimeError)):
        cli_backup.write_credentials_defaults_file(path, host="h1", port=3306, user="u1", password="x")
    # No password line when the password is None.
    path2 = tmp_path / "my2.cnf"
    cli_backup.write_credentials_defaults_file(path2, host="h1", port=3306, user="u1", password=None)
    assert "password=" not in path2.read_text(encoding="utf-8")


def test_defaults_file_is_private_and_removed_after_run(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"A")
    out = tmp_path / "out"
    out.mkdir()
    password = "secret-pw-1234"
    monkeypatch.setattr(cli_backup, "cli_config", lambda: _cfg(media, password=password))
    _fake_utc_now(monkeypatch)
    observed = {}
    real_write = cli_backup.write_credentials_defaults_file

    def _spy_write(path, host, port, user, password):
        real_write(path, host=host, port=port, user=user, password=password)
        observed["path"] = os.fspath(path)
        if os.name != "nt":
            assert _stat.S_IMODE(os.stat(path).st_mode) == 0o600
        content = open(path, "r", encoding="utf-8").read()
        assert f'password="{password}"' in content and "database=" not in content

    monkeypatch.setattr(cli_backup, "write_credentials_defaults_file", _spy_write)

    def _run(argv, **kwargs):
        return _types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"nope")

    monkeypatch.setattr("subprocess.run", _run)
    assert cli_backup.run_backup_command(_ns(out=out)) == 1
    # The private defaults file existed during the run (spy saw 0600 +
    # password line) and is gone afterwards.
    assert observed["path"].endswith("cookbook-mariadb-my.cnf")
    assert not os.path.exists(observed["path"])


def test_dump_argv_has_defaults_file_first_and_no_password():
    argv = cli_backup._build_dump_argv("C:/temp/d.cnf", "C:/temp/raw.sql", "cookbook_db")
    assert argv[0] == "mariadb-dump"
    assert argv[1].startswith("--defaults-extra-file=")
    assert any(argument.startswith("--result-file=") for argument in argv)
    assert "--single-transaction" in argv
    assert "--skip-lock-tables" in argv
    assert "--skip-dump-date" in argv
    assert "--quick" in argv
    assert argv[-1] == "cookbook_db"
    # Credentials are carried ONLY by the defaults file, never the argv.
    for argument in argv:
        assert not argument.startswith("--password")
        assert not argument.startswith("--user")
        assert not argument.startswith("--host")
        assert "password=" not in argument


def test_redact_blanks_configured_credentials():
    secret = "secret-1234"
    out = cli_backup._redact(f"ERROR while using {secret}", (secret, ""))
    assert secret not in out and "<redacted>" in out
    # An empty credential (none configured) has nothing to redact.
    assert cli_backup._redact("connecting to db", ("",)) == "connecting to db"


@pytest.mark.parametrize("short_secret", ["db", "1", "x"])
def test_redact_blanks_short_credentials_including_1_to_3_chars(short_secret):
    # Issue 3: EVERY non-empty configured credential is redacted, including
    # 1-3 character strings. A credential is a credential regardless of
    # length, so a short secret must be blanked, not skipped.
    text = f"failed with {short_secret}"
    out = cli_backup._redact(text, (short_secret,))
    assert short_secret not in out
    assert "<redacted>" in out


def test_redact_blanks_all_configured_credentials():
    # host, user, password are all configured credentials and all redacted.
    out = cli_backup._redact(
        "host=db.internal user=root pw=s3cret",
        ("s3cret", "root", "db.internal", ""),
    )
    for leaked in ("s3cret", "root", "db.internal"):
        assert leaked not in out
    assert out.count("<redacted>") == 3


def test_defaults_file_value_quotes_and_escapes_metacharacters(tmp_path):
    # Issue 4: values must be quoted/escaped so spaces, #, ;, =, " and
    # backslash round-trip, and the line stays a single line.
    def rendered(value):
        return cli_backup._defaults_file_value(value)

    # spaces and a semicolon (which would otherwise start a comment)
    assert rendered("a b;c") == '"a b;c"'
    # an embedded = and leading/trailing spaces are preserved
    assert rendered("a=b ") == '"a=b "'
    # a double-quote is backslash-escaped so it does not close the string
    assert rendered('a"b') == '"a\\"b"'
    # a backslash is doubled (the only escape char inside the string)
    assert rendered("a\\b") == '"a\\\\b"'


@pytest.mark.parametrize("bad", ["a\x00b", "a\rb", "a\nb"])
def test_defaults_file_value_rejects_nul_cr_lf(bad):
    # Issue 4: NUL, CR and LF are all rejected outright (a newline would let
    # an attacker append an extra key=value option to the file).
    with pytest.raises(ValueError):
        cli_backup._defaults_file_value(bad)


def test_write_credentials_defaults_file_escapes_password(tmp_path):
    path = tmp_path / "my.cnf"
    cli_backup.write_credentials_defaults_file(
        path, host="h1", port=3306, user="u1", password='pa;ss word "q"\\z'
    )
    text = path.read_text(encoding="utf-8")
    assert 'password="pa;ss word \\"q\\"\\\\z"' in text
    # A value with an embedded newline must be rejected, not silently
    # written (it could inject a second option line).
    path2 = tmp_path / "my_evil.cnf"
    with pytest.raises(ValueError):
        cli_backup.write_credentials_defaults_file(
            path2, host="h1", port=3306, user="u1", password="pw\nuser=evil"
        )


# ---------------------------------------------------------------------------
# Restore source preflight and safe media staging (Task 3)
# ---------------------------------------------------------------------------

def _make_restore_sources(tmp_path, *, sql_name="cookbook-2026-11-09.sql.gz", media_name="media-2026-11-09.tar.gz"):
    sql_path = tmp_path / sql_name
    sql_path.write_bytes(b"gzip SQL placeholder")
    media_path = tmp_path / media_name
    with tarfile.open(media_path, "w:gz") as archive:
        payload = b"photo"
        info = tarfile.TarInfo("recipes/one.jpg")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    manifest_path = tmp_path / "manifest-2026-11-09.json"
    manifest = _valid_manifest()
    manifest["media_file_count"] = 1
    manifest["artifacts"] = [
        {"name": sql_path.name, "sha256": cli_backup.file_sha256(sql_path)},
        {"name": media_path.name, "sha256": cli_backup.file_sha256(media_path)},
    ]
    manifest_path.write_text(cli_backup.serialize_manifest(manifest), encoding="utf-8")
    return sql_path, media_path, manifest_path


def _tar_bytes(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, content in members:
            if content is not None:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            else:
                archive.addfile(info)
    return buffer.getvalue()


def test_verify_backup_sources_checks_names_and_digests(tmp_path):
    sql_path, media_path, manifest_path = _make_restore_sources(tmp_path)
    validated = cli_backup.verify_backup_sources(sql_path, media_path, manifest_path)
    assert validated["version"] == 1

    wrong_name = tmp_path / "renamed.sql.gz"
    wrong_name.write_bytes(sql_path.read_bytes())
    with pytest.raises(ValueError, match="basename"):
        cli_backup.verify_backup_sources(wrong_name, media_path, manifest_path)

    sql_path.write_bytes(b"tampered SQL")
    with pytest.raises(ValueError, match="digest"):
        cli_backup.verify_backup_sources(sql_path, media_path, manifest_path)


@pytest.mark.parametrize("name", ["/absolute.jpg", "../escape.jpg", "a/../../escape.jpg", "C:/drive.jpg", "a\\b.jpg"])
def test_inspect_media_archive_rejects_unsafe_paths(tmp_path, name):
    archive_path = tmp_path / "bad.tar.gz"
    archive_path.write_bytes(_tar_bytes([(tarfile.TarInfo(name), b"x")]))
    with pytest.raises(ValueError, match="unsafe|absolute|path"):
        cli_backup.inspect_media_archive(archive_path)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_inspect_media_archive_rejects_links_and_special_files(tmp_path, member_type):
    archive_path = tmp_path / "bad.tar.gz"
    info = tarfile.TarInfo("entry")
    info.type = member_type
    info.linkname = "target"
    archive_path.write_bytes(_tar_bytes([(info, None)]))
    with pytest.raises(ValueError, match="regular|directory|link|special"):
        cli_backup.inspect_media_archive(archive_path)


@pytest.mark.parametrize("names", [["a//b.jpg", "a/b.jpg"], ["a/./b.jpg", "a/b.jpg"], ["a", "a/b"]])
def test_inspect_media_archive_rejects_duplicate_or_conflicting_normalized_paths(tmp_path, names):
    archive_path = tmp_path / "bad.tar.gz"
    members = [(tarfile.TarInfo(name), b"x") for name in names]
    archive_path.write_bytes(_tar_bytes(members))
    with pytest.raises(ValueError, match="unsafe|duplicate|conflict|normal"):
        cli_backup.inspect_media_archive(archive_path)


def test_stage_media_archive_extracts_only_after_validation_into_new_directory(tmp_path):
    archive_path = tmp_path / "media.tar.gz"
    archive_path.write_bytes(_tar_bytes([(tarfile.TarInfo("nested/photo.jpg"), b"photo")]))
    staging_parent = tmp_path / "stage-parent"
    staging_parent.mkdir()
    staged = cli_backup.stage_media_archive(archive_path, staging_parent)
    assert staged.is_dir() and staged.parent == staging_parent
    assert (staged / "nested" / "photo.jpg").read_bytes() == b"photo"
    assert list(staging_parent.iterdir()) == [staged]


def test_inspect_media_archive_rejects_corrupt_archive(tmp_path):
    archive_path = tmp_path / "corrupt.tar.gz"
    archive_path.write_bytes(b"not a tar archive")
    with pytest.raises(ValueError, match="archive|tar|corrupt"):
        cli_backup.inspect_media_archive(archive_path)


def test_inspect_media_archive_rejects_corrupt_gzip_trailer(tmp_path):
    archive_path = tmp_path / "corrupt-trailer.tar.gz"
    payload = bytearray(_tar_bytes([(tarfile.TarInfo("photo.jpg"), b"photo")]))
    payload[-8] ^= 0xFF  # damage the gzip CRC32 while preserving tar headers
    archive_path.write_bytes(payload)
    with pytest.raises(ValueError, match="archive|tar|corrupt|unreadable"):
        cli_backup.inspect_media_archive(archive_path)


def test_restore_targets_require_replace_for_nonempty_database_and_media(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "old.jpg").write_bytes(b"old")
    supported_table = next(iter(SUPPORTED_TABLES))
    with pytest.raises(ValueError, match="database.*non-empty|database.*replace"):
        cli_backup.validate_restore_targets(
            db_tables={supported_table, "alembic_version"},
            unsupported_objects=[],
            media_dir=media_dir,
            replace=False,
        )
    with pytest.raises(ValueError, match="media.*non-empty|media.*replace"):
        cli_backup.validate_restore_targets(
            db_tables=set(), unsupported_objects=[], media_dir=media_dir, replace=False
        )


def test_restore_targets_allow_supported_occupied_targets_with_replace(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "old.jpg").write_bytes(b"old")
    supported_table = next(iter(SUPPORTED_TABLES))
    result = cli_backup.validate_restore_targets(
        db_tables={supported_table, "alembic_version"},
        unsupported_objects=[],
        media_dir=media_dir,
        replace=True,
    )
    assert result == {"database_nonempty": True, "media_nonempty": True}


@pytest.mark.parametrize(
    "tables,objects,reason",
    [
        ({"cookbook_user_defined"}, [], "unknown table"),
        (set(), ["TRIGGER:custom_trigger"], "unsupported schema object"),
    ],
)
def test_restore_targets_always_reject_unknown_schema_even_with_replace(tmp_path, tables, objects, reason):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    with pytest.raises(ValueError, match=reason):
        cli_backup.validate_restore_targets(
            db_tables=tables,
            unsupported_objects=objects,
            media_dir=media_dir,
            replace=True,
        )


def test_restore_targets_reject_media_file_instead_of_directory(tmp_path):
    media_file = tmp_path / "media"
    media_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="media target.*directory"):
        cli_backup.validate_restore_targets(
            db_tables=set(), unsupported_objects=[], media_dir=media_file, replace=False
        )


def test_read_target_schema_inventory_classifies_tables_and_unsupported_objects():
    import asyncio

    events = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement):
            events.append(("execute", asyncio.get_running_loop()))
            query = str(statement)
            if "information_schema.TABLES" in query:
                return Result([("recipes", "BASE TABLE"), ("custom_view", "VIEW")])
            if "information_schema.TRIGGERS" in query:
                return Result([("custom_trigger",)])
            if "information_schema.ROUTINES" in query:
                return Result([("custom_proc", "PROCEDURE")])
            if "information_schema.EVENTS" in query:
                return Result([("custom_event",)])
            raise AssertionError(query)

    class Engine:
        def connect(self):
            events.append(("connect", asyncio.get_running_loop()))
            return Connection()

        async def dispose(self):
            events.append(("dispose", asyncio.get_running_loop()))

    def create_engine(url, **kwargs):
        events.append(("create", asyncio.get_running_loop()))
        assert url == "mysql+asyncmy://user:pass@host/cookbook"
        assert kwargs == {"echo": False}
        return Engine()

    tables, objects = cli_backup.read_target_schema_inventory(
        "mysql+asyncmy://user:pass@host/cookbook", create_async_engine=create_engine
    )
    assert tables == {"recipes"}
    assert set(objects) == {
        "VIEW:custom_view",
        "TRIGGER:custom_trigger",
        "PROCEDURE:custom_proc",
        "EVENT:custom_event",
    }
    assert len({id(loop) for _, loop in events}) == 1
    assert events[-1][0] == "dispose"


def test_preflight_validates_before_creating_media_stage(tmp_path):
    sql, media_archive, manifest_path = _make_restore_sources(tmp_path)
    target_media = tmp_path / "target-media"
    target_media.mkdir()
    args = _ns(
        sql=sql,
        media_archive=media_archive,
        manifest=manifest_path,
        db_url="not-used-in-mocked-preflight",
        media_dir=target_media,
        replace=False,
    )
    with pytest.raises(ValueError, match="database.*non-empty|database.*replace"):
        cli_backup.preflight_restore(
            args,
            inventory=({"recipes"}, []),
            staging_parent=tmp_path,
        )
    assert not list(tmp_path.glob("cookbook-restore-stage-*"))


def test_preflight_returns_validated_manifest_and_staged_media(tmp_path):
    sql, media_archive, manifest_path = _make_restore_sources(tmp_path)
    target_media = tmp_path / "target-media"
    target_media.mkdir()
    args = _ns(
        sql=sql,
        media_archive=media_archive,
        manifest=manifest_path,
        db_url="not-used-in-mocked-preflight",
        media_dir=target_media,
        replace=False,
    )
    result = cli_backup.preflight_restore(
        args,
        inventory=(set(), []),
        staging_parent=tmp_path,
    )
    assert result["manifest"]["media_file_count"] == 1
    assert result["database_nonempty"] is False
    assert result["media_nonempty"] is False
    assert (result["media_stage"] / "recipes" / "one.jpg").read_bytes() == b"photo"


# ---------------------------------------------------------------------------
# Restore replacement and verification (Task 4)
# ---------------------------------------------------------------------------

def test_build_import_argv_keeps_defaults_file_first_and_database_explicit():
    argv = cli_backup._build_import_argv("C:/private/restore.cnf", "cookbook_target")
    assert argv == [
        "mariadb",
        "--defaults-extra-file=C:/private/restore.cnf",
        "cookbook_target",
    ]
    assert all("password" not in argument.lower() for argument in argv)


def test_run_mariadb_import_streams_decompressed_sql_and_removes_credentials(tmp_path, monkeypatch):
    sql_path = tmp_path / "dump.sql.gz"
    import gzip
    sql_path.write_bytes(gzip.compress(b"CREATE TABLE recipes (id INT);\nINSERT INTO recipes VALUES (1);\n", mtime=0))
    captured = {}

    class CapturedStdin:
        def __init__(self):
            self.buffer = bytearray()

        def write(self, data):
            self.buffer.extend(data)

        def close(self):
            captured["stdin"] = bytes(self.buffer)

    class Process:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            defaults_path = _Path(argv[1].split("=", 1)[1])
            captured["defaults"] = defaults_path.read_text(encoding="utf-8")
            self.stdin = CapturedStdin()

        def wait(self):
            return 0

    cli_backup.run_mariadb_import(
        sql_path,
        "mysql+asyncmy://restore_user:p%40ss%20word@db.internal:3307/cookbook_target",
        tmp_path,
        popen_factory=Process,
    )
    assert captured["stdin"].startswith(b"CREATE TABLE recipes")
    assert captured["argv"][0] == "mariadb"
    assert captured["argv"][1].startswith("--defaults-extra-file=")
    assert captured["argv"][-1] == "cookbook_target"
    assert "shell" in captured["kwargs"] and captured["kwargs"]["shell"] is False
    defaults_path = _Path(captured["argv"][1].split("=", 1)[1])
    assert not defaults_path.exists()
    assert "p@ss word" not in " ".join(captured["argv"])
    assert 'password="p@ss word"' in captured["defaults"]
    assert 'user="restore_user"' in captured["defaults"]


def test_clear_supported_database_deletes_children_first_and_only_existing_tables():
    import asyncio

    executed = []
    loops = set()

    class Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement):
            loops.add(id(asyncio.get_running_loop()))
            executed.append(str(statement))

    class Engine:
        def begin(self):
            return Connection()

        async def dispose(self):
            loops.add(id(asyncio.get_running_loop()))

    def create_engine(url, **kwargs):
        loops.add(id(asyncio.get_running_loop()))
        return Engine()

    existing = {"alembic_version", "recipes", "recipe_ingredients"}
    cli_backup.clear_supported_database(
        "mysql+asyncmy://user:pass@host/db",
        existing,
        create_async_engine=create_engine,
    )
    names = [statement.rsplit(" ", 1)[-1].strip("`\"") for statement in executed]
    expected = [
        table.name
        for table in reversed(Base.metadata.sorted_tables)
        if table.name in existing
    ]
    expected.append("alembic_version")
    assert names == expected
    assert len(loops) == 1


def test_async_db_snapshot_counts_missing_older_schema_tables_as_zero(monkeypatch):
    class Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class Count:
        def scalar_one(self):
            return 5

    class Revision:
        def all(self):
            return [("a901619780e2",)]

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement):
            query = str(statement)
            if "information_schema.TABLES" in query:
                return Rows([("recipes",), ("alembic_version",)])
            if "version_num" in query:
                return Revision()
            if "recipes" in query:
                return Count()
            raise AssertionError(f"snapshot queried a table absent from this old revision: {query}")

    class Engine:
        def connect(self):
            return Conn()

        async def dispose(self):
            pass

    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine",
        lambda *args, **kwargs: Engine(),
    )
    counts, revision = cli_backup._async_db_snapshot("mysql+asyncmy://user:pass@host/db", None)
    assert revision == "a901619780e2"
    assert counts["recipes"] == 5
    assert counts == {**dict.fromkeys(SUPPORTED_TABLES, 0), "recipes": 5}


def test_restore_replaces_both_targets_and_verifies_counts(tmp_path, monkeypatch, capsys):
    sql, media_archive, manifest_path = _make_restore_sources(tmp_path)
    media_target = tmp_path / "media-target"
    media_target.mkdir()
    (media_target / "old.jpg").write_bytes(b"old")
    expected_counts = _valid_manifest()["table_row_counts"]
    monkeypatch.setattr(cli_backup, "read_target_schema_inventory", lambda url: ({"recipes", "alembic_version"}, []))
    cleared = []
    imported = []
    monkeypatch.setattr(cli_backup, "clear_supported_database", lambda url, tables: cleared.append(set(tables)))
    monkeypatch.setattr(cli_backup, "run_mariadb_import", lambda path, url, workdir: imported.append((path, url)))
    monkeypatch.setattr(cli_backup, "_async_db_snapshot", lambda *args: (expected_counts, "6d22befb6ec9"))
    args = _ns(
        sql=sql,
        media_archive=media_archive,
        manifest=manifest_path,
        db_url="mysql+asyncmy://user:pass@host/db",
        media_dir=media_target,
        replace=True,
    )
    assert cli_backup.run_restore_command(args) == 0
    assert cleared and cleared[0] == {"recipes", "alembic_version"}
    assert imported
    assert not (media_target / "old.jpg").exists()
    assert (media_target / "recipes" / "one.jpg").read_bytes() == b"photo"
    assert "restore complete" in capsys.readouterr().out.lower()


def test_restore_import_failure_keeps_existing_media_target(tmp_path, monkeypatch, capsys):
    sql, media_archive, manifest_path = _make_restore_sources(tmp_path)
    media_target = tmp_path / "media-target"
    media_target.mkdir()
    (media_target / "old.jpg").write_bytes(b"old")
    monkeypatch.setattr(cli_backup, "read_target_schema_inventory", lambda url: ({"recipes"}, []))
    monkeypatch.setattr(cli_backup, "clear_supported_database", lambda *args: None)

    def fail_import(*args):
        raise ValueError("import failed")

    monkeypatch.setattr(cli_backup, "run_mariadb_import", fail_import)
    args = _ns(
        sql=sql,
        media_archive=media_archive,
        manifest=manifest_path,
        db_url="mysql+asyncmy://user:pass@host/db",
        media_dir=media_target,
        replace=True,
    )
    assert cli_backup.run_restore_command(args) == 1
    assert (media_target / "old.jpg").read_bytes() == b"old"
    assert not (media_target / "recipes" / "one.jpg").exists()
    assert "target database URL" not in capsys.readouterr().err


def test_restore_row_count_mismatch_fails_verification(tmp_path, monkeypatch, capsys):
    sql, media_archive, manifest_path = _make_restore_sources(tmp_path)
    media_target = tmp_path / "media-target"
    media_target.mkdir()
    expected_counts = dict(_valid_manifest()["table_row_counts"])
    wrong_counts = dict(expected_counts)
    wrong_counts["recipes"] = expected_counts["recipes"] + 1
    monkeypatch.setattr(cli_backup, "read_target_schema_inventory", lambda url: (set(), []))
    monkeypatch.setattr(cli_backup, "run_mariadb_import", lambda *args: None)
    monkeypatch.setattr(cli_backup, "_async_db_snapshot", lambda *args: (wrong_counts, "6d22befb6ec9"))
    args = _ns(
        sql=sql,
        media_archive=media_archive,
        manifest=manifest_path,
        db_url="mysql+asyncmy://user:pass@host/db",
        media_dir=media_target,
        replace=False,
    )
    assert cli_backup.run_restore_command(args) == 1
    assert "verification" in capsys.readouterr().err.lower()


def test_restore_media_count_mismatch_fails_preflight_before_import(tmp_path, monkeypatch, capsys):
    sql, media_archive, manifest_path = _make_restore_sources(tmp_path)
    manifest = cli_backup.load_manifest(manifest_path)
    manifest["media_file_count"] = 2
    manifest_path.write_text(cli_backup.serialize_manifest(manifest), encoding="utf-8")
    media_target = tmp_path / "media-target"
    media_target.mkdir()
    monkeypatch.setattr(cli_backup, "read_target_schema_inventory", lambda url: (set(), []))
    imported = []
    monkeypatch.setattr(cli_backup, "run_mariadb_import", lambda *args: imported.append(True))
    args = _ns(
        sql=sql,
        media_archive=media_archive,
        manifest=manifest_path,
        db_url="mysql+asyncmy://user:pass@host/db",
        media_dir=media_target,
        replace=False,
    )
    assert cli_backup.run_restore_command(args) == 1
    assert not imported
    assert not list(tmp_path.glob("cookbook-restore-stage-*"))
    assert list(media_target.iterdir()) == []
    assert "preflight" in capsys.readouterr().err.lower()


def test_restore_readiness_failure_is_reported(tmp_path, monkeypatch, capsys):
    sql, media_archive, manifest_path = _make_restore_sources(tmp_path)
    media_target = tmp_path / "media-target"
    media_target.mkdir()
    expected_counts = _valid_manifest()["table_row_counts"]
    monkeypatch.setattr(cli_backup, "read_target_schema_inventory", lambda url: (set(), []))
    monkeypatch.setattr(cli_backup, "run_mariadb_import", lambda *args: None)
    monkeypatch.setattr(cli_backup, "_async_db_snapshot", lambda *args: (expected_counts, "6d22befb6ec9"))

    def fail_readiness(path):
        raise ValueError("media is not writable")

    monkeypatch.setattr(cli_backup, "_check_media_writable", fail_readiness)
    args = _ns(
        sql=sql,
        media_archive=media_archive,
        manifest=manifest_path,
        db_url="mysql+asyncmy://user:pass@host/db",
        media_dir=media_target,
        replace=False,
    )
    assert cli_backup.run_restore_command(args) == 1
    assert "readiness" in capsys.readouterr().err.lower()
