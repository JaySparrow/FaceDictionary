#!/bin/bash
# Launch FaceDictionary with all network access denied by the macOS sandbox.
# Double-click in Finder, or run from a terminal. Extra arguments are passed through.
set -euo pipefail
cd "$(dirname "$0")"

ENV_NAME="${FACEDICT_CONDA_ENV:-facedict}"
PY="$(conda run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    # conda is a shell function that may be missing in a non-interactive shell
    for base in "$HOME/anaconda3" "$HOME/miniconda3" "$HOME/miniforge3" "/opt/homebrew/Caskroom/miniforge/base"; do
        if [ -x "$base/envs/$ENV_NAME/bin/python" ]; then PY="$base/envs/$ENV_NAME/bin/python"; break; fi
    done
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "Cannot find the '$ENV_NAME' conda environment. See README.md." >&2
    exit 1
fi

exec "$PY" -m facedict "$@"      # the app sandboxes itself; no network from here on
