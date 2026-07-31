#!/usr/bin/env python3
"""Live memory-bandwidth monitor for AMD systems (Zen UMC counters).

Architecture
------------
Counters are a single shared resource, so only ONE privileged collector should
own them. Everything else just reads the collector's output.

  collector (privileged):   membw.py --serve /run/membw/latest.json --source perf
  gui       (your user):    membw.py --source file --file /run/membw/latest.json

Run the collector once (e.g. as a systemd service with AmbientCapabilities=
CAP_PERFMON, so no sudo and no global perf_event_paranoid change). Then any
number of unprivileged GUIs can read the file. See the shipped .service unit.

Sources
-------
  perf   AMD UMC via perf, tab-separated machine output under LC_ALL=C.
         Metrics are keyed by UNIT ('%' vs 'B/s'), never by locale text or
         column position. Needs CAP_PERFMON (or root). Reports MEASURED
         data-bus utilization.
  file   Reads a JSON sample written by a --serve collector. Unprivileged.
  mbm    resctrl MBM byte counters (unreliable on some Zen parts).
  demo   Fake data:  membw.py --demo

Debug:  membw.py --source perf --debug   (prints raw rows + samples, no graph)
Ctrl-C to quit.
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque

COUNTER_GLOB = "/sys/fs/resctrl/mon_data/mon_L3_*/mbm_total_bytes"
BLOCKS = " ▁▂▃▄▅▆▇█"
RESET = "\033[0m"

# metrics requested from perf; identified downstream by their unit
PERF_METRICS = "umc_mem_bandwidth,umc_data_bus_utilization"
CLOCK_EVENT = "amd_umc/umc_mem_clk/"
_UNITS = ("%", "TB/s", "GB/s", "MB/s", "KB/s", "B/s")

DEBUG = False

# Sample intervals reachable with +/- at runtime. Off-ladder values from the
# command line still work; stepping just snaps to the nearest rung first.
INTERVAL_LADDER = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0]


def fmt_dur(seconds):
    """Compact human duration: 47s, 4m12s, 1h03m."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def parse_interval(text):
    """Accept 250ms, 0.5s, or a bare number of seconds."""
    s = str(text).strip().lower()
    try:
        if s.endswith("ms"):
            v = float(s[:-2]) / 1000.0
        elif s.endswith("s"):
            v = float(s[:-1])
        else:
            v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"bad interval {text!r}; try 250ms, 0.5s or 1")
    if v < 0.02:
        raise argparse.ArgumentTypeError("interval must be at least 20ms")
    return v


class Tick:
    """The sample interval, shared live with whichever source is running.

    Sources read .value (or .ms) every iteration rather than capturing it, so
    a change takes effect without restarting anything. .generation exists for
    the one source that cannot retune in place: perf bakes -I into its argv,
    so gen_perf watches the counter and respawns when it moves.
    """

    def __init__(self, seconds):
        self.value = max(0.02, float(seconds))
        self.generation = 0

    @property
    def ms(self):
        return max(20, int(round(self.value * 1000)))

    def step(self, direction):
        """direction -1 samples faster, +1 samples slower."""
        i = min(range(len(INTERVAL_LADDER)),
                key=lambda k: abs(INTERVAL_LADDER[k] - self.value))
        j = max(0, min(len(INTERVAL_LADDER) - 1, i + direction))
        if abs(INTERVAL_LADDER[j] - self.value) < 1e-9:
            return False
        self.value = INTERVAL_LADDER[j]
        self.generation += 1
        return True


def dbg(msg):
    if DEBUG:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def find_counters():
    return sorted(glob.glob(COUNTER_GLOB))


def read_total(paths):
    total = 0
    for p in paths:
        try:
            with open(p) as f:
                total += int(f.read().strip())
        except (OSError, ValueError):
            pass
    return total


def sat_color(frac):
    s = max(0.0, min(1.0, frac))
    if s <= 0.5:
        t = s / 0.5
        r, g, b = int(255 * t), 255, 0
    else:
        t = (s - 0.5) / 0.5
        r, g, b = 255, int(255 * (1 - t)), 0
    return f"\033[38;2;{r};{g};{b}m"


def hbar(frac, width):
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def _to_gbps(value, unit):
    u = unit.lower()
    if u.startswith("tb"):
        return value * 1e3
    if u.startswith("mb"):
        return value / 1e3
    if u.startswith("kb"):
        return value / 1e6
    if u == "b" or u.startswith("b/"):
        return value / 1e9
    return value  # GB/s or bare number -> assume GB/s


def parse_perf_row(line):
    """Parse one 'perf stat -x <TAB> -M ... -I' row.

    Locale-proof (LC_ALL=C -> '.' decimals) and layout-proof: we tokenize on ANY
    whitespace and locate the metric by its UNIT token, taking the number right
    before it as the value. This works whether perf prints the unit as its own
    field ('... 0.6 % umc_data_bus_utilization') or glued to the metric name
    ('... 513.6 MB/s umc_mem_bandwidth'), and tolerates the trailing CR a pty
    adds. Returns (timestamp, kind, value) with kind in {'bw','util'} and value
    in GB/s (bw) or percent (util); or None if the row carries no metric.
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    toks = s.split()  # collapses tabs and the spaces before the metric name
    if len(toks) < 2:
        return None
    try:
        ts = float(toks[0])
    except ValueError:
        return None
    for i in range(len(toks) - 1, 0, -1):
        if toks[i] in _UNITS:
            try:
                val = float(toks[i - 1])
            except ValueError:
                return None
            unit = toks[i]
            if unit == "%":
                return ts, "util", val
            return ts, "bw", _to_gbps(val, unit)
    return None


# ----------------------------------------------------------------------------
# theoretical upper cap: derived from the machine, never hardcoded
# ----------------------------------------------------------------------------
def cap_from_umc_clock(seconds=1.0):
    """Derive the bus cap from the memory controller's own clock counter.

    Each UMC instance drives one 64-bit (8-byte) channel, and perf sums
    umc_mem_clk across every instance. So the whole derivation is:

        summed_clk_hz  x  2 (double data rate)  x  8 bytes  =  peak bytes/s

    For DDR5-6000 on dual channel that is 6.0e9 x 2 x 8 = 96.0 GB/s, and the
    instance count falls out of the sum for free.
    """
    if shutil.which("perf") is None:
        return None, None
    cmd = ["perf", "stat", "-a", "-x", ",", "-e", CLOCK_EVENT,
           "--", "sleep", str(seconds)]
    env = dict(os.environ, LC_ALL="C")
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           timeout=seconds + 15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        dbg(f"[cap] perf clock probe failed: {exc}")
        return None, None
    elapsed = time.monotonic() - t0
    if elapsed <= 0:
        return None, None
    for line in p.stderr.splitlines():
        parts = line.split(",")
        if len(parts) < 3 or "umc_mem_clk" not in parts[2]:
            continue
        try:
            count = float(parts[0])
        except ValueError:
            continue
        if count <= 0:
            continue
        gbs = count / elapsed * 2.0 * 8.0 / 1e9
        dbg(f"[cap] umc_mem_clk {count:.0f} in {elapsed:.3f}s -> {gbs:.1f} GB/s")
        return gbs, "umc_mem_clk"
    dbg(f"[cap] no umc_mem_clk row; perf said: {p.stderr.strip()[:200]}")
    return None, None


def cap_from_dmidecode():
    """Fallback: sum Configured Memory Speed x Data Width over populated slots."""
    if shutil.which("dmidecode") is None:
        return None, None
    try:
        out = subprocess.run(["dmidecode", "--type", "17"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    total = 0.0
    for block in out.split("Memory Device")[1:]:
        if "No Module Installed" in block:
            continue
        mt = re.search(r"Configured Memory Speed:\s*(\d+)", block)
        dw = re.search(r"Data Width:\s*(\d+)", block)
        if not mt:
            continue
        width_bytes = (float(dw.group(1)) / 8.0) if dw else 8.0
        total += float(mt.group(1)) * 1e6 * width_bytes / 1e9
    if total > 0:
        dbg(f"[cap] dmidecode -> {total:.1f} GB/s")
        return total, "dmidecode"
    return None, None


def detect_cap(probe_seconds=1.0):
    """Best available cap, most trustworthy source first."""
    for fn in (lambda: cap_from_umc_clock(probe_seconds), cap_from_dmidecode):
        cap, src = fn()
        if cap and cap > 0:
            return cap, src
    return None, None


class Scale:
    """Owns the two vertical references the display needs.

    cap           theoretical upper cap of the bus, derived once at startup and
                  continuously cross-checked against the controller's own
                  utilization metric (utilization is bandwidth/cap by
                  definition, so bw / (util/100) recovers the cap exactly).
    observed_max  all-time high-water mark of measured bandwidth. This, not the
                  cap, is what the plot is normalised against, so a full bar
                  always means "as busy as this machine has ever been".
    """

    def __init__(self, cap=None, cap_src="manual", autocap=True):
        self.cap = cap
        self.cap_src = cap_src if cap else "unknown"
        self.autocap = autocap
        self.observed_max = 0.0
        self.note = None
        self._ratios = deque(maxlen=64)
        # avg and observed max describe one shared period, tracked in wall time
        # so it stays honest when the sample interval changes mid-run.
        self.started = None
        self._last_ts = None
        self._area = 0.0      # bandwidth integrated over time
        self._elapsed = 0.0   # seconds actually covered by samples

    @property
    def mean(self):
        return self._area / self._elapsed if self._elapsed > 0 else 0.0

    @property
    def window(self):
        return (time.monotonic() - self.started) if self.started else 0.0

    def reset_stats(self, current=0.0):
        """Forget the run so far; avg, max and the period all restart here."""
        self.observed_max = current or 0.0
        self.started = time.monotonic()
        self._last_ts = self.started
        self._area = 0.0
        self._elapsed = 0.0

    def observe(self, bw, util):
        if bw is not None:
            if bw > self.observed_max:
                self.observed_max = bw
            ts = time.monotonic()
            if self.started is None:
                self.started = ts
            elif self._last_ts is not None:
                dt = ts - self._last_ts
                if 0.0 < dt < 60.0:      # ignore stalls and clock jumps
                    self._area += bw * dt
                    self._elapsed += dt
            self._last_ts = ts
        if not self.autocap or bw is None or util is None or util < 15.0:
            return
        self._ratios.append(bw / (util / 100.0))
        if len(self._ratios) < 8:
            return
        live = sorted(self._ratios)[len(self._ratios) // 2]
        if self.cap is None:
            self.cap, self.cap_src = live, "bus util"
        elif abs(live - self.cap) / self.cap > 0.10:
            self.note = (f"cap {self.cap:.1f} disagrees with bus util "
                         f"({live:.1f} GB/s) - check the source")

    def adopt(self, cap, src):
        if cap and cap > 0:
            self.cap, self.cap_src = cap, src


# ----------------------------------------------------------------------------
# data sources: each yields (bw_gbps_or_None, util_pct_or_None, status_or_None)
# ----------------------------------------------------------------------------
def gen_demo(tick, peak):
    import random
    val = peak * 0.1
    while True:
        time.sleep(tick.value)
        if random.random() < 0.05:
            val = random.uniform(peak * 0.4, peak * 0.95)
        val += random.uniform(-1, 1) * peak * 0.05
        val = max(0.0, min(peak, val))
        util = min(100.0, val / peak * 100.0) if peak else None
        yield val, util, None


def gen_mbm(tick, peak):
    paths = find_counters()
    if not paths:
        raise SystemExit(
            "No MBM counters at " + COUNTER_GLOB + "\n"
            "Mount resctrl:  sudo mount -t resctrl resctrl /sys/fs/resctrl\n"
            "or use:  --source perf"
        )
    prev = read_total(paths)
    prev_t = time.monotonic()
    last_change = prev_t
    last_glitch = 0.0
    while True:
        time.sleep(tick.value)
        cur = read_total(paths)
        now_t = time.monotonic()
        dt = now_t - prev_t
        delta = cur - prev
        prev, prev_t = cur, now_t
        if dt <= 0 or delta < 0 or dt > tick.value * 10:
            if delta < 0:
                last_glitch = now_t
            status = None
            if now_t - last_glitch < 4:
                status = "MBM returned garbage - unreliable on this CPU (use --source perf)"
            elif now_t - last_change > 3:
                status = "MBM counter not advancing (use --source perf, or reboot)"
            yield None, None, status
            continue
        gbps = delta / dt / 1e9
        if gbps > peak * 1.5:
            last_glitch = now_t
            yield None, None, "MBM returned garbage - unreliable on this CPU (use --source perf)"
            continue
        if delta > 0:
            last_change = now_t
        status = None
        if now_t - last_change > 3:
            status = "MBM counter not advancing (use --source perf, or reboot)"
        yield gbps, None, status


def gen_perf(tick, peak):
    if shutil.which("perf") is None:
        raise SystemExit("perf not found. Install it:  sudo pacman -S perf")
    import pty
    import fcntl
    import select
    import struct
    import termios

    env = dict(os.environ, LC_ALL="C")
    t0 = time.monotonic()

    # perf bakes -I into its argv, so a live interval change means respawning.
    # The outer loop does exactly that and nothing else.
    while True:
        spawned_at = tick.generation
        ms = max(50, tick.ms)
        # tab separator + LC_ALL=C: machine-readable, locale-proof numbers.
        cmd = ["perf", "stat", "-a", "-M", PERF_METRICS, "-I", str(ms), "-x", "\t"]
        dbg(f"[perf] spawning: {cmd!r}")

        # A pty makes perf line-buffer like it does in a real shell (a pipe makes
        # it block-buffer and look hung). Wide window so nothing wraps.
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 50, 250, 0, 0))
        except OSError:
            pass
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=slave_fd, close_fds=True, env=env,
            )
        except OSError as e:
            os.close(master_fd)
            os.close(slave_fd)
            raise SystemExit(f"could not start perf: {e}")
        os.close(slave_fd)
        dbg(f"[perf] pid={proc.pid} interval={ms}ms")

        group = {}
        last_ts = None
        buf = b""
        respawn = False
        try:
            while True:
                # Short poll instead of a blocking read, so an interval change
                # is noticed within 200ms rather than at the next sample.
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if tick.generation != spawned_at:
                    dbg(f"[perf] interval changed -> respawn at {tick.ms}ms")
                    respawn = True
                    break
                if not ready:
                    continue
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""  # child closed its end -> perf exited
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    rawb, buf = buf.split(b"\n", 1)
                    raw = rawb.decode("utf-8", "replace")
                    dbg(f"[{time.monotonic()-t0:6.2f}s] raw: {raw!r}")
                    low = raw.lower()
                    if ("permission" in low or "paranoid" in low
                            or "not permitted" in low or "access denied" in low
                            or "consider tweaking" in low):
                        proc.kill()
                        raise SystemExit(
                            "perf lacks access to counters. Options:\n"
                            "  - run the collector with CAP_PERFMON (see the .service unit), or\n"
                            "  - run as root, or\n"
                            "  - sudo sysctl kernel.perf_event_paranoid=0\n" + raw.strip()
                        )
                    p = parse_perf_row(raw)
                    dbg(f"          parsed: {p}")
                    if p is None:
                        continue
                    ts, kind, value = p
                    if last_ts is not None and ts != last_ts:
                        group = {}
                    last_ts = ts
                    group[kind] = value
                    if "bw" in group and "util" in group:
                        yield group["bw"], group["util"], None
                        group = {}
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                pass
        if not respawn:
            raise SystemExit(f"perf exited (code {proc.poll()}).")


def gen_file(tick, path, scale=None):
    """Read samples written by a --serve collector. Unprivileged.

    Polls at the local tick but only emits genuinely new readings. Without the
    dedup, a GUI sampling faster than the collector would plot the same value
    several times and invent detail the data does not contain.
    """
    last_ts = None
    while True:
        time.sleep(tick.value)
        try:
            st = os.stat(path)
            with open(path) as fp:
                rec = json.load(fp)
        except FileNotFoundError:
            yield None, None, f"no collector file at {path} (is the collector running?)"
            continue
        except PermissionError:
            yield None, None, f"cannot read {path} (permission denied - not world-readable?)"
            continue
        except (OSError, ValueError):
            yield None, None, "collector file unreadable"
            continue
        if scale is not None and rec.get("cap"):
            scale.adopt(rec["cap"], rec.get("cap_src", "collector"))
        status = rec.get("status")
        age = time.time() - st.st_mtime
        if age > max(3 * tick.value, 3):
            # Always emit while stale, or a dead collector would freeze the GUI
            # on its last frame with no explanation.
            yield (rec.get("bw"), rec.get("util"),
                   f"collector data is stale ({age:.0f}s) - collector may be stopped")
            continue
        ts = rec.get("ts")
        if ts is not None and ts == last_ts:
            continue
        last_ts = ts
        yield rec.get("bw"), rec.get("util"), status


def make_source(args, tick, scale=None):
    fallback = (scale.cap if scale and scale.cap else None) or args.cap or 96.0
    if args.source == "demo":
        return gen_demo(tick, fallback)
    if args.source == "perf":
        return gen_perf(tick, fallback)
    if args.source == "file":
        return gen_file(tick, args.file, scale)
    return gen_mbm(tick, fallback)


# ----------------------------------------------------------------------------
# collector (writes latest sample atomically to a file)
# ----------------------------------------------------------------------------
def run_serve(gen, path, frames=0, scale=None):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    sys.stderr.write(f"[collector] writing samples to {path}\n")
    sys.stderr.flush()
    n = 0
    for bw, util, status in gen:
        if scale is not None:
            scale.observe(bw, util)
        rec = {"bw": bw, "util": util, "ts": time.time(), "status": status}
        if scale is not None and scale.cap:
            rec["cap"] = scale.cap
            rec["cap_src"] = scale.cap_src
        with open(tmp, "w") as fp:
            json.dump(rec, fp)
        os.chmod(tmp, 0o644)   # world-readable so the unprivileged GUI can read it
        os.replace(tmp, path)  # atomic swap; readers never see a partial file
        n += 1
        if frames and n >= frames:
            break


# ----------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------
def build_frame(history, scale, interval, cols, rows, use_color,
                latest_util=None, status=None, keys=False):
    def c(frac):
        return sat_color(frac) if use_color else ""

    def rst():
        return RESET if use_color else ""

    bws = [b for (b, _u) in history]
    now = bws[-1] if bws else 0.0
    wavg = scale.mean or now

    cap = scale.cap
    # The plot is normalised against the running high-water mark, not the cap,
    # so a full bar always reads as "as busy as this machine has ever been".
    top = scale.observed_max if scale.observed_max > 0 else max(cap or 0.0, 1e-3)
    rel = min(1.0, now / top) if top else 0.0

    # Three header rows, each pairing a live value on the left with the
    # reference it should be read against on the right. Ordered so the two
    # slowest-moving numbers sit at the top and the bar sits nearest the plot.
    lines = []
    src = "measured bus util" if latest_util is not None else "estimated"
    LEFT = 20

    # 6.1f against 7.2f below puts every decimal point in the same column
    left = (f" bus  {latest_util:6.1f}% used" if latest_util is not None
            else f" bus  {'n/a':>6}")
    if cap:
        right = f"theoretical upper cap {cap:7.1f} GB/s   (from {scale.cap_src})"
    else:
        right = "theoretical upper cap       n/a   (no readout available)"
    lines.append(f"{left:<{LEFT}}{right}"[:cols])

    of_cap = f"{scale.observed_max / cap * 100:.1f}% of cap" if cap else "cap unknown"
    left = f" avg  {wavg:7.2f} GB/s"
    right = f"observed max          {scale.observed_max:7.1f} GB/s   ({of_cap})"
    lines.append(f"{left:<{LEFT}}{right}"[:cols])

    lines.append(
        f" now  {c(rel)}{now:7.2f} GB/s{rst()}  "
        f"{c(rel)}{hbar(rel, 22)}{rst()}  {rel*100:5.1f}% of observed max"
    )

    note = status or scale.note
    if note:
        warn = "\033[38;2;255;80;80m" if use_color else ""
        lines.append(f" {warn}! {note}{rst()}")

    header_n = len(lines)
    graph_rows = max(6, rows - header_n - 1 - 1)
    gutter = 7
    graph_w = max(10, cols - gutter - 1)

    vis = list(history)[-graph_w:]
    pad = graph_w - len(vis)
    ceiling = max(top, 1e-3)

    col_e = [0] * pad
    col_c = [""] * pad
    for (b, _u) in vis:
        e = int(round((b / ceiling) * graph_rows * 8))
        col_e.append(max(0, min(graph_rows * 8, e)))
        col_c.append(c(min(1.0, b / ceiling)))

    for r in range(graph_rows - 1, -1, -1):
        if r == graph_rows - 1:
            label = f"{ceiling:6.1f} "
        elif r == graph_rows // 2:
            label = f"{ceiling/2:6.1f} "
        elif r == 0:
            label = f"{0.0:6.1f} "
        else:
            label = " " * gutter
        row = [label]
        base = r * 8
        for i in range(graph_w):
            filled = col_e[i] - base
            filled = 0 if filled < 0 else (8 if filled > 8 else filled)
            ch = BLOCKS[filled]
            if use_color and ch != " ":
                row.append(col_c[i] + ch + RESET)
            else:
                row.append(ch)
        lines.append("".join(row))

    span = len(vis) * interval
    bits = [f"{span:5.1f}s shown", f"{interval*1000:.0f}ms sample", f"[{src}]"]
    if keys:
        bits.append("+/- rate   r reset stats   q quit")
    # The period every stat above is computed over. Last so it reads as the
    # closing qualifier on the whole frame.
    bits.append(f"avg/max over {fmt_dur(scale.window)}")
    lines.append((" " + "   ".join(bits))[:cols])
    return lines


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Live DRAM bandwidth monitor (AMD).")
    ap.add_argument("-s", "--source", choices=["perf", "file", "mbm", "demo"],
                    default="mbm", help="data source (default mbm)")
    ap.add_argument("-i", "--interval", type=parse_interval, default=0.25,
                    metavar="T",
                    help="sample interval, e.g. 250ms / 0.5s / 2 "
                         "(default 250ms; adjust live with +/-)")
    ap.add_argument("-p", "--peak", "--cap", dest="cap", type=float, default=None,
                    help="override the theoretical upper cap in GB/s "
                         "(default: derive it from the machine)")
    ap.add_argument("--no-autocap", action="store_true",
                    help="do not derive or refine the cap; use --cap verbatim")
    ap.add_argument("--cap-probe", type=float, default=1.0,
                    help="seconds to sample the UMC clock when deriving the cap")
    ap.add_argument("--file", default="/run/membw/latest.json",
                    help="path for --source file and --serve (default /run/membw/latest.json)")
    ap.add_argument("--serve", action="store_true",
                    help="collector mode: write samples to --file instead of drawing")
    ap.add_argument("--no-color", action="store_true", help="disable color")
    ap.add_argument("--demo", action="store_true", help="alias for --source demo")
    ap.add_argument("--debug", action="store_true",
                    help="print raw source lines and samples instead of the graph")
    ap.add_argument("--frames", type=int, default=0, help="exit after N samples (0=forever)")
    args = ap.parse_args()

    if args.demo:
        args.source = "demo"

    global DEBUG
    DEBUG = args.debug

    autocap = not args.no_autocap
    scale = Scale(cap=args.cap, cap_src="manual", autocap=autocap)
    # The file source inherits the collector's cap; everything else derives it.
    if autocap and scale.cap is None and args.source != "file":
        cap, csrc = detect_cap(args.cap_probe)
        scale.adopt(cap, csrc)

    tick = Tick(args.interval)

    # collector mode
    if args.serve:
        gen = make_source(args, tick, scale)
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        run_serve(gen, args.file, args.frames, scale)
        return

    gen = make_source(args, tick, scale)

    # debug mode: no screen control, just stream
    if args.debug:
        sys.stderr.write(f"[debug] source={args.source} interval={tick.value}s\n")
        sys.stderr.flush()
        t0 = time.monotonic()
        try:
            for bw, util, status in gen:
                sys.stderr.write(
                    f"[{time.monotonic()-t0:6.2f}s] bw={bw} util={util} status={status}\n"
                )
                sys.stderr.flush()
        except KeyboardInterrupt:
            pass
        return

    # gui mode
    use_color = not args.no_color and sys.stdout.isatty()
    it = iter(gen)
    first = next(it)  # surfaces setup errors before touching the terminal

    history = deque(maxlen=8192)
    latest_util = None

    # Live controls, only when we actually own a terminal. cbreak gives us
    # unbuffered single keys without taking over the tty completely.
    keys_on = False
    tty_saved = None
    try:
        import termios as _termios
        import tty as _tty
        if sys.stdin.isatty():
            tty_saved = _termios.tcgetattr(sys.stdin.fileno())
            _tty.setcbreak(sys.stdin.fileno())
            keys_on = True
    except Exception:
        keys_on, tty_saved = False, None

    def untty():
        if tty_saved is not None:
            try:
                _termios.tcsetattr(sys.stdin.fileno(), _termios.TCSADRAIN, tty_saved)
            except Exception:
                pass

    def read_keys():
        """Apply any pending keypresses. Returns False if the user quit."""
        if not keys_on:
            return True
        import select as _select
        while _select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            if not ch or ch in "qQ\x03\x04":
                return False
            if ch in "+=":
                tick.step(-1)
            elif ch in "-_":
                tick.step(+1)
            elif ch in "rR":
                scale.reset_stats(history[-1][0] if history else 0.0)
        return True

    sys.stdout.write("\033[2J\033[?25l")
    sys.stdout.flush()

    def restore(*_):
        untty()
        sys.stdout.write("\033[?25h\033[0m\n")
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, restore)
    signal.signal(signal.SIGTERM, restore)

    frame = 0
    sample = first
    try:
        while True:
            bw, util, status = sample
            if util is not None:
                latest_util = util
            if bw is not None:
                history.append((bw, latest_util))
            scale.observe(bw, latest_util)
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            lines = build_frame(history, scale, tick.value,
                                cols, rows, use_color, latest_util, status,
                                keys_on)
            buf = ["\033[H"]
            for ln in lines:
                buf.append(ln + "\033[K\n")
            buf.append("\033[J")
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            frame += 1
            if args.frames and frame >= args.frames:
                break
            if not read_keys():
                break
            sample = next(it)
    except StopIteration:
        pass
    finally:
        untty()
        sys.stdout.write("\033[?25h\033[0m\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
