"""Chat folders: membership, order, argument resolution. Pure functions over domain models, no Telegram."""

from collections.abc import Sequence

from tg_bridge.errors import AmbiguousFolderError, FolderNotFoundError
from tg_bridge.resolve import normalize
from tg_bridge.source import Chat, Folder


def folder_contains(folder: Folder, chat: Chat) -> bool:
    """Telegram Desktop's `ChatFilter::contains`, so counts match the app."""
    if chat.id in folder.exclude_ids:
        return False
    if chat.id in folder.pinned_ids or chat.id in folder.include_ids:
        return True  # explicit picks ignore the exclude_* switches
    if not _type_matches(folder, chat):
        return False
    if folder.exclude_muted and chat.is_muted and not (chat.has_unread_mention and not chat.is_archived):
        return False
    if folder.exclude_read and not chat.is_unread:
        return False
    return not (folder.exclude_archived and chat.is_archived)


def _type_matches(folder: Folder, chat: Chat) -> bool:
    if chat.kind == "user":
        is_contact = chat.peer is not None and chat.peer.is_contact
        return folder.contacts if is_contact else folder.non_contacts
    if chat.kind == "bot":
        return folder.bots
    if chat.kind in ("group", "supergroup"):
        return folder.groups
    return folder.broadcasts


def folder_chats(folder: Folder, chats: Sequence[Chat]) -> list[Chat]:
    """Members in folder order: pinned first, then by recency. Peers absent from `chats` (left chats) are ignored."""
    members = [c for c in chats if folder_contains(folder, c)]
    by_id = {c.id: c for c in members}
    pinned = [by_id[i] for i in folder.pinned_ids if i in by_id]
    pinned_set = {c.id for c in pinned}
    rest = [c for c in members if c.id not in pinned_set]
    # Stable sort: None sorts last, ties keep dialog order.
    rest.sort(key=lambda c: (c.last_message_at is None, -(c.last_message_at.timestamp() if c.last_message_at else 0)))
    return pinned + rest


def chat_folders(chat: Chat, folders: Sequence[Folder]) -> list[Folder]:
    return [f for f in folders if folder_contains(f, chat)]


def resolve_folder(arg: str, folders: Sequence[Folder]) -> Folder:
    """Id first (digits only), then exact normalised title, then title containing all tokens. Never guesses."""
    raw = arg.strip()
    if raw.isdigit():
        by_id = [f for f in folders if f.id == int(raw)]
        if by_id:
            return by_id[0]
    query = normalize(raw)
    tokens = query.split()
    if not tokens:
        raise FolderNotFoundError(arg, list(folders))
    exact = [f for f in folders if normalize(f.title) == query]
    hits = exact or [f for f in folders if all(t in normalize(f.title) for t in tokens)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise AmbiguousFolderError(arg, hits)
    raise FolderNotFoundError(arg, list(folders))


def rule_summary(folder: Folder) -> str:
    """What the folder holds, in a few words: type rules and/or hand-picked chats."""
    flags = [
        name
        for name, on in (
            ("contacts", folder.contacts),
            ("non-contacts", folder.non_contacts),
            ("groups", folder.groups),
            ("channels", folder.broadcasts),
            ("bots", folder.bots),
        )
        if on
    ]
    sections: list[str] = []
    if flags:
        rules = "rules: " + "+".join(flags)
        excl = [
            name
            for name, on in (
                ("muted", folder.exclude_muted),
                ("read", folder.exclude_read),
                ("archived", folder.exclude_archived),
            )
            if on
        ]
        if excl:
            rules += ", excl. " + "+".join(excl)
        sections.append(rules)
    counts = [
        f"{n} {label}"
        for n, label in (
            (len(folder.include_ids), "included"),
            (len(folder.pinned_ids), "pinned"),
            (len(folder.exclude_ids), "excluded"),
        )
        if n
    ]
    if counts:
        sections.append(", ".join(counts))
    return " · ".join(sections) or "empty"
