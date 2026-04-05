from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


@dataclass(frozen=True)
class CommandAvailability:
    available: bool
    reason: str | None = None


CommandHandler = Callable[["AgentZeroCLI"], Awaitable[None]]
CommandAvailabilityPredicate = Callable[["AgentZeroCLI"], CommandAvailability]


@dataclass(frozen=True)
class CommandSpec:
    canonical_name: str
    aliases: tuple[str, ...]
    description: str
    availability: CommandAvailabilityPredicate
    handler: CommandHandler

    def names(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.aliases)

    def matches(self, token: str) -> bool:
        normalized = token.strip().lower()
        return normalized in self.names()
