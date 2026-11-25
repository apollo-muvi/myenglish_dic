#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

git pull --ff-only
systemctl --user restart myenglish-dic.service
systemctl --user is-active --quiet myenglish-dic.service

echo "Deployment complete: myenglish-dic.service is active."
