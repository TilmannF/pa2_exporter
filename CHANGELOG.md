# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is pre-1.0, metric names and labels may still change in a
minor release; every such change is called out under **Changed** with the query
you need to update.

## [Unreleased]

### Added

- Example alerting rules in `examples/alerts.yml`, covering exporter and device
  availability, a connected-but-silent session, input clipping, sustained
  limiter activity, off-hours settings changes and unsaved front-panel edits.
  Every rule has a `promtool` test in `examples/alerts_test.yml`, negative cases
  included, and CI runs them.
- `examples/prometheus.yml`, a minimal scrape configuration that loads the
  rules, checked by `promtool check config` in CI so the README snippet cannot
  drift into something that does not load.
- `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, issue templates and a pull
  request template.
- Tagging a release now creates the GitHub release from that version's
  changelog section.
- `pa2_exporter_build_info{version}`, reported even while the device is
  unreachable — that is exactly when somebody needs to know which version is
  running.
- `--version`, and a landing page at `/` linking to `/metrics`.
- `--log-level` / `PA2_LOG_LEVEL` (`debug`, `info`, `warning`, `error`).

### Changed

- Log lines now carry a level: `2026-08-29T14:03:37 INFO connected to ...`.
  The timestamp format is unchanged.
- **Only `/metrics` serves the exposition now.** `prometheus_client`'s WSGI app
  answers every path with the full metrics text, so `/` used to return a wall
  of text in a browser and a typo'd scrape path used to succeed silently. Other
  paths now return 404. If you scrape something other than `/metrics`, fix the
  path.

## [0.3.0] — 2026-08-08

### Changed

- **Default metrics port moved from 10048 to 10049.** 10049 is this exporter's
  allocation in the [Prometheus default port allocation registry][ports]; 10048
  was free when 0.2.x shipped but has since been allocated to another exporter.

  *Upgrade note:* if you deployed 0.2.1 or earlier, either pin
  `PA2_EXPORTER_PORT=10048` or move your scrape target when you upgrade.

[ports]: https://github.com/prometheus/prometheus/wiki/Default-port-allocations

## [0.2.1] — 2026-08-08

### Added

- The release workflow can be dry-run on demand, so an action upgrade is
  testable before a release depends on it.

### Changed

- CI now also runs the tests on the Python version the container image ships,
  so a base-image bump cannot ship an untested interpreter.
- The Grafana provisioning example uses a service account token rather than
  basic auth.
- Container base image moved to `python:3.14-alpine`.

## [0.2.0] — 2026-08-08

### Added

- Grafana dashboard covering levels, dynamics, routing and the settings audit
  trail, including a front-panel row that mirrors the hardware.
- `tools/mock_pa2.py`, a fake PA2 that speaks the console protocol with
  show-like dynamics, so the exporter and dashboard can be driven with no
  hardware present.
- Images are published to Docker Hub alongside GHCR.

### Changed

- **Breaking: `pa2_limiter_state` and `pa2_compressor_state` now carry a `stat`
  label.** Queries need `stat="max"` for the last-known state. The device's
  `ThresholdMeter` pushes at ~3.5 Hz, so storing it as a plain gauge kept one
  arbitrary sample per scrape; it now goes through the same rolling window as
  the level meters, and `stat="avg"` becomes a real measure of how hard a
  limiter is working.
- Session state starts from a clean slate on every reconnect. Blocks that a
  preset does not use (the mid limiter under a 2-way preset) send nothing at
  all, and their values from an earlier session were lingering indefinitely.
  Cumulative counters still survive a reconnect.

### Fixed

- `pa2_limiter_gain_reduction_db` is documented as unreliable and no longer
  graphed. The device's `GainReductionMeter` is latched: it answers the initial
  subscription and then never updates, by push or by poll. The metric is kept
  because it is what the device reports, but nothing should be built on it.

## [0.1.0] — 2026-07-28

### Added

- Initial release. Prometheus exporter for the dbx DriveRack PA2, verified
  against firmware 1.2.0.1.
- Read-only by design: the exporter only ever sends `connect`, `get`, `ls` and
  `sub`, never `set`.
- Rolling-window `min`/`avg`/`max` statistics for the fluid meters, so 13 Hz
  meter pushes are not aliased away by a 15 s scrape.
- Multi-arch container image (amd64 + arm64) published to GHCR on a version
  tag, plus a compose example.
- `pa2_poc.py`, a stdlib-only protocol explorer for finding parameter paths on
  your own unit.
- CI: ruff, pytest across the supported Python range, `gitleaks` over full
  history, and a container smoke test against an unreachable device.

[Unreleased]: https://github.com/TilmannF/pa2_exporter/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/TilmannF/pa2_exporter/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/TilmannF/pa2_exporter/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TilmannF/pa2_exporter/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/TilmannF/pa2_exporter/releases/tag/v0.1.0
