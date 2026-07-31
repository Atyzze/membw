#!/usr/bin/env bash
# Installer for the AMD memory-bandwidth monitor (Arch Linux).
# Sets up a privileged collector (systemd + CAP_PERFMON, no sudo for the GUI)
# and a 'membw' launcher for the unprivileged live graph.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo ./install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/membw
UNIT=/etc/systemd/system/membw-collector.service
LAUNCHER=/usr/local/bin/membw
STATE=/run/membw/latest.json

echo "==> Checking prerequisites"
if ! grep -qi 'AuthenticAMD' /proc/cpuinfo; then
  echo "   ! CPU is not AMD. The perf source is AMD-UMC specific; see README (Intel/other)."
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "   ! python3 not found. Install it first:  pacman -S python" >&2
  exit 1
fi

if ! command -v perf >/dev/null 2>&1; then
  echo "   perf not found; installing via pacman..."
  pacman -S --needed perf
fi

# Load the uncore PMU driver and check the memory-controller PMU appeared. On
# kernels built with CONFIG_PERF_EVENTS_AMD_UNCORE=y this modprobe is a harmless
# no-op, because the driver is built in rather than a loadable module.
modprobe amd_uncore 2>/dev/null || true
if [ -d /sys/bus/event_source/devices/amd_umc_0 ]; then
  UMC_N=$(find /sys/bus/event_source/devices -maxdepth 1 -name 'amd_umc_*' | wc -l)
  echo "   amd_umc PMU present (${UMC_N} instance(s))."
else
  echo "   ! amd_umc PMU not found even after 'modprobe amd_uncore'."
  echo "     The collector cannot get data until this PMU exists (needs a kernel"
  echo "     with AMD Zen UMC uncore support). Installing anyway; fix the kernel"
  echo "     side and 'systemctl restart membw-collector' later."
fi

echo "==> Validating source"
python3 -m py_compile "$SRC_DIR/membw.py"
echo "   membw.py compiles."

echo "==> Installing files"
install -d "$PREFIX"
install -m 0755 "$SRC_DIR/membw.py" "$PREFIX/membw.py"
install -m 0644 "$SRC_DIR/membw-collector.service" "$UNIT"

# No bandwidth ceiling is baked in here, on purpose. membw.py derives the
# theoretical upper cap at runtime from the UMC memory clock and cross-checks it
# against the controller's own utilization metric, re-deriving on every start.
# An install-time constant would silently go stale the moment memory speed
# changed (enabling EXPO, swapping DIMMs) with nothing on screen to say so.
echo "==> Installing launcher: ${LAUNCHER}"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# Unprivileged live GUI for the memory-bandwidth collector.
exec python3 ${PREFIX}/membw.py --source file --file ${STATE} "\$@"
EOF
chmod 0755 "$LAUNCHER"

echo "==> Enabling and (re)starting collector service"
systemctl daemon-reload
systemctl enable membw-collector.service
# restart (not just enable --now): picks up new code/unit when re-running the
# installer to upgrade an already-running service.
if ! systemctl restart membw-collector.service; then
  echo "   ! Service failed to start. Check: journalctl -u membw-collector -e" >&2
  exit 1
fi

echo "==> Verifying the collector is producing data"
REPORT=""
for _ in $(seq 1 20); do
  if [ -r "$STATE" ]; then
    REPORT=$(python3 - "$STATE" <<'PY' 2>/dev/null || true
import json, sys
try:
    rec = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if rec.get("bw") is None:
    sys.exit(1)
cap = rec.get("cap")
print(f"{cap:.1f} GB/s (from {rec.get('cap_src', '?')})" if cap else "not derived yet")
PY
)
    [ -n "$REPORT" ] && break
  fi
  sleep 0.5
done

if [ -n "$REPORT" ]; then
  echo "   collector is writing ${STATE}"
  echo "   theoretical upper cap: ${REPORT}"
else
  echo "   ! No usable sample in ${STATE} after 10s."
  echo "     Check: journalctl -u membw-collector -e"
fi

echo
echo "Done."
echo "  View live:       membw"
echo "  Faster sampling: membw -i 100ms      (or press + while it runs)"
echo "  Collector logs:  journalctl -u membw-collector -f"
echo "  Status:          systemctl status membw-collector"
echo "  Uninstall:       sudo ./uninstall.sh"
