# 07 — Folders

**Design refs:** §6 (models), §7 (chat arguments, output budget) · **Depends on:** 01

## Goal
The model knows the user's chat folders and can work "per folder": list them, list the chats in one, and see which folders a chat belongs to. A chat may be in several folders at once.
*"What's new in my WAR folder since yesterday?"*, *"Which folders is chat X in?"*

## Background (Telegram API)
- Folders = `messages.GetDialogFilters` (read-only). Kinds: `DialogFilter` (custom), `DialogFilterChatlist` (shared folder via invite link), `DialogFilterDefault` ("All Chats", skip it).
- Membership is **explicit + rule-based**:
  - explicit: `pinned_peers` + `include_peers`, minus `exclude_peers`;
  - rules: `contacts`, `non_contacts`, `groups`, `broadcasts`, `bots`, filtered by `exclude_muted`, `exclude_read`, `exclude_archived`.
- So a folder like "Unread" (rules only, `exclude_read`) has **no stored list**: membership is computed against the current dialog state and changes as the user reads.
- Newer API layers store the title as text with entities (custom emoji). Render plain text and drop custom emoji.

## Scope
- Domain model `Folder`: `id`, `title`, `emoticon`, `kind` (`custom|shared`), rule flags, explicit include/exclude/pinned ids.
- `list_folders()`: id, title, kind, chat count, unread chat count, short rule summary (e.g. `rules: groups+channels, excl. muted`). Folder order as in the app.
- `list_chats(folder=…)`: chats in a folder (accepts folder id or title fragment, ambiguous → candidates), in the folder's order (pinned first).
- `get_chat_info(chat)`: add `folders: [titles]`.
- Membership resolution is one pure function `(folder, dialog) → bool`, unit-tested for every rule flag and the include/exclude precedence. It reuses the dialog cache from 01.
- Folders are cached together with the dialog cache (same TTL).

## Out of scope (maybe later)
- Reading or searching **across all chats of a folder** in one call (e.g. `search_messages(folder=…)`). For now the model loops over `list_chats(folder=…)`. If smoke tests show that's too clumsy, open a follow-up feature.
- Changing folders (read-only).

## Done when
- Membership function tested against fakes for: explicit include, exclude beats rule, pinned, each rule flag, `exclude_read`, `exclude_archived`, `exclude_muted`.
- From LM Studio: "list my folders" and "show chats in folder WAR" work.

## Smoke checklist (manual, real account)
- [ ] `list_folders` returns the same folders, in the same order, as the app's sidebar.
- [ ] Chat count of an explicit folder matches the app.
- [ ] A chat that sits in two folders lists both in `get_chat_info`.
- [ ] The rule-based "Unread" folder lists the currently unread chats, and its count shrinks within 5 min (dialog cache TTL) after reading one in the app.
- [ ] "Which chats are in two or more folders?" → the model calls `list_chats(min_folders=2)` (not one call per chat), and the folders shown match the app.
- [ ] A shared folder (if any) is listed with `kind=shared`.
- [ ] A chat muted only through the global notification defaults is left out of an `exclude_muted` folder, as in the app.
