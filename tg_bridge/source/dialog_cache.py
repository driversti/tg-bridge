"""Dialog list and chat folders on disk, so the cache survives LM Studio restarting the server for every chat message.

The file holds names, usernames, phones and folder titles, so it lives next to the session (dir 700, file 600).
Any problem reading it is a cache miss: the caller just fetches from Telegram again.
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from tg_bridge.source.models import Chat, Folder, Person

log = logging.getLogger(__name__)

VERSION = 2  # 2: folders and the chat state their rules need


class DialogCache:
    def __init__(self, path: Path):
        self._path = path

    def load(self, now: float, max_age: float) -> tuple[list[Chat], list[Folder], float] | None:
        """(chats, folders, saved_at) if the file is younger than max_age, else None."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("version") != VERSION:
                return None
            saved_at = float(data["saved_at"])
            if not 0 <= now - saved_at < max_age:  # a future timestamp means the clock moved: don't trust it
                return None
            chats = [_chat_from_dict(c) for c in data["chats"]]
            return chats, [_folder_from_dict(f) for f in data["folders"]], saved_at
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("dialog cache unreadable, ignoring it: %s", type(e).__name__)
            return None

    def save(self, chats: list[Chat], folders: list[Folder], now: float) -> None:
        data = {
            "version": VERSION,
            "saved_at": now,
            "chats": [_chat_to_dict(c) for c in chats],
            "folders": [asdict(f) for f in folders],
        }
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp creates the file 600; replace() makes the write atomic, so a crash never leaves half a file.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=f".{self._path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _chat_to_dict(chat: Chat) -> dict[str, Any]:
    d = asdict(chat)
    d["last_message_at"] = chat.last_message_at.isoformat() if chat.last_message_at else None
    return d


def _chat_from_dict(d: dict[str, Any]) -> Chat:
    peer = d["peer"]
    last = d["last_message_at"]
    return Chat(
        id=d["id"],
        kind=d["kind"],
        title=d["title"],
        username=d["username"],
        peer=Person(**peer) if peer is not None else None,
        last_message_at=datetime.fromisoformat(last) if last else None,
        is_archived=d["is_archived"],
        is_forum=d["is_forum"],
        is_muted=d["is_muted"],
        is_unread=d["is_unread"],
        has_unread_mention=d["has_unread_mention"],
        # Added later: an older cache file has no such keys and must still load.
        is_self=d.get("is_self", False),
        is_creator=d.get("is_creator", False),
        photo_id=d.get("photo_id"),
    )


def _folder_from_dict(d: dict[str, Any]) -> Folder:
    # JSON has no tuples; the dataclass is frozen and hashable, so give the ids back as tuples.
    return Folder(
        **{k: tuple(v) if k in ("pinned_ids", "include_ids", "exclude_ids") else v for k, v in d.items()}
    )
