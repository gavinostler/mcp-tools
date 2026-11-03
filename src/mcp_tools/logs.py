"""Reading the end of `rojo serve`'s and Roblox Studio's log files (the rojo_logs and studio_logs
tools). Plain file reading, so unlike the rest of mcp-tools this works on any OS."""
import datetime as dt
import re
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Studio: "2026-11-03T12:34:56.789Z,123.456789,1a2b,6 [FLog::Output] message". The fields after the
# time (uptime, thread, level) are dropped to save tokens.
STUDIO_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d))(?:\.\d+)?Z?,\S* ")
MAX_BYTES = 16 << 20  # Studio logs grow to tens of MB; only the end matters
MAX_LINE = 1000


def read_lines(path: Path, max_bytes: int = MAX_BYTES) -> list[str]:
    """The non-blank lines in the last `max_bytes` of `path`, without terminal colour codes."""
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = ANSI.sub("", data.decode("utf-8", errors="replace")).splitlines()
    if size > max_bytes:
        lines = lines[1:]  # cut mid-line
    return [l[:MAX_LINE] + (" ..." if len(l) > MAX_LINE else "") for l in lines if l.strip()]


def studio_log_files(logs_dir: Path) -> list[Path]:
    return [p for p in logs_dir.glob("*.log") if "Studio" in p.name and "Installer" not in p.name]


def newest_studio_log(logs_dir: Path) -> Path | None:
    return max(studio_log_files(logs_dir), key=lambda p: p.stat().st_mtime, default=None)


def studio_lines(path: Path, pattern: re.Pattern | None, since: dt.datetime | None) -> list[str]:
    """Lines of a Studio log matching `pattern`, written at or after `since` (UTC). A line with no
    timestamp of its own counts as written when the line before it was."""
    out, t = [], None
    for line in read_lines(path):
        m = STUDIO_LINE.match(line)
        if m:
            t = dt.datetime.fromisoformat(m[1]).replace(tzinfo=dt.timezone.utc)
            line = f"{m[2]}Z {line[m.end():]}"
        if since and (t is None or t < since):
            continue
        if pattern and not pattern.search(line):
            continue
        out.append(line)
    return out
