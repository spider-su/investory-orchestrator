#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repo_dir"
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

if [ -s "$HOME/.nvm/nvm.sh" ]; then
    # SSH forced commands do not load interactive shell profiles.
    . "$HOME/.nvm/nvm.sh"
    nvm use --silent default >/dev/null
    node_bin=$(nvm which default)
    PATH="$(dirname -- "$node_bin"):$PATH"
    export PATH
fi

if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

exec "${MAC_CLI_PYTHON:-python3}" scripts/mac_ssh_entrypoint.py
