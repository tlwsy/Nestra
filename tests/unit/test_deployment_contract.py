"""M0 容器部署文件的静态契约测试。

CI 不一定有 Docker daemon，因此这里先验证不会随重构悄悄丢掉的安全属性；
实际镜像构建仍应在有 Docker 的发布流水线执行。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]


def compose() -> dict:
    return yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_is_single_service() -> None:
    assert set(compose()["services"]) == {"nestra"}


def test_compose_only_binds_host_loopback() -> None:
    service = compose()["services"]["nestra"]
    ports = service["ports"]
    assert ports
    assert all(str(port).startswith("127.0.0.1:") for port in ports)
    assert service["environment"]["NESTRA__WEB__HOST"] == "0.0.0.0"  # noqa: S104
    assert str(service["environment"]["NESTRA__WEB__PORT"]) == "8080"


def test_compose_has_healthcheck_and_restart_policy() -> None:
    service = compose()["services"]["nestra"]
    assert service["restart"] == "unless-stopped"
    assert "/healthz" in " ".join(service["healthcheck"]["test"])
    assert service["healthcheck"]["retries"] >= 3


def test_compose_persists_data_and_mounts_config_readonly() -> None:
    service = compose()["services"]["nestra"]
    volumes = service["volumes"]
    assert any("../data:/app/data" in volume for volume in volumes)
    assert any(volume.endswith(":ro") and "/app/config/config.yaml" in volume for volume in volumes)
    # 容器非 root UID/GID 可与宿主 bind mount 所有者对齐。
    assert "NESTRA_UID" in service["build"]["args"]
    assert "NESTRA_GID" in service["build"]["args"]
    assert service["build"]["target"] == "${NESTRA_IMAGE_TARGET:-runtime}"


def test_compose_caps_memory_and_rotates_logs() -> None:
    service = compose()["services"]["nestra"]
    assert service["mem_limit"] == "1400m"
    assert service["pids_limit"] == 256
    assert service["init"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["logging"]["driver"] == "json-file"
    assert service["logging"]["options"]["max-size"] == "10m"
    assert service["security_opt"] == ["no-new-privileges:true"]


def test_dockerfile_runs_as_non_root() -> None:
    text = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER\s+nestra$", text, re.MULTILINE)
    assert "--no-dev" in text
    assert "uv sync --frozen" in text
    assert "COPY --from=builder /opt/venv" in text
    assert "scripts/*.py /app/scripts/" in text
    assert "chmod 0700 data data/db data/attachments data/models data/models/tagsets" in text
    assert 'test "${NESTRA_UID}" -gt 0' in text
    assert 'test "${NESTRA_GID}" -gt 0' in text
    for target in ("runtime", "runtime-render", "runtime-local", "runtime-full"):
        assert f"AS {target}" in text


def test_entrypoint_is_executable_and_fail_fast() -> None:
    path = ROOT / "deploy/entrypoint.sh"
    assert path.stat().st_mode & 0o111
    text = path.read_text(encoding="utf-8")
    assert "set -eu" in text
    assert "data_directory_not_writable" in text
    assert "uid_gid_must_not_be_zero" in text
    assert text.index("nestra config check") < text.index("nestra db migrate")
    assert text.count("--log-format json") >= 3
    assert text.count(">/dev/null") >= 2
    assert "[nestra]" not in text
    assert text.rstrip().endswith('exec "$@"')


def test_dockerignore_excludes_secrets_and_runtime_data() -> None:
    lines = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {".env", "config/config.yaml", "data"} <= lines
