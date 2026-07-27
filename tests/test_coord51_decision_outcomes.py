"""COORD-51 — close the decision-corpus loop (docs/DECISION-CORPUS-OUTCOMES-SPEC.md).

COORD-50 shipped the ledger with six outcome columns under the comment "backfilled by
reconcile / webhook". That backfill was never built, so every episode ever written read
``outcome='open'``: the corpus could prove a loop occurred and never that any attempt
accomplished anything. On the live CO-20 window of 2026-07-25, 21 episodes across four
heads all read ``open`` / ``generations_spent=0`` / ``head_advanced=False``, including
the seventeen definitively superseded when the head moved on.

These tests pin the three writers:

* §3.1 the outcome closes — merge, head advance, terminal Done, human intervention,
  retry-budget abandonment, and idempotent re-backfill;
* §3.2 the episode joins to the run it produced, via ``execution_id``;
* §3.3 a CI-failure episode names *which* required check failed, instead of
  recomputing it by cloning the head and running the suite by hand.

Plus the two guarantees that make those safe: §3.3 changes no route, and the failing
check identity never crosses the export boundary.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sqlite3
import unittest
from unittest.mock import patch

from path_setup import ROOT  # noqa: F401

from switchboard.application import completion_driver
from switchboard.domain.completion import state_machine
from switchboard.domain.completion.executor import CompletionEffectAdapters
from switchboard.domain.decisions import features as features_mod
from switchboard.domain.decisions import outcomes as outcomes_mod
from switchboard.domain.decisions import reason_codes
from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import claims as claims_repo
from switchboard.storage.repositories import completion_runs, decision_records
from switchboard.storage.repositories import provenance as provenance_repo
from switchboard.storage.repositories import tasks as tasks_repo


HEAD = "a" * 40
OTHER_HEAD = "b" * 40
THIRD_HEAD = "c" * 40

# The four heads of the live CO-20 window, spec §2.
CO20_HEADS = ("8a79c572", "d150fd1b", "c11fadf5", "a894afc0")

VM_GATE = "Switchboard CI / VM gate"
FAILING_URL = "https://github.com/acme/private/actions/runs/1234567890"
FAILING_SUMMARY = "1 failed: tests/test_bug174_repair_claim.py"


def _snapshot(*, task_id="CO-20", head=HEAD, ci="SUCCESS", generation=1,
              host_id="host-1", execution_id="", check_url="", description="",
              contexts=True, review="passed", runner_live=False):
    """A completion snapshot, optionally carrying the failing check's own identity."""
    row = {"context": VM_GATE, "state": ci, "failure_attribution": "product"}
    if check_url:
        row["target_url"] = check_url
    if description:
        row["description"] = description
    return state_machine.build_completion_snapshot(
        task={"task_id": task_id, "status": "In Review",
              "title": "a private task title nobody may pool",
              "git_state": {"head_sha": head, "pr_number": 810,
                            "pr_url": "https://github.com/acme/private/pull/810"}},
        github_pr={
            "number": 810, "state": "OPEN", "draft": False, "mergeable": True,
            "mergeStateStatus": "CLEAN", "head": {"sha": head},
            "status_contexts": [row] if contexts else [],
            "url": "https://github.com/acme/private/pull/810",
        },
        required_status_contexts=[VM_GATE],
        review={"status": review, "head_sha": head,
                "pr_url": "https://github.com/acme/private/pull/810"},
        merge_gate={"findings": [],
                    "pr_url": "https://github.com/acme/private/pull/810"},
        runner={"live": runner_live, "generation": generation, "host_id": host_id,
                "execution_id": execution_id, "role": "review_merge",
                "head_sha": head},
    )


# ---------------------------------------------------------------------------
# §3.1 the vocabulary has an owner
# ---------------------------------------------------------------------------

class DecisionOutcomeRegistryTest(unittest.TestCase):
    def test_the_spec_vocabulary_is_registered(self):
        self.assertEqual(
            {"open", "merged", "done", "superseded", "abandoned", "human_resolved"}
            - outcomes_mod.OUTCOMES,
            set(),
            "every outcome the spec names must be declared, or a writer using it "
            "would be rejected at the boundary",
        )

    def test_open_is_the_only_non_terminal_outcome(self):
        non_terminal = outcomes_mod.OUTCOMES - outcomes_mod.TERMINAL_OUTCOMES
        self.assertEqual(non_terminal, {"open"})

    def test_an_unregistered_outcome_is_never_treated_as_terminal(self):
        # Guessing terminality for a value nobody declared is how a stalled loop
        # reads as resolved.
        self.assertFalse(outcomes_mod.is_terminal_outcome("probably_fine"))
        self.assertFalse(outcomes_mod.is_registered_outcome("probably_fine"))

    def test_the_legacy_coord50_outcomes_stay_registered(self):
        # Additive only: outcomes are API, and compact_decision_snapshots reads
        # `escalated` to decide which snapshot bodies survive their TTL.
        self.assertTrue(outcomes_mod.is_registered_outcome("converged"))
        self.assertTrue(outcomes_mod.is_registered_outcome("escalated"))
        self.assertIn("escalated", decision_records.RETAINED_OUTCOMES)

    def test_the_retry_budget_vocabulary_excludes_codes_still_retrying(self):
        self.assertTrue(
            reason_codes.is_retry_budget_exhausted("review_retry_budget_exhausted"))
        for still_trying in ("merge_queue_locked", "pr_mergeability_unknown",
                             "required_exact_head_ci_failed"):
            self.assertFalse(
                reason_codes.is_retry_budget_exhausted(still_trying),
                f"{still_trying} is emitted while a bounded retry is still live; "
                "counting it as abandoned would label a working loop as given-up",
            )

    def test_every_registered_reason_code_used_for_abandonment_exists(self):
        for code in reason_codes.RETRY_BUDGET_EXHAUSTED_CODES:
            self.assertTrue(
                reason_codes.is_registered(code),
                f"{code} drives an outcome write and must be a registered code",
            )


# ---------------------------------------------------------------------------
# §3.3 the failing check identity — retained, and route-neutral
# ---------------------------------------------------------------------------

class FailingCheckIdentityTest(unittest.TestCase):
    def test_a_ci_failure_names_the_failing_context(self):
        decision = state_machine.classify_completion(
            None,
            _snapshot(ci="failure", check_url=FAILING_URL,
                      description=FAILING_SUMMARY),
        )
        self.assertEqual(decision["reason_code"], "required_exact_head_ci_failed")
        self.assertEqual(decision["failing_contexts"], [VM_GATE])
        self.assertEqual(decision["failing_check_url"], FAILING_URL)
        self.assertEqual(decision["failing_check_summary"], FAILING_SUMMARY)

    def test_the_context_name_keeps_its_case_and_spacing(self):
        # It is an identifier that must match GitHub's required-context string; the
        # feature normalizer folds case, which would break the match.
        decision = state_machine.classify_completion(None, _snapshot(ci="failure"))
        self.assertEqual(decision["failing_contexts"], ["Switchboard CI / VM gate"])

    def test_a_passing_or_pending_snapshot_claims_no_failing_context(self):
        # A decision that never looked at a failing context must not report that it
        # found none — the key is absent, not empty.
        for state in ("SUCCESS", "pending"):
            decision = state_machine.classify_completion(
                None, _snapshot(ci=state)) or {}
            self.assertNotIn("failing_contexts", decision, state)

    def test_retaining_the_identity_changes_no_route(self):
        """§3.3 is feature-only. A route change here would be a defect, not a feature."""
        routing_keys = ("state", "route", "reason_code", "desired_role",
                        "retry_policy", "board_projection", "effect")
        for ci in ("failure", "error", "timed_out", "action_required", "pending",
                   "SUCCESS", "cancelled"):
            bare = state_machine.classify_completion(None, _snapshot(ci=ci)) or {}
            rich = state_machine.classify_completion(
                None,
                _snapshot(ci=ci, check_url=FAILING_URL, description=FAILING_SUMMARY),
            ) or {}
            self.assertEqual(
                {key: bare.get(key) for key in routing_keys},
                {key: rich.get(key) for key in routing_keys},
                f"carrying the failing check identity moved the route for ci={ci}",
            )

    def test_the_identity_does_not_vary_with_context_presentation_order(self):
        """COORD-49 was a source-ordering bug that chose a decision.

        `_select_decision` exists so provider ordering cannot pick the blocker. A
        diagnostic that varied under permutation would put that non-determinism back
        into the classifier's output through a side door.
        """
        rows = [
            {"context": "Zeta gate", "state": "failure",
             "failure_attribution": "product", "target_url": "https://ci/zeta",
             "description": "zeta failed"},
            {"context": "Alpha gate", "state": "failure",
             "failure_attribution": "product", "target_url": "https://ci/alpha",
             "description": "alpha failed"},
        ]
        decisions = []
        for ordering in (rows, list(reversed(rows))):
            pr_url = "https://github.com/acme/private/pull/810"
            snapshot = state_machine.build_completion_snapshot(
                task={"task_id": "CO-20", "status": "In Review",
                      "git_state": {"head_sha": HEAD, "pr_number": 810,
                                    "pr_url": pr_url}},
                github_pr={"number": 810, "state": "OPEN", "mergeable": True,
                           "mergeStateStatus": "CLEAN", "head": {"sha": HEAD},
                           "status_contexts": ordering, "url": pr_url},
                required_status_contexts=[row["context"] for row in ordering],
                review={"status": "passed", "head_sha": HEAD, "pr_url": pr_url},
                merge_gate={"findings": [], "pr_url": pr_url},
                runner={"live": False, "generation": 1},
            )
            decisions.append(state_machine.classify_completion(None, snapshot))

        self.assertEqual(decisions[0], decisions[1])
        self.assertEqual(decisions[0]["failing_contexts"], ["Alpha gate", "Zeta gate"])
        self.assertEqual(decisions[0]["failing_check_url"], "https://ci/alpha")

    def test_the_identity_is_bounded(self):
        # A PR with fifty required contexts must not write fifty names into every
        # episode, and an upstream description is untrusted text.
        diagnostics = features_mod.project_diagnostics(
            {},
            {
                "failing_contexts": [f"context-{index}" for index in range(50)],
                "failing_check_summary": "x" * 5000,
            },
        )
        self.assertEqual(
            len(diagnostics["failing_contexts"]),
            features_mod.MAX_FAILING_CONTEXTS,
        )
        self.assertEqual(
            len(diagnostics["failing_check_summary"]),
            features_mod.MAX_SUMMARY_CHARS,
        )

    def test_diagnostics_are_not_part_of_the_poolable_projection(self):
        snapshot = _snapshot(ci="failure", check_url=FAILING_URL)
        decision = state_machine.classify_completion(None, snapshot)
        projected = features_mod.project_features(snapshot, decision)
        self.assertEqual(tuple(projected), features_mod.FEATURE_FIELDS)
        for name in features_mod.DIAGNOSTIC_FIELDS:
            self.assertNotIn(name, projected)


# ---------------------------------------------------------------------------
# the ledger — outcomes, joins, and the export boundary
# ---------------------------------------------------------------------------

class DecisionOutcomeStorageTest(unittest.TestCase):
    project = "switchboard"

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE tasks ("
            "task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "assignee TEXT, updated_at REAL)")
        self.db.execute(
            "CREATE TABLE deliverable_task_links ("
            "id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL, "
            "project_id TEXT NOT NULL, task_id TEXT NOT NULL, created_at REAL)")
        wanted = {
            "0111_completion_runs",
            "0112_ux_completion_runs_task",
            "0117_decision_records",
            "0118_ix_decision_records_projection",
            "0119_ix_decision_records_convergence",
        }
        for name, sql in migrations.DDL_MIGRATIONS:
            if name in wanted:
                self.db.execute(sql)
        self.patches = [
            patch.object(decision_records, "_conn", return_value=self.db),
            patch.object(
                decision_records, "_write_through",
                side_effect=lambda _project, fn: fn()),
            patch.object(completion_runs, "_conn", return_value=self.db),
            patch.object(
                completion_runs, "_write_through",
                side_effect=lambda _project, fn: fn()),
        ]
        for p in self.patches:
            p.start()
        self.now = 1_700_000_000.0

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()

    def _tick(self, snapshot, decision=None, *, at=None):
        decision = decision or state_machine.classify_completion(None, snapshot)
        return decision_records.record_decision_episode(
            project=self.project,
            snapshot=snapshot,
            decision=decision,
            classifier_version=state_machine.COMPLETION_CLASSIFIER_VERSION,
            now=self.now if at is None else at,
        )

    def _verdict(self, reason_code, route="coordination_retry", role=""):
        return {"schema": state_machine.COMPLETION_DECISION_SCHEMA,
                "state": "blocked", "route": route,
                "reason_code": reason_code, "desired_role": role}

    def _rows(self, task_id="CO-20"):
        return {
            row["record_id"]: row for row in self.db.execute(
                "SELECT * FROM decision_records WHERE task_id=?", (task_id,),
            ).fetchall()
        }

    def _outcomes(self, task_id="CO-20"):
        return [row["outcome"] for row in self._rows(task_id).values()]

    # -- §3.1 the migration claim -------------------------------------------

    def test_no_migration_was_needed_for_the_outcome_columns(self):
        """The spec's load-bearing claim: all six columns already existed."""
        present = {
            row["name"] for row in
            self.db.execute("PRAGMA table_info(decision_records)").fetchall()
        }
        self.assertEqual(
            {"outcome", "head_advanced", "generations_spent", "merged_sha",
             "human_intervened", "human_action"} - present,
            set(),
        )

    def test_an_episode_is_still_born_open(self):
        record = self._tick(_snapshot())
        self.assertEqual(record["outcome"], "open")
        self.assertFalse(record["outcome_terminal"])
        self.assertTrue(record["outcome_registered"])

    # -- §3.1 head advance --------------------------------------------------

    def test_a_head_advance_supersedes_the_prior_episodes(self):
        verdict = self._verdict("required_exact_head_ci_failed", "remediation")
        first = self._tick(_snapshot(head=HEAD), verdict)
        self._tick(_snapshot(head=OTHER_HEAD), verdict)

        rows = self._rows()
        prior = rows[first["record_id"]]
        self.assertEqual(prior["outcome"], "superseded")
        self.assertEqual(prior["head_advanced"], 1)
        self.assertEqual(prior["generations_spent"], 1)

    def test_the_episode_on_the_current_head_stays_open(self):
        verdict = self._verdict("required_exact_head_ci_failed", "remediation")
        self._tick(_snapshot(head=HEAD), verdict)
        current = self._tick(_snapshot(head=OTHER_HEAD), verdict)
        self.assertEqual(self._rows()[current["record_id"]]["outcome"], "open")

    def test_generations_spent_accumulates_across_successive_advances(self):
        # An episode carried across three advances reports three spent generations,
        # so `generations_spent` increments and is never merely set.
        verdict = self._verdict("required_exact_head_ci_failed", "remediation")
        first = self._tick(_snapshot(head=HEAD), verdict)
        self._tick(_snapshot(head=OTHER_HEAD), verdict)
        self._tick(_snapshot(head=THIRD_HEAD), verdict)
        # The first episode closed on the first advance and must not be re-charged.
        self.assertEqual(self._rows()[first["record_id"]]["generations_spent"], 1)
        still_open = [
            row for row in self._rows().values() if row["outcome"] == "open"
        ]
        self.assertEqual(len(still_open), 1)
        self.assertEqual(still_open[0]["head_sha"], THIRD_HEAD)

    def test_losing_pr_hydration_is_not_a_head_advance(self):
        # An empty head is a head regression, not an advance. Recording
        # head_advanced=1 for it would be a lie about what happened.
        verdict = self._verdict("required_exact_head_ci_failed", "remediation")
        first = self._tick(_snapshot(head=HEAD), verdict)
        self._tick(
            _snapshot(head="", contexts=False),
            self._verdict("exact_head_pr_missing"),
        )
        self.assertEqual(self._rows()[first["record_id"]]["outcome"], "open")

    def test_a_repeated_tick_on_an_unchanged_head_supersedes_nothing(self):
        snapshot = _snapshot()
        for index in range(20):
            record = self._tick(snapshot, at=self.now + index)
        self.assertEqual(record["tick_count"], 20)
        self.assertEqual(record["superseded_prior_episodes"], 0)
        self.assertEqual(self._outcomes(), ["open"])

    # -- §3.1 merge, done, abandonment, human intervention ------------------

    def test_a_merge_closes_every_open_episode_with_the_merged_sha(self):
        self._tick(_snapshot(head=HEAD),
                   self._verdict("required_exact_head_ci_failed", "remediation"))
        self._tick(_snapshot(head=HEAD),
                   self._verdict("review_required", "review_merge", "review_merge"))
        result = decision_records.close_merged_episodes(
            project=self.project, task_id="CO-20", merged_sha="d" * 40)

        self.assertEqual(result["updated"], 2)
        for row in self._rows().values():
            self.assertEqual(row["outcome"], "merged")
            self.assertEqual(row["merged_sha"], "d" * 40)

    def test_a_terminal_done_closes_every_open_episode(self):
        self._tick(_snapshot(), self._verdict("review_required", "review_merge"))
        decision_records.close_done_episodes(
            project=self.project, task_id="CO-20")
        self.assertEqual(self._outcomes(), ["done"])

    def test_the_retry_budget_abandons_the_task_on_the_tick_that_spends_it(self):
        self._tick(_snapshot(head=HEAD),
                   self._verdict("required_exact_head_ci_failed", "remediation"))
        exhausted = self._tick(
            _snapshot(head=HEAD),
            self._verdict("review_retry_budget_exhausted", "human"))

        # Including the episode that classified it: the budget is spent for the task,
        # not for one row.
        self.assertEqual(set(self._outcomes()), {"abandoned"})
        self.assertGreaterEqual(exhausted["abandoned_open_episodes"], 2)

    def test_a_human_intervention_flags_the_verb_and_leaves_the_loop_open(self):
        # An operator revoking a claim has intervened in the loop, not ended it.
        # Calling that terminal would hide a task that is still spinning.
        self._tick(_snapshot(), self._verdict("required_exact_head_ci_failed",
                                             "remediation"))
        result = decision_records.mark_human_intervention(
            project=self.project, task_id="CO-20", human_action="revoke_claim")

        self.assertEqual(result["updated"], 1)
        row = next(iter(self._rows().values()))
        self.assertEqual(row["human_intervened"], 1)
        self.assertEqual(row["human_action"], "revoke_claim")
        self.assertEqual(row["outcome"], "open")

    def test_an_operator_closing_the_task_out_is_human_resolved(self):
        self._tick(_snapshot(), self._verdict("human_review_findings", "human"))
        decision_records.mark_human_intervention(
            project=self.project, task_id="CO-20",
            human_action="update_task:status=Cancelled", resolved=True)
        row = next(iter(self._rows().values()))
        self.assertEqual(row["outcome"], "human_resolved")
        self.assertEqual(row["human_intervened"], 1)

    def test_an_unregistered_outcome_is_refused_at_the_writer(self):
        self._tick(_snapshot(), self._verdict("review_required"))
        with self.assertRaises(decision_records.DecisionRecordError):
            decision_records.record_episode_outcome(
                project=self.project, task_id="CO-20", outcome="probably_fine")
        with self.assertRaises(decision_records.DecisionRecordError):
            decision_records._close_open_episodes_in(
                self.db, project=self.project, task_id="CO-20",
                outcome="probably_fine")

    def test_closing_an_episode_open_is_refused(self):
        with self.assertRaises(decision_records.DecisionRecordError):
            decision_records._close_open_episodes_in(
                self.db, project=self.project, task_id="CO-20", outcome="open")

    # -- §3.1 idempotence and immutability ----------------------------------

    def test_re_backfilling_a_closed_episode_changes_nothing(self):
        self._tick(_snapshot(), self._verdict("review_required", "review_merge"))
        first = decision_records.close_merged_episodes(
            project=self.project, task_id="CO-20", merged_sha="d" * 40)
        before = dict(next(iter(self._rows().values())))

        second = decision_records.close_merged_episodes(
            project=self.project, task_id="CO-20", merged_sha="e" * 40)
        third = decision_records.close_done_episodes(
            project=self.project, task_id="CO-20")
        after = dict(next(iter(self._rows().values())))

        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0, "a closed episode must not reopen")
        self.assertEqual(third["updated"], 0)
        self.assertEqual(before, after)

    def test_a_repeated_human_intervention_is_not_re_reported(self):
        self._tick(_snapshot(), self._verdict("review_required"))
        first = decision_records.mark_human_intervention(
            project=self.project, task_id="CO-20", human_action="revoke_claim")
        second = decision_records.mark_human_intervention(
            project=self.project, task_id="CO-20", human_action="revoke_claim")
        self.assertEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0)

    def test_closing_never_rewrites_the_decision_or_its_inputs(self):
        record = self._tick(
            _snapshot(ci="failure", check_url=FAILING_URL,
                      description=FAILING_SUMMARY))
        before = dict(next(iter(self._rows().values())))
        decision_records.close_merged_episodes(
            project=self.project, task_id="CO-20", merged_sha="d" * 40)
        after = dict(next(iter(self._rows().values())))

        for immutable in ("reason_code", "features_json", "snapshot_hash",
                         "decision_json", "classifier_version", "route",
                         "desired_role", "first_seen_at"):
            self.assertEqual(before[immutable], after[immutable], immutable)
        self.assertEqual(record["record_id"], after["record_id"])

    def test_an_empty_merged_sha_does_not_erase_one_already_recorded(self):
        self._tick(_snapshot(), self._verdict("review_required"))
        decision_records.close_merged_episodes(
            project=self.project, task_id="CO-20", merged_sha="d" * 40)
        # Reopen one row by hand to prove the CASE guard, not the open-only filter.
        self.db.execute("UPDATE decision_records SET outcome='open'")
        decision_records.close_done_episodes(
            project=self.project, task_id="CO-20")
        row = next(iter(self._rows().values()))
        self.assertEqual(row["merged_sha"], "d" * 40)
        self.assertEqual(row["outcome"], "done")

    # -- §3.2 the episode → execution join ----------------------------------

    def test_list_decision_episodes_returns_the_execution_id(self):
        self._tick(_snapshot(execution_id="run_e9d5a081"))
        episodes = decision_records.list_decision_episodes(
            project=self.project, task_id="CO-20")
        self.assertEqual(episodes[0]["execution_id"], "run_e9d5a081")

    def test_the_join_triple_is_carried_alongside_the_execution_id(self):
        # (host_id, generation, fence_epoch) resolves a runner session for episodes
        # written before the execution_id column existed; the id makes it direct.
        self._tick(_snapshot(execution_id="run_e9d5a081", generation=4,
                             host_id="host-7"))
        episode = decision_records.list_decision_episodes(
            project=self.project, task_id="CO-20")[0]
        self.assertEqual(episode["execution_id"], "run_e9d5a081")
        self.assertEqual(episode["host_id"], "host-7")
        self.assertEqual(episode["generation"], 4)

    def test_an_execution_id_that_arrives_late_is_filled_in_once(self):
        self._tick(_snapshot(execution_id=""))
        self._tick(_snapshot(execution_id="run_fadc7ab2"), at=self.now + 1)
        row = next(iter(self._rows().values()))
        self.assertEqual(row["execution_id"], "run_fadc7ab2")
        self.assertEqual(row["tick_count"], 2, "filling identity is not a new episode")

    def test_a_recorded_execution_id_is_never_overwritten(self):
        self._tick(_snapshot(execution_id="run_first"))
        self._tick(_snapshot(execution_id="run_second"), at=self.now + 1)
        self.assertEqual(
            next(iter(self._rows().values()))["execution_id"], "run_first")

    def test_a_changing_check_url_does_not_fragment_one_episode(self):
        # Hashing a per-run URL would split one episode into many and defeat the
        # collapsing in parent spec §5 — the same trap that kept episode_hash off
        # the raw snapshot body.
        self._tick(_snapshot(ci="failure", check_url=FAILING_URL))
        record = self._tick(
            _snapshot(ci="failure", check_url=FAILING_URL + "/attempts/2"),
            at=self.now + 1)
        self.assertEqual(record["tick_count"], 2)
        self.assertEqual(len(self._rows()), 1)

    # -- §3.3 the identity survives to storage, and stops at the boundary ---

    def test_a_ci_failure_episode_names_its_failing_context_in_features(self):
        self._tick(_snapshot(ci="failure", check_url=FAILING_URL,
                             description=FAILING_SUMMARY))
        episode = decision_records.list_decision_episodes(
            project=self.project, task_id="CO-20")[0]
        self.assertEqual(episode["reason_code"], "required_exact_head_ci_failed")
        self.assertEqual(episode["features"]["failing_contexts"], [VM_GATE])
        self.assertEqual(episode["features"]["failing_check_url"], FAILING_URL)
        self.assertEqual(
            episode["features"]["failing_check_summary"], FAILING_SUMMARY)

    def test_the_poolable_allowlist_still_rides_alongside_the_diagnostics(self):
        self._tick(_snapshot(ci="failure", check_url=FAILING_URL))
        episode = decision_records.list_decision_episodes(
            project=self.project, task_id="CO-20")[0]
        self.assertEqual(
            set(features_mod.FEATURE_FIELDS) - set(episode["features"]), set())
        self.assertTrue(episode["features"]["any_required_context_failed"])

    def test_the_export_strips_every_diagnostic_key(self):
        """The commercial boundary: names and URLs are content, not shape.

        Parent spec §4.2 excludes status-context names and PR/check URLs from the
        poolable tier. §3.3 stores them where a reader looks, so the export has to
        strip them — and that has to be asserted, not implied.
        """
        self._tick(_snapshot(ci="failure", check_url=FAILING_URL,
                             description=FAILING_SUMMARY))
        exported = decision_records.export_projection(project=self.project)
        self.assertEqual(len(exported), 1)

        raw = json.dumps(exported[0])
        for leak in (VM_GATE, FAILING_URL, FAILING_SUMMARY, "acme/private"):
            self.assertNotIn(leak, raw, f"{leak!r} crossed the export boundary")

        # Exactly the allowlist: nothing dropped, and nothing extra. (features_json is
        # canonically key-sorted on write, so membership — not order — is the property
        # storage actually carries.)
        features = json.loads(exported[0]["features_json"])
        self.assertEqual(set(features), set(features_mod.FEATURE_FIELDS))

    def test_the_execution_id_is_never_exported(self):
        self._tick(_snapshot(execution_id="run_e9d5a081"))
        exported = decision_records.export_projection(project=self.project)
        self.assertNotIn("execution_id", exported[0])
        self.assertNotIn("run_e9d5a081", json.dumps(exported[0]))
        self.assertIn("execution_id", decision_records.PRIVATE_COLUMNS)

    def test_every_decision_records_column_is_still_classified(self):
        # Same guarantee COORD-50 pinned, re-asserted now that 0120 added one.
        columns = {
            row["name"] for row in
            self.db.execute("PRAGMA table_info(decision_records)").fetchall()
        }
        classified = (set(decision_records.EXPORT_COLUMNS)
                      | set(decision_records.PRIVATE_COLUMNS)
                      | {"snapshot_retained", "features_version", "classifier_version",
                         "project", "route", "desired_role", "features_json",
                         "advice_version", "tick_count", "reason_code", "outcome",
                         "head_advanced", "generations_spent"})
        self.assertEqual(
            columns - classified, set(),
            "a new column must be declared export-safe or private before it ships",
        )

    # -- the counts answer the §3.1 question --------------------------------

    def test_counts_report_which_reason_codes_never_reach_a_terminal_outcome(self):
        # One code closes when the head advances; one never closes at all.
        closing = self._verdict("required_exact_head_ci_failed", "remediation")
        self._tick(_snapshot(head=HEAD), closing)
        self._tick(_snapshot(head=OTHER_HEAD), closing)
        decision_records.close_merged_episodes(
            project=self.project, task_id="CO-20", merged_sha="d" * 40)

        self._tick(_snapshot(task_id="CO-21", head=THIRD_HEAD),
                   self._verdict("exact_head_pr_missing"))

        counts = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(
            counts["never_terminal_reason_codes"], ["exact_head_pr_missing"])
        self.assertEqual(counts["open_episodes"], 1)
        self.assertEqual(counts["terminal_episodes"], 2)

        by_code = {item["reason_code"]: item for item in counts["codes"]}
        failed = by_code["required_exact_head_ci_failed"]
        self.assertFalse(failed["never_terminal"])
        self.assertEqual(failed["outcomes"], {"superseded": 1, "merged": 1})
        self.assertEqual(failed["head_advanced_episodes"], 1)
        self.assertEqual(failed["generations_spent"], 1)
        self.assertTrue(by_code["exact_head_pr_missing"]["never_terminal"])

    def test_counts_surface_an_unregistered_outcome_rather_than_counting_it(self):
        self._tick(_snapshot(), self._verdict("review_required"))
        self.db.execute("UPDATE decision_records SET outcome='probably_fine'")
        counts = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(counts["unregistered_outcomes"], ["probably_fine"])
        # Never silently counted as closed.
        self.assertEqual(counts["terminal_episodes"], 0)
        self.assertEqual(counts["open_episodes"], 1)

    def test_counts_keep_their_coord50_shape(self):
        self._tick(_snapshot(), self._verdict("review_required"))
        counts = decision_records.count_reason_code_episodes(project=self.project)
        for field in ("total_episodes", "total_ticks", "distinct_tasks",
                      "dominant_reason_code", "unregistered_reason_codes", "codes"):
            self.assertIn(field, counts)
        item = counts["codes"][0]
        for field in ("reason_code", "route", "registered", "family", "expected",
                      "resolver", "episodes", "ticks", "tasks", "share",
                      "first_seen_at", "last_seen_at"):
            self.assertIn(field, item)

    def test_ticks_and_episodes_still_separate_after_the_outcome_grouping(self):
        snapshot = _snapshot()
        for index in range(30):
            self._tick(snapshot, at=self.now + index)
        counts = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(counts["total_episodes"], 1)
        self.assertEqual(counts["total_ticks"], 30)

    # -- the CO-20 window ---------------------------------------------------

    def test_replaying_the_co20_window_closes_every_superseded_episode(self):
        """Spec §2's measured window, replayed.

        The head sequences and reason codes are the spec's own table. The live run's
        exact episode total is not reconstructible from that table — what is asserted
        is the property the acceptance criterion is about: every episode belonging to
        a head that was left behind reads `superseded` with `head_advanced=1` and a
        non-zero `generations_spent`, and only the final head's episodes stay open.
        Before this task, all of them read open / generations_spent=0.
        """
        head_a, head_b, head_c, head_d = (sha + "0" * 32 for sha in CO20_HEADS)
        remediation = ("required_exact_head_ci_failed", "remediation", "remediation")

        window: list[tuple[str, tuple, int]] = [
            (head_a, ("missing_executed_test_run", "coordination_retry", ""), 1),
            (head_a, ("exact_head_pr_missing", "coordination_retry", ""), 1),
            (head_b, ("required_ci_hydration_missing", "coordination_retry", ""), 1),
            (head_b, ("required_exact_head_ci_pending", "wait", ""), 1),
        ]
        # Seven separate remediation dispatches against ONE head, twice interrupted
        # by exact_head_pr_missing. Each dispatch is its own generation, which is what
        # keeps them seven episodes rather than one collapsed row.
        for generation in range(1, 8):
            window.append((head_b, remediation, generation))
            if generation in (3, 6):
                window.append(
                    (head_b, ("exact_head_pr_missing", "coordination_retry", ""),
                     generation))
        window += [
            (head_c, ("required_exact_head_ci_pending", "wait", ""), 1),
            (head_c, ("review_required", "review_merge", "review_merge"), 1),
            (head_c, ("exact_head_pr_missing", "coordination_retry", ""), 1),
            (head_d, ("required_exact_head_ci_pending", "wait", ""), 1),
            (head_d, ("review_required", "review_merge", "review_merge"), 1),
        ]

        for index, (head, verdict, generation) in enumerate(window):
            self._tick(
                _snapshot(head=head, generation=generation),
                self._verdict(*verdict),
                at=self.now + index,
            )

        rows = list(self._rows().values())
        self.assertEqual(len(rows), len(window), "each dispatch is its own episode")

        left_behind = [row for row in rows if row["head_sha"] != head_d]
        current = [row for row in rows if row["head_sha"] == head_d]
        self.assertEqual(len(current), 2)

        for row in left_behind:
            self.assertEqual(row["outcome"], "superseded", row["reason_code"])
            self.assertEqual(row["head_advanced"], 1, row["reason_code"])
            self.assertGreater(row["generations_spent"], 0, row["reason_code"])
        for row in current:
            self.assertEqual(row["outcome"], "open")

        # The seven remediation dispatches specifically: the corpus can now show that
        # all seven achieved nothing.
        seven = [row for row in rows
                 if row["reason_code"] == "required_exact_head_ci_failed"]
        self.assertEqual(len(seven), 7)
        self.assertEqual({row["outcome"] for row in seven}, {"superseded"})

        counts = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(counts["open_episodes"], 2)
        self.assertEqual(counts["terminal_episodes"], len(window) - 2)

    def test_no_episode_remains_open_once_the_task_is_terminal(self):
        """The headline acceptance criterion, over a whole multi-head history."""
        for index, head in enumerate((HEAD, OTHER_HEAD, THIRD_HEAD)):
            self._tick(_snapshot(head=head),
                       self._verdict("required_exact_head_ci_failed", "remediation"),
                       at=self.now + index)
        decision_records.close_merged_episodes(
            project=self.project, task_id="CO-20", merged_sha="d" * 40)

        still_open = [row for row in self._rows().values()
                      if row["outcome"] == "open"]
        self.assertEqual(still_open, [])

    # -- the production tick actually closes --------------------------------

    def test_a_real_completion_tick_supersedes_the_previous_head(self):
        """The wiring in the driver, not just the repository."""
        adapters = CompletionEffectAdapters(
            ensure_review_generation=lambda plan: {"ok": True},
            start_remediation=lambda plan: {"ok": True},
            mark_ready=lambda plan: {"ok": True},
            enqueue=lambda plan: {"ok": True},
            repair_dispatch=lambda plan: {"ok": True},
            fence_runner=lambda plan: {"ok": True},
            reconcile_provenance=lambda plan: {"ok": True},
        )
        patches = (
            patch(
                "switchboard.storage.repositories.completion_runs."
                "get_active_completion_run",
                return_value={"run_id": "run-810", "state_version": 1,
                              "attempt": 1},
            ),
            patch(
                "switchboard.domain.completion.executor._persist_run",
                return_value={"run_id": "run-810", "state_version": 1},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "claim_external_effect",
                return_value={"claimed": True, "effect_key": "effect-810"},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "verify_external_effect",
                return_value={"effect_key": "effect-810"},
            ),
            patch(
                "switchboard.storage.repositories.external_effects."
                "mark_external_effect_issued",
                return_value={"effect_key": "effect-810"},
            ),
        )

        def tick(snapshot):
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                return completion_driver.run_completion_tick(
                    "CO-20", project=self.project, actor="test",
                    agent_id="claude/COORD-51",
                    hydrator=lambda task_id, **_: snapshot,
                    adapters=adapters,
                )

        tick(_snapshot(ci="failure", head=HEAD, execution_id="run_first",
                       check_url=FAILING_URL, description=FAILING_SUMMARY))
        tick(_snapshot(ci="failure", head=OTHER_HEAD, execution_id="run_second",
                       check_url=FAILING_URL, description=FAILING_SUMMARY))

        episodes = decision_records.list_decision_episodes(
            project=self.project, task_id="CO-20")
        self.assertEqual(len(episodes), 2)
        first, second = episodes
        self.assertEqual(first["outcome"], "superseded")
        self.assertTrue(first["head_advanced"])
        self.assertEqual(first["generations_spent"], 1)
        self.assertEqual(second["outcome"], "open")
        # §3.2 and §3.3 arrive through the production path too, not only the fixture.
        self.assertEqual(first["execution_id"], "run_first")
        self.assertEqual(first["features"]["failing_contexts"], [VM_GATE])


# ---------------------------------------------------------------------------
# the hooks are wired
# ---------------------------------------------------------------------------

def _called_attributes(module, receiver: str) -> set[str]:
    """Attribute names called on ``receiver`` anywhere in ``module``'s source."""
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == receiver
    }


class CorpusHasNoAuthorityTest(unittest.TestCase):
    """A history table must never be able to lose merge truth.

    The closers are called on the merge, Done, and revoke transactions. On a DB whose
    additive migration has not run, an unguarded UPDATE raises `no such table` and
    takes the whole webhook down with it — trading an unrecorded outcome for a lost
    merge, which is the strictly worse defect. The skip is named and carried in the
    result, per the fail-fix rule that a fallback must preserve its signal.
    """

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row

    def tearDown(self):
        self.db.close()

    def test_closing_on_a_db_without_the_corpus_is_a_named_skip(self):
        closers = (
            lambda: decision_records.close_merged_episodes_in(
                self.db, project="switchboard", task_id="CO-20",
                merged_sha="d" * 40),
            lambda: decision_records.close_done_episodes_in(
                self.db, project="switchboard", task_id="CO-20"),
            lambda: decision_records.abandon_open_episodes_in(
                self.db, project="switchboard", task_id="CO-20"),
            lambda: decision_records.supersede_prior_episodes_in(
                self.db, project="switchboard", task_id="CO-20", head_sha=HEAD),
        )
        for index, closer in enumerate(closers):
            result = closer()
            self.assertEqual(result["updated"], 0)
            self.assertTrue(result["skipped"], index)
            self.assertEqual(result["reason"], "decision_records_absent", index)
            self.assertEqual(result["failure_class"], "missing_data", index)

    def test_marking_intervention_on_a_db_without_the_corpus_is_a_named_skip(self):
        result = decision_records.mark_human_intervention_in(
            self.db, project="switchboard", task_id="CO-20",
            human_action="revoke_claim")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "decision_records_absent")

    def test_a_real_storage_fault_still_propagates(self):
        # Only a missing table is tolerated. A corrupt or renamed schema must fail
        # loudly rather than read as "nothing to close".
        self.db.execute("CREATE TABLE decision_records (record_id TEXT PRIMARY KEY)")
        with self.assertRaises(sqlite3.OperationalError):
            decision_records.close_done_episodes_in(
                self.db, project="switchboard", task_id="CO-20")

    def test_an_invalid_outcome_is_still_refused_before_the_table_check(self):
        with self.assertRaises(decision_records.DecisionRecordError):
            decision_records._close_open_episodes_in(
                self.db, project="switchboard", task_id="CO-20",
                outcome="probably_fine")


class OutcomeHooksAreWiredTest(unittest.TestCase):
    """The writers exist only if something calls them.

    A closer with no call site is exactly the defect this task repairs: COORD-50
    shipped `record_episode_outcome` and nothing ever invoked it, so six columns sat
    at their defaults for the corpus's whole life. These assertions are structural so
    that deleting a hook fails here rather than silently freezing the columns again.
    """

    def test_the_merge_and_done_paths_close_episodes(self):
        called = _called_attributes(provenance_repo, "decision_records")
        self.assertIn("close_merged_episodes_in", called)
        self.assertIn("close_done_episodes_in", called)

    def test_revoke_claim_records_the_intervention(self):
        self.assertIn(
            "mark_human_intervention_in",
            _called_attributes(claims_repo, "decision_records"),
        )

    def test_an_operator_status_edit_records_the_intervention(self):
        self.assertIn(
            "mark_human_intervention_in",
            _called_attributes(tasks_repo, "decision_records"),
        )

    def test_the_head_advance_and_abandonment_hooks_live_in_the_writer(self):
        source = pathlib.Path(
            decision_records.__file__).read_text(encoding="utf-8")
        self.assertIn("supersede_prior_episodes_in(", source)
        self.assertIn("abandon_open_episodes_in(", source)

    def test_the_execution_id_column_ships_with_a_ledgered_migration(self):
        names = {name for name, *_ in migrations.ADDITIVE_COLUMN_MIGRATIONS}
        self.assertIn("0120_decision_records_execution_id", names)
        entry = next(item for item in migrations.ADDITIVE_COLUMN_MIGRATIONS
                     if item[0] == "0120_decision_records_execution_id")
        self.assertEqual(entry[1], "decision_records")
        self.assertEqual(entry[2], "execution_id")
        # A fresh DB gets it from the create, so the additive pass reconciles the
        # ledger without an ALTER. Both paths must name the same column.
        self.assertIn("execution_id", migrations.DECISION_RECORDS_SQL)


if __name__ == "__main__":
    unittest.main()
