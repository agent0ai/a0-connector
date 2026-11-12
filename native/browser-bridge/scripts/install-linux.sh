#!/bin/sh
set -eu

if [ "$(uname -s)" != Linux ] || [ "$(id -u)" = 0 ]; then
    printf '%s\n' 'Run this on your Linux desktop as your normal user, without sudo.' >&2
    exit 2
fi
if ! command -v a0 >/dev/null 2>&1; then
    printf '%s\n' 'Install the Agent Zero CLI first, then run: a0 browser-extension install' >&2
    exit 3
fi
# The CLI owns pinned downloads; native code owns verification and registration.
exec a0 browser-extension install "$@"
