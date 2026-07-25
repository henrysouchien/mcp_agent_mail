#!/usr/bin/env bash
set -euo pipefail
bash -n scripts/install.sh
rg -q "Python environment not found" scripts/install.sh
rg -q "Re-run scripts/install.sh --dir" scripts/install.sh
