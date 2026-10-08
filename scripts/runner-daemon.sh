#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
config_file=${RUNNER_ENV_FILE:-"$HOME/.config/investory-orchestrator/runner.env"}

if [ ! -r "$config_file" ]; then
    echo "Runner environment file is not readable: $config_file" >&2
    exit 78
fi

# This file is an operator-owned shell environment file and must be mode 0600.
set -a
. "$config_file"
set +a

cd "$repo_root"
python=${RUNNER_PYTHON:-"$repo_root/.venv/bin/python"}
if [ ! -x "$python" ]; then
    python=$(command -v python3 || true)
fi
if [ -z "$python" ]; then
    echo "Python 3 was not found; set RUNNER_PYTHON in $config_file" >&2
    exit 78
fi

exec "$python" -m app.runner_daemon
