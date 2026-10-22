from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point: `gaffer run <ISSUE> [--adopt-head] [--more-cycles N] [--continue] [--fresh]`.

    Exit codes (R8): 0 READY, 1 internal error, 2 BLOCKED, 3 CLOSED, 4 refused.
    """
    from gaffer.runner import main as run_main

    return run_main(argv)
