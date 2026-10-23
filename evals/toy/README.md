# toy

A tiny Python package used by `gaffer eval` (R19). Text and size helpers in `toy/`, pytest tests in `tests/`.

- Install: `sh scripts/install.sh` creates `.venv` and installs `requirements.txt` with uv.
- Test: `.venv/bin/python -m pytest`
- Dependencies are declared in `requirements.txt` only (there is no `[project]` table, so `uv add` doesn't apply).
  To add one, append it to `requirements.txt` and rerun `sh scripts/install.sh`.
