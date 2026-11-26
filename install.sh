#!/bin/sh
# Installs ChemLinker for the current user (macOS / Linux).
#
# Usage:  sh install.sh [options]
#
# The script
#   1. looks for uv (the Python package manager the project uses) and offers to install it
#      for the current user when it is missing;
#   2. creates the project-local Python environment (.venv, .python, .uv-cache in this folder);
#   3. starts the guided installation, "compound-registry setup". It asks for the data folder
#      (where the registry database, its backups and the compound files are stored), the
#      browser(s) and the address of the ELN, registers the registry program with the
#      browser, builds the extension (needs Node.js) and lists what is left to do by hand.
#
# Running it again is safe: every question then shows the previous choice as its default.
# To bring an installation up to date, use update.sh.
#
# Every option of "compound-registry setup" can be added and is passed on unchanged:
#
#   --home PATH          the data folder (not asked then)
#   --browser NAME       firefox, chrome or both
#   --eln-origin URL     address of the ELN, e.g. https://eln.example.org
#   --create-registry    create a NEW, EMPTY registry in the data folder without asking
#   --id-prefix ABC      the first three letters (A-Z) of every ID of a NEW registry (permanent)
#   --id-format TEMPLATE full ID template of a NEW registry (permanent)
#   --mnova yes|no       the MestReNova add-on (Windows only)
#   --recognition yes|no ChemResolver, the optional part that reads reaction schemes (about 2.8 GB)
#   --skip-extension     do not build the extension
#   --force              replace a browser registration of another installation
#   --yes                ask nothing: take every default or previous choice
#   --upgrade            ask nothing and keep everything as it is installed
#   --dry-run            only print what would be done; change nothing
#
# Examples:
#   sh install.sh --dry-run
#   sh install.sh --home ~/CompoundRegistry --browser both
#   sh install.sh --yes --home ~/CompoundRegistry --create-registry --id-prefix ABC

set -u

# The whole script is inside main, so that the shell has read all of it before anything
# runs (update.sh may replace this file while it is running).
main() {
    ROOT=$(cd "$(dirname "$0")" && pwd)
    UV_INSTALL_PAGE="https://docs.astral.sh/uv/getting-started/installation/"
    CLOSE_HINT="Close Firefox/Chrome, then run this again."
    DRY_RUN=""
    NO_QUESTIONS=""

    for argument in "$@"; do
        case "$argument" in
            --dry-run) DRY_RUN=1 ;;
            --yes|--upgrade) NO_QUESTIONS=1 ;;
            -h|--help)
                sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'
                exit 0
                ;;
        esac
    done

    find_uv || install_uv

    # Keep Python and the uv cache inside the project folder.
    UV_PYTHON_INSTALL_DIR="$ROOT/.python"
    UV_CACHE_DIR="$ROOT/.uv-cache"
    export UV_PYTHON_INSTALL_DIR UV_CACHE_DIR

    CLI="$ROOT/.venv/bin/compound-registry"
    cd "$ROOT" || fail "Cannot change to $ROOT."

    if [ -n "$DRY_RUN" ]; then
        if [ ! -x "$CLI" ]; then
            echo "Dry run: the Python environment does not exist yet. An installation would"
            echo "create it in $ROOT/.venv (uv sync --locked --no-dev) and then start"
            echo "'compound-registry setup'. Nothing was changed."
            exit 0
        fi
        echo "==> Dry run: the Python environment is left as it is (no uv sync)"
    else
        if host_is_running; then
            echo "The registry program of this folder is running (a browser has it open). The"
            echo "browser uses the new version after it was closed and started again."
        fi
        echo "==> Creating the Python environment (uv sync --locked --no-dev)"
        uv sync --locked --no-dev || fail "uv sync failed; see its message above. If it says that a file is in use: $CLOSE_HINT"
        [ -x "$CLI" ] || fail "$CLI was not created by uv sync."
    fi

    echo ""
    "$CLI" setup "$@"
    code=$?
    if [ "$code" -ne 0 ]; then
        echo ""
        echo "compound-registry setup ended with exit code $code; see its messages above."
    fi
    exit "$code"
}

fail() {
    echo ""
    echo "ERROR: $1"
    exit 1
}

can_ask() {
    [ -z "$NO_QUESTIONS" ] && [ -t 0 ]
}

find_uv() {
    command -v uv >/dev/null 2>&1 && return 0
    # Where the official installer puts uv; a terminal opened before it ran does not see it yet.
    for folder in "${XDG_BIN_HOME:-}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -n "$folder" ] && [ -x "$folder/uv" ]; then
            PATH="$folder:$PATH"
            export PATH
            return 0
        fi
    done
    return 1
}

install_uv() {
    echo "uv, the Python package manager this project uses, was not found."
    if [ -n "$DRY_RUN" ]; then
        echo "Dry run: uv would have to be installed first ($UV_INSTALL_PAGE). Nothing was changed."
        exit 0
    fi
    can_ask || fail "Install uv first ($UV_INSTALL_PAGE), open a new terminal and run this script again."
    echo "It can be installed now for the current user (no administrator rights) with its"
    echo "official installer: curl -LsSf https://astral.sh/uv/install.sh | sh"
    printf "Install uv now? [y/N] "
    read -r answer || answer=""
    case "$answer" in
        y|Y|yes|Yes|YES) ;;
        *) fail "uv is needed. Install it ($UV_INSTALL_PAGE), open a new terminal and run this script again." ;;
    esac
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh || fail "The uv installer failed. Install uv by hand: $UV_INSTALL_PAGE"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh || fail "The uv installer failed. Install uv by hand: $UV_INSTALL_PAGE"
    else
        fail "Neither curl nor wget was found to download the uv installer. Install uv by hand: $UV_INSTALL_PAGE"
    fi
    find_uv || fail "uv was installed but is not found from this terminal. Open a new terminal and run this script again."
}

host_is_running() {
    command -v pgrep >/dev/null 2>&1 || return 1
    pgrep -f "$ROOT/.venv/bin/compound-registry-host" >/dev/null 2>&1
}

main "$@"
