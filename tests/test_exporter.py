"""Tests for the PA2 exporter.

No hardware needed: the reader is driven by feeding it protocol lines
recorded from a real PA2 (firmware 1.2.0.1), and its socket is a stub that
only records what the exporter would have sent back.
"""

import logging

import pytest

import pa2_exporter as ex


class FakeSocket:
    """Captures sent lines; the exporter only writes during these tests."""

    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data.decode("ascii").rstrip("\r\n"))


@pytest.fixture
def clock(monkeypatch):
    """Controllable time source, so window expiry is testable without sleeping."""
    now = [1_000_000.0]
    monkeypatch.setattr(ex.time, "time", lambda: now[0])
    return now


@pytest.fixture
def reader(clock):
    state = ex.State(window=15.0)
    state.connected = True    # collect() emits device state only while up
    r = ex.PA2Reader(state, "192.0.2.10", 19272, "secret")
    r.sock = FakeSocket()
    return r


def collect(state):
    """Flatten a collector pass into {(metric, frozenset(labels)): value}."""
    out = {}
    for fam in ex.PA2Collector(state).collect():
        for sample in fam.samples:
            out[(sample.name, frozenset(sample.labels.items()))] = sample.value
    return out


def value(state, metric, /, **labels):
    # Positional-only: label names like `name=` would otherwise collide.
    return collect(state)[(metric, frozenset(labels.items()))]


# --- protocol parsing -------------------------------------------------------

def test_subr_line_populates_window(reader):
    reader.handle_line(
        r'subr "\\Preset\InputMeters\SV\LeftInput\*" "-97.7dB" "-97.717674" '
        r'"18.568605%" "-97.717674"')
    assert value(reader.state, "pa2_input_level_db",
                 channel="left", stat="avg") == pytest.approx(-97.717674)


def test_paths_with_spaces_parse(reader):
    """Node names like "High Outputs Limiter" contain spaces; quoting matters."""
    reader.handle_line(
        r'set "\\Preset\High Outputs Limiter\SV\GainReductionMeter\*" "-3.0dB" '
        r'"-3.0" "50%" "-3.0"')
    assert value(reader.state, "pa2_limiter_gain_reduction_db",
                 band="high", stat="max") == pytest.approx(-3.0)


def test_non_numeric_value_is_ignored(reader):
    reader.handle_line(
        r'set "\\Preset\InputMeters\SV\LeftInput\*" "n/a" "n/a" "0%" "n/a"')
    assert reader.state.windows == {}


def test_unknown_path_is_ignored(reader):
    reader.handle_line(r'set "\\Preset\Nonsense\SV\Whatever\*" "1" "1" "100%" "1"')
    assert reader.state.windows == {} and reader.state.gauges == {}


def test_device_info_from_get(reader):
    reader.handle_line(r'get "\\Node\AT\Instance_Name" "My DriveRack"')
    reader.handle_line(r'get "\\Node\AT\Software_Version" "1.2.0.1"')
    assert reader.state.instance_name == "My DriveRack"
    assert reader.state.firmware == "1.2.0.1"


# --- rolling window ---------------------------------------------------------

def push_level(reader, val):
    reader.handle_line(
        rf'set "\\Preset\InputMeters\SV\LeftInput\*" "{val}dB" "{val}" "0%" "{val}"')


def test_min_avg_max_over_window(reader):
    for v in (-20.0, -10.0, -30.0):
        push_level(reader, v)
    st = reader.state
    assert value(st, "pa2_input_level_db", channel="left", stat="min") == -30.0
    assert value(st, "pa2_input_level_db", channel="left", stat="max") == -10.0
    assert value(st, "pa2_input_level_db", channel="left", stat="avg") == -20.0


def test_samples_older_than_window_drop_out(reader, clock):
    push_level(reader, -60.0)
    clock[0] += 20            # older than the 15 s window
    push_level(reader, -10.0)
    st = reader.state
    assert value(st, "pa2_input_level_db", channel="left", stat="min") == -10.0


def test_quiet_meter_falls_back_to_last_value(reader, clock):
    """Meters parked at a constant stop pushing; the series must not vanish."""
    push_level(reader, -12.5)
    clock[0] += 300
    st = reader.state
    assert value(st, "pa2_input_level_db", channel="left", stat="avg") == -12.5


# --- clip counter -----------------------------------------------------------

def push_clip(reader, val):
    reader.handle_line(
        rf'set "\\Preset\InputMeters\SV\LeftInputClip\*" "{val}" "{val}" "0%" "{val}"')


def test_clip_counts_rising_edge_only(reader):
    push_clip(reader, 0)
    push_clip(reader, 1)
    push_clip(reader, 1)      # still clipping — same event
    assert value(reader.state, "pa2_input_clip_total", channel="left") == 1
    push_clip(reader, 0)
    push_clip(reader, 1)      # new event
    assert value(reader.state, "pa2_input_clip_total", channel="left") == 2


def test_clip_counters_start_at_zero(reader):
    """Pre-seeded so rate() works before the first clip ever happens."""
    for ch in ("left", "right"):
        assert value(reader.state, "pa2_input_clip_total", channel=ch) == 0


# --- settings changes -------------------------------------------------------

MUTE = r'\\Preset\OutputGains\SV\HighLeftOutputMute'


def test_initial_subr_is_not_a_settings_change(reader):
    """subr is the initial read-back, not somebody touching the device."""
    reader.handle_line(rf'subr "{MUTE}\*" "On" "1" "100%" "1"')
    assert value(reader.state, "pa2_settings_changes_total", module="OutputGains") == 0
    assert value(reader.state, "pa2_output_muted", band="high", channel="left") == 1


def test_set_counts_as_settings_change(reader):
    reader.handle_line(rf'subr "{MUTE}\*" "On" "1" "100%" "1"')
    reader.handle_line(rf'set "{MUTE}\*" "Off" "0" "0%" "0"')
    st = reader.state
    assert value(st, "pa2_settings_changes_total", module="OutputGains") == 1
    assert value(st, "pa2_output_muted", band="high", channel="left") == 0


# --- preset tracking --------------------------------------------------------

PRESET = r"\\Storage\Presets\SV\CurrentPreset"


def test_preset_recall_counts_and_fetches_name(reader):
    reader.handle_line(rf'subr "{PRESET}\*" "66" "66" "65%" "66"')
    assert reader.state.preset_changes == 0        # initial read-back
    assert any("Name_66" in line for line in reader.sock.sent)

    reader.sock.sent.clear()
    reader.handle_line(rf'set "{PRESET}\*" "12" "12" "12%" "12"')
    st = reader.state
    assert st.preset_changes == 1
    assert value(st, "pa2_preset_number") == 12
    assert st.preset_name is None                  # stale name dropped
    assert any("Name_12" in line for line in reader.sock.sent)

    reader.handle_line(r'get "\\Storage\Presets\SV\Name_12" "Jazz Night"')
    assert value(st, "pa2_preset_info", number="12", name="Jazz Night") == 1


def test_stale_preset_name_reply_is_ignored(reader):
    """A Name_<n> reply that lost the race with a newer recall must not stick."""
    reader.handle_line(rf'subr "{PRESET}\*" "12" "12" "12%" "12"')
    reader.handle_line(r'get "\\Storage\Presets\SV\Name_66" "Old Preset"')
    assert reader.state.preset_name is None


# --- limiter threshold state ------------------------------------------------

THRESHOLD = r"\\Preset\{} Outputs Limiter\SV\ThresholdMeter"
WORDS = {0: "Under", 1: "Knee", 2: "Over"}


def push_threshold(reader, val, band="High"):
    reader.handle_line(
        rf'set "{THRESHOLD.format(band)}\*" "{WORDS[val]}" "{val}" '
        rf'"{val * 50}%" "{val}"')


def test_limiter_state_is_windowed(reader):
    """The device pushes this ~3.5 Hz; a scrape must summarise, not sample."""
    for val in (0, 1, 1, 2, 0):
        push_threshold(reader, val)
    st = reader.state
    assert value(st, "pa2_limiter_state", band="high", stat="min") == 0.0
    assert value(st, "pa2_limiter_state", band="high", stat="max") == 2.0
    assert value(st, "pa2_limiter_state", band="high", stat="avg") == 0.8


def test_limiter_state_max_survives_a_transient(reader):
    """A limiter that touched knee between scrapes must not report 'under'.

    This is the bug that made the front-panel lamp a coin flip: the last
    pushed value happened to be 0, but the band was at knee for most of the
    window.
    """
    for val in (1, 1, 1, 1, 0):
        push_threshold(reader, val)
    st = reader.state
    assert value(st, "pa2_limiter_state", band="high", stat="max") == 1.0
    assert value(st, "pa2_limiter_state", band="high", stat="avg") == 0.8


# --- session-scoped state ---------------------------------------------------

def test_reconnect_forgets_paths_the_device_stops_serving(reader):
    """A band the current preset does not use must not linger in the metrics.

    The device answers every `sub` with a `subr`, so whatever a new session
    still serves comes straight back. What it no longer serves — here, the
    mid limiter — must disappear rather than report a value from an earlier
    preset.
    """
    push_threshold(reader, 1, band="Mid")
    reader.handle_line(
        r'subr "\\Preset\Mid Outputs Limiter\SV\Limiter\*" "On" "1" "100%" "1"')
    push_threshold(reader, 1, band="High")
    assert value(reader.state, "pa2_limiter_enabled", band="mid") == 1

    with reader.state.lock:
        reader.state.forget_device_state()
    push_threshold(reader, 2, band="High")      # only high comes back

    snapshot = collect(reader.state)
    bands = {dict(lbls).get("band") for (name, lbls) in snapshot
             if name == "pa2_limiter_state"}
    assert bands == {"high"}
    assert ("pa2_limiter_enabled", frozenset({("band", "mid")})) not in snapshot


def test_reconnect_keeps_cumulative_counters(reader):
    """Counters describe the exporter's history, not the device's state."""
    push_clip(reader, 0)
    push_clip(reader, 1)
    reader.handle_line(rf'set "{MUTE}\*" "On" "1" "100%" "1"')

    with reader.state.lock:
        reader.state.forget_device_state()

    assert value(reader.state, "pa2_input_clip_total", channel="left") == 1
    assert value(reader.state, "pa2_settings_changes_total",
                 module="OutputGains") == 1


# --- connection state -------------------------------------------------------

def test_disconnected_drops_device_state_but_keeps_counters(reader):
    push_level(reader, -20.0)
    reader.handle_line(rf'subr "{MUTE}\*" "On" "1" "100%" "1"')
    reader.handle_line(rf'subr "{PRESET}\*" "66" "66" "65%" "66"')
    assert value(reader.state, "pa2_input_level_db",
                 channel="left", stat="avg") == -20.0

    with reader.state.lock:
        reader.state.connected = False
    snapshot = collect(reader.state)
    names = {name for name, _ in snapshot}
    assert snapshot[("pa2_up", frozenset())] == 0
    # Stale readings would be indistinguishable from live ones on a dashboard.
    assert not {"pa2_input_level_db", "pa2_output_muted", "pa2_preset_number"} & names
    # Counters are cumulative; they must survive the outage.
    assert {"pa2_input_clip_total", "pa2_settings_changes_total",
            "pa2_preset_changes_total", "pa2_reconnects_total"} <= names


# --- password resolution ----------------------------------------------------

def test_password_defaults_to_factory(monkeypatch):
    monkeypatch.delenv("PA2_PASSWORD", raising=False)
    monkeypatch.delenv("PA2_PASSWORD_FILE", raising=False)
    assert ex.default_password() == ex.DEFAULT_PASSWORD


def test_password_from_env(monkeypatch):
    monkeypatch.delenv("PA2_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("PA2_PASSWORD", "from-env")
    assert ex.default_password() == "from-env"


def test_password_file_wins_and_strips_newline(monkeypatch, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("from-file\n")
    monkeypatch.setenv("PA2_PASSWORD", "from-env")
    monkeypatch.setenv("PA2_PASSWORD_FILE", str(secret))
    assert ex.default_password() == "from-file"


# --- HTTP surface -----------------------------------------------------------

def request(app, path):
    """Drive a WSGI app by hand; returns (status, headers dict, body bytes)."""
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(app({"PATH_INFO": path, "REQUEST_METHOD": "GET"},
                        start_response))
    return captured["status"], captured["headers"], body


@pytest.fixture
def app():
    # An empty registry: these tests are about routing, not about content.
    from prometheus_client import CollectorRegistry
    return ex.make_app(CollectorRegistry())


def test_root_serves_a_landing_page(app):
    status, headers, body = request(app, "/")
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/html")
    assert b"/metrics" in body


def test_metrics_path_serves_the_exposition(app):
    status, headers, _ = request(app, "/metrics")
    assert status.startswith("200")
    assert "text/plain" in headers["Content-Type"]


def test_unknown_path_is_not_silently_metrics(app):
    # prometheus_client's own WSGI app answers every path with the exposition,
    # so a typo'd scrape path would succeed and hide the mistake.
    status, _, body = request(app, "/metric")
    assert status.startswith("404")
    assert b"/metrics" in body


# --- version reporting ------------------------------------------------------

def test_build_info_survives_the_device_going_dark(reader):
    with reader.state.lock:
        reader.state.connected = False
    snapshot = collect(reader.state)
    assert snapshot[("pa2_exporter_build_info",
                     frozenset({("version", ex.__version__)}))] == 1.0


def test_version_comes_from_the_running_module(monkeypatch):
    """Not from importlib.metadata, which reports whatever is *installed*.

    A checkout ahead of an older install on sys.path — or just a stale
    *.egg-info in the repo root — used to make --version and build_info
    advertise a release that is not the code being executed.
    """
    import importlib.metadata

    def wrong(_name):
        return "9.9.9-not-what-is-running"

    monkeypatch.setattr(importlib.metadata, "version", wrong)
    assert ex.__version__ != "9.9.9-not-what-is-running"
    assert ex.__version__.strip()


def test_packaging_metadata_matches_the_module():
    """pyproject reads the version back from the module; keep that wired up."""
    from importlib.metadata import version as installed_version
    assert installed_version("pa2_exporter") == ex.__version__


# --- argument parsing under a broken environment ----------------------------

BROKEN_ENV = [
    ("PA2_PASSWORD_FILE", "/nonexistent/pa2-password"),
    ("PA2_PORT", "notanumber"),
    ("PA2_EXPORTER_PORT", "notanumber"),
    ("PA2_WINDOW_SECONDS", "abc"),
    ("PA2_LOG_LEVEL", "bogus"),
]


@pytest.mark.parametrize("var,value", BROKEN_ENV)
@pytest.mark.parametrize("flag", ["--version", "--help"])
def test_diagnostic_flags_survive_a_broken_environment(monkeypatch, capsys,
                                                       var, value, flag):
    """--version and --help are what you reach for when a deploy is broken.

    Resolving these defaults while building the parser made them raise before
    argparse could answer, so the diagnostics failed exactly when needed.
    """
    monkeypatch.setenv("PA2_HOST", "192.0.2.10")
    monkeypatch.setenv(var, value)
    monkeypatch.setattr("sys.argv", ["pa2-exporter", flag])

    with pytest.raises(SystemExit) as exit_info:
        ex.main()

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("var,value", BROKEN_ENV)
def test_broken_environment_fails_with_one_readable_line(monkeypatch, capsys,
                                                         var, value):
    monkeypatch.setenv("PA2_HOST", "192.0.2.10")
    monkeypatch.setenv(var, value)
    monkeypatch.setattr("sys.argv", ["pa2-exporter"])

    with pytest.raises(SystemExit) as exit_info:
        ex.main()

    message = str(exit_info.value.code)
    assert var in message
    assert "Traceback" not in message


def test_flags_win_over_a_broken_environment(monkeypatch):
    """An explicit flag must not be poisoned by a bad env var it overrides."""
    monkeypatch.setenv("PA2_PORT", "notanumber")
    monkeypatch.setenv("PA2_HOST", "192.0.2.10")
    monkeypatch.delenv("PA2_PASSWORD_FILE", raising=False)
    monkeypatch.setattr("sys.argv", ["pa2-exporter", "--port", "19999",
                                     "--version"])

    with pytest.raises(SystemExit) as exit_info:
        ex.main()

    assert exit_info.value.code == 0


# --- debug logging ----------------------------------------------------------

def test_debug_level_traces_the_protocol(reader, caplog):
    """--log-level debug is advertised, and the bug template asks for it."""
    with caplog.at_level(logging.DEBUG, logger="pa2_exporter"):
        reader.send_line('get "\\\\Node\\AT\\Software_Version"')
        reader.handle_line(
            r'subr "\\Preset\InputMeters\SV\LeftInput\*" "-20.0dB" "-20.0" '
            r'"50%" "-20.0"')

    assert "> get" in caplog.text
    assert "< subr" in caplog.text


def test_info_level_stays_quiet(reader, caplog):
    with caplog.at_level(logging.INFO, logger="pa2_exporter"):
        reader.send_line('get "\\\\Node\\AT\\Software_Version"')
    assert caplog.text == ""


def test_debug_never_logs_the_password(reader, caplog):
    """Debug output gets pasted into public issues."""
    with caplog.at_level(logging.DEBUG, logger="pa2_exporter"):
        reader.send_line('connect administrator "hunter2"')

    assert "hunter2" not in caplog.text
    assert "********" in caplog.text
    assert "connect administrator" in caplog.text   # still diagnosable


def test_redact_leaves_ordinary_lines_alone():
    line = r'sub "\\Preset\InputMeters\SV\LeftInput\*"'
    assert ex.redact(line) == line
