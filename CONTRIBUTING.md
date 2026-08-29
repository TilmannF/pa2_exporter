# Contributing to pa2_exporter

Issues and pull requests are welcome. This is a small project built around one
piece of hardware in one venue, so the most valuable contributions are usually
the ones that widen what we know about the device — not necessarily code.

## Especially wanted

- **Results from other firmware versions or DriveRack models.** The exporter is
  verified against a PA2 on firmware 1.2.0.1 and nothing else. If yours differs,
  open a *Hardware report* issue; `python3 pa2_poc.py --explore` prints the
  parameter tree, and that output alone is useful even if nothing works.
  VENU360 owners in particular: see the *Other DriveRack models* section of the
  README for what is expected to port and what is not.
- **Parameter paths this exporter does not expose yet.**
- **Grafana dashboards and alert rules.**

## The one rule

**Keep the read-only guarantee intact: no `set` commands, ever.** The exporter
only sends `connect`, `get`, `ls` and `sub`. It runs against live PA systems
during shows, where a monitoring tool that can change a tuning, a mute or a
preset is a monitoring tool that will eventually ruin somebody's night. A pull
request that adds a write path will not be merged, however well guarded.

## Never commit a device password

CI runs `gitleaks` over the full history, but that is the backstop, not the
defence. Use `PA2_PASSWORD_FILE` for deployments, keep the file out of version
control, and use RFC 5737 documentation addresses (`192.0.2.x`) in anything you
write down.

## Development loop

```bash
pip install -e ".[dev]"
pytest              # the full suite, no hardware required
ruff check .
```

Tests drive the protocol reader against a stub socket using lines recorded from
a real PA2, so the whole suite runs offline.

### Working without a PA2

[`tools/mock_pa2.py`](tools/mock_pa2.py) is a fake device that speaks the
console protocol well enough to drive the exporter and the dashboard for real:

```bash
python3 tools/mock_pa2.py &
PA2_HOST=127.0.0.1 PA2_PORT=19998 pa2-exporter
```

It simulates a show rather than idling, so every panel and alert has something
to display.

If you extend the mock, **copy what the hardware actually does, including the
awkward parts**. `GainReductionMeter` is latched in the mock because it is
latched on the device; an earlier version pushed a plausible-looking animated
value and taught us a model the real PA2 does not honour. A mock that is nicer
than the hardware is worse than no mock.

### Alert rules

```bash
docker run --rm -v "$PWD:/w" -w /w prom/prometheus:v3.1.0 \
    promtool test rules examples/alerts_test.yml
```

Every rule in `examples/alerts.yml` has a test. If you add a rule, add its test
— CI runs the same command.

## Sign your commits

This repository enforces the [Developer Certificate of Origin](DCO) — a short
statement that you wrote the patch, or otherwise have the right to submit it
under this project's licence. It is not a copyright assignment and it does not
give anyone rights over your work; you keep those.

Certify it by adding a `Signed-off-by` line to every commit, which `git` writes
for you with `-s`:

```bash
git commit -s -m "Your message"
```

```
Signed-off-by: Your Name <your@email.example>
```

The name and address must be real and must match the commit author. A bot
checks every pull request and blocks it if any commit is missing the line.

Forgot on the last commit:

```bash
git commit --amend --signoff
```

Forgot across a branch — sign off the last N commits, then force-push:

```bash
git rebase HEAD~N --signoff
git push --force-with-lease
```

Only rebase a branch nobody else is building on.

## Pull requests

- Every commit carries a `Signed-off-by` line — see above.
- Tests pass and `ruff check .` is clean; CI runs both across the supported
  Python range plus the version the container image ships.
- New behaviour comes with a test. The suite is hardware-free by design; if
  something can only be verified against a real device, say so in the PR and
  describe what you observed.
- Add a `## [Unreleased]` entry to [CHANGELOG.md](CHANGELOG.md). If a metric
  name or label changes, say so explicitly and give the query people need to
  update — the project is pre-1.0 and that is allowed, but it is not allowed to
  be silent.
- Commit messages explain *why*, not just what. The history is the best
  documentation this protocol has.

## Reporting a vulnerability

See [SECURITY.md](SECURITY.md) — please do not open a public issue for one.
