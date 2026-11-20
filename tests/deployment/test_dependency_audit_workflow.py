"""Regression guards for the dependency-audit job in the reusable tests workflow.

This pins the *shape* of the approved dependency-audit-only security baseline:
the job, its pinned tools, the audit command, and its read-only, credential-free
character must stay that way. It inspects only the workflow YAML, so it can run
anywhere without network, Docker, secrets, or the MariaDB service.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"

# The two action SHAs the workflow already pins for the tests job; the audit
# job must reuse these exact pinned revisions, not a mutable tag.
CHECKOUT_SHA = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SETUP_UV_SHA = "astral-sh/setup-uv@eb1897b8dc4b5d5bfe39a428a8f2304605e0983c"
# The audit must use a single, explicitly pinned uv binary version.
PINNED_UV_VERSION = "0.13.7"
# The exact command that performs the dependency audit (and nothing else).
AUDIT_COMMAND = "uv audit --locked"


def _flow() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _jobs(flow: dict) -> dict:
    jobs = flow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def test_dependency_audit_job_exists_and_is_a_reusable_gate() -> None:
    jobs = _jobs(_flow())
    assert "dependency-audit" in jobs
    job = jobs["dependency-audit"]
    # GitHub-hosted only; the audit must never land on a self-hosted runner.
    assert job["runs-on"] == "ubuntu-latest"
    assert isinstance(job.get("timeout-minutes"), int)
    assert job["timeout-minutes"] > 0
    # It must be a real step-bearing job, not a thin `uses` caller.
    steps = job.get("steps")
    assert isinstance(steps, list) and steps


def test_pinned_tools_and_explicit_uv_version() -> None:
    job = _jobs(_flow())["dependency-audit"]
    uses = [step.get("uses") for step in job["steps"] if step.get("uses")]
    checkout = next(s for s in job["steps"] if s.get("uses") == CHECKOUT_SHA)
    setup_uv = next(s for s in job["steps"] if s.get("uses") == SETUP_UV_SHA)
    # Pinned to commit SHAs, never a tag; no mutable 'main'/'v*' revisions.
    assert all("@" in u and not u.split("@", 1)[1].startswith("v") for u in uses)
    assert checkout.get("with", {}).get("persist-credentials") is False
    # The uv binary itself is pinned to an exact version for reproducibility.
    assert setup_uv.get("with", {}).get("version") == PINNED_UV_VERSION


def test_audit_command_is_dependency_audit_only() -> None:
    job = _jobs(_flow())["dependency-audit"]
    run_steps = [step for step in job["steps"] if step.get("run")]
    assert len(run_steps) == 1, "audit job must run exactly one command"
    assert run_steps[0]["run"].strip() == AUDIT_COMMAND
    # The audit reads and asserts the lockfile; it must not write or re-resolve.
    assert "--locked" in run_steps[0]["run"]


def test_read_only_permissions_and_no_credentials() -> None:
    flow = _flow()
    job = _jobs(flow)["dependency-audit"]
    # Least privilege: read the repository only — no packages, no write.
    assert job["permissions"] == {"contents": "read"}
    # The repository-level permissions stay read-only for pull requests too.
    assert flow["permissions"] == {"contents": "read"}
    # Checkout must not inject credentials into the working tree.
    checkout = next(s for s in job["steps"] if s.get("uses") == CHECKOUT_SHA)
    assert checkout.get("with", {}).get("persist-credentials") is False


def test_no_secret_references_in_the_audit_job() -> None:
    job = _jobs(_flow())["dependency-audit"]
    dumped = yaml.dump(job).lower()
    assert "secrets." not in dumped
    # No environment variables or step `env` blocks that could smuggle in auth.
    for step in job["steps"]:
        assert "env" not in step, "audit job must not set environment variables"


def test_job_runs_on_all_reusable_triggers() -> None:
    # PyYAML reads the YAML 1.1 `on` key as the boolean True.
    trigger = _flow().get("on", _flow().get(True))
    assert isinstance(trigger, dict)
    for event in ("pull_request", "push", "workflow_dispatch", "workflow_call"):
        assert event in trigger
    assert set(trigger["pull_request"]["branches"]) == {"dev", "main"}
    assert set(trigger["push"]["branches"]) == {"dev", "main"}
    # The audit job must not be conditionally skipped by any `if` expression.
    assert "if" not in _jobs(_flow())["dependency-audit"]
