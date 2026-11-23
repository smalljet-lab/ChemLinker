#!/bin/sh
# Brings an installed ChemLinker up to date (macOS / Linux, current user).
#
# Usage:  sh update.sh [--no-pull] [options of "compound-registry setup"]
#
# The script
#   1. fetches the new program files when this folder is a git checkout
#      (git pull --ff-only). It stops when files in this folder were changed locally or
#      the branch has diverged. When the folder is not a git checkout: download the new
#      version, unpack it over this folder, then run this script;
#   2. runs the installer with --upgrade: nothing is asked and every choice of the
#      installation is kept (data folder, browsers, ELN address). The Python environment
#      and the browser registration are refreshed, the extension is built again, and the
#      stored structures are checked if the chemistry version changed;
#   3. ends with the list of what is left to do by hand (the extension in the browser).
#
# The registry itself (database, journal, backups, compound files) is never replaced.
#
#   --no-pull    do not fetch anything: use the program files that are in this folder
#   --dry-run    only print what would be done; nothing is fetched or changed

set -u

# The whole script is inside main, so that the shell has read all of it before git
# replaces this file.
main() {
    ROOT=$(cd "$(dirname "$0")" && pwd)
    NO_PULL=""
    DRY_RUN=""

    # Take --no-pull out of the arguments; everything else goes to the installer.
    count=$#
    while [ "$count" -gt 0 ]; do
        argument=$1
        shift
        count=$((count - 1))
        case "$argument" in
            --no-pull) NO_PULL=1 ;;
            -h|--help)
                sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
                exit 0
                ;;
            --dry-run)
                DRY_RUN=1
                set -- "$@" "$argument"
                ;;
            *) set -- "$@" "$argument" ;;
        esac
    done

    before=$(program_version)
    echo "ChemLinker: installed program version $before"

    if [ -n "$NO_PULL" ]; then
        echo "==> Not fetching anything (--no-pull): using the files in $ROOT"
    elif [ -n "$DRY_RUN" ]; then
        echo "==> Dry run: nothing is fetched"
    elif [ ! -e "$ROOT/.git" ]; then
        echo "This folder is not a git checkout, so the new version is not fetched automatically."
        echo "To get it: download the new version of the project and unpack it over this folder"
        echo "($ROOT), replacing the files, then run this script again."
        echo "==> Going on with the files that are here now"
    elif ! command -v git >/dev/null 2>&1; then
        fail "This folder is a git checkout, but git was not found. Install git, or download the new version, unpack it over this folder and run this script with --no-pull."
    else
        remotes=$(git -C "$ROOT" remote) || fail "git could not read this folder ($ROOT); see its message above."
        if [ -z "$remotes" ]; then
            echo "==> This git checkout has no remote to fetch from: using the files that are here"
        else
            changes=$(git -C "$ROOT" status --porcelain --untracked-files=no) || fail "git status failed in $ROOT; see its message above."
            if [ -n "$changes" ]; then
                echo ""
                echo "Program files in this folder were changed locally:"
                echo "$changes" | sed 's/^/  /'
                fail "Nothing was fetched or changed. Undo or commit these changes (to set them aside: git stash), then run this script again; or run it with --no-pull to update with the files as they are."
            fi
            branch=$(git -C "$ROOT" symbolic-ref -q --short HEAD) || branch=""
            if [ -z "$branch" ]; then
                fail "This checkout is at a fixed version (a tag or a single commit), not on a branch, so there is nothing to pull. Nothing was fetched or changed. To follow the new versions: git checkout main, then run this script again. To go to one version: git fetch --tags, git checkout <tag>, then run this script with --no-pull."
            fi
            upstream=$(git -C "$ROOT" for-each-ref --format='%(upstream)' "refs/heads/$branch")
            if [ -z "$upstream" ]; then
                fail "The branch '$branch' of this checkout has no upstream branch, so there is nothing to pull. Nothing was fetched or changed. Switch to the main branch (git checkout main) and run this script again; or get the new files with git yourself, then run this script with --no-pull."
            fi
            echo "==> Fetching the new version (git pull --ff-only)"
            git -C "$ROOT" pull --ff-only || fail "git pull --ff-only failed; see its message above. Nothing was updated. Usual reasons: no connection, or this checkout has commits of its own (a diverged branch), which git does not overwrite. Resolve that with git, or run this script with --no-pull to go on with the files that are here."
            after=$(program_version)
            if [ "$after" = "$before" ]; then
                echo "Program version: $after (unchanged)"
            else
                echo "Program version: $before -> $after"
            fi
        fi
    fi

    echo ""
    sh "$ROOT/install.sh" --upgrade "$@"
    code=$?
    if [ "$code" -ne 0 ]; then
        echo ""
        echo "The update did not finish (exit code $code); see the messages above."
    fi
    exit "$code"
}

fail() {
    echo ""
    echo "ERROR: $1"
    exit 1
}

program_version() {
    version=$(sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p' "$ROOT/src/compound_registry/__init__.py" 2>/dev/null | head -n 1)
    echo "${version:-unknown}"
}

main "$@"
