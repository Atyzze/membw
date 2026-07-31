#!/usr/bin/env bash
# Remove the memory-bandwidth monitor.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo ./uninstall.sh" >&2
  exit 1
fi

systemctl disable --now membw-collector.service 2>/dev/null || true
rm -f /etc/systemd/system/membw-collector.service
systemctl daemon-reload
# Clears any lingering failed state so the removed unit stops showing up in
# 'systemctl --failed'.
systemctl reset-failed membw-collector.service 2>/dev/null || true

rm -f /usr/local/bin/membw
rm -rf /opt/membw
# systemd normally clears RuntimeDirectory on stop; tidy it if the service was
# killed rather than stopped cleanly.
rm -rf /run/membw

echo "Removed collector service, launcher, /opt/membw and /run/membw."
echo "The amd_uncore module stays loaded until reboot; nothing here configures it to autoload."
