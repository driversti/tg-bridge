"""Headline extraction and repost grouping for folder digests. Pure functions over domain models; no LLM (D1)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from tg_bridge.format import one_line
from tg_bridge.source import Message

HEADLINE_CHARS = 140
MIN_WORD_CHARS = 3
TOKEN_CHARS = 6
MAX_TOKENS = 60
MIN_TOKENS = 5
OVERLAP = 0.7
REPOST_WINDOW = timedelta(hours=72)

_URL = re.compile(r"https?://\S+|t\.me/\S+")
_NOISE = re.compile(r"https?://\S+|t\.me/\S+|#\w+|@\w+")
_WORD = re.compile(r"\w")
_MENTION = re.compile(r"@\w+")


def headline(text: str) -> str | None:
    """First line with real words in it, URLs removed and cut to HEADLINE_CHARS; None when there is none."""
    for line in text.splitlines():
        if len(_WORD.findall(_NOISE.sub("", line))) < MIN_WORD_CHARS:
            continue
        flat = " ".join(one_line(_URL.sub("", line)).split())
        return _cut(flat)
    return None


def _cut(line: str) -> str:
    if len(line) <= HEADLINE_CHARS:
        return line
    # Room for the "…" inside the limit: prefer a word boundary in the last 40 characters.
    space = line.rfind(" ", 100, HEADLINE_CHARS)
    return line[: space if space > 0 else HEADLINE_CHARS - 1].rstrip() + "…"


def repost_tokens(text: str) -> frozenset[str]:
    """Stemmed word set used to compare posts. Cutting words to 6 characters folds Ukrainian/Polish endings well
    enough (`contract`/`contracts`) without a language model or stemmer."""
    cleaned = _MENTION.sub(" ", _URL.sub(" ", text.casefold()))
    words = [w[:TOKEN_CHARS] for w in re.findall(r"\w+", cleaned) if len(w) >= 2]
    return frozenset(words[:MAX_TOKENS])


def similar(a: frozenset[str], b: frozenset[str]) -> bool:
    """Overlap coefficient, so a short repost of a long post still matches; tiny sets must be equal."""
    smaller = min(len(a), len(b))
    if smaller < MIN_TOKENS:
        return a == b and bool(a)
    return len(a & b) / smaller >= OVERLAP


@dataclass(frozen=True)
class Repost:
    posts: tuple[Message, ...]  # chronological, first = the one shown; len 1 = no repost

    @property
    def first(self) -> Message:
        return self.posts[0]


def group_reposts(messages: Sequence[Message]) -> list[Repost]:
    """Greedy chronological clustering. Same-chat posts never merge: templated posts ("FP1 results", "FP2 results")
    in one channel are different news. `messages` must be chronological."""
    groups: list[tuple[frozenset[str], list[Message]]] = []  # first post's tokens, members
    for msg in messages:
        tokens = repost_tokens(msg.text)
        for first_tokens, members in groups:
            if (
                msg.date - members[0].date <= REPOST_WINDOW
                and all(m.chat_id != msg.chat_id for m in members)
                and similar(first_tokens, tokens)
            ):
                members.append(msg)
                break
        else:
            groups.append((tokens, [msg]))
    return [Repost(tuple(members)) for _, members in groups]
