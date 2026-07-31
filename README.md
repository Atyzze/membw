# membw - live memory bandwidth monitor (AMD)

A terminal monitor for DRAM bandwidth on AMD systems, like `btop` but focused on
the memory bus. It reads the AMD memory-controller (UMC) counters through `perf`
and shows a live, scrolling graph with the **measured** data-bus utilization
percentage, not an estimate.

```
 MEM BANDWIDTH    peak 76.8 GB/s    sample 250 ms    [measured bus util]

 now    41.20 GB/s  ████████████████░░░░░░  bus  53.6% used
 avg    12.10 GB/s      max  44.90 GB/s      (~54% of 77)

  44.9 |                                      ▁▂▅█▇▅▂
       |                          ▁▂▃▂▁     ▂▅███████▅▂
  22.5 |            ▁▂▃▂▁   ▁▂▃▄▄▄████▄▃▂▃▄▅███████████▇▅
   0.0 |▁▂▂▃▂▁▁▂▃▄▄██████████████████████████████████████
       10.0s history      green <30%  yellow  red >70%
```

Bar **height** is auto-scaled so idle traffic stays visible; bar **color**
shows saturation from the real utilization metric (green low, red high).

## Requirements

- An AMD Zen CPU whose kernel exposes the UMC uncore PMU (`amd_umc_0` under
  `/sys/bus/event_source/devices/`). This needs the `amd_uncore` module; the
  installer and the service load it for you.
- `perf` (the installer offers to install it).
- Python 3 (standard library only, no pip packages).

## Quick start (Arch)

```sh
sudo ./install.sh      # installs collector service + a 'membw' launcher
membw                  # run the live graph as your normal user, no sudo
```

`install.sh` will:
1. install `perf` if missing,
2. load `amd_uncore` and verify the `amd_umc` PMU exists,
3. copy `membw.py` to `/opt/membw/`,
4. install and enable `membw-collector.service`,
5. detect your peak bandwidth via `dmidecode` and install a `membw` launcher.

Remove everything with `sudo ./uninstall.sh`.

## Why a collector + a file?

The memory-controller counters are a single shared resource. If two
`perf stat -a` sessions try to own them at once, they fight and one gets
starved. So the design splits in two:

- **Collector** (privileged, one instance): owns the counters, samples them,
  and writes the latest reading to `/run/membw/latest.json`.
- **GUI** (unprivileged, any number): just reads that file and draws.

This also means the GUI needs no privileges, and you can open several GUIs at
once. `/run` is a **tmpfs** (RAM-backed), so the per-second writes never touch
your SSD.

## No sudo, the clean way

The collector runs under systemd with `AmbientCapabilities=CAP_PERFMON` and
`DynamicUser=yes`. That grants exactly one capability (perf monitoring) to the
collector and the `perf` it spawns, with no global `perf_event_paranoid` change
and no `setcap` on binaries. The GUI runs as you and only reads a file.

## Manual usage

```sh
# collector (needs CAP_PERFMON or root):
python3 membw.py --serve --file /run/membw/latest.json --source perf --interval 1

# gui (unprivileged):
python3 membw.py --source file --file /run/membw/latest.json

# one-shot single process (no service), needs sudo:
sudo python3 membw.py --source perf

# see raw perf rows + parsed samples (no graph), for debugging:
sudo python3 membw.py --source perf --debug

# try the interface with fake data, no hardware needed:
python3 membw.py --demo
```

Sources: `perf` (AMD UMC, measured utilization), `file` (read a collector),
`mbm` (resctrl byte counters; unreliable on some Zen parts), `demo` (fake).
Flags: `-i/--interval` seconds, `-p/--peak` GB/s for the estimate line,
`--file` path, `--no-color`, `--frames N` (exit after N).

## Robust parsing

`perf` output is read as tab-separated machine output under `LC_ALL=C`, and each
metric is identified by its **unit** (`%` = utilization, `*B/s` = bandwidth),
never by column position or by a translatable metric name. Locale settings
(decimal comma vs point) and column reordering can't break it.

## Troubleshooting

- GUI shows "no collector file" or "stale": the collector isn't writing. Check
  `systemctl status membw-collector` and `journalctl -u membw-collector -e`.
- perf "access"/"paranoid" error: the collector lacks `CAP_PERFMON`. The service
  grants it; if running by hand, use `sudo` or
  `sudo sysctl kernel.perf_event_paranoid=0`.
- `amd_umc_0` missing: your kernel didn't load/expose the UMC PMU. Try
  `sudo modprobe amd_uncore` and re-check `/sys/bus/event_source/devices/`. If
  still absent, you need a kernel with AMD Zen UMC uncore support (mainline
  6.11+).
- perf can't open counters only under the service: comment out the
  `Protect*`/`Restrict*` hardening block in the unit and restart.
- Numbers look off by ~1000x: a unit slipped through; run with `--debug` and
  share a row.

## Porting to Intel or other CPUs (not implemented)

This tool is AMD-specific: it uses the AMD UMC PMU and the `umc_mem_bandwidth` /
`umc_data_bus_utilization` perf metrics. The architecture (collector + file +
GUI, unit-keyed parsing, CAP_PERFMON service) is portable; only the perf source
needs changing.

On Intel the equivalent is the integrated memory controller uncore PMU,
`uncore_imc_*` under `/sys/bus/event_source/devices/`, with events
`cas_count_read` and `cas_count_write`. There is no ready-made
"data_bus_utilization" metric, so you compute bandwidth yourself:

    bytes = (cas_count_read + cas_count_write) * 64
    GB/s  = bytes / interval_seconds / 1e9

To adapt, change `PERF_METRICS` (and the `parse_perf_row` unit mapping) in
`membw.py` to request those events across all `uncore_imc_*` PMUs and sum them,
or feed the `file` source from any external collector that writes the same
`{"bw":..., "util":..., "ts":...}` JSON. Utilization % on Intel would be derived
from bandwidth over a known peak rather than read directly. This path is left as
an exercise; the file format and GUI are ready for whatever fills it.

## Uninstall

```sh
sudo ./uninstall.sh
```
