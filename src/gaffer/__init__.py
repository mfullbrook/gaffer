from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point: `gaffer run <ISSUE> [--adopt-head] [--more-cycles N] [--continue] [--fresh]`,
    `gaffer doctor [--config PATH]` (R18) and `gaffer eval [--config PATH] [--save-baseline | --promote RUN]`
    (R19).

    Exit codes (R8): 0 READY (doctor: passed; eval: baseline stored or no regression), 1 internal error,
    2 BLOCKED (eval: a regression), 3 CLOSED (eval: incomplete run), 4 refused (doctor: a probe failed).
    """
    from gaffer.runner import main as run_main

    return run_main(argv)
