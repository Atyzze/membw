# membw - live memory bandwidth monitor (AMD)

A terminal monitor for DRAM bandwidth on AMD systems, like `btop` but focused on
the memory bus. It reads the AMD memory-controller (UMC) counters through `perf`
and shows a live, scrolling graph with the **measured** data-bus utilization
percentage, not an estimate.

```
 bus    65.2% used  theoretical upper cap    96.0 GB/s   (from umc_mem_clk)
 avg    43.41 GB/s  observed max             65.9 GB/s   (68.6% of cap)
 now    62.55 GB/s  ██████████████████████   94.9% of observed max
  65.9 |                                      ▁▂▅█▇▅▂
       |                          ▁▂▃▂▁     ▂▅███████▅▂
  33.0 |            ▁▂▃▂▁   ▁▂▃▄▄▄████▄▃▂▃▄▅███████████▇▅
   0.0 |▁▂▂▃▂▁▁▂▃▄▄██████████████████████████████████████
   76.2s shown   250ms sample   [measured bus util]   +/- rate   r reset stats   q quit   avg/max over 12m04s
```

Each header row pairs a live value on the left with the reference it should be
read against on the right.

## Reading the display

**`bus N% used`** is the real utilization figure from the memory controller, not
a ratio someone computed. It is the one number that needs no assumptions.

**`theoretical upper cap`** is what the bus could carry if it never idled. It is
**derived from your machine at runtime**, never hardcoded, from three sources in
order of trust:

1. `umc_mem_clk`, the memory controller's own clock counter. Each UMC instance
   drives one 64-bit channel and perf sums the event across instances, so
   `summed_clk_hz x 2 (double data rate) x 8 bytes` is the peak directly. For
   DDR5-6000 on dual channel that is `6.0e9 x 2 x 8 = 96.0 GB/s`, and the
   instance count falls out of the sum for free.
2. `dmidecode`, summing `Configured Memory Speed x Data Width` over populated
   slots. Used when perf counters are unavailable.
3. **Bus utilization, continuously.** Utilization is bandwidth divided by the
   cap by definition, so `bw / (util/100)` recovers the cap exactly. This runs
   as a live cross-check on every sample above 15% utilization. If it disagrees
   with the startup figure by more than 10%, a warning row appears naming the
   value it measured.

That last one means a stale or wrong cap announces itself instead of quietly
skewing every percentage on screen. Override with `--cap N` if you need to;
`--no-autocap` pins it and disables the cross-check.

**The graph is normalised to `observed max`, not to the cap.** A full-height bar
means "as busy as this machine has ever been", so idle traffic stays legible
instead of being crushed against a ceiling you may never reach in practice. The
scale rescales downward whenever a new maximum arrives, which settles once
you have hit your real ceiling for the session. Press `r` to reset it.

**`avg`** is time-weighted, integrating bandwidth over elapsed seconds rather
than averaging samples. That keeps it honest when the sample interval changes
mid-run. Gaps longer than 60s (suspend, a stalled collector) are skipped rather
than averaged in. `avg/max over ...` in the footer is the period both it and
`observed max` cover.

## Keys

| key | action |
|---|---|
| `+` | sample faster (50ms / 100ms / 250ms / 500ms / 1s / 2s / 5s) |
| `-` | sample slower |
| `r` | reset stats: observed max, average, and the period all restart here |
| `q` | quit |

Keys are read once per sample, so at a 5s interval a keypress can take up to
five seconds to register. They activate only on a real terminal, so `--serve`
and piped output are unaffected.

## Requirements

- An AMD Zen CPU whose kernel exposes the UMC uncore PMU (`amd_umc_0` under
  `/sys/bus/event_source/devices/`). This needs AMD Zen UMC uncore support,
  mainline 6.11+; the installer and the service load `amd_uncore` for you.
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
3. compile-check and copy `membw.py` to `/opt/membw/`,
4. install and enable `membw-collector.service`,
5. install a `membw` launcher,
6. wait for the first sample and print the cap the collector derived.

Remove everything with `sudo ./uninstall.sh`.

## Why a collector + a file?

The memory-controller counters are a single shared resource. If two
`perf stat -a` sessions try to own them at once, they multiplex and both get
partial coverage. So the design splits in two:

- **Collector** (privileged, one instance): owns the counters, samples them,
  and writes the latest reading to `/run/membw/latest.json`.
- **GUI** (unprivileged, any number): just reads that file and draws.

The GUI needs no privileges and you can open several at once. `/run` is a
**tmpfs** (RAM-backed), so the writes never touch your SSD.

The GUI polls at its own interval and discards readings it has already seen, so
running it faster than the collector costs plot detail rather than inventing
duplicate samples. Both default to 250ms.

### This matters if you also run perf by hand

While the collector is running it holds the UMC counters. Any `perf stat` of
your own on `amd_umc/*` will multiplex against it and report reduced coverage
(the `(60.02%)` figures perf prints beside each count). Stop the collector
first:

```sh
sudo systemctl stop membw-collector
sudo perf stat -a -e amd_umc/umc_act_cmd.all/,amd_umc/umc_cas_cmd.all/ -- sleep 30
sudo systemctl start membw-collector
```

## No sudo, the clean way

The collector runs under systemd with `AmbientCapabilities=CAP_PERFMON` and
`DynamicUser=yes`. That grants exactly one capability (perf monitoring) to the
collector and the `perf` it spawns, with no global `perf_event_paranoid` change
and no `setcap` on binaries. The GUI runs as you and only reads a file.

## Manual usage

```sh
# collector (needs CAP_PERFMON or root):
python3 membw.py --serve --file /run/membw/latest.json --source perf -i 250ms

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

| flag | meaning |
|---|---|
| `-i`, `--interval T` | sample interval as `250ms`, `0.5s` or a bare number of seconds (default 250ms, adjustable live with `+`/`-`) |
| `-p`, `--peak`, `--cap N` | override the derived cap, in GB/s |
| `--no-autocap` | do not derive or cross-check the cap; use `--cap` verbatim |
| `--cap-probe T` | seconds spent sampling the UMC clock at startup (default 1) |
| `-s`, `--source` | `perf`, `file`, `mbm`, `demo` |
| `--file PATH` | collector state file |
| `--serve` | run as the collector instead of the GUI |
| `--no-color` | plain output |
| `--frames N` | exit after N frames |
| `--debug` | raw perf rows and parsed samples, no graph |

`--peak` is kept as an alias for `--cap` for backward compatibility.

## Robust parsing

`perf` output is read as tab-separated machine output under `LC_ALL=C`, and each
metric is identified by its **unit** (`%` = utilization, `*B/s` = bandwidth),
never by column position or by a translatable metric name. Locale settings
(decimal comma vs point) and column reordering cannot break it.

perf is spawned on a pty rather than a pipe, because it line-buffers on a
terminal and block-buffers on a pipe, which makes it look hung.

Since the interval is baked into perf's `-I` argument, changing it live means
respawning perf. The source watches for that and restarts itself, noticing
within 200ms.

## Troubleshooting

- **GUI shows "no collector file" or "stale"**: the collector isn't writing.
  Check `systemctl status membw-collector` and `journalctl -u membw-collector -e`.
- **`! cap N disagrees with bus util (M GB/s)`**: something forced a wrong cap.
  Most likely a `--cap`/`--peak` flag in a launcher script, shell alias, or unit
  file. Find it with `ps -eo pid,args | grep membw` and remove it; the derived
  value is the one to trust.
- **perf "access"/"paranoid" error**: the collector lacks `CAP_PERFMON`. The
  service grants it; if running by hand, use `sudo` or
  `sudo sysctl kernel.perf_event_paranoid=0`.
- **`amd_umc_0` missing**: your kernel didn't load/expose the UMC PMU. Try
  `sudo modprobe amd_uncore` and re-check `/sys/bus/event_source/devices/`. If
  still absent, you need a kernel with AMD Zen UMC uncore support (mainline
  6.11+).
- **perf can't open counters only under the service**: comment out the
  `Protect*`/`Restrict*` hardening block in the unit and restart.
- **Your own `perf stat` shows percentages like `(60.02%)` beside counts**: that
  is multiplexing coverage, not a value. Stop the collector first (see above).
- **Numbers look off by ~1000x**: a unit slipped through; run with `--debug` and
  share a row.

## Porting to Intel or other CPUs (not implemented)

This tool is AMD-specific: it uses the AMD UMC PMU and the `umc_mem_bandwidth` /
`umc_data_bus_utilization` perf metrics. The architecture (collector + file +
GUI, unit-keyed parsing, CAP_PERFMON service) is portable; only the perf source
and the cap derivation need changing.

On Intel the equivalent is the integrated memory controller uncore PMU,
`uncore_imc_*` under `/sys/bus/event_source/devices/`, with events
`cas_count_read` and `cas_count_write`. There is no ready-made
"data_bus_utilization" metric, so you compute bandwidth yourself:

    bytes = (cas_count_read + cas_count_write) * 64
    GB/s  = bytes / interval_seconds / 1e9

To adapt, change `PERF_METRICS` (and the `parse_perf_row` unit mapping) in
`membw.py` to request those events across all `uncore_imc_*` PMUs and sum them,
or feed the `file` source from any external collector that writes the same
`{"bw":..., "util":..., "ts":..., "cap":..., "cap_src":...}` JSON. Without a
utilization metric the live cap cross-check cannot run, so `cap_from_dmidecode`
becomes the primary source. The file format and GUI are ready for whatever
fills them.

## Uninstall

```sh
sudo ./uninstall.sh
```
