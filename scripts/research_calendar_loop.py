#!/usr/bin/env python3
"""Launcher for the calendar research loop in the sibling ``data-forecasting`` repo.

From the macrosys repo root::

    python scripts/research_calendar_loop.py sync --from 2026-10-01 --to 2026-11-30 \\
        --granularity month --cache-dir saved_months --db economic_data.db

Override location with env ``DATA_FORECASTING_ROOT`` if the clone is not at
``../data-forecasting`` next to macrosys.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _data_forecasting_root() -> Path:
    env = os.environ.get("DATA_FORECASTING_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2] / "data-forecasting"


def main() -> int:
    root = _data_forecasting_root()
    script = root / "research_calendar_loop.py"
    if not script.is_file():
        print(
            f"Missing {script}\n"
            "Clone https://github.com/rrrudolph/data-forecasting next to macrosys, "
            "or set DATA_FORECASTING_ROOT to a copy that includes research_calendar_loop.py.",
            file=sys.stderr,
        )
        return 2
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from research_calendar_loop import main as loop_main

    return loop_main()


if __name__ == "__main__":
    raise SystemExit(main())
