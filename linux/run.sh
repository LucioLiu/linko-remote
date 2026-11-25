#!/bin/sh
set -eu

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    printf '%s\n' '邻控需要 Python 3.9 或更新版本。' >&2
    exit 1
fi

app_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd -- "$app_dir/code"
# The service's stdout includes the private loopback session URL.
exec python3 -E -s -B -m lanpilot.peer_client --open-browser "$@" >/dev/null
