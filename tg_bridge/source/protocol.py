"""The interface tools depend on; stage 1 has LiveSource, stage 2 adds ArchiveSource."""

from typing import Protocol

from tg_bridge.source.models import Person


class MessageSource(Protocol):
    async def whoami(self) -> Person:
        """The account the bridge is logged in as."""
        ...

    async def close(self) -> None:
        """Release connections; safe to call when nothing is open."""
        ...
