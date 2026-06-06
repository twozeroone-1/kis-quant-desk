#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export CODEX_HOME="$ROOT_DIR/.codex"
export PATH="$HOME/.local/bin:$PATH"

exec codex "$@"
