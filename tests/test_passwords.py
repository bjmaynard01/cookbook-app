"""Password hashing/verification service behavior (auth plan S3, R2 + O-f).

Pure service — no database, no env/SECRET_KEY. Covers the O-f policy
(minimum 8 characters, no complexity rules), the argon2id output contract
(exact configured parameters), the verify-does-not-reapply-policy rule
(existing shorter passwords keep working), safe failure on malformed
stored hashes, and the dummy-verify path (exactly one verifier call with
the submitted candidate, returning no authentication result).

Note: `PasswordHasher` uses `__slots__` (no `__dict__`), so tests cannot
attach an instance-level `verify` override on the real hasher. Instead,
tests replace ``pw._hasher`` with a minimal stand-in exposing a `verify`
method; monkeypatch restores it automatically.

No wall-clock timing comparisons anywhere in this file (timing parity is a
design property, not something a flaky benchmark can prove).
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import argon2
import pytest
from argon2 import PasswordHasher, Type, exceptions as argon2_exc

from cookbook import auth as auth_pkg
from cookbook.auth import passwords as pw


@pytest.fixture(autouse=True)
def _restore_module() -> Iterator[None]:
    """Guarantee tests leave the module in a clean (un-reloaded) state."""
    yield
    importlib.reload(pw)


class _FakeHasher:
    """Minimal stand-in for ``pw._hasher`` exposing only ``verify``.

    ``PasswordHasher`` uses ``__slots__`` and does not support per-instance
    assignment of ``verify``, so the tests replace ``pw._hasher`` itself
    with this object; monkeypatch (which reads the current module attribute
    before the patch) handles restoration automatically.
    """

    def __init__(self, verify_fn) -> None:
        self._verify_fn = verify_fn

    def verify(self, stored_hash: str, candidate: str) -> bool:
        return self._verify_fn(stored_hash, candidate)


def _install_spy(monkeypatch: pytest.MonkeyPatch, verify_fn) -> None:
    monkeypatch.setattr(pw, "_hasher", _FakeHasher(verify_fn))


# --- O-f policy: minimum 8 characters, no complexity rules ------------------

@pytest.mark.parametrize("candidate", ["", "a", "seven77", "1234567", "    123"])
def test_reject_shorter_than_eight(candidate: str) -> None:
    with pytest.raises(ValueError, match="at least 8"):
        pw.validate_password(candidate)
    # hash_password must enforce the same policy.
    with pytest.raises(ValueError, match="at least 8"):
        pw.hash_password(candidate)


def test_accept_exactly_eight_and_simple_lowercase_digits() -> None:
    pw.validate_password("abcdef12")
    assert isinstance(pw.hash_password("abcd1234"), str)
    assert isinstance(pw.hash_password("password"), str)


def test_unicode_passwords_counted_as_characters() -> None:
    # 8 code points -> accepted; 7 code points -> rejected. No
    # normalization or case folding is allowed (O-f: length only).
    pw.validate_password("p\u00e4ssw\u00f6rd")
    pw.validate_password("\U0001F600" * 8)
    with pytest.raises(ValueError, match="at least 8"):
        pw.validate_password("p\u00e4ssw\u00f6r")
    with pytest.raises(ValueError, match="at least 8"):
        pw.validate_password("\U0001F600" * 7)


def test_whitespace_is_preserved_not_trimmed() -> None:
    padded = "  abcdefgh  "
    pw.validate_password(padded)
    stored = pw.hash_password(padded)
    assert pw.verify_password(padded, stored) is True
    # Changing the whitespace changes the password — no silent trimming.
    assert pw.verify_password("abcdefgh", stored) is False
    assert pw.verify_password("abcdefgh  ", stored) is False


def test_policy_error_message_has_no_password_leak() -> None:
    secret = "sup3r-secr3t-candidate"
    with pytest.raises(ValueError) as excinfo:
        pw.validate_password(secret[:6])
    assert secret[:6] not in str(excinfo.value)
    assert "at least 8" in str(excinfo.value)


# --- hashing / verification ---------------------------------------------------

def test_round_trip_and_wrong_password_rejected() -> None:
    password = "correct-horse-battery"
    stored = pw.hash_password(password)
    assert pw.verify_password(password, stored) is True
    assert pw.verify_password("Correct-Horse-Battery", stored) is False
    assert pw.verify_password(password + "!", stored) is False


def test_two_hashes_of_same_password_differ_and_both_verify() -> None:
    first = pw.hash_password("same-password")
    second = pw.hash_password("same-password")
    assert first != second
    assert pw.verify_password("same-password", first) is True
    assert pw.verify_password("same-password", second) is True


def test_hash_output_parses_as_argon2id_with_exact_configured_parameters() -> None:
    stored = pw.hash_password("parameter-check")
    assert isinstance(stored, str)
    # Argon2 encoded-string layout: $<alg>$<version>$<params>$<salt>$<hash>,
    # giving 6 segments when split on '$' (leading empty included).
    parts = stored.split("$")
    assert len(parts) == 6
    assert parts[1] == "argon2id"
    assert parts[2] == "v=19"
    assert parts[3] == "m=19456,t=2,p=1"
    assert parts[4] and parts[5]  # salt and hash are non-empty
    # The configured hasher itself must match, not just the output.
    assert pw._hasher.memory_cost == 19456  # 19 MiB, in KiB units
    assert pw._hasher.time_cost == 2
    assert pw._hasher.parallelism == 1
    assert pw._hasher.hash_len == 32
    assert pw._hasher.salt_len == 16
    assert pw._hasher.type is Type.ID
    # And the output must agree with the hasher's configuration.
    params = argon2.extract_parameters(stored)
    assert params.type is Type.ID
    assert params.memory_cost == 19456
    assert params.time_cost == 2
    assert params.parallelism == 1
    assert params.hash_len == 32
    assert params.salt_len == 16


def test_existing_valid_short_hash_still_verifies() -> None:
    # Simulates a legacy stored hash written before the policy existed:
    # hash_password now rejects short new passwords, but verify_password
    # must NOT apply the new-password policy to existing ones.
    legacy = PasswordHasher(
        time_cost=1, memory_cost=8192, parallelism=1,
        hash_len=32, salt_len=16, type=Type.ID,
    ).hash("short9x")
    assert pw.verify_password("short9x", legacy) is True
    assert pw.verify_password("wrong9x", legacy) is False
    with pytest.raises(ValueError, match="at least 8"):
        pw.hash_password("short9x")


def test_malformed_stored_hashes_fail_safely() -> None:
    plain = "whatever-123"
    for bad in ("", "not-a-hash", "$argon2id$only", "$argon2b$v=99$garbage"):
        assert pw.verify_password(plain, bad) is False, repr(bad)


def test_no_password_or_hash_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    password = "caplog-secret-77"
    stored = pw.hash_password(password)
    assert pw.verify_password("nope", stored) is False
    assert pw.verify_password("nope", "junk-hash") is False
    pw.dummy_verify(password)
    for record in caplog.records:
        message = f"{record.message} {record.args}"
        assert password not in message
        assert stored not in message


# --- dummy verify path (unknown-user timing parity) ---------------------------

def test_dummy_verify_calls_library_verifier_exactly_once_with_candidate(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    def spy(stored_hash: str, candidate: str) -> bool:
        seen.append((stored_hash, candidate))
        return True

    _install_spy(monkeypatch, spy)
    candidate = "submitted-candidate-pw"
    assert pw.dummy_verify(candidate) is None
    # Exactly one call, with the committed dummy hash and the submitted
    # candidate (argon2-cffi verify takes (hash, password)).
    assert seen == [(pw.DUMMY_HASH, candidate)]


def test_dummy_verify_matches_and_mismatches_both_return_none(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # Match path: verifier reports True — still no result, no authentication.
    _install_spy(monkeypatch, lambda h, p: True)
    assert pw.dummy_verify("candidate-a") is None

    # Mismatch path: the real library raises VerifyMismatchError; the dummy
    # path must swallow it and still return None.
    def raising_verify(stored_hash: str, candidate: str) -> bool:
        raise argon2_exc.VerifyMismatchError("wrong password")
    _install_spy(monkeypatch, raising_verify)
    assert pw.dummy_verify("candidate-b") is None

    # Same, via an explicit False return (defensive against both library
    # behaviors: it must ignore the outcome either way).
    _install_spy(monkeypatch, lambda h, p: False)
    assert pw.dummy_verify("candidate-c") is None


def test_dummy_hash_is_valid_for_the_configured_hasher() -> None:
    # Guarding the committed constant: it must be a well-formed argon2id hash
    # the configured hasher will fully process (so the unknown-user path does
    # full argon2id work). We do not know the pre-generated plaintext, so a
    # mismatch at verify time is the normal, expected outcome; a structural
    # InvalidHashError would mean the constant is corrupt.
    assert "$argon2id$" in pw.DUMMY_HASH
    try:
        pw._hasher.verify(pw.DUMMY_HASH, "any-candidate")
    except argon2_exc.Argon2Error:
        pass  # mismatch is the normal outcome — the real work still ran
    except argon2_exc.InvalidHashError:
        raise AssertionError("committed DUMMY_HASH is malformed")


def test_known_user_failure_performs_exactly_one_verify_call(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def failing_verify(stored_hash: str, candidate: str) -> bool:
        calls.append(1)
        raise argon2_exc.VerifyMismatchError("wrong password")
    _install_spy(monkeypatch, failing_verify)
    stored = "$argon2id$v=19$stub$stub$"  # never reached; spy raises first
    assert pw.verify_password("failed-attempt", stored) is False
    assert calls == [1]


def test_unknown_user_path_performs_exactly_one_verify_call(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def dummy_verify_spy(stored_hash: str, candidate: str) -> bool:
        calls.append(stored_hash)
        return True
    _install_spy(monkeypatch, dummy_verify_spy)
    assert pw.dummy_verify("unknown-user-attempt") is None
    assert calls == [pw.DUMMY_HASH]


def test_module_import_does_not_perform_hashing() -> None:
    # Proof by isolation: a fresh interpreter imports the module while
    # PasswordHasher.hash (the only call site that would do work during
    # import) is stubbed to hard-fail. The child exits 0 and prints
    # "import-clean" only if no hashing ran; otherwise the FAIL marker
    # appears and the assertion below rejects it.
    script = (
        "import argon2\n"
        "def boom(self, password, *a, **k):\n"
        "    raise SystemExit('FAIL: hashing ran during import')\n"
        "argon2.PasswordHasher.hash = boom\n"
        "from cookbook.auth import passwords as pw\n"
        "assert pw.DUMMY_HASH.startswith('$argon2id$')\n"
        "print('import-clean')\n"
    )
    src_dir = Path(__file__).resolve().parent.parent / "src"
    prior = os.environ.get("PYTHONPATH", "")
    env = {**os.environ,
           "PYTHONPATH": src_dir.as_posix() if not prior
                          else f"{src_dir.as_posix()}{os.pathsep}{prior}"}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    combined = result.stdout + result.stderr
    assert "FAIL: hashing ran during import" not in combined, combined
    assert "import-clean" in combined, combined


def test_package_import_path_unchanged() -> None:
    # Scope guard: existing auth package exports are untouched; consumers of
    # this service import it directly from the module (no __init__ change).
    assert auth_pkg.__all__ == ["client_identity", "cookie_secure"]
    assert not hasattr(auth_pkg, "hash_password")
