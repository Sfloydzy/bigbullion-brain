#!/usr/bin/env bash
# Add a brand: clone its repo, build its venv, register its timer (not started yet).
# Usage (as root): bash new_brand.sh <name> <git-url> [branch]
# A repo with its own deploy key: DEPLOY_KEY=/home/brand/.ssh/id_other bash new_brand.sh ...
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
NAME="${1:?usage: new_brand.sh <name> <git-url> [branch]}"
URL="${2:?usage: new_brand.sh <name> <git-url> [branch]}"
BRANCH="${3:-}"
[[ "$NAME" =~ ^[a-z0-9-]+$ ]] || { echo "name: lowercase letters, numbers, dashes only"; exit 1; }
DIR="/srv/brands/$NAME"
KEYCMD="ssh -i ${DEPLOY_KEY:-/home/brand/.ssh/id_github} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [ -d "$DIR/.git" ]; then
  echo "$DIR already cloned, skipping clone"
else
  sudo -u brand env GIT_SSH_COMMAND="$KEYCMD" git clone ${BRANCH:+--branch "$BRANCH"} "$URL" "$DIR"
fi
sudo -u brand git -C "$DIR" config core.sshCommand "$KEYCMD"

[ -d "$DIR/.venv" ] || sudo -u brand python3 -m venv "$DIR/.venv"
sudo -u brand "$DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u brand "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

# stagger this brand's tick by 15 s per brand so several brands never start together
N=$(find /srv/brands -mindepth 1 -maxdepth 1 -type d | wc -l)
OFFSET=$(( ((N - 1) * 15) % 60 ))
mkdir -p "/etc/systemd/system/brand-runner@$NAME.timer.d"
printf '[Timer]\nOnCalendar=\nOnCalendar=*-*-* *:*:%02d\n' "$OFFSET" > "/etc/systemd/system/brand-runner@$NAME.timer.d/offset.conf"

# dashboard: one localhost port per brand (8765, 8766, ...), always on
PORT=$(( 8764 + N ))
mkdir -p /etc/brand-dashboard
echo "DASHBOARD_PORT=$PORT" > "/etc/brand-dashboard/$NAME.env"
systemctl daemon-reload
systemctl enable --now "brand-dashboard@$NAME.service"

cat <<MSG

Brand '$NAME' is installed in $DIR (tick offset ${OFFSET}s). The timer is NOT started yet.
Dashboard is running on the server. View it from your PC: ssh -N -L $PORT:127.0.0.1:$PORT brand@SERVER, then open http://localhost:$PORT
Next:
  1. Copy this brand's secrets and data into $DIR (see deploy/DEPLOY.md, "Deploy to a VPS").
  2. sudo -u brand $DIR/.venv/bin/python $DIR/deploy/verify.py
  3. systemctl enable --now brand-runner@$NAME.timer
MSG
