# toy: conventions

- Python 3.11+, type hints on public functions and a one-line docstring on each.
- Tests are plain pytest functions in `tests/`, importing from `toy.<module>` at the top of the file.
- Install dependencies with `sh scripts/install.sh` (uv, with its cache under `$TMPDIR`).
- Declare dependencies in `requirements.txt`. There is no `[project]` table, so `uv add` doesn't apply.
- Run the tests with `.venv/bin/python -m pytest`.
