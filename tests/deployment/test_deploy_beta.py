"""Exercise deploy failure handling with mocked Docker and HTTP commands."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "bin" / "deploy-beta.ps1"
SHA = "a" * 40

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
log_path = pathlib.Path(os.environ["COOKBOOK_TEST_DOCKER_LOG"])
with log_path.open("a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "image": os.environ.get("COOKBOOK_IMAGE")}) + "\n")

stage = os.environ.get("COOKBOOK_TEST_FAIL_STAGE", "")
if args[:1] == ["compose"] and "run" in args:
    if stage == "backup":
        print("mock backup failed", file=sys.stderr)
        raise SystemExit(20)
    out = args[args.index("--out") + 1]
    backup_dir = pathlib.Path(os.environ["COOKBOOK_BETA_DATA_DIR"]) / "backups" / pathlib.PurePosixPath(out).name
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "cookbook-20261120.sql.gz").write_bytes(b"mock sql")
    (backup_dir / "media-20261120.tar.gz").write_bytes(b"mock media")
    (backup_dir / "manifest-20261120.json").write_text("{}", encoding="utf-8")
elif args[:1] == ["compose"] and "pull" in args and stage == "pull":
    print("mock pull failed", file=sys.stderr)
    raise SystemExit(21)
elif args[:1] == ["compose"] and "up" in args and stage == "migration":
    print("mock app failed to start", file=sys.stderr)
    raise SystemExit(22)
elif args[:1] == ["compose"] and "ps" in args:
    print("app-container" if args[-1] == "app" else "db-container")
elif args[:1] == ["inspect"]:
    if "{{.State.Health.Status}}" in args:
        print("unhealthy" if stage == "app-unhealthy" and args[-1] == "app-container" else "healthy")
    elif "{{.Config.Image}}" in args:
        print("ghcr.io/bjmaynard01/cookbook:sha-" + "b" * 40)
raise SystemExit(0)
"""

HARNESS = r"""param([string] $DeployScript, [string] $CommitSha)
function global:docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $DockerArgs)
    & $env:COOKBOOK_TEST_PYTHON $env:COOKBOOK_TEST_DOCKER_SCRIPT @DockerArgs
    $global:LASTEXITCODE = $LASTEXITCODE
}
function global:Invoke-WebRequest {
    param([string] $Uri, [string] $Method, [int] $TimeoutSec, [switch] $UseBasicParsing)
    if ($env:COOKBOOK_TEST_FAIL_STAGE -eq 'readiness' -and $Uri.EndsWith('/readyz')) {
        return [PSCustomObject]@{ StatusCode = 503 }
    }
    return [PSCustomObject]@{ StatusCode = 200 }
}
function global:Start-Sleep { param([int] $Seconds) }
& $DeployScript $CommitSha
exit $LASTEXITCODE
"""


@pytest.fixture
def deploy_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell Core is unavailable")

    data_dir = tmp_path / "cookbook-beta-data"
    data_dir.mkdir()
    env_file = tmp_path / "cookbook-beta.env"
    env_file.write_text("BETA_SECRET_KEY=placeholder-only\n", encoding="utf-8")
    fake_docker = tmp_path / "fake-docker.py"
    fake_docker.write_text(FAKE_DOCKER, encoding="utf-8")
    harness = tmp_path / "deploy-harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8")
    log_file = tmp_path / "docker-calls.jsonl"

    monkeypatch.setenv("COOKBOOK_BETA_ENV_FILE", str(env_file))
    monkeypatch.setenv("COOKBOOK_BETA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COOKBOOK_BETA_PUBLIC_URL", "https://cookbook-dev.maynardfolks.com")
    monkeypatch.setenv("COOKBOOK_BETA_PORT", "18080")
    monkeypatch.setenv("COOKBOOK_PROXY_NETWORK", "reverse-proxy")
    monkeypatch.setenv("COOKBOOK_TEST_DOCKER_LOG", str(log_file))
    monkeypatch.setenv("COOKBOOK_TEST_DOCKER_SCRIPT", str(fake_docker))
    monkeypatch.setenv("COOKBOOK_TEST_PYTHON", sys.executable)
    monkeypatch.delenv("COOKBOOK_TEST_FAIL_STAGE", raising=False)
    return data_dir, log_file


def _run_deploy(log_file: Path, stage: str = "") -> subprocess.CompletedProcess[str]:
    pwsh = shutil.which("pwsh")
    assert pwsh
    env = os.environ.copy()
    env["COOKBOOK_TEST_FAIL_STAGE"] = stage
    return subprocess.run(
        [pwsh, "-NoProfile", "-File", str(Path(log_file).parent / "deploy-harness.ps1"), str(DEPLOY), SHA],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("stage", ["backup", "pull", "migration", "readiness"])
def test_post_stop_failures_leave_app_offline_and_preserve_backups(
    stage: str, deploy_sandbox: tuple[Path, Path]
) -> None:
    data_dir, log_file = deploy_sandbox
    result = _run_deploy(log_file, stage)
    assert result.returncode == 1, result.stderr
    calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    compose_calls = [call for call in calls if call["args"][:1] == ["compose"]]
    assert compose_calls[-1]["args"][-2:] == ["stop", "app"]
    assert not any("restore" in call["args"] for call in calls)
    if stage == "backup":
        assert any("run" in call["args"] for call in compose_calls)
    else:
        backup_dirs = list((data_dir / "backups").iterdir())
        assert len(backup_dirs) == 1
        assert len(list(backup_dirs[0].glob("*.gz"))) == 2
        assert len(list(backup_dirs[0].glob("manifest-*.json"))) == 1
    assert "ghcr.io/bjmaynard01/cookbook:sha-" + "b" * 40 in result.stderr


def test_success_uses_old_image_for_backup_then_new_sha(deploy_sandbox: tuple[Path, Path]) -> None:
    data_dir, log_file = deploy_sandbox
    result = _run_deploy(log_file)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    backup_call = next(call for call in calls if "run" in call["args"])
    pull_call = next(call for call in calls if "pull" in call["args"])
    up_call = next(call for call in calls if "up" in call["args"])
    assert backup_call["image"] == "ghcr.io/bjmaynard01/cookbook:sha-" + "b" * 40
    expected = "ghcr.io/bjmaynard01/cookbook:sha-" + SHA
    assert pull_call["image"] == expected
    assert up_call["image"] == expected
    assert len(list((data_dir / "backups").glob("*/manifest-*.json"))) == 1


def test_unhealthy_existing_app_fails_before_stop_or_backup(
    deploy_sandbox: tuple[Path, Path]
) -> None:
    data_dir, log_file = deploy_sandbox
    result = _run_deploy(log_file, "app-unhealthy")
    assert result.returncode == 1
    calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    compose_calls = [call for call in calls if call["args"][:1] == ["compose"]]
    assert not any(call["args"][-2:] == ["stop", "app"] for call in compose_calls)
    assert not any("run" in call["args"] for call in compose_calls)
    assert not (data_dir / "backups").exists()
