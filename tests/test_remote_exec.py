from __future__ import annotations

import os

import pytest

from agent_zero_cli.remote_exec import PythonTTYManager


def _manager() -> PythonTTYManager:
    return PythonTTYManager(
        cwd=os.getcwd(),
        exec_timeouts=(1.0, 0.4, 4.0),
        output_timeouts=(0.5, 0.4, 2.0),
    )


@pytest.mark.asyncio
async def test_remote_exec_python_runtime_roundtrip() -> None:
    manager = _manager()
    try:
        result = await manager.handle_exec_op(
            {
                "op_id": "exec-1",
                "runtime": "python",
                "session": 0,
                "code": "print(42)",
            }
        )
    finally:
        await manager.close()

    assert result["ok"] is True
    payload = result["result"]
    assert payload["running"] is False
    assert "42" in payload["output"]


@pytest.mark.asyncio
async def test_remote_exec_input_runtime_continues_interactive_session() -> None:
    manager = _manager()
    try:
        started = await manager.handle_exec_op(
            {
                "op_id": "exec-2",
                "runtime": "python",
                "session": 3,
                "code": "name = input('Name? ')\nprint(f'Hello {name}')",
            }
        )
        resumed = await manager.handle_exec_op(
            {
                "op_id": "exec-3",
                "runtime": "input",
                "session": 3,
                "keyboard": "Ada",
            }
        )
    finally:
        await manager.close()

    assert started["ok"] is True
    assert started["result"]["running"] is True
    assert "Name?" in started["result"]["output"]

    assert resumed["ok"] is True
    assert resumed["result"]["running"] is False
    assert "Hello Ada" in resumed["result"]["output"]


@pytest.mark.asyncio
async def test_remote_exec_output_requires_existing_session() -> None:
    manager = _manager()
    try:
        result = await manager.handle_exec_op(
            {
                "op_id": "exec-4",
                "runtime": "output",
                "session": 99,
            }
        )
    finally:
        await manager.close()

    assert result["ok"] is False
    assert "not initialized" in result["error"]


@pytest.mark.asyncio
async def test_remote_exec_reset_clears_session() -> None:
    manager = _manager()
    try:
        await manager.handle_exec_op(
            {
                "op_id": "exec-5",
                "runtime": "python",
                "session": 7,
                "code": "print('ready')",
            }
        )
        reset = await manager.handle_exec_op(
            {
                "op_id": "exec-6",
                "runtime": "reset",
                "session": 7,
                "reason": "test cleanup",
            }
        )
        output_after_reset = await manager.handle_exec_op(
            {
                "op_id": "exec-7",
                "runtime": "output",
                "session": 7,
            }
        )
    finally:
        await manager.close()

    assert reset["ok"] is True
    assert "reset" in reset["result"]["message"].lower()
    assert output_after_reset["ok"] is False
    assert "not initialized" in output_after_reset["error"]
