from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tests.fakes import make_chat, make_message, make_person
from tg_bridge.format import FOOTER_RESERVE, quote_query, render_hits, search_head, snippet

W = ZoneInfo("Europe/Warsaw")
ANNA = make_chat(id=123456, kind="user", title="Anna K", username="anna_k")
FAMILY = make_chat(id=-1001234567890, title="Family")
OTHER = make_chat(id=-77, kind="group", title="Chess")
person = make_person(id=123456, first_name="Anna", last_name="K", username="anna_k")


def utc(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def hint(boundary):
    return f'call search_messages(query="адреса", cursor="h{boundary.id}")'


def hits_out(hits, head="HEAD", chats=None, query="адреса", max_chars=12000, more=False):
    return render_hits(
        head, hits, chats=chats, query=query, tz=W, max_chars=max_chars, more=more, next_hint=hint
    )


def test_per_chat_output():
    hits = [
        make_message(id=4812, chat_id=123456, date=utc(25, 12, 3), out=True, sender=None, text="моя адреса Хмільна 7"),
        make_message(id=4790, chat_id=123456, date=utc(20, 7, 10), sender=person, text="скинь адресу"),
    ]
    head = search_head("адреса", ANNA, "all time", W, True)
    assert head == 'Search "адреса" in Anna K (123456) · all time (Europe/Warsaw)\nFrom me only'
    out = hits_out(hits, head=head, more=True)
    assert out.splitlines()[2:] == [
        "#4812 2026-10-25 13:03 Me: моя адреса Хмільна 7",
        "#4790 2026-10-20 09:10 Anna K (@anna_k): скинь адресу",
        "Shown 2 hits (2026-10-25 13:03 → 2026-10-20 09:10). Older hits remain. "
        'Next: call search_messages(query="адреса", cursor="h4790")',
        'Context: call get_messages(chat="<id>", around=<#id>).',
    ]


def test_head_links_for_supergroup_and_global():
    assert search_head("x", FAMILY, "all time", W, False).splitlines()[-1] == "Links: https://t.me/c/1234567890/<id>"
    assert search_head("x", None, "last 7 days", W, False) == 'Search "x" in all chats · last 7 days (Europe/Warsaw)'


def test_global_grouped_by_chat_newest_first_with_unknown_chat():
    hits = [
        make_message(id=5, chat_id=FAMILY.id, date=utc(25, 12), text="адреса раз"),
        make_message(id=9, chat_id=OTHER.id, date=utc(24, 12), text="адреса два"),
        make_message(id=4, chat_id=FAMILY.id, date=utc(23, 12), text="адреса три"),
        make_message(id=1, chat_id=555, date=utc(22, 12), text="адреса чотири"),
    ]
    out = hits_out(hits, chats={FAMILY.id: FAMILY, OTHER.id: OTHER})
    lines = out.splitlines()
    assert lines[1:] == [
        "— Family (-1001234567890) · Links: https://t.me/c/1234567890/<id> —",
        "#5 2026-10-25 13:00 Anna K (@anna_k): адреса раз",
        "#4 2026-10-23 14:00 Anna K: адреса три",
        "— Chess (-77) —",
        "#9 2026-10-24 14:00 Anna K: адреса два",
        "— chat 555 —",
        "#1 2026-10-22 14:00 Anna K: адреса чотири",
        "Shown 4 hits (2026-10-25 13:00 → 2026-10-22 14:00). No more hits.",
        'Context: call get_messages(chat="<id>", around=<#id>).',
    ]


def test_snippet_centres_on_late_match():
    text = "а" * 1000 + " адреса " + "б" * 1000
    s = snippet(text, "адреса")
    assert "адреса" in s and s.startswith("…") and s.endswith("…") and len(s) <= 302


def test_snippet_short_text_whole_and_no_match_takes_head():
    assert snippet("коротко", "x") == "коротко"
    s = snippet("я" * 1000, "zzz")
    assert s == "я" * 300 + "…"


def test_snippet_match_near_start_has_no_leading_ellipsis():
    s = snippet("адреса " + "б" * 1000, "адреса")
    assert s.startswith("адреса") and s.endswith("…")


def test_query_with_quotes_is_quoted_once():
    q = 'say "hi"\nnow'
    head = search_head(q, ANNA, "all time", W, False)
    assert len(head.splitlines()) == 1
    assert '"say \\"hi\\" ⏎ now"' in head
    assert quote_query(q) == '"say \\"hi\\" ⏎ now"'
    hits = [make_message(id=1, chat_id=123456, date=utc(25, 12), text="say hi")]
    out = render_hits(
        head, hits, chats=None, query=q, tz=W, max_chars=5000, more=True,
        next_hint=lambda b: f"call search_messages(query={quote_query(q)})",
    )
    assert len(out.splitlines()) == 1 + 1 + 1 + 1  # head, hit, footer, context
    assert quote_query(q) in out


def test_budget_keeps_newest_hits():
    hits = [make_message(id=1000 - i, chat_id=123456, date=utc(25, 12, 0), text="адреса " + "x" * 40) for i in range(60)]
    out = hits_out(hits, max_chars=2000)
    assert len(out) <= 2000
    assert "#1000 " in out and "#941 " not in out
    assert "Next: call search_messages" in out


def test_single_huge_hit_is_cut():
    hits = [make_message(id=1, chat_id=123456, date=utc(25, 12), text="а" * 5000, media=None)]
    out = hits_out(hits, head="H" * 100, max_chars=250, more=True)
    assert len(out) <= 250 and "#1 " in out


def test_longest_footer_fits_reserve():
    q = "ж" * 60
    lines = [
        f"Shown 100 hits (2026-10-25 13:00 → 2026-10-22 14:00). Older hits remain. Next: call "
        f"search_messages(query={quote_query(q)}, chat=\"-1001234567890\", cursor=\"g4790_d1792000000_n1001234567890_s1792000000_e1792000000_m_q417\")",
        'Context: call get_messages(chat="<id>", around=<#id>).',
    ]
    assert len("\n".join(lines)) <= FOOTER_RESERVE


def test_empty_hint_suggests_variants():
    out = hits_out([], head="HEAD")
    assert out.startswith("HEAD\nNo hits.") and "word forms" in out and "Next" not in out
