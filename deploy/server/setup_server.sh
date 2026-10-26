#!/usr/bin/env bash
# One-time server prep for a fresh Ubuntu 24.04 droplet. Run as root. Safe to re-run.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
HERE="$(cd "$(dirname "$0")" && pwd)"

apt-get update && apt-get -y upgrade
apt-get -y install python3-venv python3-pip git sqlite3 ufw fail2ban unattended-upgrades

# 2 GB swap, used only under pressure
[ -f /swapfile ] || { fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab; }
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swap.conf && sysctl -p /etc/sysctl.d/99-swap.conf

# firewall: SSH only
ufw allow OpenSSH && ufw --force enable

# non-root user that runs everything
id brand >/dev/null 2>&1 || adduser --disabled-password --gecos "" brand
mkdir -p /home/brand/.ssh
[ -f /home/brand/.ssh/authorized_keys ] || cp /root/.ssh/authorized_keys /home/brand/.ssh/
chown -R brand:brand /home/brand/.ssh && chmod 700 /home/brand/.ssh && chmod 600 /home/brand/.ssh/authorized_keys
mkdir -p /srv/brands && chown brand:brand /srv/brands
[ -f /home/brand/.ssh/id_github ] || sudo -u brand ssh-keygen -t ed25519 -N "" -f /home/brand/.ssh/id_github -C brand-engine-deploy

timedatectl set-timezone UTC   # brand schedules use each brand's own timezone from brand.json

# systemd units (installed from this folder)
if [ -f "$HERE/brand-runner@.service" ]; then
  install -m 644 "$HERE/brand-runner@.service" "$HERE/brand-runner@.timer" "$HERE/brand-health.service" "$HERE/brand-health.timer" "$HERE/brand-dashboard@.service" /etc/systemd/system/
  install -m 755 "$HERE/health.sh" /usr/local/bin/brand-health
  systemctl daemon-reload
  systemctl enable --now brand-health.timer
fi
echo "Server ready. Next: new_brand.sh <name> <git-url>"
