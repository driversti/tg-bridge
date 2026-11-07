# 17 — Cloud scope: public chats + an allowlist page

**Design refs:** §6 (architecture, `MessageSource`), §7 (tool contract), §8 (security) · **Depends on:** 01, 03, 08 · **Blocks:** 11 going live

## Goal
Let a **cloud model** read Telegram through the bridge without ever seeing private correspondence. Motivating setup (decided 2026-11-07): an always-on agent (Hermes, LXC 110) with a cloud model talks to the owner from the phone and reads Telegram through a networked bridge (feature 11) in its own LXC, with its own session. Private chats stay readable only by the local model in LM Studio, which opts in with `scope=all`.

The rule is enforced **in the bridge, in code**, never by a prompt: a chat outside the scope does not exist for the model.

## Scope
- Setting `TG_BRIDGE_SCOPE=all|public`, default **`public`** (owner's call, 2026-11-07: a forgotten setting must never open DMs). One process = one scope. An unknown value refuses to start. `all` is opt-in: the LM Studio entry in `mcp.json` sets it explicitly, and upgrading without it means LM Studio loses DMs until it is set (documented in `setup.md`).
- Under `public` a chat is visible when it is:
  - a channel or supergroup with a public `@username` (anyone can find and read it anyway), or
  - a private group or channel (basic group, or supergroup/channel without a username) the owner switched on in the allowlist.
- **Never visible**, whatever the allowlist says: 1:1 chats with people, bots, Saved Messages.
- **Fail closed:** a chat whose kind or username is unknown counts as private.
- Publicity is checked on every call, not when the chat was added: if a group loses its username, it becomes private and closes until switched on.
- Allowlist keys are chat ids, so renames don't matter. A basic group upgraded to a supergroup gets a new id and therefore closes again (safe; the owner re-enables it).
- The filter sits at the source layer (a scoped wrapper around `MessageSource`), so every tool inherits it. Places that need explicit care, each with tests:
  - chat resolution: hidden chats are never candidates, and asking for one gives the same answer as a chat that does not exist (no "exists but forbidden");
  - `list_chats` (also `folder=`, `min_folders=`), `list_folders` counts (only visible chats);
  - `search_messages` global and `folder=` hits (incl. `from_me`), `get_messages(folder=)` and headlines;
  - `get_chat_info`, `get_messages`, `get_comments`, `export_chat`, `download_media`, `find_deleted_users`/`export_deleted_users` (off entirely under `public`).
- Comments under a public channel stay readable even when its discussion group is private (anyone can read them via the channel).
- Tool descriptions (or results) say that private chats are out of reach in this mode, so the model doesn't keep retrying.

### Allowlist page
- A small web page served by **the same process** (one session file = one process, see Log 16), on its own listener, **not** reachable with the MCP token.
- Lists only the private groups and channels the owner is in (incl. archived): icon, title, kind (group/supergroup/channel/forum), `t.me/c/<id>` link with a copy button, on/off switch. Search by title; filter by folder (folders are only a filter, never the access rule: the owner sorts folders by topic, mixing trusted and private chats).
- New private chats start **off**. Changes apply on the next tool call, no restart.
- Owner-only: listens on localhost, reached through `tailscale serve`, which passes the viewer's tailnet login; the page accepts only the configured owner login. Switches are POSTs with CSRF protection. The plan session confirms the details.
- The allowlist lives in the bridge's config dir (dir `700`, file `600`) and is written only by the page; the owner never edits it by hand.
- Icons are profile photo downloads (read-only), cached on disk.

## Out of scope
- Member counts (one extra request per chat; dropped by the owner, 2026-11-07).
- Creating invite links (a write action; private chats get `t.me/c/<id>` only).
- A Telegram admin bot (the owner has too many bots already).
- Using Telegram folders, mute, archive or pins as the access rule.
- A read-only "what the cloud sees" view of public chats: maybe later.
- The network MCP transport and its auth (feature 11).

## Done when
- Filter logic tested with FakeSource for every kind: user, bot, self, basic group, private/public supergroup, private/public channel, unknown kind; allowlist on/off; username lost; migrated group.
- Every tool in the list above has a test showing a hidden chat stays hidden (incl. global search hits and ambiguous-name candidates).
- Page: tests for the owner check (wrong/missing login → refused), CSRF, toggle persists and takes effect without restart.
- `scope=all` behaves exactly as before (existing suite green, run with `all`); no setting → `public`; unknown value → refuses to start.
- Logs never contain chat titles, names or message text; only ids and counts.
- Docs updated: `design.md` §7 (scope note), §8 (scope + page), `setup.md` (settings, `TG_BRIDGE_SCOPE=all` in the LM Studio `mcp.json` example, `tailscale serve`), roadmap row.

## Smoke checklist (manual, real account)
- [ ] Under `public`, "what did X write to me yesterday?" (a 1:1 chat) → the model can't find the chat; no name leaks in candidates.
- [ ] Global search for a word that appears in a DM and in a public channel → only the channel hit.
- [ ] A private group that is off → invisible; switch it on in the page → visible on the next call; off again → gone.
- [ ] The page lists the same private groups/channels as the app; icons and `t.me/c/…` links open the right chat.
- [ ] Opening the page from a device signed in to another tailnet account (or from Hermes's LXC) → refused.
- [ ] Comments of a public channel with a private discussion group are readable.
- [ ] `scope=all` in LM Studio: everything as before.
- [ ] LM Studio entry without `TG_BRIDGE_SCOPE`: DMs are out of reach (default `public`).
