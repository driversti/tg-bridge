"""Domain models. Telethon-free so tools and future sources (archive) share them."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Person:
    id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    phone: str | None
    is_deleted: bool
    is_bot: bool

    @property
    def display_name(self) -> str:
        if self.is_deleted:
            return "Deleted Account"
        name = " ".join(p.strip() for p in (self.first_name, self.last_name) if p and p.strip())
        return name or "(no name)"
