#!/usr/bin/env python3
"""Prometheus exporter for the dbx DriveRack PA2 loudspeaker management system.

Connects to the PA2's line-based TCP control protocol (port 19272), subscribes
to meters and settings, and exposes them as Prometheus metrics.

Fluid metrics (levels, gain reduction) carry a `stat` label with min/avg/max
computed over a trailing rolling window (default 15 s, sized to the scrape
interval), so short peaks between scrapes are never lost. Averages are computed
in the dB domain. State metrics (mutes, preset, enabled flags) are plain
gauges; transient events (clips, settings changes) are counters.

Read-only: never sends `set` commands.

Configuration (env var / flag):
  PA2_HOST / --host                PA2 address (required)
  PA2_PORT / --port                PA2 TCP port (default 19272)
  PA2_PASSWORD / --password        administrator password (default "administrator")
  PA2_PASSWORD_FILE                read the password from a file (Docker/K8s secrets)
  PA2_EXPORTER_PORT / --listen-port  metrics listen port (default 10048)
  PA2_WINDOW_SECONDS / --window    rolling stats window (default 15)
"""

import argparse
import os
import re
import signal
import socket
import socketserver
import threading
import time
from collections import defaultdict, deque
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import make_wsgi_app
from prometheus_client.core import (
    REGISTRY,
    CounterMetricFamily,
    GaugeMetricFamily,
)

DEFAULT_PORT = 19272
DEFAULT_PASSWORD = "administrator"
DEFAULT_EXPORTER_PORT = 10048
DEFAULT_WINDOW = 15.0

BANDS = ("High", "Mid", "Low")
CHANNELS = ("Left", "Right")

# Fluid metrics: path -> (kind, label values). Windowed min/avg/max.
FLUID = {}
# State gauges: path -> (kind, label values, settings-change module or None).
STATE = {}

for _ch in CHANNELS:
    FLUID[rf"\\Preset\InputMeters\SV\{_ch}Input"] = ("input_level", (_ch.lower(),))
for _b in BANDS:
    for _ch in CHANNELS:
        FLUID[rf"\\Preset\OutputMeters\SV\{_b}{_ch}Output"] = (
            "output_level", (_b.lower(), _ch.lower()))
        STATE[rf"\\Preset\OutputGains\SV\{_b}{_ch}OutputGain"] = (
            "output_gain", (_b.lower(), _ch.lower()), "OutputGains")
        STATE[rf"\\Preset\OutputGains\SV\{_b}{_ch}OutputMute"] = (
            "output_muted", (_b.lower(), _ch.lower()), "OutputGains")
    FLUID[rf"\\Preset\{_b} Outputs Limiter\SV\GainReductionMeter"] = (
        "limiter_gr", (_b.lower(),))
    STATE[rf"\\Preset\{_b} Outputs Limiter\SV\ThresholdMeter"] = (
        "limiter_state", (_b.lower(),), None)
    STATE[rf"\\Preset\{_b} Outputs Limiter\SV\Limiter"] = (
        "limiter_enabled", (_b.lower(),), "Limiter")

FLUID[r"\\Preset\Compressor\SV\GainReductionMeter"] = ("compressor_gr", ())
STATE[r"\\Preset\Compressor\SV\ThresholdMeter"] = ("compressor_state", (), None)
STATE[r"\\Preset\Compressor\SV\Compressor"] = ("compressor_enabled", (), "Compressor")
STATE[r"\\Preset\Afs\SV\AFS"] = ("afs_enabled", (), "Afs")
STATE[r"\\Preset\Afs\SV\NumFilters"] = ("afs_filters", (), None)
STATE[r"\\Storage\Presets\SV\Changed"] = ("preset_modified", (), "Presets")

CLIPS = {rf"\\Preset\InputMeters\SV\{_ch}InputClip": _ch.lower() for _ch in CHANNELS}
PRESET_PATH = r"\\Storage\Presets\SV\CurrentPreset"

SUB_PATHS = list(FLUID) + list(STATE) + list(CLIPS) + [PRESET_PATH]

INFO_GETS = [r"\\Node\AT\Instance_Name", r"\\Node\AT\Software_Version"]

QUOTED = re.compile(r'"([^"]*)"')


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


class State:
    """Shared metric state; every access goes through `lock`."""

    def __init__(self, window):
        self.lock = threading.Lock()
        self.window = window
        self.connected = False
        self.reconnects = 0
        self.last_push = None
        self.windows = defaultdict(deque)   # (kind, labels) -> deque[(ts, val)]
        self.fluid_last = {}                # (kind, labels) -> last pushed val
        self.gauges = {}                    # (kind, labels) -> val
        self.clip_total = {ch.lower(): 0 for ch in CHANNELS}
        self.clip_last = {}
        self.settings_changes = defaultdict(int, {
            m: 0 for m in ("OutputGains", "Limiter", "Compressor", "Afs", "Presets")})
        self.preset_changes = 0
        self.preset_number = None
        self.preset_name = None
        self.instance_name = None
        self.firmware = None


class PA2Reader(threading.Thread):
    """Owns the PA2 TCP session; reconnects forever with backoff."""

    daemon = True

    def __init__(self, state, host, port, password):
        super().__init__(name="pa2-reader")
        self.state = state
        self.host = host
        self.port = port
        self.password = password

    def run(self):
        backoff = 5
        while True:
            try:
                self.session()
            except Exception as exc:  # noqa: BLE001 - reader must outlive any
                # session failure: socket errors, protocol surprises, a device
                # rebooting mid-push. Dying here would leave a live exporter
                # permanently reporting pa2_up 0.
                log(f"session error: {exc!r}, reconnecting in {backoff}s")
            with self.state.lock:
                if self.state.connected:
                    self.state.reconnects += 1
                self.state.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def session(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        try:
            self.sock = sock
            self.buf = b""
            greeting = self.read_line(10.0)
            if greeting is None or "HiQnet" not in greeting:
                raise ConnectionError(f"unexpected greeting: {greeting!r}")
            self.send_line(f'connect administrator "{self.password}"')
            reply = self.read_line(10.0)
            if reply is None or "logged in" not in reply:
                raise ConnectionError(f"login failed: {reply!r}")
            for path in INFO_GETS:
                self.send_line(f'get "{path}"')
            for path in SUB_PATHS:
                self.send_line(f'sub "{path}\\*"')
            with self.state.lock:
                self.state.connected = True
            log(f"connected to {self.host}:{self.port}")
            while True:
                # Meters push constantly; a long silence means a dead session.
                line = self.read_line(60.0)
                if line is None:
                    raise ConnectionError("no data for 60s")
                self.handle_line(line)
        finally:
            sock.close()

    def send_line(self, line):
        self.sock.sendall(line.encode("ascii") + b"\r\n")

    def read_line(self, timeout):
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        return raw.decode("ascii", errors="replace").rstrip("\r")

    def handle_line(self, line):
        cmd, _, rest = line.partition(" ")
        fields = QUOTED.findall(rest)
        if cmd == "get" and len(fields) >= 2:
            self.handle_get(fields[0], fields[1])
        elif cmd in ("subr", "set") and len(fields) >= 3:
            self.handle_push(cmd, fields)

    def handle_get(self, path, value):
        with self.state.lock:
            if path.endswith(r"\Instance_Name"):
                self.state.instance_name = value
            elif path.endswith(r"\Software_Version"):
                self.state.firmware = value
            else:
                m = re.search(r"\\Name_(\d+)$", path)
                if m and int(m.group(1)) == self.state.preset_number:
                    self.state.preset_name = value

    def handle_push(self, cmd, fields):
        path = fields[0]
        path = path.removesuffix(r"\*")
        try:
            val = float(fields[2])
        except ValueError:
            return
        now = time.time()
        fetch_name = None
        st = self.state
        with st.lock:
            st.last_push = now
            if path in FLUID:
                dq = st.windows[FLUID[path]]
                dq.append((now, val))
                st.fluid_last[FLUID[path]] = val
                horizon = now - st.window - 1
                while dq and dq[0][0] < horizon:
                    dq.popleft()
            elif path in CLIPS:
                ch = CLIPS[path]
                if val >= 1 and st.clip_last.get(ch, 0) < 1:
                    st.clip_total[ch] += 1
                st.clip_last[ch] = val
            elif path == PRESET_PATH:
                number = int(val)
                if number != st.preset_number:
                    if cmd == "set":
                        st.preset_changes += 1
                        st.settings_changes["Presets"] += 1
                    st.preset_number = number
                    st.preset_name = None
                    fetch_name = number
            elif path in STATE:
                kind, labels, module = STATE[path]
                st.gauges[(kind, labels)] = val
                if cmd == "set" and module:
                    st.settings_changes[module] += 1
        if fetch_name is not None:
            self.send_line(f'get "\\\\Storage\\Presets\\SV\\Name_{fetch_name}"')


FLUID_FAMILIES = {
    "input_level": ("pa2_input_level_db", "Input level (dBFS)", ["channel"]),
    "output_level": ("pa2_output_level_db",
                     "Output level (dBFS, post-mute)", ["band", "channel"]),
    "limiter_gr": ("pa2_limiter_gain_reduction_db",
                   "Output limiter gain reduction (dB)", ["band"]),
    "compressor_gr": ("pa2_compressor_gain_reduction_db",
                      "Compressor gain reduction (dB)", []),
}

STATE_FAMILIES = {
    "output_gain": ("pa2_output_gain_db", "Output gain setting (dB)",
                    ["band", "channel"]),
    "output_muted": ("pa2_output_muted", "Output mute (1=muted)",
                     ["band", "channel"]),
    "limiter_state": ("pa2_limiter_state",
                      "Limiter threshold state (0=under 1=knee 2=over)", ["band"]),
    "limiter_enabled": ("pa2_limiter_enabled", "Limiter enabled (1=on)", ["band"]),
    "compressor_state": ("pa2_compressor_state",
                         "Compressor threshold state (0=under 1=knee 2=over)", []),
    "compressor_enabled": ("pa2_compressor_enabled", "Compressor enabled (1=on)", []),
    "afs_enabled": ("pa2_afs_enabled", "AFS feedback suppression enabled (1=on)", []),
    "afs_filters": ("pa2_afs_filters_active", "AFS filters currently set", []),
    "preset_modified": ("pa2_preset_modified",
                        "Preset has unsaved changes (1=modified)", []),
}


class PA2Collector:
    def __init__(self, state):
        self.state = state

    def collect(self):
        st = self.state
        now = time.time()
        with st.lock:
            connected = st.connected
            reconnects = st.reconnects
            last_push = st.last_push
            # Quiet meters (e.g. gain reduction parked at 0) stop pushing;
            # fall back to the last pushed value so series never vanish.
            windows = {k: ([v for t, v in dq if t >= now - st.window]
                           or [st.fluid_last[k]])
                       for k, dq in st.windows.items()}
            gauges = dict(st.gauges)
            clip_total = dict(st.clip_total)
            settings_changes = dict(st.settings_changes)
            preset_changes = st.preset_changes
            preset_number = st.preset_number
            preset_name = st.preset_name
            instance_name = st.instance_name
            firmware = st.firmware

        yield GaugeMetricFamily(
            "pa2_up", "PA2 session established and authenticated",
            value=1.0 if connected else 0.0)
        c = CounterMetricFamily("pa2_reconnects", "Lost PA2 sessions", labels=[])
        c.add_metric([], reconnects)
        yield c
        if last_push is not None:
            yield GaugeMetricFamily(
                "pa2_last_push_timestamp_seconds",
                "Unix time of the last update pushed by the device",
                value=last_push)
        if instance_name is not None and firmware is not None:
            fam = GaugeMetricFamily(
                "pa2_device_info", "Device identity",
                labels=["instance_name", "firmware"])
            fam.add_metric([instance_name, firmware], 1.0)
            yield fam

        if not connected:
            # Device unreachable: everything below describes device state we can
            # no longer observe. Emitting the last known values would be a lie
            # that alerting rules latch onto, so let those series go stale and
            # leave gaps in graphs. Counters keep their totals (they are
            # cumulative) and are yielded further down regardless.
            yield from self.counters(clip_total, settings_changes,
                                     preset_changes)
            return

        if preset_number is not None:
            yield GaugeMetricFamily(
                "pa2_preset_number", "Currently loaded preset number",
                value=float(preset_number))
            if preset_name is not None:
                fam = GaugeMetricFamily(
                    "pa2_preset_info", "Currently loaded preset",
                    labels=["number", "name"])
                fam.add_metric([str(preset_number), preset_name], 1.0)
                yield fam
        fluid = {}
        for kind, (name, doc, labels) in FLUID_FAMILIES.items():
            fluid[kind] = GaugeMetricFamily(name, doc, labels=labels + ["stat"])
        for (kind, labels), vals in windows.items():
            if not vals:
                continue
            fam = fluid[kind]
            fam.add_metric(list(labels) + ["min"], min(vals))
            fam.add_metric(list(labels) + ["avg"], sum(vals) / len(vals))
            fam.add_metric(list(labels) + ["max"], max(vals))
        yield from fluid.values()

        states = {}
        for kind, (name, doc, labels) in STATE_FAMILIES.items():
            states[kind] = GaugeMetricFamily(name, doc, labels=labels)
        for (kind, labels), val in gauges.items():
            states[kind].add_metric(list(labels), val)
        yield from states.values()

        yield from self.counters(clip_total, settings_changes, preset_changes)

    @staticmethod
    def counters(clip_total, settings_changes, preset_changes):
        c = CounterMetricFamily("pa2_preset_changes", "Preset recalls", labels=[])
        c.add_metric([], preset_changes)
        yield c
        c = CounterMetricFamily("pa2_input_clip", "Input clip events",
                                labels=["channel"])
        for ch, n in sorted(clip_total.items()):
            c.add_metric([ch], n)
        yield c
        c = CounterMetricFamily("pa2_settings_changes",
                                "Settings changed on the device",
                                labels=["module"])
        for module, n in sorted(settings_changes.items()):
            c.add_metric([module], n)
        yield c


class MetricsServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def server_bind(self):
        # BaseHTTPServer's server_bind() calls socket.getfqdn(), which blocks
        # for ~35s per lookup on networks whose DNS drops PTR queries (common
        # on venue/consumer routers). Bind without the reverse lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
        self.setup_environ()


class SilentHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass

    def address_string(self):
        return self.client_address[0]  # skip reverse DNS per request too


def serve_metrics(addr, port):
    httpd = make_server(addr, port, make_wsgi_app(), MetricsServer, SilentHandler)
    threading.Thread(target=httpd.serve_forever, name="metrics-http",
                     daemon=True).start()


def default_password():
    """Password from PA2_PASSWORD_FILE, else PA2_PASSWORD, else the factory one.

    The *_FILE form keeps the secret out of the process environment, where
    `docker inspect` and /proc/<pid>/environ would expose it; it is how Docker
    and Kubernetes secrets are mounted.
    """
    path = os.environ.get("PA2_PASSWORD_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip("\r\n")
    return os.environ.get("PA2_PASSWORD", DEFAULT_PASSWORD)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=os.environ.get("PA2_HOST"),
                    help="PA2 address (env: PA2_HOST)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PA2_PORT", DEFAULT_PORT)),
                    help=f"PA2 TCP port (env: PA2_PORT, default {DEFAULT_PORT})")
    ap.add_argument("--password", default=default_password(),
                    help="administrator password "
                         "(env: PA2_PASSWORD or PA2_PASSWORD_FILE)")
    ap.add_argument("--listen-addr",
                    default=os.environ.get("PA2_EXPORTER_ADDR", "0.0.0.0"),
                    help="metrics bind address (env: PA2_EXPORTER_ADDR, "
                         "default 0.0.0.0)")
    ap.add_argument("--listen-port", type=int,
                    default=int(os.environ.get("PA2_EXPORTER_PORT",
                                               DEFAULT_EXPORTER_PORT)),
                    help="metrics HTTP port (env: PA2_EXPORTER_PORT, "
                         f"default {DEFAULT_EXPORTER_PORT})")
    ap.add_argument("--window", type=float,
                    default=float(os.environ.get("PA2_WINDOW_SECONDS",
                                                 DEFAULT_WINDOW)),
                    help="rolling stats window in seconds "
                         f"(env: PA2_WINDOW_SECONDS, default {DEFAULT_WINDOW:g})")
    args = ap.parse_args()
    if not args.host:
        ap.error("PA2 address required: set --host or PA2_HOST")

    state = State(args.window)
    REGISTRY.register(PA2Collector(state))
    PA2Reader(state, args.host, args.port, args.password).start()
    serve_metrics(args.listen_addr, args.listen_port)
    log(f"exporter listening on {args.listen_addr}:{args.listen_port}, "
        f"PA2 at {args.host}:{args.port}, window {args.window:g}s")

    # `docker stop` sends SIGTERM; without a handler the default disposition
    # kills us mid-scrape and exits 143. Wake up and leave cleanly instead.
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    log("shutting down")


if __name__ == "__main__":
    main()
