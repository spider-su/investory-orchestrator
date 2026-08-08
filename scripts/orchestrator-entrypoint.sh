#!/bin/sh
set -eu

mkdir -p /root/.codex

if [ -d /root/.codex-source ]; then
    cp -R /root/.codex-source/. /root/.codex/
fi

exec "$@"
