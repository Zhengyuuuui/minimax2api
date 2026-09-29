"""Configuration from the environment.

The console edits every tunable setting at runtime and stores them in SQLite,
which is the right home for values an operator changes while watching the pool —
a routing strategy, a cooldown.  It is the wrong home for two kinds of value:

- **secrets** — a mail passkey or a proxy password sits in the database in clear
  text and is echoed back by ``/admin/api/settings``.  A deployment wants those
  supplied by its orchestrator, not typed into a web page.
- **deployment facts** — the data directory, the listen address, which proxy to
  reach the internet through.  These belong to the environment the process runs
  in, not to a row a different deployment shares.

So: environment wins, always, and this module is how.  A value present in the
environment is written into the settings object after every load and after every
console edit, so the two can never disagree and a console edit to an
env-controlled field is visible as a no-op rather than a silent override.

The spelling is ``MINIMAX2API_<SECTION>__<FIELD>`` — double underscore between
section and field, because field names never contain one and section names never
do either, while single underscores are all over both (``mail_pass``,
``base_url_cn``).  Parsing on a single underscore is how ``MAIL_PASS`` and a
hypothetical ``MAIL`` ``PASS`` collide.

``.env`` is read for development convenience and is never required: an operator
who exports the variables (or runs in a container) gets the same result.  A real
environment variable always beats the file, so ``.env`` is a default and not a
policy.
"""

from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path
from typing import Any

PREFIX = "MINIMAX2API_"
SECTION_SEP = "__"

# Settings a hostile or careless environment must not be able to repoint: these
# decide where the process reads and writes, and an env var that moves the data
# directory would silently orphan the existing database.
_PROTECTED = frozenset()

_ENV_CACHE: dict[str, str] | None = None


def load_dotenv(path: str | os.PathLike[str] = ".env") -> dict[str, str]:
    """Parse a ``.env`` file into a mapping.  Missing file → empty mapping.

    Deliberately small: ``KEY=VALUE`` per line, ``#`` comments, optional single or
    double quotes around the value, and ``export `` tolerated because it gets
    copy-pasted from shells.  No interpolation — ``${OTHER}`` in a value stays
    literal, because the environment is the thing being described and inventing a
    second expansion layer is how a value ends up different in two places.
    """
    result: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def environ(*, dotenv: str | os.PathLike[str] = ".env", refresh: bool = False) -> dict[str, str]:
    """The effective environment: ``.env`` first, real variables on top.

    Cached because it is read on every settings write; ``refresh`` exists for the
    test that has to prove a change is picked up.
    """
    global _ENV_CACHE
    if _ENV_CACHE is not None and not refresh:
        return _ENV_CACHE
    merged = load_dotenv(dotenv)
    merged.update(os.environ)
    _ENV_CACHE = merged
    return merged


def _coerce(current: Any, raw: str) -> Any:
    """Read an env string as the type the field already holds.

    Typed by the field rather than by parsing the value, so ``""`` can mean an
    empty string for a text field or an error for an int — and a bad int is
    refused rather than silently becoming zero, which None of the callers could
    tell from an intentional zero.
    """
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
        return current
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw.strip())
        except ValueError:
            return current
    if isinstance(current, float):
        try:
            return float(raw.strip())
        except ValueError:
            return current
    return raw


def apply_env(settings: Any, env: dict[str, str] | None = None) -> list[str]:
    """Overwrite settings from the environment; return the field paths applied.

    A path is reported so a startup log can say what the environment is forcing —
    otherwise a console edit that will never take effect looks like a bug in the
    console rather than what it is.
    """
    env = env if env is not None else environ()
    applied: list[str] = []
    for section in fields(settings):
        holder = getattr(settings, section.name)
        for item in fields(holder):
            name = f"{section.name}.{item.name}"
            if name in _PROTECTED:
                continue
            key = f"{PREFIX}{section.name.upper()}{SECTION_SEP}{item.name.upper()}"
            if key not in env:
                continue
            value = _coerce(getattr(holder, item.name), env[key])
            if value != getattr(holder, item.name):
                setattr(holder, item.name, value)
                applied.append(name)
    return applied


def locked_fields(env: dict[str, str] | None = None) -> set[str]:
    """Field paths the environment controls, for the console to mark read-only."""
    env = env if env is not None else environ()
    locked: set[str] = set()
    for key in env:
        if not key.startswith(PREFIX):
            continue
        body = key[len(PREFIX):]
        section, _, field = body.partition(SECTION_SEP)
        if section and field:
            locked.add(f"{section.lower()}.{field.lower()}")
    return locked
