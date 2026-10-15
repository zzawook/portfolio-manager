"""Read a .env file (KEY=VALUE lines) next to config.yaml into the environment.

Keys such as ANTHROPIC_API_KEY, OPENAI_API_KEY or IBKR_ACCOUNT_ID can live there
instead of in config.yaml. The file is in .gitignore. Variables that are already
set in the environment win, and values are never printed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping, Optional

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: str | Path, environ: Optional[MutableMapping[str, str]] = None) -> list[str]:
    """Load the file if it exists. Returns the names that were set."""
    env = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return []
    loaded: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if not _NAME.match(name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if name not in env:
            env[name] = value
            loaded.append(name)
    return loaded
