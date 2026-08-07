#!/usr/bin/env bash
# Start the mock UI with the right interpreter, whatever your shell or editor
# happens to have on PATH. The commonest failure is running it under a Python
# that has nothing installed, which exits instantly with "No module named
# mock_ui" and reads as the server crashing.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="$here/../scheduling_engine/.venv/bin/python"

if [[ ! -x "$python" ]]; then
  echo "No venv at $python" >&2
  echo "Create it with:" >&2
  echo "  cd $here/../scheduling_engine && python3 -m venv .venv \\" >&2
  echo "    && .venv/bin/pip install -e ../calendar_store -e . -e ../mock_ui" >&2
  exit 1
fi

exec "$python" -m mock_ui "$@"
