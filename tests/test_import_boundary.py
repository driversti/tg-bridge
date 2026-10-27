"""Read-only by construction: Telethon may only be used behind tg_bridge/telegram/."""

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "tg_bridge"
ALLOWED = PACKAGE_ROOT / "telegram"


def find_telethon_imports(root: Path) -> list[Path]:
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            if any(m.split(".")[0] == "telethon" for m in modules):
                offenders.append(path)
                break
    return offenders


def test_telethon_only_imported_in_telegram_package():
    offenders = [p for p in find_telethon_imports(PACKAGE_ROOT) if not p.is_relative_to(ALLOWED)]
    assert offenders == []


def test_boundary_detects_violation(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("from telethon import TelegramClient\n")
    (tmp_path / "ok.py").write_text("import telethonish\n")

    assert find_telethon_imports(tmp_path) == [bad]
