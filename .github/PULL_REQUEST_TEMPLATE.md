## What this changes

<!-- And why. The commit history is the best documentation this protocol has. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`) — see [the DCO section of
      CONTRIBUTING.md](https://github.com/TilmannF/pa2_exporter/blob/main/CONTRIBUTING.md#sign-your-commits).
      A bot blocks the PR otherwise.
- [ ] `pytest` passes and `ruff check .` is clean
- [ ] New behaviour has a test, and the suite still runs without hardware
- [ ] **The read-only guarantee is intact — no `set` commands, ever**
- [ ] No device password, real address or venue preset name in the diff
- [ ] `CHANGELOG.md` has an `[Unreleased]` entry
- [ ] If a metric name or label changed, the changelog says so and gives the
      query people need to update

## Verified against

<!-- Mock, real hardware (which model and firmware), or both. If you could only
     test one path, say which — that is useful, not disqualifying. -->
