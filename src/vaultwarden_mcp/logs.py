"""One-line JSON logs on stderr (stdout carries the MCP protocol).

MCP clients keep the server's stderr in their logs, which gives an audit trail of unlocks
and of every secret written: which item, which field, where, why. Never the value.
"""

import json
from collections.abc import Callable
from datetime import datetime
from typing import TextIO

type LogValue = str | int | bool | None


class Log:
    """Writes `{"ts": ..., "event": ..., **fields}` lines to a stream."""

    def __init__(self, stream: TextIO, now: Callable[[], datetime]) -> None:
        """Log to `stream`, timestamps from `now` (aware datetimes)."""
        self._stream = stream
        self._now = now

    def event(self, event: str, **fields: LogValue) -> None:
        """Write one event; field names are stable, values never include secrets."""
        line = {"ts": self._now().isoformat(), "event": event, **fields}
        self._stream.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._stream.flush()
