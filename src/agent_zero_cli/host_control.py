"""Exclusive remote viewer ownership. Persist fences, never pixels or keystrokes.

The gate closes before draining operations. Expiry/disconnect only revoke human
input; neither grants automation permission. All callers share this instance.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path


class ControlError(ValueError):
    pass


class HostControl:
    def __init__(self, path: Path, *, clock=time.monotonic):
        self.path = path
        self.clock = clock
        self.enabled = False
        self.active = 0
        self.setup_active = False
        self.drained = asyncio.Event()
        self.drained.set()
        self.serial = asyncio.Lock()
        self.deadline = 0.0
        self.state = {"epoch": uuid.uuid4().hex, "phase": "watching", "lease": "", "context": "", "sequence": 0}
        if path.exists():
            try:
                value = json.loads(path.read_text())
                if not isinstance(value, dict) or not isinstance(value.get("epoch"), str):
                    raise ValueError()
                self.state.update(value)
                if self.state["phase"] != "watching":
                    self.state["phase"] = "held"
            except (OSError, ValueError):
                # Corruption cannot silently grant automation access.
                self.state["phase"] = "held"
        self.frames = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".pending")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def snapshot(self):
        if self.state["phase"] == "human" and self.clock() >= self.deadline:
            self.hold()
        return {"version": 1, **self.state}

    def hold(self):
        if self.state["phase"] != "watching":
            self.state["phase"] = "held"
            self.deadline = 0
            self.frames.clear()
            self.save()

    @asynccontextmanager
    async def operation(self, payload):
        if self.setup_active:
            raise ControlError("A local connection test is in progress. No host action was dispatched.")
        if self.snapshot()["phase"] != "watching":
            raise ControlError("Human takeover holds this host. Return to A0 before automation can continue.")
        if self.enabled:
            if payload.get("host_epoch") != self.state["epoch"]:
                raise ControlError("Host observation is obsolete. Observe the host again before acting.")
        self.active += 1
        self.drained.clear()
        try:
            yield
        finally:
            self.active -= 1
            if not self.active:
                self.drained.set()

    @asynccontextmanager
    async def setup_operation(self):
        """Local explicit test, never a takeover, release, or agent permission."""
        async with self.serial:
            if self.snapshot()["phase"] != "watching" or self.active or self.setup_active:
                raise ControlError("Finish host activity or Return to A0 before testing the connection.")
            self.setup_active = True
            self.active += 1
            self.drained.clear()
            try:
                yield
            finally:
                self.setup_active = False
                self.active -= 1
                if not self.active:
                    self.drained.set()

    async def acquire(self, lease, context, *, blocked=lambda: False, timeout=15):
        if not isinstance(lease, str) or len(lease) != 32 or not context:
            raise ControlError("Invalid takeover identity")
        async with self.serial:
            state = self.snapshot()
            if state["phase"] != "watching" and (state["lease"] != lease or state["context"] != context):
                raise ControlError("Another viewer holds this host")
            self.state.update(phase="held", lease=lease, context=context, epoch=uuid.uuid4().hex)
            self.frames.clear()
            self.save()  # fence first, then drain
            try:
                await asyncio.wait_for(self.drained.wait(), timeout)
            except asyncio.TimeoutError as exc:
                raise ControlError("Waiting for an outstanding host operation; automation remains held") from exc
            if blocked():
                raise ControlError("A remote execution session is still open. Close it locally before takeover.")
            self.state.update(phase="human", sequence=0)
            self.deadline = self.clock() + 15
            self.save()
            return self.snapshot()

    def validate(self, payload, *, allow_held=False):
        state = self.snapshot()
        phases = {"human", "held", "returning"} if allow_held else {"human"}
        if state["phase"] not in phases or any(payload.get(key) != state[key] for key in ("lease", "epoch", "context")):
            raise ControlError("Control is no longer current. Review the host before continuing.")

    def heartbeat(self, payload):
        self.validate(payload)
        self.deadline = self.clock() + 15
        return self.snapshot()

    def remember_frame(self, source, frame):
        previous = self.frames.get(source, {})
        recent = previous.get("recent", []) + ([{k: v for k, v in previous.items() if k != "recent"}] if previous else [])
        frame["recent"] = [f for f in recent[-2:] if self.clock() - f["time"] <= 5]
        self.frames[source] = frame

    async def input(self, payload, callback):
        async with self.serial:
            self.validate(payload)
            sequence = payload.get("sequence")
            if type(sequence) is not int or sequence != self.state["sequence"] + 1:
                raise ControlError("Input sequence already consumed or out of order; input was not replayed")
            frame = self.frames.get(payload.get("source"))
            if frame and payload.get("frame") != frame["id"]:
                frame = next((f for f in frame.get("recent", []) if f["id"] == payload.get("frame")), None)
            if not frame or self.clock() - frame["time"] > 5:
                raise ControlError("The displayed frame is stale. Wait for a fresh frame.")
            self.state["sequence"] = sequence
            self.save()  # consume before effects, including an uncertain effect
            try:
                result = await callback(frame)
                self.validate(payload)
                return result
            except BaseException:
                self.hold()
                raise

    async def prepare_return(self, payload, capture):
        async with self.serial:
            self.validate(payload, allow_held=True)
            self.state["phase"] = "returning"
            self.frames.clear()
            self.save()
            # Awaited input methods release buttons/keys in finally blocks.
            frame = await capture()
            return frame

    async def release(self, payload):
        async with self.serial:
            self.validate(payload, allow_held=True)
            if self.state["phase"] != "returning":
                raise ControlError("A fresh handback observation is required")
            self.state.update(phase="watching", epoch=uuid.uuid4().hex, lease="", context="", sequence=0)
            self.frames.clear()
            self.save()
            return self.snapshot()
