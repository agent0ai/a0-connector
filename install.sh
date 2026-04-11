#!/usr/bin/env bash

set -euo pipefail

PACKAGE_SPEC="${A0_PACKAGE_SPEC:-git+https://github.com/agent0ai/a0-connector}"
UV_INSTALL_URL="${UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

ensure_uv() {
    if have_cmd uv; then
        return
    fi

    if have_cmd curl; then
        curl -LsSf "$UV_INSTALL_URL" | sh
    elif have_cmd wget; then
        wget -qO- "$UV_INSTALL_URL" | sh
    else
        echo "curl or wget is required to install uv." >&2
        exit 1
    fi

    export PATH="$HOME/.local/bin:$PATH"

    if ! have_cmd uv; then
        cat >&2 <<'EOF'
uv was installed but is not on PATH in this shell yet.
Open a new terminal, then rerun this installer.
EOF
        exit 1
    fi
}

main() {
    local uv_bin_dir

    ensure_uv

    uv_bin_dir="$(uv tool dir --bin)"
    export PATH="$uv_bin_dir:$PATH"

    uv tool update-shell >/dev/null 2>&1 || true
    uv tool install --upgrade "$PACKAGE_SPEC"

    cat <<EOF

a0 is installed.

Run:
  a0

If 'a0' is not available in your current shell yet, open a new terminal.
uv installs tool executables in:
  $uv_bin_dir
EOF
}

main "$@"
