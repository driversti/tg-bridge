import stat

from tg_bridge.login import prepare_session_dir, secure_session_file


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_prepare_creates_dir_700(tmp_path):
    session = tmp_path / "cfg" / "tg-bridge" / "x.session"

    prepare_session_dir(session)

    assert mode(session.parent) == 0o700


def test_prepare_leaves_existing_dir_and_warns(tmp_path, caplog):
    # The parent may be any dir the user chose (even ~), so we never chmod what we didn't create.
    folder = tmp_path / "shared"
    folder.mkdir()
    folder.chmod(0o755)

    prepare_session_dir(folder / "x.session")

    assert mode(folder) == 0o755
    assert any("chmod 700" in r.getMessage() for r in caplog.records)


def test_secure_session_file_sets_600(tmp_path):
    session = tmp_path / "x.session"
    journal = tmp_path / "x.session-journal"
    for f in (session, journal):
        f.touch()
        f.chmod(0o644)

    secure_session_file(session)

    assert mode(session) == 0o600
    assert mode(journal) == 0o600


def test_secure_handles_missing_journal(tmp_path):
    session = tmp_path / "x.session"
    session.touch()
    session.chmod(0o644)

    secure_session_file(session)

    assert mode(session) == 0o600


def test_ctrl_c_prints_cancelled_without_traceback(monkeypatch, tmp_path, capsys):
    import pytest

    from tg_bridge import login

    async def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_BRIDGE_SESSION", str(tmp_path / "s" / "x.session"))
    monkeypatch.setattr(login, "interactive_login", interrupted)

    with pytest.raises(SystemExit) as exc:
        login.main(["--method", "code"])

    assert exc.value.code == 130
    assert "Login cancelled" in capsys.readouterr().err


def test_login_error_exits_1_with_message(monkeypatch, tmp_path, capsys):
    import pytest

    from tg_bridge import login
    from tg_bridge.errors import RateLimitedError

    async def flooded(*args, **kwargs):
        raise RateLimitedError(300)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_BRIDGE_SESSION", str(tmp_path / "s" / "x.session"))
    monkeypatch.setattr(login, "interactive_login", flooded)

    with pytest.raises(SystemExit) as exc:
        login.main(["--method", "code"])

    assert exc.value.code == 1
    assert "retry in 300 s" in capsys.readouterr().err


def test_choose_method_defaults_to_qr():
    from tg_bridge.login import choose_method

    answers = iter(["nonsense", ""])

    assert choose_method(lambda prompt: next(answers), print) == "qr"


def test_choose_method_accepts_number_and_name():
    from tg_bridge.login import choose_method

    assert choose_method(lambda prompt: "2", print) == "code"
    assert choose_method(lambda prompt: " QR ", print) == "qr"


def test_method_flag_skips_prompt(monkeypatch, tmp_path):
    import pytest

    from tg_bridge import login

    seen = {}

    async def fake_login(settings, method, show_qr):
        seen["method"] = method
        seen["show_qr"] = show_qr
        raise KeyboardInterrupt

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_BRIDGE_SESSION", str(tmp_path / "s" / "x.session"))
    monkeypatch.setattr(login, "interactive_login", fake_login)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt"))

    with pytest.raises(SystemExit):
        login.main(["--method", "qr"])

    assert seen["method"] == "qr"
    assert seen["show_qr"] is login.show_qr
