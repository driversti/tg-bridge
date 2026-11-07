# 17 — Cloud scope: public chats + an allowlist page

**Design refs:** §6 (architecture, `MessageSource`), §7 (tool contract), §8 (security) · **Depends on:** 01, 03, 08 · **Blocks:** 11 going live

## Goal
Let a **cloud model** read Telegram through the bridge, seeing public chats always and private ones only where the owner said yes. Motivating setup (decided 2026-11-07): an always-on agent (Hermes, LXC 110) with a cloud model talks to the owner from the phone and reads Telegram through a networked bridge (feature 11) in its own LXC, with its own session. Everything private stays readable by the local model in LM Studio, which opts in with `scope=all`.

The rule is enforced **in the bridge, in code**, never by a prompt: a chat outside the scope does not exist for the model.

## Scope
- Setting `TG_BRIDGE_SCOPE=all|public`, default **`public`** (owner's call, 2026-11-07: a forgotten setting must never open DMs). One process = one scope. An unknown value refuses to start. `all` is opt-in: the LM Studio entry in `mcp.json` sets it explicitly, and upgrading without it means LM Studio loses DMs until it is set (documented in `setup.md`).
- Under `public` a chat is visible when it is:
  - a channel or supergroup with a public `@username` (anyone can find and read it anyway), or
  - any private chat the owner switched on in the allowlist: a basic group, a supergroup/channel without a username, a 1:1 chat with a person or a bot, or Saved Messages.
- Everything private starts **off**. (Revised after the first smoke, 2026-11-07, owner's call: first DMs were never allowed; now one rule covers all kinds, "public always, private only with permission". A person's `@username` never makes a 1:1 chat public.)
- **Fail closed:** a chat whose kind or username is unknown counts as private.
- Publicity is checked on every call, not when the chat was added: if a group loses its username, it becomes private and closes until switched on.
- Allowlist keys are chat ids, so renames don't matter. A basic group upgraded to a supergroup gets a new id and therefore closes again (safe; the owner re-enables it).
- The filter sits at the source layer (a scoped wrapper around `MessageSource`), so every tool inherits it. Places that need explicit care, each with tests:
  - chat resolution: hidden chats are never candidates, and asking for one gives the same answer as a chat that does not exist (no "exists but forbidden");
  - `list_chats` (also `folder=`, `min_folders=`), `list_folders` counts (only visible chats);
  - `search_messages` global and `folder=` hits (incl. `from_me`), `get_messages(folder=)` and headlines;
  - `get_chat_info`, `get_messages`, `get_comments`, `export_chat`, `download_media`, `find_deleted_users`/`export_deleted_users` (feature 05, not built yet: they must see only visible chats).
- Comments under a public channel stay readable even when its discussion group is private (anyone can read them via the channel).
- Tool descriptions (or results) say that private chats are out of reach in this mode, so the model doesn't keep retrying.

### Allowlist page
- Run as its own command, `tg-bridge-admin`: LM Studio starts a fresh stdio process per chat message, so the page can't live inside it. It talks to Telegram only for a few seconds at start and on "Refresh" (dialog list + small chat photos), then lets go of the session, so it doesn't hold the session lock while LM Studio works (see Log 16). When feature 11 brings a long-running networked process, the page moves into it, still on its own listener and **not** reachable with the MCP token.
- Lists the owner's private chats (incl. archived) in tabs: **Groups** (basic groups, private supergroups/forums), **Channels** (private channels), **People** (1:1 chats incl. bots and deleted accounts), **Mine** (groups and channels the owner created, plus Saved Messages; own public ones shown as "always on" without a switch). A chat can sit in two tabs; the switch is the same. Each row: icon, title, kind, `t.me/c/<id>` link with a copy button where one exists (basic groups and people have none; people with a username get `t.me/<username>`), on/off switch. Switching on a person, a bot or Saved Messages asks for confirmation, and switched-on 1:1 chats are marked. Search by title; filter by folder (folders are only a filter, never the access rule: the owner sorts folders by topic, mixing trusted and private chats).
- New private chats start **off**. Changes apply on the next tool call, no restart.
- Owner-only: listens on `127.0.0.1` only. At start it prints a one-time login link with a random token to the terminal; opening it sets an HttpOnly, SameSite=Strict cookie, and every other request needs that cookie. Remote access (phone) goes through `tailscale serve`, so only devices in the owner's tailnet reach it, and they still need the link. Switches are POSTs with a CSRF token. Chat titles are HTML-escaped (they are attacker-controlled text).
- The allowlist lives in the bridge's config dir (dir `700`, file `600`) and is written only by the page; the owner never edits it by hand.
- Icons are small profile photo downloads (read-only), cached on disk in the config dir (dir `700`) and re-downloaded only when the photo changed, so Refresh takes seconds. The Refresh button locks while it runs.

## Out of scope
- Member counts (one extra request per chat; dropped by the owner, 2026-11-07).
- Creating invite links (a write action; private chats get `t.me/c/<id>` only).
- A Telegram admin bot (the owner has too many bots already).
- Using Telegram folders, mute, archive or pins as the access rule.
- A read-only "what the cloud sees" view of public chats: maybe later.
- The network MCP transport and its auth (feature 11).

## Done when
- Filter logic tested with FakeSource for every kind (each off and on): user (also with a username), bot, self, basic group, private/public supergroup, private/public channel, unknown kind; allowlist on/off; username lost; migrated group.
- Every tool in the list above has a test showing a hidden chat stays hidden (incl. global search hits and ambiguous-name candidates).
- Page: tests for the owner check (wrong/missing login → refused), CSRF, toggle persists and takes effect without restart.
- `scope=all` behaves exactly as before (existing suite green, run with `all`); no setting → `public`; unknown value → refuses to start.
- Logs never contain chat titles, names or message text; only ids and counts.
- Docs updated: `design.md` §7 (scope note), §8 (scope + page), `setup.md` (settings, `TG_BRIDGE_SCOPE=all` in the LM Studio `mcp.json` example, `tailscale serve`), roadmap row.

## Smoke checklist (manual, real account)
- [x] Under `public`, "what did X write to me yesterday?" (a 1:1 chat, off) → the model can't find the chat; no name leaks in candidates.
- [ ] Global search for a word that appears in a DM and in a public channel → only the channel hit.
- [ ] A private group that is off → invisible; switch it on in the page → visible on the next call; off again → gone.
- [ ] The page lists the same private groups/channels as the app; icons and `t.me/c/…` links open the right chat.
- [ ] Opening the page without the login link (fresh browser, or another device) → refused; with the link → works, also from the phone via `tailscale serve`.
- [ ] Comments of a public channel with a private discussion group are readable.
- [ ] `scope=all` in LM Studio: everything as before.
- [x] LM Studio entry without `TG_BRIDGE_SCOPE`: DMs are out of reach (default `public`).
- [ ] Switching on one person in the People tab asks for confirmation; the model then reads only that DM, no other.
- [ ] Mine tab lists the groups/channels the owner created and Saved Messages.
- [ ] Second Refresh takes a few seconds (photos come from the disk cache).
