#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repo_dir"
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

exec "${MAC_CLI_PYTHON:-python3}" scripts/mac_ssh_entrypoint.py
