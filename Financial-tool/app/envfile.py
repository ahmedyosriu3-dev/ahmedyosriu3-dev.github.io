"""Reading and writing the .env file in place.

The Settings page can save credentials rather than making you hand-edit a file,
so this rewrites `.env` while preserving every comment, blank line and ordering
choice already in it -- the file stays the documented, readable thing it is,
with only the values you changed touched.

A caveat worth knowing, and surfaced in the UI: a real environment variable of
the same name outranks this file in pydantic-settings. Writing here cannot
override what the shell already exported.
"""
from __future__ import annotations

import os
import re

from app.config import BASE_DIR

ENV_PATH = BASE_DIR / ".env"

# KEY=value, tolerating leading whitespace and an `export ` prefix.
_LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")


def _strip_value(raw: str) -> str:
    """Turn the right-hand side of a KEY= line into its value.

    Mirrors python-dotenv: quotes come off, and an unquoted value ends at a
    whitespace-preceded `#`. Note that a *blank* value followed by a comment
    keeps the comment as its value in dotenv -- we do not reproduce that bug,
    but we also never write a line in that shape.
    """
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == '"':
            # Undo what _format escaped, so a value holding a quote or a
            # backslash survives a save-then-read round trip unchanged.
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return v.split(" #", 1)[0].split("\t#", 1)[0].strip()


def read_values() -> dict[str, str]:
    """Every KEY=value pair currently in .env. Missing file reads as empty."""
    out: dict[str, str] = {}
    try:
        text = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(2)] = _strip_value(m.group(4))
    return out


def _format(value: str) -> str:
    """Quote only when the value would otherwise not survive a round trip."""
    if value == "":
        return ""
    if re.search(r'[\s#"\']', value):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def write_values(updates: dict[str, str]) -> None:
    """Set each KEY to its value in .env, in place and atomically.

    Keys already present keep their position and any comment block above them;
    unknown keys are appended under a trailing section. An empty string clears
    a key rather than deleting the line, so the documentation around it stays.
    """
    if not updates:
        return

    try:
        original = ENV_PATH.read_text(encoding="utf-8", newline="")
    except OSError:
        original = ""

    # Keep whatever line ending the file already uses, so saving one key does
    # not rewrite every line of a file you may be diffing or backing up.
    eol = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    remaining = dict(updates)

    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if m and m.group(2) in remaining:
            key = m.group(2)
            lines[i] = f"{m.group(1)}{key}{m.group(3)}{_format(remaining.pop(key))}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Added from the Settings page.")
        lines.extend(f"{k}={_format(v)}" for k, v in remaining.items())

    # Write beside the target and replace, so an interrupted save cannot leave
    # a half-written .env -- which would lock you out of your own broker keys.
    tmp = ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text(eol.join(lines) + eol, encoding="utf-8", newline="")
    os.replace(tmp, ENV_PATH)


def shadowed(key: str) -> bool:
    """True when a real environment variable will outrank .env for this key."""
    return key in os.environ


def mask(value: str) -> str:
    """A value's shape, never its content. Safe to render."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * min(len(value) - 4, 20) + value[-4:]
