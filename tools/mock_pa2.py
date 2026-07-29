#!/usr/bin/env python3
"""A fake dbx DriveRack PA2, for developing against without hardware.

Speaks enough of the device's console protocol to drive the exporter for
real: greeting, authentication, `get`, `sub`, and a continuous stream of
`set` pushes. The simulated show has dynamics — a music-like envelope with
quiet passages, limiters biting on peaks, occasional input clips, preset
recalls and operator tweaks — so dashboards and alert rules can be built and
seen working before anyone touches a live rack.

    python3 tools/mock_pa2.py &
    PA2_HOST=127.0.0.1 PA2_PORT=19998 pa2-exporter

Any password is accepted. Stdlib only; not part of the installed package.
"""

import argparse
import math
import random
import socket
import socketserver
import threading
import time

DEFAULT_PORT = 19998
BANDS = ("High", "Mid", "Low")
CHANNELS = ("Left", "Right")
PRESETS = {66: "MainHall Rock", 12: "Acoustic Night", 3: "Club Night"}

START = time.time()
STATE = {
    "preset": 66,
    "muted": 0.0,
    "afs_filters": 2,
    "gains": {("High", "Left"): 0.0, ("High", "Right"): -1.46,
              ("Mid", "Left"): 0.0, ("Mid", "Right"): 0.0,
              ("Low", "Left"): 0.589, ("Low", "Right"): 0.482},
}


def program_level(t, seed=0.0):
    """A rough music envelope in dBFS: slow swells, beats, occasional gaps."""
    slow = math.sin((t + seed) / 37.0) * 6.0
    beat = max(0.0, math.sin((t + seed) * 2.3)) ** 3 * 9.0
    gap = -25.0 if (int(t / 53) % 7) == 3 else 0.0
    return -18.0 + slow + beat + gap + random.gauss(0, 0.8)


def line(cmd, path, display, value):
    """One protocol line. Percent and raw fields are padded, as on the device."""
    return f'{cmd} "{path}\\*" "{display}" "{value}" "50%" "{value}"\r\n'


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.sendall(b"HiQnet Console\r\n")
        sock.settimeout(0.05)
        self.buf = b""
        self.authed = False
        self.last_tweak = time.time()
        while True:
            if not self.pump_commands(sock):
                return
            if not self.authed:
                continue
            if not self.push(sock):
                return
            time.sleep(0.08)

    def pump_commands(self, sock):
        """Read whatever the client sent; False means the peer went away."""
        try:
            chunk = sock.recv(4096)
            if not chunk:
                return False
            self.buf += chunk
        except socket.timeout:
            pass
        except OSError:
            return False

        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            cmd = raw.decode("ascii", "replace").strip()
            try:
                self.dispatch(sock, cmd)
            except OSError:
                return False
        return True

    def dispatch(self, sock, cmd):
        if cmd.startswith("connect administrator"):
            self.authed = True                      # any password is accepted
            sock.sendall(b"connect logged in as administrator\r\n")
        elif cmd.startswith("get "):
            self.handle_get(sock, cmd.split('"')[1])
        elif cmd.startswith("sub "):
            path = cmd.split('"')[1]
            path = path.removesuffix("\\*")
            sock.sendall(self.initial_value(path).encode())

    def handle_get(self, sock, path):
        if path.endswith("Instance_Name"):
            sock.sendall(f'get "{path}" "Main Hall PA"\r\n'.encode())
        elif path.endswith("Software_Version"):
            sock.sendall(f'get "{path}" "1.2.0.1"\r\n'.encode())
        elif "Name_" in path:
            n = int(path.rsplit("_", 1)[1].strip('"'))
            sock.sendall(
                f'get "{path}" "{PRESETS.get(n, f"Preset {n}")}"\r\n'.encode())

    def initial_value(self, path):
        """The `subr` reply carrying a subscription's current value."""
        values = {
            r"\\Storage\Presets\SV\CurrentPreset": (str(STATE["preset"]),
                                                    str(STATE["preset"])),
            r"\\Storage\Presets\SV\Changed": ("0", "0"),
            r"\\Preset\Afs\SV\AFS": ("On", "1"),
            r"\\Preset\Afs\SV\NumFilters": (str(STATE["afs_filters"]),
                                            str(STATE["afs_filters"])),
            r"\\Preset\Compressor\SV\Compressor": ("On", "1"),
            r"\\Preset\Compressor\SV\ThresholdMeter": ("Under", "0"),
        }
        for band in BANDS:
            values[rf"\\Preset\{band} Outputs Limiter\SV\Limiter"] = ("On", "1")
            for ch in CHANNELS:
                gain = STATE["gains"][(band, ch)]
                values[rf"\\Preset\OutputGains\SV\{band}{ch}OutputGain"] = (
                    f"{gain}dB", f"{gain:.6f}")
                values[rf"\\Preset\OutputGains\SV\{band}{ch}OutputMute"] = (
                    "Off", "0")
        display, value = values.get(path, ("0", "0"))
        return line("subr", path, display, value)

    def push(self, sock):
        """Emit one round of meter updates, plus the odd operator action."""
        t = time.time() - START
        out = []

        for ch in CHANNELS:
            level = program_level(t, 0 if ch == "Left" else 1.7)
            out.append(line("set", rf"\\Preset\InputMeters\SV\{ch}Input",
                            f"{level:.1f}dB", f"{level:.6f}"))
            clipping = 1 if level > -3.0 else 0
            out.append(line("set", rf"\\Preset\InputMeters\SV\{ch}InputClip",
                            str(clipping), str(clipping)))

        for band in BANDS:
            trim = {"High": 1.0, "Mid": 0.0, "Low": 2.0}[band]
            peak = max(program_level(t, 0), program_level(t, 1.7)) + trim
            reduction = min(0.0, -(peak + 6.0)) if peak > -6.0 else 0.0
            out.append(line(
                "set", rf"\\Preset\{band} Outputs Limiter\SV\GainReductionMeter",
                f"{reduction:.1f}dB", f"{reduction:.6f}"))
            state = 2.0 if reduction < -3 else (1.0 if reduction < -0.2 else 0.0)
            out.append(line(
                "set", rf"\\Preset\{band} Outputs Limiter\SV\ThresholdMeter",
                ("Over" if state == 2 else "Knee" if state else "Under"),
                f"{state:.6f}"))
            for ch in CHANNELS:
                level = program_level(t, 0 if ch == "Left" else 1.7) + trim
                level += reduction
                if STATE["muted"]:
                    level = -120.0          # post-mute, exactly like the device
                out.append(line("set",
                                rf"\\Preset\OutputMeters\SV\{band}{ch}Output",
                                f"{level:.1f}dB", f"{level:.6f}"))

        comp = min(0.0, -(program_level(t, 0) + 10.0))
        out.append(line("set", r"\\Preset\Compressor\SV\GainReductionMeter",
                        f"{comp:.1f}dB", f"{comp:.6f}"))

        if time.time() - self.last_tweak > self.server.tweak_interval:
            self.last_tweak = time.time()
            out.extend(self.operator_action())

        try:
            sock.sendall("".join(out).encode())
        except OSError:
            return False
        return True

    def operator_action(self):
        """Somebody touching the device, so audit panels have something to show."""
        out = []
        action = random.choice(["mute", "gain", "preset", "afs"])
        if action == "mute":
            STATE["muted"] = 0.0 if STATE["muted"] else 1.0
            for band in BANDS:
                for ch in CHANNELS:
                    out.append(line(
                        "set", rf"\\Preset\OutputGains\SV\{band}{ch}OutputMute",
                        "On" if STATE["muted"] else "Off",
                        f"{STATE['muted']:.6f}"))
        elif action == "gain":
            key = random.choice(list(STATE["gains"]))
            STATE["gains"][key] = round(random.uniform(-3, 1), 3)
            band, ch = key
            out.append(line(
                "set", rf"\\Preset\OutputGains\SV\{band}{ch}OutputGain",
                f"{STATE['gains'][key]}dB", f"{STATE['gains'][key]:.6f}"))
        elif action == "preset":
            STATE["preset"] = random.choice(list(PRESETS))
            out.append(line("set", r"\\Storage\Presets\SV\CurrentPreset",
                            str(STATE["preset"]), str(STATE["preset"])))
        else:
            STATE["afs_filters"] = random.randint(0, 6)
            out.append(line("set", r"\\Preset\Afs\SV\NumFilters",
                            str(STATE["afs_filters"]),
                            str(STATE["afs_filters"])))
        return out


class MockPA2(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    tweak_interval = 45.0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0", help="bind address")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"listen port (default {DEFAULT_PORT})")
    ap.add_argument("--tweak-interval", type=float, default=45.0,
                    help="seconds between simulated operator actions")
    args = ap.parse_args()

    MockPA2.tweak_interval = args.tweak_interval
    server = MockPA2((args.host, args.port), Handler)
    print(f"mock PA2 listening on {args.host}:{args.port}", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
