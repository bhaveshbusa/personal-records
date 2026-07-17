"""Configuration: where user data lives.

One local profile: user data never lives in the repo tree — default is
`~/.personal-records/`, overridable via `PERSONAL_RECORDS_HOME` (tests point it
at a tmp dir).
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Resolve the data directory. Does not create it; write paths do."""
    return Path(os.environ.get("PERSONAL_RECORDS_HOME", "~/.personal-records")).expanduser()
