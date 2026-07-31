"""T2 Observe: same scenario vocabulary and production tick, observe-cut effects.

T1 (``tests/conformance/test_completion_conformance.py``) proves the
classifier/planner against synthetic snapshots with mocked effect adapters.
T2 adds one thing: it can point ``run_completion_tick`` at a *real* hydrator
(real GitHub checks/reviews/queue state on the sandbox `conformance` project)
while still never spending capacity — every mutating effect port is swapped
for an :class:`observe.adapters.ObserveEffectAdapters` capture instead of the
production ``task_execution.start_task`` / `gh` adapters.

Modules:

- ``adapters``: the observe-cut effect ports (capture, never call out).
- ``scoreboard``: turn a tick result into one scoreboard row, with the
  expected_human/unexpected_human classification the design doc calls out.
- ``assignment_preview``: project the ``execution_assignment`` a live
  ``start_task`` would have received from an observed plan, for humans/CI
  logs — never invoked.
- ``runner``: ``run_observe_tick`` — fixture mode (hermetic, default) and
  live mode (``CONFORMANCE_LIVE=1``, real hydrator, sandbox project only).
- ``reaper``: clean up `gh` artifacts (PRs/branches) and archived tasks
  tagged with a ``run_id`` after a live T2 sweep.
- ``coverage``: the same axis-coverage idea as T1, reusable by the CLI.
"""
from __future__ import annotations
