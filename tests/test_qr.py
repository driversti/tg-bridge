from tg_bridge.qr import render_qr


def test_render_qr_is_a_square_block():
    lines = render_qr("tg://login?token=abc").splitlines()

    assert len(lines) > 10
    assert len({len(line) for line in lines}) == 1


def test_render_qr_is_deterministic():
    assert render_qr("tg://login?token=abc") == render_qr("tg://login?token=abc")
    assert render_qr("tg://login?token=abc") != render_qr("tg://login?token=xyz")
