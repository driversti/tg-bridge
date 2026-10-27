"""stderr-only logging: stdout belongs to the MCP stdio transport."""

import logging
import sys


def configure_logging(level: int = logging.INFO) -> None:
    """Must run before MCPServer is constructed: its basicConfig is a no-op once we have a handler."""
    root = logging.getLogger()
    root.handlers[:] = []
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("telethon").setLevel(logging.WARNING)
