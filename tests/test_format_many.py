from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tests.fakes import make_chat, make_message, make_person
from tg_bridge.format_many import LABEL_CHARS, Item, chat_labels, legend_lines, render_many
from tg_bridge.headlines import Repost

W = ZoneInfo("Europe/Warsaw")
NEWS = make_chat(id=-1001234567890, kind="channel", title="F1 News", username="f1news")
UA = make_chat(id=-911, kind="group", title="Motorsport UA")
PADDOCK = make_chat(id=-7777, kind="group", title="F1 Paddock")
CHATS = {c.id: c for c in (NEWS, UA, PADDOCK)}
LABELS = chat_labels(list(CHATS.values()))
T = datetime(2026, 10, 26, 13, 3, tzinfo=UTC)  # 14:03 in Warsaw (DST ended on the 25th)


def post(id, chat, minutes=0, text="Verstappen signs a new contract", **kw):
    fields = dict(sender=None, sender_title=chat.title)
    fields.update(kw)
    return make_message(id=id, chat_id=chat.id, date=T + timedelta(minutes=minutes), text=text, **fields)


def totals(a, b):
    return f"TOTALS showing {a}–{b}"


def hint(boundary):
    return f'call get_messages(folder="5", cursor="r{boundary.id}")'


def many(items, *, labels=LABELS, offset=0, max_chars=12000, limit=100, empty="Nothing."):
    return render_many(
        "HEAD", totals, items, offset=offset, chats=CHATS, labels=labels, tz=W, max_chars=max_chars,
        limit=limit, next_hint=hint, empty_text=empty,
    )


def test_labels_cut_long_titles():
    chat = make_chat(id=-5, title="A" * 30)
    label = chat_labels([chat])[chat.id]
    assert label == "A" * (LABEL_CHARS - 1) + "…" and len(label) == LABEL_CHARS


def test_labels_keep_exact_length_title():
    chat = make_chat(id=-5, title="A" * LABEL_CHARS)
    assert chat_labels([chat])[chat.id] == "A" * LABEL_CHARS


def test_labels_disambiguate_duplicates():
    a = make_chat(id=-1001234567890, title="News")
    b = make_chat(id=-1009876543123, title="News")
    c = make_chat(id=-3, title="Other")
    assert chat_labels([a, b, c]) == {a.id: "News ~890", b.id: "News ~123", c.id: "Other"}


def test_labels_strip_line_breaks():
    chat = make_chat(id=-5, title="A\nB")
    assert "\n" not in chat_labels([chat])[chat.id]


def test_full_line_channel_post():
    out = many([post(4812, NEWS, text="text")])
    assert "#4812 14:03 F1 News: text" in out.splitlines()


def test_full_line_group_message():
    anna = make_message(
        id=7, chat_id=PADDOCK.id, date=T - timedelta(hours=5), text="hi", sender=make_person(id=42, username="anna_k")
    )
    out = many([anna])
    assert "#7 09:03 F1 Paddock › Anna K (@anna_k): hi" in out.splitlines()


def test_full_line_cut_title_still_counts_as_the_chat():
    chat = make_chat(id=-5, kind="channel", title="B" * 30)
    labels = chat_labels([chat])
    msg = make_message(id=1, chat_id=-5, sender=None, sender_title=chat.title, text="t", date=T)
    out = render_many(
        "HEAD", totals, [msg], offset=0, chats={-5: chat}, labels=labels, tz=W, max_chars=5000, limit=10,
        next_hint=hint, empty_text="-",
    )
    assert f"#1 14:03 {labels[-5]}: t" in out.splitlines()


def test_headline_line_single_and_repost():
    single = Repost((post(4812, NEWS, text="Norris wins"),))
    group = Repost((post(4812, NEWS), post(911, UA, 5), post(77, PADDOCK, 9)))
    lines = many([single, group]).splitlines()
    assert "14:03 F1 News #4812: Norris wins" in lines
    assert "14:03 ×3 F1 News #4812, Motorsport UA #911, F1 Paddock #77: Verstappen signs a new contract" in lines


def test_headline_line_single_chat_has_no_label_or_legend():
    items = [Repost((post(4812, NEWS, text="Norris wins"),))]
    out = many(items, labels=None)
    assert out.splitlines() == [
        "HEAD",
        "TOTALS showing 1–1",
        "— 2026-10-26 Mon —",
        "14:03 #4812: Norris wins",
        "End of period.",
    ]


def test_legend_only_for_chats_on_the_page_with_links_only_where_available():
    out = many([post(1, NEWS), post(2, PADDOCK, 1)], labels=LABELS)
    lines = out.splitlines()
    assert lines[2] == "F1 News = -1001234567890 · Links: https://t.me/f1news/<id>"
    assert lines[3] == "F1 Paddock = -7777"
    assert not any(line.startswith("Motorsport UA =") for line in lines)


def test_legend_lines_function():
    assert legend_lines([UA.id, NEWS.id], CHATS, LABELS) == [
        "Motorsport UA = -911",
        "F1 News = -1001234567890 · Links: https://t.me/f1news/<id>",
    ]
    assert legend_lines([55], CHATS, LABELS) == ["55 = 55"]


def test_day_headers():
    out = many([post(1, NEWS), post(2, NEWS, 24 * 60)])
    lines = out.splitlines()
    assert "— 2026-10-26 Mon —" in lines and "— 2026-10-27 Tue —" in lines


def test_everything_fits_ends_the_period():
    out = many([post(1, NEWS)])
    assert out.splitlines()[-1] == "End of period."
    assert "TOTALS showing 1–1" in out


def test_budget_shrinks_page_and_says_how_much_is_shown():
    items: list[Item] = [post(i, NEWS, i, text=f"message number {i}") for i in range(1, 301)]
    out = many(items, max_chars=2000)
    assert len(out) <= 2000
    last = out.splitlines()[-1]
    assert last.startswith("More remain. Next: call get_messages(folder=\"5\", cursor=\"r")
    shown = sum(1 for line in out.splitlines() if line.startswith("#"))
    assert 0 < shown < 300
    assert f"TOTALS showing 1–{shown}" in out
    assert last.endswith(f'r{shown}")')  # the boundary is the last shown item


def test_limit_caps_the_page():
    items = [post(i, NEWS, i) for i in range(1, 11)]
    out = many(items, limit=4)
    assert sum(1 for line in out.splitlines() if line.startswith("#")) == 4
    assert "showing 1–4" in out and "More remain." in out


def test_offset_shifts_showing_range():
    out = many([post(1, NEWS), post(2, NEWS, 1)], offset=88)
    assert "showing 89–90" in out


def test_repost_boundary_is_the_first_post():
    group = Repost((post(4812, NEWS), post(911, UA, 5)))
    out = many([group, post(5, NEWS, 60)], limit=1)
    assert out.splitlines()[-1].endswith('r4812")')


def test_empty():
    assert many([], empty="No messages in this period.") == "HEAD\nNo messages in this period."


def test_one_huge_item_is_cut_but_shown():
    huge = post(1, NEWS, text="word " * 3000)
    out = many([huge, post(2, NEWS, 5)], max_chars=1500)
    assert len(out) <= 1500
    assert "#1 14:03 F1 News: word" in out
    assert "More remain." in out
