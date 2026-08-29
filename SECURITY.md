# Security policy

## Supported versions

The latest release receives fixes. The project is pre-1.0 and there are no
maintenance branches — if you are running something older, upgrade first and
check whether the problem persists.

## Reporting a vulnerability

Please report privately through GitHub's
[private vulnerability reporting](https://github.com/TilmannF/pa2_exporter/security/advisories/new)
rather than opening a public issue.

Include what you did, what happened, and the exporter version
(`pa2-exporter --version`). Expect an acknowledgement within a week; this is a
spare-time project, not a staffed one, so please be patient with the fix.

## What this exporter does and does not do

Worth knowing before you assess a finding:

- **It is read-only.** The exporter only ever sends `connect`, `get`, `ls` and
  `sub` to the device. It never sends `set`, so it cannot change a tuning, a
  mute or a preset. Anything that breaks that guarantee is a security bug, not
  just a feature regression.
- **`/metrics` is unauthenticated**, like every conventional Prometheus
  exporter. Bind it to a network your monitoring stack can reach and nothing
  else — `PA2_EXPORTER_ADDR` exists for that. The exposed data describes levels,
  mutes, presets and who changed what; it contains no credentials.
- **The device password is the exporter's most sensitive input.** Prefer
  `PA2_PASSWORD_FILE` over `PA2_PASSWORD`: the environment form is readable via
  `docker inspect` and `/proc/<pid>/environ` to anyone on the host, while the
  file form is how Docker and Kubernetes secrets are meant to be mounted. The
  password is never logged and never exported as a metric.
- **The control protocol itself is plaintext and the device's factory password
  is `administrator`.** That is the PA2's design, not this exporter's, and it
  cannot be fixed from here. Put the device on a management VLAN and change the
  password.
- **The container runs as an unprivileged user** (uid 10001) with a read-only
  root filesystem and all capabilities dropped in the shipped compose file.

## Out of scope

- The PA2's own network stack, its plaintext protocol and its default
  credentials — report those to Harman.
- Exposing `/metrics` to an untrusted network on purpose.
