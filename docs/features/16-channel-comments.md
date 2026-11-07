# 16 — Channel post comments

**Design refs:** §7 (`get_comments`), §6 (read-only) · **Depends on:** 02

## Goal
Read the comment thread under a channel post, the same way the Telegram client does: **without** being a member of the channel's linked discussion group. Today the bridge only sees the posts; the comments live in a separate discussion supergroup that is not in the user's dialogs, so they are invisible.

Motivating case: collect what people built with AI from the comments under the weekly "НЕДІЛЬНИЙ AI ЧЕКІН" posts of `@imatrofAI` (`t.me/imatrofAI/3186`), ~60 threads since 2025.

## Scope
- `get_comments(chat, post, cursor?, limit?)`: the comment thread of that post, in time order, in the usual compact line format (author, time, text, `↩#id` for replies inside the thread). `chat` is the channel; `post` is the post `#id` seen in `get_messages`/`search_messages`. (First built as `get_messages(comments_of=…)`; the smoke test showed a ~26B model never found that parameter, so it became its own tool.)
- Channel post lines in `get_messages`/`search_messages` end in `💬N` when the post has N comments (Telegram already sends the count), and each such page names one ready `get_comments(chat=…, post=…)` call.
- Under the hood: MTProto `messages.GetReplies` on the channel peer (Telethon: `iter_messages(channel, reply_to=<post id>)`). Read-only; it needs no join, no read receipt.
- Pagination through the existing short `cursor` token; the footer keeps the "…remain… Next: call get_comments(…)" wording (see Log 02).
- Each line links to the comment: `https://t.me/<channel>/<post id>?comment=<comment id>`, and names the author with a profile link (`Name (https://t.me/<username>)`, or `(id N)` without a username) on every line, so the model can pair "who" with "what they built". The header says once which post the thread belongs to (first line of the post, trimmed, post link) and the total comment count.
- Comment text is returned whole (not cut at 1500 chars); the bridge returns raw comments, summarising is the model's job.
- The cursor carries the post id, so a thread can be continued from a cursor alone, also in a new dialog.
- `period`/`days`/`since`/`until` work as filters inside the thread; `from_me`, `topic`, `around`, `folder` and `mode` are not parameters of `get_comments`.
- Clear errors (no stack traces) for: channel without comments, post without replies (an empty thread is a normal result, "0 comments"), non-channel `chat`, unknown post id.
- Tool description tells the model: to analyse a recurring series (e.g. weekly check-ins), first list the posts with `search_messages(query=<hashtag>, chat=<channel>)`, then call `get_comments` once per post.

## Out of scope
- Joining the discussion group, or reading it as a normal chat.
- Searching inside comments across many posts (a later feature, if needed).
- Reactions, view counts, anything that writes.
- `download_media` for comment attachments (their ids live in the discussion group).
- Notes/progress tracking across dialogs: the model's job (e.g. a filesystem MCP server + system prompt), not the bridge's (decided 2026-11-07).

## Done when
- Logic tested with FakeSource: pagination, empty thread, reply markers, link format, period filter, error cases. No real account in tests.
- Only `tg_bridge/telegram/` touches Telethon; the new source method returns a bridge-level model.
- Logs (stderr) never contain comment text, author names or phones.
- Docs updated: tool description, `design.md` §7 tool contract, roadmap row.

## Smoke checklist (manual, real account)
- [ ] The model calls `get_comments` on its own for the check-in prompt (no hint from the user).
- [ ] `get_comments(chat="@imatrofAI", post=3186)` returns comments although the user is **not** a member of the discussion group.
- [ ] Paging a thread with 100+ comments: no duplicates or gaps across pages.
- [ ] A post with comments disabled, and one with zero comments, give clear one-line answers.
- [ ] Links `…/3186?comment=<id>` open the right comment in Telegram.
- [ ] Author links open the right profile; authors without a username show `(id N)`.
- [ ] A reply inside the thread shows `↩#<id>` pointing at the right comment.
- [ ] After a first `FloodWait`-prone burst (10 threads in a row) the tool still answers.
