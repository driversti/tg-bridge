from datetime import UTC, datetime, timedelta

from tests.fakes import make_message
from tg_bridge.headlines import HEADLINE_CHARS, Repost, group_reposts, headline, repost_tokens, similar

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def msg(id, chat, text, minutes=0, **kw):
    return make_message(id=id, chat_id=chat, text=text, date=T0 + timedelta(minutes=minutes), **kw)


def test_headline_first_line_with_enough_words():
    assert headline("BREAKING\nVerstappen signs") == "BREAKING"


def test_headline_skips_decoration_lines():
    assert headline("🔥🔥\n— — —\nVerstappen signs") == "Verstappen signs"


def test_headline_skips_hashtags_urls_and_mentions_only_lines():
    assert headline("#F1 #news\nhttps://t.me/x/1\nNorris wins") == "Norris wins"


def test_headline_removes_urls_inside_a_line():
    assert headline("Read: https://x.com/a more") == "Read: more"


def test_headline_none_without_text():
    assert headline("") is None
    assert headline("🔥 https://t.me/x") is None


def test_headline_cut_at_a_space():
    text = " ".join(["word"] * 60)  # 299 chars
    out = headline(text)
    assert out is not None and len(out) <= HEADLINE_CHARS and out.endswith("…")
    assert out[:-1].endswith("word")


def test_headline_hard_cut_without_spaces():
    out = headline("x" * 300)
    assert out == "x" * 139 + "…" and len(out) == HEADLINE_CHARS


def test_repost_tokens():
    tokens = repost_tokens("Contracts @bob https://t.me/x/1 a Contract X")
    assert tokens == frozenset({"contra"})
    long = " ".join(f"word{i:03d}" for i in range(100))
    assert len(repost_tokens(long)) <= 60
    assert repost_tokens("ÉCOLE") == frozenset({"école"})


def test_similar_repost_pair():
    a = repost_tokens("Verstappen signs new contract with Red Bull until 2028")
    b = repost_tokens("Max Verstappen has signed a new contract with Red Bull until 2028!")
    assert similar(a, b)


def test_similar_different_news_with_shared_names():
    a = repost_tokens("Norris wins the Mexican GP ahead of Leclerc")
    b = repost_tokens("Norris signs a new contract with McLaren until 2030")
    assert not similar(a, b)


def test_similar_short_sets_need_equality():
    assert similar(frozenset({"ok"}), frozenset({"ok"}))
    assert not similar(frozenset({"ok"}), frozenset({"ok", "go"}))
    assert not similar(frozenset(), frozenset())


def test_group_three_channels_into_one():
    posts = [
        msg(1, -1, "Verstappen signs new contract with Red Bull until 2028", 0),
        msg(2, -2, "Max Verstappen has signed a new contract with Red Bull until 2028!", 30),
        msg(3, -3, "Verstappen signed new contract with Red Bull until 2028 official", 120),
    ]
    groups = group_reposts(posts)
    assert len(groups) == 1
    assert groups[0].first is posts[0]
    assert groups[0].posts == tuple(posts)


def test_group_same_text_days_later_is_a_new_group():
    text = "Verstappen signs new contract with Red Bull until 2028"
    groups = group_reposts([msg(1, -1, text, 0), msg(2, -2, text, 4 * 24 * 60)])
    assert [len(g.posts) for g in groups] == [1, 1]


def test_group_same_chat_never_merges():
    text = "Verstappen signs new contract with Red Bull until 2028"
    groups = group_reposts([msg(1, -1, text, 0), msg(2, -1, text, 10)])
    assert len(groups) == 2


def test_group_goes_to_next_matching_group_when_chat_taken():
    text = "Verstappen signs new contract with Red Bull until 2028"
    posts = [msg(1, -1, text, 0), msg(2, -1, text, 5), msg(3, -2, text, 10)]
    groups = group_reposts(posts)
    assert [[m.id for m in g.posts] for g in groups] == [[1, 3], [2]]


def test_group_output_ordered_by_first_post_and_empty():
    posts = [
        msg(1, -1, "Norris wins the Mexican GP ahead of Leclerc today", 0),
        msg(2, -2, "Verstappen signs new contract with Red Bull until 2028", 10),
    ]
    assert [g.first.id for g in group_reposts(posts)] == [1, 2]
    assert group_reposts([]) == []
    assert isinstance(group_reposts(posts)[0], Repost)


# Real pairs from the feature 08 smoke test (folder F1, 2026-10-22…28): channels rewrite the news in their own words,
# so the bodies differ and only the headlines are near-identical. Bodies here are stand-ins of the same shape.
POLE_UF = "⬆️ Кімі Антонеллі забирає поул позицію на Гран-прі США\n\nДругим став Шарль Леклер, третім Макс Ферстаппен"
POLE_DRS = "Кімі Антонеллі завойовує поул-позицію на Гран-прі США 🇺🇸🔥\n\nІталієць випередив суперників на дві десятих"
WIN_UF = "⬆️ Кімі Антонеллі перемагає на гонці Гран-прі США\n\nUkrFormula | Inst | TikTok | Shop"
WIN_DRS = (
    "Кімі Антонеллі перемагає на Гран-прі США, а разом із ним на подіумі – Шарль Леклер та Макс Ферстаппен!\n\n"
    "До чемпіонства йому лишається зовсім небагато"
)
FP3_RESULTS = "⬆️ Результати третьої практики Гран-прі США\n\nНайшвидшим був Гамільтон"
QUALI_RESULTS = "🏁 РЕЗУЛЬТАТИ КВАЛІФІКАЦІЇ ГРАН-ПРІ США 🏁\n\n1. Антонеллі 2. Леклер 3. Ферстаппен"
RACE_RESULTS = "🏁 РЕЗУЛЬТАТИ ГРАН-ПРІ США 🏁\n\n1. Антонеллі 2. Леклер 3. Ферстаппен"
GRID_F1U = "Решітка Гран-прі Пендосії США 🦅\n\nПоул у Антонеллі"
GRID_UF = "🔴 Стартова решітка, можливі стратегії та залишок гуми на гонку Гран-прі США\n\nДеталі на картинці"


def groups_of(*posts):
    return [[m.id for m in g.posts] for g in group_reposts(posts)]


def test_group_rewritten_reposts_by_headline():
    assert groups_of(msg(1, -1, POLE_UF, 0), msg(2, -2, POLE_DRS, 12)) == [[1, 2]]
    assert groups_of(msg(1, -1, WIN_UF, 0), msg(2, -2, WIN_DRS, 120)) == [[1, 2]]


def test_group_headline_match_needs_posts_close_in_time():
    assert groups_of(msg(1, -1, POLE_UF, 0), msg(2, -2, POLE_DRS, 7 * 60)) == [[1], [2]]


def test_group_headlines_sharing_only_the_event_name_stay_apart():
    # Overlaps 0.75–0.80 in the smoke data: same Grand Prix, different news.
    assert groups_of(msg(1, -1, FP3_RESULTS, 0), msg(2, -2, QUALI_RESULTS, 210)) == [[1], [2]]
    assert groups_of(msg(1, -1, GRID_F1U, 0), msg(2, -2, GRID_UF, 200)) == [[1], [2]]
    assert groups_of(msg(1, -1, RACE_RESULTS, 0), msg(2, -2, WIN_UF, 5)) == [[1], [2]]


def test_group_short_headlines_do_not_match_by_headline():
    # Fewer than 4 tokens in a headline: too little to tell news apart.
    assert groups_of(msg(1, -1, "Берман всьо\n\nзійшов з траси", 0), msg(2, -2, "Берман всьо\n\nкінець гонки", 5)) == [
        [1],
        [2],
    ]
