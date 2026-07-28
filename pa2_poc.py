#!/usr/bin/env python3
"""Proof of concept: talk to dbx DriveRack PA2 over its TCP control protocol.

Protocol is line-based ASCII on TCP port 19272 (community reverse-engineered,
https://gist.github.com/ForsakenHarmony/8526cbf73e9bea9cf9811490fb743fc9).
Read-only: only connect/get/ls/sub are used, never set.

Configuration via env vars (PA2_HOST, PA2_PORT, PA2_PASSWORD) or CLI flags.

Usage:
  PA2_HOST=192.0.2.10 python3 pa2_poc.py   # auth, device info, subscribe + print
  python3 pa2_poc.py --host 192.0.2.10 --explore   # dump tree
"""

import argparse
import os
import socket
import sys
import time

DEFAULT_PORT = 19272
DEFAULT_PASSWORD = "administrator"  # device factory default

INFO_PATHS = [
    r"\\Node\AT\Class_Name",
    r"\\Node\AT\Instance_Name",
    r"\\Node\AT\Software_Version",
]

# Subscriptions are per-parameter: "<node>\SV\<name>\*".
# Initial value comes back as a "subr" line; subsequent live updates are
# pushed as "set" lines (~10 Hz per meter, event-driven for settings).
SUB_PATHS = [
    r"\\Storage\Presets\SV\CurrentPreset\*",
    r"\\Preset\InputMeters\SV\LeftInput\*",
    r"\\Preset\InputMeters\SV\RightInput\*",
    r"\\Preset\InputMeters\SV\LeftInputClip\*",
    r"\\Preset\InputMeters\SV\RightInputClip\*",
    r"\\Preset\OutputMeters\SV\HighLeftOutput\*",
    r"\\Preset\OutputMeters\SV\MidLeftOutput\*",
    r"\\Preset\OutputMeters\SV\LowLeftOutput\*",
    r"\\Preset\OutputGains\SV\HighLeftOutputMute\*",
    r"\\Preset\High Outputs Limiter\SV\GainReductionMeter\*",
    r"\\Preset\High Outputs Limiter\SV\ThresholdMeter\*",
    r"\\Preset\Compressor\SV\GainReductionMeter\*",
    r"\\Preset\Afs\SV\NumFilters\*",
]

EXPLORE_ROOTS = [
    r"\\Node",
    r"\\Preset",
    r"\\Storage",
]


def log(direction, line):
    print(f"{time.strftime('%H:%M:%S')} {direction} {line}", flush=True)


class PA2Connection:
    def __init__(self, host, port, timeout=5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""

    def send_line(self, line):
        log(">>", line)
        self.sock.sendall(line.encode("ascii") + b"\r\n")

    def read_line(self, timeout=5.0):
        """Return next line from device, or None on timeout. Logs raw lines."""
        self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("PA2 closed connection")
            self.buf += chunk
        raw, self.buf = self.buf.split(b"\n", 1)
        line = raw.decode("ascii", errors="replace").rstrip("\r")
        log("<<", line)
        return line

    def drain(self, timeout=1.0):
        """Read and log lines until quiet for `timeout` seconds."""
        lines = []
        while True:
            line = self.read_line(timeout=timeout)
            if line is None:
                return lines
            lines.append(line)

    def login(self, password):
        greeting = self.read_line()
        if greeting is None or "HiQnet" not in greeting:
            print(f"warning: unexpected greeting: {greeting!r}", flush=True)
        self.send_line(f'connect administrator "{password}"')
        reply = self.read_line()
        if reply is None or "logged in" not in reply:
            sys.exit(f"login failed: {reply!r}")


def device_info(conn):
    for path in INFO_PATHS:
        conn.send_line(f"get {path}")
        conn.read_line()


def explore(conn, paths=None):
    for root in paths or EXPLORE_ROOTS:
        conn.send_line(f'ls "{root}"')
        conn.drain(timeout=2.0)


def monitor(conn):
    for path in SUB_PATHS:
        conn.send_line(f'sub "{path}"')
    conn.drain(timeout=2.0)
    print("--- subscribed, waiting for changes (Ctrl-C to quit) ---", flush=True)
    while True:
        conn.read_line(timeout=60.0)


def main():
    ap = argparse.ArgumentParser(description="dbx DriveRack PA2 monitor PoC")
    ap.add_argument("--host", default=os.environ.get("PA2_HOST"),
                    help="PA2 address (env: PA2_HOST)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PA2_PORT", DEFAULT_PORT)),
                    help=f"PA2 TCP port (env: PA2_PORT, default {DEFAULT_PORT})")
    ap.add_argument("--password",
                    default=os.environ.get("PA2_PASSWORD", DEFAULT_PASSWORD),
                    help="administrator password (env: PA2_PASSWORD, "
                         f"default {DEFAULT_PASSWORD!r})")
    ap.add_argument("--explore", action="store_true",
                    help="dump object tree instead of monitoring")
    ap.add_argument("--ls", nargs="+", metavar="PATH",
                    help="explore specific paths instead of default roots")
    args = ap.parse_args()
    if not args.host:
        ap.error("PA2 address required: set --host or PA2_HOST")

    print(f"connecting to {args.host}:{args.port} ...", flush=True)
    conn = PA2Connection(args.host, args.port)
    conn.login(args.password)
    device_info(conn)
    if args.explore or args.ls:
        explore(conn, args.ls)
    else:
        monitor(conn)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
