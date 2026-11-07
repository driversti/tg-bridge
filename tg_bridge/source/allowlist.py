"""The owner's switched-on private chats, as a small JSON file next to the session (dir 700, file 600).

Written only by the admin page, read on every call so a toggle needs no restart.
Anything unreadable counts as empty: the scope filter then hides every private chat (fail closed).
"""

import builtins
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 1


def allowlist_path(session_path: Path) -> Path:
    return session_path.with_name(f"{session_path.stem}.allowlist.json")


class Allowlist:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> frozenset[int]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != VERSION:
                raise ValueError("version")
            ids = data["chat_ids"]
            if not isinstance(ids, list) or not all(type(i) is int for i in ids):
                raise ValueError("chat_ids")
            return frozenset(ids)
        except FileNotFoundError:
            return frozenset()
        except (OSError, ValueError, KeyError, TypeError) as e:
            # Class name only: the file content must not reach the log.
            log.warning("allowlist unreadable, treating it as empty: %s", type(e).__name__)
            return frozenset()

    def set(self, chat_id: int, on: bool) -> None:
        ids = builtins.set(self.load())
        if on:
            ids.add(chat_id)
        else:
            ids.discard(chat_id)
        self._write(ids)

    def _write(self, ids: "builtins.set[int]") -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp creates the file 600; replace() makes the write atomic.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=f".{self._path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": VERSION, "chat_ids": sorted(ids)}, f)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
