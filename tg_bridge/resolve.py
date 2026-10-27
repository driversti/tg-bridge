"""Chat argument → exactly one dialog. Pure functions over the dialog list, so every source shares them."""

import re
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher

from tg_bridge.errors import AmbiguousChatError, ChatNotFoundError
from tg_bridge.source import Chat

MAX_SUGGESTIONS = 5
SUGGESTION_THRESHOLD = 0.5

_ID_RE = re.compile(r"-?\d+")
_TME_RE = re.compile(r"(?:https?://)?(?:www\.)?t\.me/@?([A-Za-z0-9_]+)", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^\w]|_", re.UNICODE)


def normalize(text: str) -> str:
    """NFKC → casefold → non-alphanumerics become spaces → collapsed. Works for Cyrillic, drops emoji."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_NON_ALNUM.sub(" ", folded).split())


def resolve_chat(arg: str, chats: Sequence[Chat]) -> Chat:
    """Never guesses: several hits raise AmbiguousChatError, none raises ChatNotFoundError."""
    raw = arg.strip()
    if not raw:
        raise ChatNotFoundError(arg, [])

    if _ID_RE.fullmatch(raw):
        hits = _by_id(int(raw), chats)
        return _one(arg, hits, chats, normalize(raw))

    username = _username(raw)
    if username is not None:
        hits = [c for c in chats if username in _usernames(c)]
        return _one(arg, hits, chats, normalize(username))

    query = normalize(raw)
    exact = [c for c in chats if query and query in _search_names(c)]
    return _one(arg, exact or match_chats(raw, chats), chats, query)


def match_chats(query: str, chats: Sequence[Chat]) -> list[Chat]:
    """Every chat whose name contains all query tokens (any order), in input order."""
    tokens = normalize(query).split()
    if not tokens:
        return []
    return [c for c in chats if any(all(t in name for t in tokens) for name in _search_names(c))]


def _by_id(n: int, chats: Sequence[Chat]) -> list[Chat]:
    if n < 0:
        return [c for c in chats if c.id == n]
    # Models often drop the prefix: try user, basic group, channel in that order.
    for candidate in (n, -n, -(10**12) - n):
        hits = [c for c in chats if c.id == candidate]
        if hits:
            return hits
    return []


def _username(raw: str) -> str | None:
    link = _TME_RE.fullmatch(raw)
    if link:
        return link.group(1).casefold()
    if raw.startswith("@"):
        return raw[1:].strip().casefold()
    return None


def _usernames(chat: Chat) -> set[str]:
    names = {chat.username} if chat.username else set()
    if chat.peer is not None and chat.peer.username:
        names.add(chat.peer.username)
    return {n.casefold() for n in names}


def _search_names(chat: Chat) -> list[str]:
    names = [normalize(chat.title)]
    if chat.peer is not None and chat.peer.username:
        names.append(normalize(chat.peer.username))
    return names


def _one(arg: str, hits: list[Chat], chats: Sequence[Chat], query: str) -> Chat:
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise AmbiguousChatError(arg, hits)
    raise ChatNotFoundError(arg, _suggest(query, chats))


def _suggest(query: str, chats: Sequence[Chat]) -> list[Chat]:
    if not query:
        return []
    scored: list[tuple[float, Chat]] = []
    for chat in chats:
        names = _search_names(chat) + [normalize(u) for u in _usernames(chat)]
        candidates = {part for name in names for part in (name, *name.split())}
        best = max((SequenceMatcher(None, query, n).ratio() for n in candidates if n), default=0.0)
        if best >= SUGGESTION_THRESHOLD:
            scored.append((best, chat))
    scored.sort(key=lambda pair: -pair[0])  # stable: ties keep dialog order
    return [c for _, c in scored[:MAX_SUGGESTIONS]]
