from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent_zero_cli.instance_discovery as instance_discovery


pytestmark = pytest.mark.anyio


def _command_result(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _container(
    *,
    container_id: str,
    name: str,
    image: str,
    host_bindings: list[dict[str, str]] | None,
    path: str = "",
    args: tuple[str, ...] = (),
    cmd: tuple[str, ...] = (),
    entrypoint: tuple[str, ...] = (),
    mounts: list[dict[str, str]] | None = None,
    host_binds: tuple[str, ...] = (),
    running: bool = True,
) -> dict[str, object]:
    return {
        "Id": container_id,
        "Name": f"/{name}",
        "Image": image,
        "Path": path,
        "Args": list(args),
        "State": {"Running": running},
        "Config": {
            "Image": image,
            "Cmd": list(cmd),
            "Entrypoint": list(entrypoint),
        },
        "HostConfig": {
            "Binds": list(host_binds),
        },
        "Mounts": mounts or [],
        "NetworkSettings": {
            "Ports": {
                "80/tcp": host_bindings,
            }
        },
    }


def _stub_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ps_ids: list[str],
    inspect_payload: list[dict[str, object]],
) -> None:
    monkeypatch.setattr(instance_discovery, "_find_docker_cli", lambda: "/usr/bin/docker")

    async def fake_run_command(*args: str) -> SimpleNamespace:
        if args[1:] == ("ps", "--format", "{{.ID}}"):
            stdout = "\n".join(ps_ids)
            if stdout:
                stdout += "\n"
            return _command_result(stdout=stdout)
        if len(args) >= 3 and args[1] == "inspect":
            assert list(args[2:]) == ps_ids
            return _command_result(stdout=json.dumps(inspect_payload))
        raise AssertionError(f"Unexpected docker command: {args}")

    monkeypatch.setattr(instance_discovery, "_run_command", fake_run_command)


async def test_discovery_returns_unavailable_when_docker_cli_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(instance_discovery, "_find_docker_cli", lambda: None)

    result = await instance_discovery.discover_local_instances()

    assert result.status == "unavailable"
    assert result.instances == ()


async def test_discovery_ignores_non_agent_zero_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_docker(
        monkeypatch,
        ps_ids=["nginx-1"],
        inspect_payload=[
            _container(
                container_id="nginx-1",
                name="nginx",
                image="nginx:latest",
                host_bindings=[{"HostIp": "0.0.0.0", "HostPort": "50001"}],
            )
        ],
    )

    result = await instance_discovery.discover_local_instances()

    assert result.status == "empty"
    assert result.instances == ()


async def test_discovery_finds_agent_zero_by_image_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_docker(
        monkeypatch,
        ps_ids=["agent-zero-1"],
        inspect_payload=[
            _container(
                container_id="agent-zero-1",
                name="agent-zero",
                image="frdel/agent-zero:latest",
                host_bindings=[{"HostIp": "0.0.0.0", "HostPort": "50001"}],
            )
        ],
    )

    result = await instance_discovery.discover_local_instances()

    assert result.status == "ready"
    assert [instance.url for instance in result.instances] == ["http://localhost:50001"]


@pytest.mark.parametrize(
    ("path", "cmd", "mounts"),
    [
        ("", (), [{"Destination": "/a0", "Type": "bind"}]),
        ("/usr/bin/python", ("run_ui.py",), []),
    ],
    ids=["mount-signal", "command-signal"],
)
async def test_discovery_finds_image_id_container_via_agent_zero_signals(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    cmd: tuple[str, ...],
    mounts: list[dict[str, str]],
) -> None:
    _stub_docker(
        monkeypatch,
        ps_ids=["sha-container"],
        inspect_payload=[
            _container(
                container_id="sha-container",
                name="agent-zero-dev",
                image="sha256:deadbeef",
                path=path,
                cmd=cmd,
                mounts=mounts,
                host_bindings=[{"HostIp": "127.0.0.1", "HostPort": "50002"}],
            )
        ],
    )

    result = await instance_discovery.discover_local_instances()

    assert result.status == "ready"
    assert [instance.url for instance in result.instances] == ["http://127.0.0.1:50002"]


async def test_discovery_deduplicates_multiple_bindings_by_final_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_docker(
        monkeypatch,
        ps_ids=["agent-zero-1"],
        inspect_payload=[
            _container(
                container_id="agent-zero-1",
                name="agent-zero",
                image="frdel/agent-zero:latest",
                host_bindings=[
                    {"HostIp": "0.0.0.0", "HostPort": "50001"},
                    {"HostIp": "::", "HostPort": "50001"},
                    {"HostIp": "127.0.0.1", "HostPort": "50001"},
                ],
            )
        ],
    )

    result = await instance_discovery.discover_local_instances()

    assert result.status == "ready"
    assert [instance.url for instance in result.instances] == [
        "http://localhost:50001",
        "http://127.0.0.1:50001",
    ]


async def test_discovery_maps_empty_host_bindings_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_docker(
        monkeypatch,
        ps_ids=["agent-zero-1"],
        inspect_payload=[
            _container(
                container_id="agent-zero-1",
                name="agent-zero",
                image="frdel/agent-zero:latest",
                host_bindings=[{"HostIp": "", "HostPort": "50003"}],
            )
        ],
    )

    result = await instance_discovery.discover_local_instances()

    assert result.status == "ready"
    assert result.instances[0].url == "http://localhost:50003"
