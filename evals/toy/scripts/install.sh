#!/bin/sh
# Create .venv and install requirements.txt with uv. uv's cache lives under TMPDIR,
# so this also works inside sandboxes that may only write the checkout's directory.
set -eu
export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/uv-cache}"
export UV_PYTHON_DOWNLOADS=never
[ -x .venv/bin/python ] || uv venv --quiet .venv
uv pip install --quiet --python .venv/bin/python -r requirements.txt
