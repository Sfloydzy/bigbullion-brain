#!/usr/bin/env bash
# Pull the latest code for a brand and refresh its packages. Run as root.
# Usage: bash update_brand.sh <name>
set -euo pipefail
NAME="${1:?usage: update_brand.sh <name>}"
DIR="/srv/brands/$NAME"
sudo -u brand git -C "$DIR" pull --ff-only
sudo -u brand "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
echo "$NAME updated"
