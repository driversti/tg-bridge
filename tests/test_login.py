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
