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


def _valid_stored_hash() -> str:
    """A structurally valid argon2id hash (low params, cheap for tests).

    ``argon2.extract_parameters`` accepts it, so the service's structural
    pre-check passes and the test reaches the verifier (real or faked). This
    is deliberately structurally *valid* so the only distinguishing factor in
    the operational-failure tests below is the exception the verifier raises
    — not a malformed-hash short-circuit.
    """
    return PasswordHasher(
        time_cost=1, memory_cost=8192, parallelism=1,
        hash_len=32, salt_len=16, type=Type.ID,
    ).hash("dummy-plaintext-for-test")


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


def test_malformed_decodable_hash_returns_false_not_decoding_error() -> None:
    # Codex 7.3 regression (HEAD 05771ea, argon2-cffi 25.1.0): this exact
    # stored hash is a recognized argon2id string with valid segment COUNT and
    # valid segment LENGTHS (its non-base64 "!-fill" still decodes to exactly
    # the required salt_len=24 and hash_len=24), so argon2.extract_parameters
    # ACCEPTS it. The C verifier then rejects it with the library's
    # VerificationError "Decoding failed". That malformed stored hash must
    # fold to a clean False; the previous extract_parameters pre-check passed
    # and re-raised instead. (Real library path, no spy.)
    bad = ("$argon2id$v=19$m=19456,t=2,p=1$"
           + "!" * 32 + "$" + "!" * 32)
    assert pw.verify_password("candidate", bad) is False


@pytest.mark.parametrize("message", [
    "Decoding failed",          # bad base64 in salt or tag (the Codex case)
    "Output is too short",     # tag shorter than the encoded hash length
    "Salt is too short",        # zero-byte / undersized salt
    "Memory cost is too small",  # m below the lib's floor
    "Time cost is too small",    # t below the lib's floor
    "Too few lanes",             # p below the lib's floor
])
def test_known_malformed_verification_error_messages_return_false(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    # Full set of "stored hash the C library refuses" VerificationError
    # messages the installed argon2-cffi emits (probed against low_level
    # directly). Each must fold to a clean False — they describe malformed
    # *stored data*, never a genuine operational failure.
    def decoding_fail_verify(stored: str, candidate: str) -> bool:
        raise argon2_exc.VerificationError(message)

    _install_spy(monkeypatch, decoding_fail_verify)
    assert pw.verify_password("candidate", "not-a-real-hash") is False


def test_memory_cost_too_small_is_not_confused_with_memory_allocation_error(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # Locks the ordering the discrimination depends on: "Memory cost is too
    # small" (a malformed/unsupported stored hash) folds to False, while the
    # near-identical operational message "Memory allocation error" must still
    # propagate. The two are told apart exactly, without substring matching.
    def cost_too_small_verify(stored: str, candidate: str) -> bool:
        raise argon2_exc.VerificationError("Memory cost is too small")
    _install_spy(monkeypatch, cost_too_small_verify)
    assert pw.verify_password("candidate", "any-stored-hash") is False

    def allocation_error_verify(stored: str, candidate: str) -> bool:
        raise argon2_exc.VerificationError("Memory allocation error")
    _install_spy(monkeypatch, allocation_error_verify)
    with pytest.raises(argon2_exc.VerificationError,
                       match="Memory allocation error"):
        pw.verify_password("candidate", "any-stored-hash")


# --- regression: operational verification failures must propagate -------------
#
# The library's VerificationError family conflates a few distinct outcomes:
# "Decoding failed" (stored hash is actually malformed, but its recognized
# prefix let it past structural validation) and genuine resource-level
# verification failures both surface as VerificationError. The service must
# not flatten that into an authentication failure. The regression tests
# below lock down that distinction.

def test_operational_verification_error_propagates_from_verify_password(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulates the library failing for an operational reason (e.g. memory
    # allocation) — VerificationError, NOT VerificationError's mismatch
    # subclass. This must not be masked as a password mismatch.
    def operational_verify(stored_hash: str, candidate: str) -> bool:
        raise argon2_exc.VerificationError("Memory allocation error")

    _install_spy(monkeypatch, operational_verify)
    # Structurally valid stored hash so the test isolates the verifier's
    # operational failure, not a malformed-hash short-circuit.
    with pytest.raises(argon2_exc.VerificationError,
                       match="Memory allocation error"):
        pw.verify_password("candidate", _valid_stored_hash())


def test_operational_verification_error_propagates_from_dummy_verify(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    def operational_verify(stored_hash: str, candidate: str) -> bool:
        raise argon2_exc.VerificationError("Memory allocation error")

    _install_spy(monkeypatch, operational_verify)
    with pytest.raises(argon2_exc.VerificationError,
                       match="Memory allocation error"):
        pw.dummy_verify("candidate")


def test_memory_error_propagates_from_verify_password(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    def verify_that_raises(stored_hash: str, candidate: str) -> bool:
        raise MemoryError("could not allocate verification buffer")

    _install_spy(monkeypatch, verify_that_raises)
    with pytest.raises(MemoryError):
        pw.verify_password("candidate", "$argon2id$v=19$m=19456,t=2,p=1$salt$tag")


def test_memory_error_propagates_from_dummy_verify(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    def verify_that_raises(stored_hash: str, candidate: str) -> bool:
        raise MemoryError("could not allocate dummy buffer")

    _install_spy(monkeypatch, verify_that_raises)
    with pytest.raises(MemoryError):
        pw.dummy_verify("candidate")


def test_runtime_error_propagates_from_verify_password(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    def verify_that_raises(stored_hash: str, candidate: str) -> bool:
        raise RuntimeError("libargon2 internal failure")

    _install_spy(monkeypatch, verify_that_raises)
    with pytest.raises(RuntimeError):
        pw.verify_password("candidate", "$argon2id$v=19$m=19456,t=2,p=1$salt$tag")


def test_runtime_error_propagates_from_dummy_verify(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    def verify_that_raises(stored_hash: str, candidate: str) -> bool:
        raise RuntimeError("libargon2 internal failure")

    _install_spy(monkeypatch, verify_that_raises)
    with pytest.raises(RuntimeError):
        pw.dummy_verify("candidate")


def test_wrong_password_still_returns_false_not_exception(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # Locks down the distinction the fix above preserves: a real mismatch
    # (VerifyMismatchError, a VerificationError subclass) must STILL be
    # folded into a clean "no", not propagated.
    def mismatch_verify(stored_hash: str, candidate: str) -> bool:
        raise argon2_exc.VerifyMismatchError("password does not match hash")

    _install_spy(monkeypatch, mismatch_verify)
    assert pw.verify_password("candidate", _valid_stored_hash()) is False


def test_malformed_stored_hashes_still_return_false_after_fix(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # After the narrow-catch change, structurally invalid stored hashes
    # (InvalidHashError — NOT an Argon2Error at all) must still fold to a
    # clean "no" rather than escaping the service.
    def structural_fail_verify(stored: str, candidate: str) -> bool:
        raise argon2_exc.InvalidHashError("structural validation failed")

    _install_spy(monkeypatch, structural_fail_verify)
    assert pw.verify_password("candidate", "not-a-hash") is False
    assert pw.verify_password("candidate", "$argon2b$v=99$garbage") is False


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
    # VerifyMismatchError at verify time is the normal, expected outcome —
    # proving real verification work ran. Anything else (structural
    # InvalidHashError, operational VerificationError) means the constant is
    # corrupt or the library is failing, and must NOT be swallowed.
    assert "$argon2id$" in pw.DUMMY_HASH
    try:
        pw._hasher.verify(pw.DUMMY_HASH, "any-candidate")
    except argon2_exc.VerifyMismatchError:
        pass  # mismatch is the normal outcome — the real work still ran
    except argon2_exc.InvalidHashError as exc:
        raise AssertionError("committed DUMMY_HASH is malformed") from exc


def test_dummy_hash_parameters_match_approved_policy() -> None:
    # The committed dummy hash must carry the exact approved argon2id
    # settings, proven by the library's own parameter extractor (not just a
    # string prefix check).
    params = argon2.extract_parameters(pw.DUMMY_HASH)
    assert params.type is Type.ID
    assert params.version == 19
    assert params.memory_cost == 19456  # 19 MiB, in KiB units
    assert params.time_cost == 2
    assert params.parallelism == 1
    assert params.hash_len == 32
    assert params.salt_len == 16


def test_dummy_verify_raises_specifically_on_real_nonmatching_candidate() -> None:
    # With the real hasher and the real committed DUMMY_HASH, a candidate
    # verification must surface as the library's specific VerifyMismatchError
    # (and nothing broader). This proves the committed constant performs full
    # argon2id verification work and that the outcome is a genuine mismatch,
    # not a masked operational or structural failure.
    with pytest.raises(argon2_exc.VerifyMismatchError):
        pw._hasher.verify(pw.DUMMY_HASH, "nonmatching-candidate-9x")


def test_fake_dummy_hash_operational_error_propagates(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # Controlled-application-data rule: the dummy hash is ours, so an
    # operational VerificationError from that path is a real failure, not an
    # authentication outcome, and must not be swallowed.
    def operational_verify(stored_hash: str, candidate: str) -> bool:
        raise argon2_exc.VerificationError("Memory allocation error")

    _install_spy(monkeypatch, operational_verify)
    with pytest.raises(argon2_exc.VerificationError,
                       match="Memory allocation error"):
        pw.dummy_verify("candidate")


def test_fake_dummy_hash_unsupported_parameter_propagates(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # UnsupportedParametersError (e.g. platform constraints) is a ValueError,
    # not an Argon2Error — it must also propagate from the dummy path rather
    # than be treated as "no match".
    def unsupported_verify(stored_hash: str, candidate: str) -> bool:
        raise argon2_exc.UnsupportedParametersError(
            "parameters unsupported on this platform"
        )

    _install_spy(monkeypatch, unsupported_verify)
    with pytest.raises(argon2_exc.UnsupportedParametersError,
                       match="unsupported on this platform"):
        pw.dummy_verify("candidate")


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
