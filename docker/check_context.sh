#!/bin/sh
# In-build check of the Docker build context: the Dockerfile's context-check stage copies the whole context to
# a folder and runs this on it before anything is taken from it, so a .dockerignore mistake fails the build
# instead of shipping a document. Fails on any PDF (any case) and any forbidden folder or file name; prints
# counts and categories only, never a file name (scripts/check_build_context.py, run on the host, says which
# pattern let a file in). The host-side check runs before the build; this one cannot be skipped.
#   sh docker/check_context.sh /ctx
set -eu
ctx="${1:?usage: check_context.sh <context folder>}"
failed=0

fail() {
    echo "context check: $1" >&2
    failed=1
}

count() {  # number of paths under the context matching the find expression "$@"
    find "$ctx" "$@" -print | wc -l | tr -d ' '
}

n=$(count -iname '*.pdf')
[ "$n" -eq 0 ] || fail "$n PDF file(s) in the build context"

# Data, output and private folders, anywhere in the context.
for d in test_protocols output_tests output output_public public_protocols reports vlm_cache render_comparison \
         data docs _chunks .venv venv .git node_modules __pycache__ .pytest_cache .mypy_cache .ruff_cache .cache; do
    n=$(count -name "$d" -type d)
    [ "$n" -eq 0 ] || fail "forbidden folder name '$d' ($n)"
done

# Secrets, local configuration, documents' derived data, compiled files.
n=$(count -type f \( -name '.env' -o -name '.env.*' -o -name 'local_config.json' -o -name 'compact_prompt.md' \))
[ "$n" -eq 0 ] || fail "$n secret / local configuration file(s) (.env*, local_config.json, compact_prompt.md)"
n=$(count -type f \( -iname '*.sqlite' -o -iname '*.sqlite-*' -o -iname '*.db' -o -iname '*.npy' -o -iname '*.npz' \
        -o -iname '*.jsonl' -o -iname '*.pyc' -o -iname '*.docx' -o -iname '*.xlsx' -o -iname '*.zip' \))
[ "$n" -eq 0 ] || fail "$n data / cache / archive file(s) (sqlite, npy, jsonl, pyc, docx, xlsx, zip)"

# Only code and build inputs (and the static UI, under service/ui only): a file type the whitelist never names
# means the whitelist is wrong.
n=$(count -type f ! \( -name '*.py' -o -name '*.sh' -o -name '*.mjs' -o -name 'package.json' \
        -o -name 'package-lock.json' -o -name 'Dockerfile' -o -name 'requirements.in' -o -name 'requirements.lock' \) \
        ! \( -path "$ctx/service/ui/*" \( -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.svg' \
        -o -name '*.png' -o -name '*.ico' \) \))
[ "$n" -eq 0 ] || fail "$n file(s) of a type the whitelist never includes"

if [ "$failed" -ne 0 ]; then
    echo "context check failed: fix .dockerignore (python -m service.runtime --write-dockerignore), then run" \
         "python scripts/check_build_context.py on the host" >&2
    exit 1
fi
echo "context check: ok ($(count -type f) files: no PDF, no forbidden path)"
