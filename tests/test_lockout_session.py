"""Lockout reset commits or rolls back together with session issuance."""
import hashlib

import pytest
from sqlalchemy import delete, func, select

from cookbook.auth import sessions
from cookbook.db import session_scope
from cookbook.models import LoginAttempt, Session, User
from test_lockout_routes import IP, probe
from test_lockout import NOW, row, service


def test_session_issue_clears_history_in_same_commit(monkeypatch):
    async def body(app, client, username, uid):
        await service().record_failure(username, IP, now=NOW)
        raw, _ = await sessions.create_session(uid, settings=app.state.settings, login_pair=(username, IP))
        assert await row(username, IP) is None
        async with session_scope() as db:
            assert await db.get(Session, hashlib.sha256(raw.encode()).hexdigest()) is not None
    probe(monkeypatch, body)


def test_reset_failure_rolls_back_history_and_session_issue(monkeypatch):
    async def body(app, client, username, uid):
        s = service()
        await s.record_failure(username, IP, now=NOW)
        original = s.clear_pair
        async def fail_after_delete(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("reset transaction fault")
        monkeypatch.setattr(s, "clear_pair", fail_after_delete)
        with pytest.raises(RuntimeError, match="reset transaction fault"):
            await sessions.create_session(uid, settings=app.state.settings, login_pair=(username, IP))
        assert (await row(username, IP)).attempts == 1
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(Session).where(Session.user_id == uid))).scalar_one() == 0
    probe(monkeypatch, body)


@pytest.mark.parametrize("refusal", ["inactive", "changed-hash"])
def test_refused_session_does_not_reset_history(monkeypatch, refusal):
    async def body(app, client, username, uid):
        await service().record_failure(username, IP, now=NOW)
        async with session_scope() as db:
            user = await db.get(User, uid)
            if refusal == "inactive":
                user.is_active = False
        with pytest.raises(sessions.SessionIssueRefused):
            await sessions.create_session(uid, settings=app.state.settings, login_pair=(username, IP),
                                           expected_password_hash="changed")
        assert (await row(username, IP)).attempts == 1
    probe(monkeypatch, body)
