#!/usr/bin/env sh
# Atropos install — one-liner: curl | sh
#
#   curl -fsSL https://raw.githubusercontent.com/a2z05/Atropos/main/install.sh | sh
#
# Pure-stdlib Python 3.10+ is assumed present (Atropos runs on stdlib only —
# no pip, no node). Installs to ~/.atropos (or $ATROPOS_HOME), clones the
# repo, and prints the dashboard URL + first commands.
set -eu

APP="Atropos"
REPO_URL="https://github.com/a2z05/Atropos.git"
BRANCH="${ATROPOS_BRANCH:-main}"
PREFIX="${ATROPOS_HOME:-$HOME/.atropos}"

# ── python ─────────────────────────────────────────────────────────────────
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PY="$c"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "✗ $APP needs Python 3.10+ (stdlib only) — none found." >&2
    exit 1
fi

# ── clone (idempotent) ──────────────────────────────────────────────────────
if [ -d "$PREFIX/src" ]; then
    echo "✓ $APP already installed at $PREFIX/src — refreshing…"
    git -C "$PREFIX/src" pull --ff-only origin "$BRANCH" 2>/dev/null || true
else
    echo "→ Installing $APP to $PREFIX/src (branch $BRANCH)…"
    mkdir -p "$PREFIX"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$PREFIX/src"
fi

# ── PATH link ───────────────────────────────────────────────────────────────
mkdir -p "$PREFIX/bin"
ln -sf "$PREFIX/src/atropos" "$PREFIX/bin/atropos"
if ! echo ":$PATH:" | grep -q ":$PREFIX/bin:"; then
    echo "→ Add $PREFIX/bin to your PATH:"
    echo "    export PATH=\"\$PATH:$PREFIX/bin\"   # bash/zsh"
    echo "  or run:  atropos  (via $PREFIX/bin/atropos)"
fi

# ── first run ───────────────────────────────────────────────────────────────
echo
"$PREFIX/bin/atropos" init 2>/dev/null || true
"$PREFIX/bin/atropos" version 2>/dev/null || "$PY" "$PREFIX/src/atropos" version

echo
echo "✅ $APP installed."
echo "   Source:  $PREFIX/src"
echo "   Binary:  $PREFIX/bin/atropos"
echo "   Try:     atropos doctor · atropos dashboard · atropos status"