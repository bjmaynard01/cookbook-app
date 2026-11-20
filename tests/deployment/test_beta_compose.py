"""Deployment configuration guardrails; config rendering never contacts Docker."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.beta.yml"


def _compose_config(tmp_path: Path) -> dict:
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose v2 is unavailable")
    env_file = tmp_path / "beta-placeholder.env"
    env_file.write_text(
        "\n".join(
            [
                "BETA_DB_ROOT_PASSWORD=placeholder-root",
                "BETA_DB_NAME=cookbook_beta",
                "BETA_DB_USER=cookbook_beta",
                "BETA_DB_PASSWORD=placeholder-db",
                "BETA_SECRET_KEY=placeholder-secret-key",
                "BETA_TRUSTED_PROXY_CIDRS=172.28.0.0/16",
                f"COOKBOOK_BETA_DATA_DIR={tmp_path.as_posix()}/beta-only",
                "COOKBOOK_BETA_PORT=18080",
                "COOKBOOK_PROXY_NETWORK=reverse-proxy",
                "COOKBOOK_IMAGE=ghcr.io/example/cookbook:sha-" + "a" * 40,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [docker, "compose", "--project-name", "cookbook-beta-test", "--env-file", str(env_file), "-f", str(COMPOSE), "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "beta compose config did not resolve"
    return json.loads(result.stdout)


def test_beta_compose_isolated_from_local_and_only_uses_beta_paths(tmp_path: Path) -> None:
    config = _compose_config(tmp_path)
    services = config["services"]
    assert set(services) == {"app", "db"}
    assert all("container_name" not in service for service in services.values())
    assert all("cookbook:dev" not in json.dumps(service) for service in services.values())
    assert config["networks"]["backend"]["internal"] is True
    assert config["networks"]["proxy"]["external"] is True
    assert set(services["db"]["networks"]) == {"backend"}
    assert set(services["app"]["networks"]) == {"backend", "proxy"}
    assert services["app"]["image"].endswith("sha-" + "a" * 40)
    assert services["app"]["ports"][0]["published"] == "18080"
    assert services["app"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["db"]["volumes"][0]["source"].endswith("/beta-only/db")
    assert {volume["source"].rsplit("/", 1)[-1] for volume in services["app"]["volumes"]} == {"app", "backups"}
    assert services["app"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert "MARIADB_ROOT_PASSWORD" not in services["app"]["environment"]


def test_deploy_script_checks_full_sha_and_never_restores_automatically() -> None:
    script = (ROOT / "bin" / "deploy-beta.ps1").read_text(encoding="utf-8")
    assert "^[0-9a-fA-F]{40}$" in script
    assert "docker compose" in script
    assert "--project-name', 'cookbook-beta'" in script
    assert "@('restore'" not in script
    assert "FileShare]::None" in script
    assert "stop', 'app'" in script


@pytest.mark.parametrize("arguments", [[], ["not-a-sha"], ["a" * 39], ["a" * 40, "extra"]])
def test_invalid_deploy_arguments_exit_before_docker(arguments: list[str]) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell Core is unavailable")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(ROOT / "bin" / "deploy-beta.ps1"), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Usage:" in result.stderr
    assert "docker compose" not in result.stderr.lower()
