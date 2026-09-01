from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_zero_cli.instance_discovery as discovery


pytestmark = pytest.mark.anyio


async def test_discover_local_instances_reports_unavailable_without_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: None)
    monkeypatch.setattr(discovery, "_find_wsl_cli", lambda: None)
    monkeypatch.setattr(discovery, "_docker_socket_paths", lambda: ())
    monkeypatch.setattr(discovery, "_docker_api_base_urls", lambda: ())

    result = await discovery.discover_local_instances()

    assert result.status == "unavailable"
    assert result.instances == ()
    assert result.detail == "No local Docker runtime responded. Enter a URL manually."


async def test_discover_local_instances_uses_local_docker_api_without_windows_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = discovery.DiscoveryResult(
        status="ready",
        instances=(
            discovery.DiscoveredInstance(
                id="container-a:5080",
                name="agent-zero",
                url="http://127.0.0.1:5080",
                host_port="5080",
                source="docker-api",
            ),
        ),
        detail="Found 1 local Agent Zero endpoint.",
    )
    calls: list[str] = []

    async def fake_discover_with_docker_api(base_url: str) -> discovery.DiscoveryResult:
        calls.append(base_url)
        return expected

    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: None)
    monkeypatch.setattr(discovery, "_find_wsl_cli", lambda: None)
    monkeypatch.setattr(discovery, "_docker_socket_paths", lambda: ())
    monkeypatch.setattr(discovery, "_docker_api_base_urls", lambda: ("http://127.0.0.1:23750",))
    monkeypatch.setattr(discovery, "_discover_with_docker_api", fake_discover_with_docker_api)

    result = await discovery.discover_local_instances()

    assert result == expected
    assert calls == ["http://127.0.0.1:23750"]


def test_docker_socket_paths_include_colima_context_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_socket = tmp_path / ".colima/a0/docker.sock"
    context_meta = tmp_path / ".docker/contexts/meta/context-a/meta.json"
    context_meta.parent.mkdir(parents=True)
    context_meta.write_text(
        json.dumps(
            {
                "Name": "colima-a0",
                "Endpoints": {
                    "docker": {
                        "Host": f"unix://{context_socket}",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    context_socket.parent.mkdir(parents=True)
    context_socket.write_text("", encoding="utf-8")

    monkeypatch.setattr(discovery.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(discovery, "_socket_path_exists", lambda path: str(path) == str(context_socket))

    assert discovery._docker_socket_paths() == (str(context_socket),)


async def test_discover_local_instances_uses_colima_socket_without_docker_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = discovery.DiscoveryResult(
        status="ready",
        instances=(
            discovery.DiscoveredInstance(
                id="container-a:32769",
                name="agent-zero-latest",
                url="http://127.0.0.1:32769",
                host_port="32769",
                source="docker-socket",
            ),
        ),
        detail="Found 1 local Agent Zero endpoint.",
    )
    calls: list[str] = []

    async def fake_discover_with_docker_socket(socket_path: str) -> discovery.DiscoveryResult:
        calls.append(socket_path)
        return expected

    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: None)
    monkeypatch.setattr(discovery, "_find_wsl_cli", lambda: None)
    monkeypatch.setattr(discovery, "_docker_socket_paths", lambda: ("/Users/test/.colima/a0/docker.sock",))
    monkeypatch.setattr(discovery, "_docker_api_base_urls", lambda: ())
    monkeypatch.setattr(discovery, "_discover_with_docker_socket", fake_discover_with_docker_socket)

    result = await discovery.discover_local_instances()

    assert result == expected
    assert calls == ["/Users/test/.colima/a0/docker.sock"]


async def test_discover_local_instances_falls_back_to_wsl_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "Id": "container-a",
            "Name": "/agent-zero",
            "Config": {"Image": "agent0ai/agent-zero:latest"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5080"}]}},
        },
    ]
    calls: list[tuple[str, ...]] = []

    async def fake_run_command(*args: str, timeout: float = 8.0) -> discovery._CommandResult:
        del timeout
        calls.append(args)
        if args[-1] == "{{.ID}}":
            return discovery._CommandResult(returncode=0, stdout="container-a\n", stderr="")
        return discovery._CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: None)
    monkeypatch.setattr(discovery, "_find_wsl_cli", lambda: "wsl.exe")
    monkeypatch.setattr(discovery, "_docker_socket_paths", lambda: ())
    monkeypatch.setattr(discovery, "_docker_api_base_urls", lambda: ())
    monkeypatch.setattr(discovery, "_run_command", fake_run_command)

    result = await discovery.discover_local_instances()

    assert result.status == "ready"
    assert len(result.instances) == 1
    assert result.instances[0].url == "http://127.0.0.1:5080"
    assert result.instances[0].source == "wsl-docker"
    assert calls[0][:3] == ("wsl.exe", "--exec", "docker")


async def test_discover_local_instances_continues_after_empty_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "Id": "container-a",
            "Name": "/agent-zero",
            "Config": {"Image": "agent0ai/agent-zero:latest"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5080"}]}},
        },
    ]
    calls: list[tuple[str, ...]] = []

    async def fake_run_command(*args: str, timeout: float = 8.0) -> discovery._CommandResult:
        del timeout
        calls.append(args)
        if args[0] == "docker":
            return discovery._CommandResult(returncode=0, stdout="", stderr="")
        if args[-1] == "{{.ID}}":
            return discovery._CommandResult(returncode=0, stdout="container-a\n", stderr="")
        return discovery._CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(discovery.sys, "platform", "win32")
    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: "docker")
    monkeypatch.setattr(discovery, "_find_wsl_cli", lambda: "wsl.exe")
    monkeypatch.setattr(discovery, "_docker_socket_paths", lambda: ())
    monkeypatch.setattr(discovery, "_docker_api_base_urls", lambda: ())
    monkeypatch.setattr(discovery, "_run_command", fake_run_command)

    result = await discovery.discover_local_instances()

    assert result.status == "ready"
    assert result.instances[0].source == "wsl-docker"
    assert calls[0][0] == "docker"
    assert calls[1][:3] == ("wsl.exe", "--exec", "docker")


async def test_discover_local_instances_returns_multiple_agent_zero_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "Id": "container-a",
            "Name": "/agent-zero",
            "Config": {"Image": "agent0ai/agent-zero:latest"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5080"}]}},
        },
        {
            "Id": "container-b",
            "Name": "/agent-zero-2",
            "Config": {"Image": "agent0ai/agent-zero:latest"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5081"}]}},
        },
    ]

    async def fake_run_command(*args: str) -> discovery._CommandResult:
        if args[-1] == "{{.ID}}":
            return discovery._CommandResult(returncode=0, stdout="container-a\ncontainer-b\n", stderr="")
        return discovery._CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: "docker")
    monkeypatch.setattr(discovery, "_run_command", fake_run_command)

    result = await discovery.discover_local_instances()

    assert result.status == "ready"
    assert [instance.url for instance in result.instances] == [
        "http://localhost:5080",
        "http://127.0.0.1:5081",
    ]
    assert [instance.host_port for instance in result.instances] == ["5080", "5081"]


async def test_discover_local_instances_prefers_launcher_friendly_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "Id": "container-a",
            "Name": "/a0-inst-agent-zero-latest-mqo7xckc",
            "Config": {
                "Image": "a0-launcher-clone:clone-mqo7vz54-0e399e4dd406",
                "Labels": {"a0.launcher.instanceName": "agent-zero-latest"},
            },
            "State": {"Running": True},
            "Mounts": [{"Destination": "/a0", "Type": "bind"}],
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32769"}]}},
        },
    ]

    async def fake_run_command(*args: str) -> discovery._CommandResult:
        if args[-1] == "{{.ID}}":
            return discovery._CommandResult(returncode=0, stdout="container-a\n", stderr="")
        return discovery._CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: "docker")
    monkeypatch.setattr(discovery, "_run_command", fake_run_command)

    result = await discovery.discover_local_instances()

    assert result.status == "ready"
    assert len(result.instances) == 1
    instance = result.instances[0]
    assert instance.name == "agent-zero-latest"
    assert instance.status_text == "agent-zero-latest"
    assert instance.url == "http://127.0.0.1:32769"
