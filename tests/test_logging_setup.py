import logging

import pytest

from tg_bridge.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_logs_go_to_stderr(capsys):
    configure_logging()

    logging.getLogger("tg_bridge.test").info("hello from test")

    captured = capsys.readouterr()
    assert "hello from test" in captured.err
    assert captured.out == ""


def test_telethon_logger_is_quiet():
    configure_logging()

    assert logging.getLogger("telethon").level == logging.WARNING
