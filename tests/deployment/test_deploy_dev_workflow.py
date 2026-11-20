"""Regression guards for the Task 4 dev delivery workflow (deploy-dev.yml)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-dev.yml"


def _load_flow() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _jobs(flow: dict) -> dict:
    jobs = flow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def test_workflow_triggers_are_only_dev_push_and_manual_dispatch() -> None:
    # PyYAML parses YAML 1.1, so the `on` key loads as the boolean True.
    trigger = _load_flow().get("on", _load_flow().get(True))
    assert isinstance(trigger, dict)
    assert set(trigger) == {"push", "workflow_dispatch"}
    assert trigger["push"] == {"branches": ["dev"]}


def test_beta_deploys_serialize_without_canceling_in_flight_run() -> None:
    concurrency = _load_flow()["concurrency"]
    assert concurrency["group"] == "beta-${{ github.workflow }}"
    assert concurrency["cancel-in-progress"] is False


def test_reusable_tests_job_has_no_additional_gate() -> None:
    tests = _jobs(_load_flow())["tests"]
    assert tests["uses"] == "./.github/workflows/tests.yml"
    # The trigger block is the only gate; tests must not be silently
    # conditional on the beta deployment variable.
    assert "if" not in tests
    assert "needs" not in tests


def _job_flow_condition(job: dict) -> str:
    expr = job["if"]
    if isinstance(expr, dict):
        expr = expr["expression"]
    assert expr.strip().startswith("${{")
    return expr.strip().removeprefix("${{").removesuffix("}}").strip()


def test_smoke_runs_only_on_trusted_dev_push_and_requires_tests() -> None:
    smoke = _jobs(_load_flow())["smoke"]
    assert _job_flow_condition(smoke) == (
        "github.event_name == 'push' && github.ref == 'refs/heads/dev'"
    )
    assert smoke["needs"] == ["tests"]
    assert smoke["with"]["push_image"] is True
    # Publishing must not depend on the beta deployment gate.
    assert "COOKBOOK_BETA_ENABLED" not in yaml.dump(smoke)


def test_deploy_is_gated_and_requires_smoke() -> None:
    deploy = _jobs(_load_flow())["deploy"]
    assert _job_flow_condition(deploy) == (
        "github.event_name == 'push' "
        "&& github.ref == 'refs/heads/dev' "
        "&& vars.COOKBOOK_BETA_ENABLED == 'true'"
    )
    assert deploy["needs"] == ["smoke"]


def test_exactly_one_self_hosted_runner_and_exact_label() -> None:
    jobs = _jobs(_load_flow())
    self_hosted = {name: job.get("runs-on") for name, job in jobs.items() if job.get("runs-on")}
    assert self_hosted == {"deploy": ["self-hosted", "dev-workstation"]}


def test_least_privilege_permissions() -> None:
    flow = _load_flow()
    jobs = _jobs(flow)
    assert flow["permissions"] == {"contents": "read"}
    # tests.yml jobs inherit contents:read only.
    assert "permissions" not in jobs["tests"]
    # packages:write is scoped to the smoke invocation alone.
    assert jobs["smoke"]["permissions"] == {"contents": "read", "packages": "write"}
    # The deploy job must not request anything extra.
    assert "permissions" not in jobs["deploy"]


def test_no_production_targets_and_no_secret_references() -> None:
    raw = WORKFLOW.read_text(encoding="utf-8")
    lower = raw.lower()
    assert "secrets." not in lower
    for token in ("production", "refs/heads/main", "refs/heads/prod", "prod-env", "main deploy"):
        assert token not in lower
    # Environment variables consumed by the deploy step must all be
    # repository variables, never runtime secrets.
    deploy = _jobs(_load_flow())["deploy"]
    for step in deploy["steps"]:
        if "env" in step:
            assert step["env"] == {
                "COOKBOOK_BETA_ENV_FILE": "${{ vars.COOKBOOK_BETA_ENV_FILE }}",
                "COOKBOOK_BETA_DATA_DIR": "${{ vars.COOKBOOK_BETA_DATA_DIR }}",
                "COOKBOOK_BETA_PUBLIC_URL": "${{ vars.COOKBOOK_BETA_PUBLIC_URL }}",
                "COOKBOOK_BETA_PORT": "${{ vars.COOKBOOK_BETA_PORT }}",
                "COOKBOOK_PROXY_NETWORK": "${{ vars.COOKBOOK_PROXY_NETWORK }}",
            }
            for value in step["env"].values():
                assert "secrets" not in value


def test_deploy_checkouts_only_the_two_pinned_files() -> None:
    deploy = _jobs(_load_flow())["deploy"]
    checkout = next(step for step in deploy["steps"] if isinstance(step.get("uses"), str))
    # Pinned commit, not a mutable tag.
    assert checkout["uses"] == "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
    options = checkout["with"]
    assert options["persist-credentials"] is False
    assert options["sparse-checkout-cone-mode"] is False
    files = [
        line.strip()
        for line in options["sparse-checkout"].splitlines()
        if line.strip()
    ]
    assert files == ["bin/deploy-beta.ps1", "docker-compose.beta.yml"]
