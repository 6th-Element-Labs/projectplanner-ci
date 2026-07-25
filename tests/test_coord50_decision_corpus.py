"""COORD-50 — the decision corpus: episodes, registry, and the projection boundary.

Origin: BREAKDOWN 9 of the DOGFOOD-19 Mac proof run. The identical reason code
``required_exact_head_ci_failed`` fired on 3 of 3 PRs while the true cause was a
single classifier ordering bug (COORD-49). Nothing counted them, so one systemic
stall presented as three unrelated per-task retries and a human had to spot it.

Counting alone could not have fixed that: ``completion_runs`` upserts one row per
task and overwrites ``reason_code`` in place, and its append-only companion has no
``reason_code`` column at all. These tests pin the two properties that make the
corpus countable — the timeline survives, and repeated ticks do not inflate it.
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
from switchboard.domain.decisions import reason_codes
from switchboard.storage.migrations import runner as migrations
from switchboard.storage.repositories import completion_runs, decision_records


HEAD = "a" * 40
OTHER_HEAD = "b" * 40


# ---------------------------------------------------------------------------
# §4.1 the registry — the vocabulary has an owner
# ---------------------------------------------------------------------------

# Reason codes built by f-string from a state set. Declared here so a NEW dynamic
# prefix in the state machine fails this test instead of quietly minting unowned
# codes, which is exactly how the two `cancelled`/`canceled` spellings got in.
_DYNAMIC_PREFIXES = {
    "required_ci_": "_COORD_CI",
    "merge_queue_": "_QUEUE_WAIT",
}

# Locals that forward a merge_gate finding code or failure class straight through.
# Their vocabulary is the finding sets and failure classes enumerated below.
_FORWARDED_FINDING_NAMES = {"code", "failure", "kind"}

# Failure classes and finding kinds reach the route when a gate finding carries no
# code of its own (`code or failure`). fail_fix_signal.v1 owns the failure classes.
_FORWARDED_FINDING_VALUES = {
    "absent_permission", "invalid_input", "hidden_fallback", "broken_connection",
    "missing_data", "stale_branch", "unreachable_agent", "failed_gate",
    "automatic", "product", "code", "judgment", "authority", "policy", "human",
}


def _possible_strings(node: ast.AST, bindings: dict[str, set[str]]) -> set[str]:
    """The string values one expression can evaluate to, or ``set()`` if unknown.

    Only value positions count. Walking the whole subtree would sweep up dict keys
    from the *condition* of an `if` expression and report them as reason codes.
    """
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return (_possible_strings(node.body, bindings)
                | _possible_strings(node.orelse, bindings))
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        values: set[str] = set()
        for operand in node.values:
            values |= _possible_strings(operand, bindings)
        return values
    if isinstance(node, ast.Name):
        return set(bindings.get(node.id, ()))
    if isinstance(node, ast.JoinedStr):
        prefix = "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        attribute = _DYNAMIC_PREFIXES.get(prefix)
        if not attribute:
            raise AssertionError(
                f"unmapped dynamic reason-code prefix {prefix!r}: add it to "
                "_DYNAMIC_PREFIXES and register the codes it can produce"
            )
        return {f"{prefix}{state}" for state in getattr(state_machine, attribute)}
    return set()


def _local_string_bindings(tree: ast.AST) -> dict[str, set[str]]:
    """Every string literal each local name can hold, module-wide.

    ``reason = "a" if cond else "b"`` is common in the classifier, so a bare Name
    argument still resolves to a closed literal set.
    """
    bindings: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        values = _possible_strings(node.value, bindings)
        if not values:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, set()).update(values)
    return bindings


def _emitted_reason_codes() -> set[str]:
    """Every literal reason code reachable in the completion authority."""
    source = pathlib.Path(state_machine.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    bindings = _local_string_bindings(tree)
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_decision"
                and len(node.args) >= 3):
            continue
        reason = node.args[2]
        resolved = _possible_strings(reason, bindings)
        if resolved:
            codes.update(resolved)
            continue
        # `code or failure` forwards a merge_gate finding verbatim. Its vocabulary is
        # the finding sets and failure classes added below, not a literal here.
        names = {
            part.id for part in ast.walk(reason) if isinstance(part, ast.Name)
        }
        if not names or not names <= _FORWARDED_FINDING_NAMES:
            raise AssertionError(
                f"unrecognised reason-code expression at line {reason.lineno}: "
                f"{sorted(names)}"
            )

    # Gate findings and failure classes reach the route verbatim via `code or failure`.
    for name in ("_HUMAN_FINDINGS", "_REVIEW_FINDINGS", "_REMEDIATION_FINDINGS",
                 "_COORD_FINDINGS"):
        codes.update(getattr(state_machine, name))
    codes.update(_FORWARDED_FINDING_VALUES)
    return codes


class ReasonCodeRegistryTest(unittest.TestCase):
    def test_every_emitted_reason_code_is_registered(self):
        emitted = _emitted_reason_codes()
        self.assertTrue(emitted, "expected the classifier to emit reason codes")
        unregistered = sorted(
            code for code in emitted if not reason_codes.is_registered(code)
        )
        self.assertEqual(
            unregistered, [],
            "unregistered reason codes reach the board; add them to "
            "switchboard.reason_code.v1",
        )

    def test_no_two_registered_codes_differ_only_by_spelling(self):
        seen: dict[str, str] = {}
        for code in reason_codes.REASON_CODES:
            key = reason_codes.spelling_key(code)
            self.assertNotIn(
                key, seen,
                f"{code!r} and {seen.get(key)!r} differ only by spelling and would "
                "split one cohort in every count",
            )
            seen[key] = code

    def test_us_spelling_folds_onto_the_registered_cohort(self):
        # The state machine emits f"required_ci_{state}" for both spellings in
        # _COORD_CI. Without folding, one cancelled CI run counts as two codes.
        self.assertIn("canceled", state_machine._COORD_CI)
        self.assertIn("cancelled", state_machine._COORD_CI)
        self.assertEqual(
            reason_codes.canonical_reason_code("required_ci_canceled"),
            "required_ci_cancelled",
        )
        self.assertTrue(reason_codes.is_registered("required_ci_canceled"))

    def test_registry_entries_declare_a_closed_vocabulary(self):
        for code, entry in reason_codes.REASON_CODES.items():
            self.assertEqual(entry.code, code)
            self.assertIn(entry.family, reason_codes.FAMILIES)
            self.assertIn(entry.expected, reason_codes.EXPECTED)
            self.assertIn(entry.resolver, reason_codes.RESOLVERS)

    def test_an_unregistered_code_is_reported_not_silently_accepted(self):
        self.assertFalse(reason_codes.is_registered("totally_made_up_code"))
        self.assertIsNone(reason_codes.get_reason_code("totally_made_up_code"))
        # Normalization must not invent a home for it.
        self.assertEqual(
            reason_codes.canonical_reason_code("totally_made_up_code"),
            "totally_made_up_code",
        )


# ---------------------------------------------------------------------------
# §4.2 the feature allowlist — shape, never content
# ---------------------------------------------------------------------------

def _snapshot(*, task_id="COORD-50", head=HEAD, ci="SUCCESS", draft=False,
              review="passed", contexts=True, generation=1, host_id="host-1",
              runner_live=False):
    status_contexts = (
        [{"context": "Switchboard CI / VM gate", "state": ci,
          "failure_attribution": "product"}] if contexts else []
    )
    return state_machine.build_completion_snapshot(
        task={"task_id": task_id, "status": "In Review",
              "title": "a private task title nobody may pool",
              "git_state": {"head_sha": head, "pr_number": 810,
                            "pr_url": "https://github.com/acme/private/pull/810"}},
        github_pr={
            "number": 810, "state": "OPEN", "draft": draft, "mergeable": True,
            "mergeStateStatus": "CLEAN", "head": {"sha": head},
            "status_contexts": status_contexts,
            "url": "https://github.com/acme/private/pull/810",
        },
        required_status_contexts=["Switchboard CI / VM gate"],
        review={"status": review, "head_sha": head,
                "pr_url": "https://github.com/acme/private/pull/810"},
        merge_gate={"findings": [],
                    "pr_url": "https://github.com/acme/private/pull/810"},
        runner={"live": runner_live, "generation": generation,
                "host_id": host_id, "role": "review_merge", "head_sha": head},
    )


class DecisionFeaturesTest(unittest.TestCase):
    def test_projection_is_exactly_the_declared_allowlist(self):
        snapshot = _snapshot()
        decision = state_machine.classify_completion(None, snapshot)
        projected = features_mod.project_features(snapshot, decision)
        self.assertEqual(
            tuple(projected), features_mod.FEATURE_FIELDS,
            "the projection must carry the allowlist in declared order and nothing else",
        )
        self.assertEqual(len(features_mod.FEATURE_FIELDS), 23)

    def test_every_value_is_a_bool_or_a_closed_enum_or_a_bucket(self):
        snapshot = _snapshot()
        decision = state_machine.classify_completion(None, snapshot)
        projected = features_mod.project_features(snapshot, decision)
        for name, value in projected.items():
            self.assertIsInstance(
                value, (bool, str), f"{name} must be a bool or a small string",
            )
            if isinstance(value, str):
                self.assertLessEqual(
                    len(value), 24, f"{name}={value!r} is too wide to be an enum",
                )

    def test_no_snapshot_content_survives_the_projection(self):
        snapshot = _snapshot()
        decision = state_machine.classify_completion(None, snapshot)
        blob = json.dumps(features_mod.project_features(snapshot, decision))
        for leak in (HEAD, "810", "acme", "private", "a private task title",
                     "Switchboard CI / VM gate", "host-1"):
            self.assertNotIn(
                leak, blob, f"{leak!r} leaked into the poolable projection",
            )

    def test_unexpected_upstream_values_collapse_to_other(self):
        snapshot = _snapshot()
        snapshot["github_pr"]["mergeStateStatus"] = "SOME_INTERNAL_TOOLING_NAME"
        projected = features_mod.project_features(snapshot, {})
        self.assertEqual(projected["pr_mergeable_state"], features_mod.OTHER)

    def test_the_draft_incident_signature_is_representable(self):
        # Spec §4.2: pr_draft=true co-occurring with contexts_fully_hydrated=false
        # is the signature that made the 2026-07-24 draft incident diagnosable.
        snapshot = _snapshot(draft=True, contexts=False)
        projected = features_mod.project_features(snapshot, {})
        self.assertTrue(projected["pr_draft"])
        self.assertFalse(projected["contexts_fully_hydrated"])


# ---------------------------------------------------------------------------
# §4.3 / §5 the ledger — episodes, not ticks
# ---------------------------------------------------------------------------

class DecisionCorpusStorageTest(unittest.TestCase):
    project = "switchboard"

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE tasks ("
            "task_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "assignee TEXT, updated_at REAL)")
        self.db.execute(
            "CREATE TABLE task_git_state ("
            "task_id TEXT PRIMARY KEY, pr_number INTEGER, head_sha TEXT, "
            "branch TEXT, pr_url TEXT, merged_sha TEXT, evidence_json TEXT)")
        self.db.execute(
            "CREATE TABLE deliverable_task_links ("
            "id TEXT PRIMARY KEY, deliverable_id TEXT NOT NULL, "
            "project_id TEXT NOT NULL, task_id TEXT NOT NULL, created_at REAL)")
        wanted = {
            "0074_task_execution_completion_phases",
            "0075_ix_task_execution_completion_identity",
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

    def _link(self, task_id, deliverable_id):
        self.db.execute(
            "INSERT INTO deliverable_task_links("
            "id, deliverable_id, project_id, task_id, created_at) VALUES (?,?,?,?,?)",
            (f"link-{task_id}", deliverable_id, self.project, task_id, 1.0))
        self.db.commit()

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

    # -- episode collapsing --------------------------------------------------

    def test_two_hundred_ticks_on_an_unchanged_head_are_one_episode(self):
        snapshot = _snapshot()
        for index in range(200):
            record = self._tick(snapshot, at=self.now + index)
        self.assertEqual(record["tick_count"], 200)
        rows = self.db.execute("SELECT COUNT(*) AS n FROM decision_records").fetchone()
        self.assertEqual(
            rows["n"], 1,
            "a stuck task must not dominate every count with polling frequency",
        )
        self.assertEqual(record["first_seen_at"], self.now)
        self.assertEqual(record["last_seen_at"], self.now + 199)

    def test_the_hydrated_snapshot_timestamp_churn_does_not_split_an_episode(self):
        # The hydrated snapshot embeds the whole task row; its updated_at moves on
        # every tick. Hashing the raw body would make every tick unique and defeat
        # episode collapsing entirely.
        first = _snapshot()
        first["task"]["updated_at"] = 1.0
        second = _snapshot()
        second["task"]["updated_at"] = 2.0
        self._tick(first)
        record = self._tick(second)
        self.assertEqual(record["tick_count"], 2)

    def test_a_changed_decision_opens_a_new_episode(self):
        snapshot = _snapshot()
        self._tick(snapshot, self._verdict("required_exact_head_ci_failed"))
        self._tick(snapshot, self._verdict("required_exact_head_ci_pending"))
        rows = self.db.execute("SELECT COUNT(*) AS n FROM decision_records").fetchone()
        self.assertEqual(rows["n"], 2)

    def test_a_new_head_opens_a_new_episode(self):
        verdict = self._verdict("required_exact_head_ci_failed")
        self._tick(_snapshot(head=HEAD), verdict)
        self._tick(_snapshot(head=OTHER_HEAD), verdict)
        heads = [
            row["head_sha"] for row in
            self.db.execute("SELECT head_sha FROM decision_records").fetchall()
        ]
        self.assertEqual(sorted(heads), sorted([HEAD, OTHER_HEAD]))

    # -- the overwrite defect ------------------------------------------------

    def test_the_reason_code_timeline_survives_where_completion_runs_overwrites(self):
        """The defect this task exists to fix, pinned against the live behaviour."""
        self.db.execute(
            "INSERT INTO tasks(task_id, status, assignee, updated_at) "
            "VALUES ('COORD-50','In Review',NULL,1.0)")
        self.db.commit()
        timeline = [
            "required_ci_hydration_missing",
            "required_exact_head_ci_pending",
            "required_exact_head_ci_failed",
            "review_required",
        ]
        for index, code in enumerate(timeline):
            snapshot = _snapshot()
            verdict = self._verdict(code)
            self._tick(snapshot, verdict, at=self.now + index)
            completion_runs.transition_completion_run(
                {
                    "task_id": "COORD-50", "pr_number": 810, "head_sha": HEAD,
                    "state": "blocked", "route": "coordination_retry",
                    "reason_code": code, "desired_role": "",
                    "board_status": "In Review",
                    "evidence_refs": {"ci": {"head_sha": HEAD, "status": code}},
                    "runner_generation": 1,
                },
                actor="test", project=self.project,
            )

        # completion_runs keeps only the latest verdict — the history is destroyed.
        current = completion_runs.get_active_completion_run(
            "COORD-50", project=self.project)
        self.assertEqual(current["reason_code"], timeline[-1])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) AS n FROM completion_runs").fetchone()["n"], 1)

        # The append-only companion still has no reason_code column at all.
        columns = {
            row["name"] for row in self.db.execute(
                "PRAGMA table_info(task_execution_completion_phases)").fetchall()
        }
        self.assertNotIn("reason_code", columns)

        # The corpus retains every verdict, in order.
        episodes = decision_records.list_decision_episodes(
            project=self.project, task_id="COORD-50")
        self.assertEqual([item["reason_code"] for item in episodes], timeline)

    # -- DOGFOOD-19 replay ---------------------------------------------------

    def test_replaying_the_dogfood19_window_surfaces_the_dominant_single_cause(self):
        """Three PRs, one classifier ordering bug, ~40 ticks each.

        Before the corpus this read as three unrelated per-task retries. The count
        must name `required_exact_head_ci_failed` as one dominant cause, and must
        do it on episodes so the retry loop cannot manufacture the signal.
        """
        stalled = ["DOGFOOD-19", "COORD-49", "BUG-178"]
        for index, task_id in enumerate(stalled):
            snapshot = _snapshot(task_id=task_id, head=chr(ord("a") + index) * 40)
            verdict = self._verdict(
                "required_exact_head_ci_failed", route="remediation",
                role="remediation")
            for tick in range(40):
                self._tick(snapshot, verdict, at=self.now + tick)
        # Unrelated background noise from a healthy task.
        self._tick(
            _snapshot(task_id="UI-70", head="f" * 40),
            self._verdict("required_exact_head_ci_pending", route="wait"),
        )

        counts = decision_records.count_reason_code_episodes(
            project=self.project, since=self.now - 1)
        dominant = counts["dominant_reason_code"]
        self.assertEqual(dominant["reason_code"], "required_exact_head_ci_failed")
        self.assertEqual(dominant["episodes"], 3)
        self.assertEqual(dominant["tasks"], 3)
        self.assertEqual(dominant["ticks"], 120)
        self.assertEqual(dominant["share"], 0.75)
        self.assertTrue(dominant["registered"])
        self.assertEqual(dominant["family"], "required_ci")
        self.assertEqual(counts["total_episodes"], 4)

    def test_counts_are_scopeable_to_a_deliverable_and_a_host(self):
        self._link("COORD-50", "DEL-CORPUS")
        self._tick(
            _snapshot(task_id="COORD-50", host_id="mac-1"),
            self._verdict("required_exact_head_ci_failed"))
        self._tick(
            _snapshot(task_id="UI-70", head="c" * 40, host_id="aws-1"),
            self._verdict("required_exact_head_ci_failed"))

        by_deliverable = decision_records.count_reason_code_episodes(
            project=self.project, deliverable_id="DEL-CORPUS")
        self.assertEqual(by_deliverable["total_episodes"], 1)

        by_host = decision_records.count_reason_code_episodes(
            project=self.project, host_id="aws-1")
        self.assertEqual(by_host["total_episodes"], 1)

        unscoped = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(unscoped["total_episodes"], 2)

    def test_a_window_excludes_episodes_outside_it(self):
        verdict = self._verdict("required_exact_head_ci_failed")
        self._tick(_snapshot(), verdict, at=self.now - 86400)
        self._tick(_snapshot(head=OTHER_HEAD), verdict, at=self.now)
        recent = decision_records.count_reason_code_episodes(
            project=self.project, since=self.now - 60)
        self.assertEqual(recent["total_episodes"], 1)

    def test_an_unregistered_code_is_visible_rather_than_silently_free_text(self):
        self._tick(_snapshot(), self._verdict("invented_by_a_new_call_site"))
        counts = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(
            counts["unregistered_reason_codes"], ["invented_by_a_new_call_site"])
        self.assertFalse(counts["codes"][0]["registered"])

    def test_both_cancel_spellings_count_as_one_cohort(self):
        self._tick(_snapshot(), self._verdict("required_ci_cancelled"))
        self._tick(
            _snapshot(head=OTHER_HEAD), self._verdict("required_ci_canceled"))
        counts = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(len(counts["codes"]), 1)
        self.assertEqual(counts["codes"][0]["reason_code"], "required_ci_cancelled")
        self.assertEqual(counts["codes"][0]["episodes"], 2)

    # -- §6 the projection boundary -----------------------------------------

    def test_export_reads_the_projected_half_only(self):
        self._tick(_snapshot(), self._verdict("required_exact_head_ci_failed"))
        rows = decision_records.export_projection(project=self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), decision_records.EXPORT_COLUMNS)
        for private in decision_records.PRIVATE_COLUMNS:
            self.assertNotIn(
                private, rows[0],
                f"{private} must never appear in an exported projection",
            )
        blob = json.dumps(rows[0])
        for leak in (HEAD, "acme", "private", "COORD-50", "host-1"):
            self.assertNotIn(leak, blob)

    def test_export_columns_and_private_columns_do_not_overlap(self):
        overlap = set(decision_records.EXPORT_COLUMNS) & set(
            decision_records.PRIVATE_COLUMNS)
        self.assertEqual(overlap, set())

    def test_every_table_column_is_classified_as_export_or_private(self):
        columns = {
            row["name"] for row in
            self.db.execute("PRAGMA table_info(decision_records)").fetchall()
        }
        classified = set(decision_records.EXPORT_COLUMNS) | set(
            decision_records.PRIVATE_COLUMNS)
        unclassified = columns - classified - {"snapshot_retained"}
        self.assertEqual(
            unclassified, set(),
            "a new column must be declared export-safe or private before it ships",
        )

    # -- §5 retention --------------------------------------------------------

    def test_compaction_drops_old_bodies_and_keeps_the_demonstrations(self):
        old = self.now - (120 * 86400)
        ordinary = self._tick(
            _snapshot(task_id="OLD-1"),
            self._verdict("required_exact_head_ci_pending"), at=old)
        escalated = self._tick(
            _snapshot(task_id="OLD-2", head=OTHER_HEAD),
            self._verdict("human_review_findings", route="human"), at=old)
        fresh = self._tick(
            _snapshot(task_id="NEW-1", head="c" * 40),
            self._verdict("review_required"), at=self.now)
        decision_records.record_episode_outcome(
            project=self.project, task_id="OLD-2", outcome="escalated",
            record_ids=[escalated["record_id"]])

        result = decision_records.compact_decision_snapshots(
            project=self.project, now=self.now)
        self.assertEqual(result["compacted"], 1)

        retained = {
            row["record_id"]: bool(row["snapshot_retained"])
            for row in self.db.execute(
                "SELECT record_id, snapshot_retained FROM decision_records").fetchall()
        }
        self.assertFalse(retained[ordinary["record_id"]])
        self.assertTrue(retained[escalated["record_id"]])
        self.assertTrue(retained[fresh["record_id"]])

        # The projected half is untouched — it is the corpus.
        counts = decision_records.count_reason_code_episodes(project=self.project)
        self.assertEqual(counts["total_episodes"], 3)

    def test_compaction_is_idempotent(self):
        self._tick(_snapshot(), self._verdict("review_required"),
                   at=self.now - (120 * 86400))
        first = decision_records.compact_decision_snapshots(
            project=self.project, now=self.now)
        second = decision_records.compact_decision_snapshots(
            project=self.project, now=self.now)
        self.assertEqual(first["compacted"], 1)
        self.assertEqual(second["compacted"], 0)

    # -- §7 off-policy hygiene ----------------------------------------------

    def test_advice_version_is_null_on_the_control_arm(self):
        record = self._tick(_snapshot(), self._verdict("review_required"))
        self.assertIsNone(record["advice_version"])

    def test_the_private_half_is_retained_on_an_automated_tick(self):
        # The attention path was the only thing persisting a snapshot, so the full
        # feature vector survived exactly the cases a human was already handling.
        record = self._tick(_snapshot(), self._verdict("review_required"))
        self.assertTrue(record["snapshot_retained"])
        self.assertEqual(record["snapshot"]["schema"],
                         state_machine.COMPLETION_SNAPSHOT_SCHEMA)
        self.assertEqual(record["classifier_version"],
                         state_machine.COMPLETION_CLASSIFIER_VERSION)

    def test_a_record_without_a_task_id_is_refused(self):
        with self.assertRaises(decision_records.DecisionRecordError):
            self._tick({"schema": state_machine.COMPLETION_SNAPSHOT_SCHEMA},
                       self._verdict("review_required"))

    def test_an_unknown_outcome_is_refused(self):
        self._tick(_snapshot(), self._verdict("review_required"))
        with self.assertRaises(decision_records.DecisionRecordError):
            decision_records.record_episode_outcome(
                project=self.project, task_id="COORD-50", outcome="probably_fine")

    # -- the production tick actually writes --------------------------------

    def test_an_automated_completion_tick_appends_to_the_corpus(self):
        """The wiring itself, not just the repository.

        Before this, the only path that persisted a snapshot was the attention
        path — so the full feature vector survived exactly the cases where a human
        was already being pulled in, and was discarded for the automated majority.
        """
        snapshot = _snapshot(ci="failure")
        adapters = CompletionEffectAdapters(
            ensure_review_generation=lambda plan: {"ok": True},
            start_remediation=lambda plan: {"ok": True},
            mark_ready=lambda plan: {"ok": True},
            enqueue=lambda plan: {"ok": True},
            requeue_merge_group=lambda plan: {"ok": True},
            repair_dispatch=lambda plan: {"ok": True},
            fence_runner=lambda plan: {"ok": True},
            reconcile_provenance=lambda plan: {"ok": True},
        )
        with (
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
        ):
            result = completion_driver.run_completion_tick(
                "COORD-50",
                project=self.project,
                actor="test",
                agent_id="claude/COORD-50",
                hydrator=lambda task_id, **_: snapshot,
                adapters=adapters,
            )

        self.assertEqual(
            result["decision"]["reason_code"], "required_exact_head_ci_failed")
        self.assertTrue(result["decision_record"]["record_id"])
        self.assertTrue(result["decision_record"]["reason_code_registered"])

        episodes = decision_records.list_decision_episodes(
            project=self.project, task_id="COORD-50")
        self.assertEqual(len(episodes), 1)
        self.assertEqual(
            episodes[0]["reason_code"], "required_exact_head_ci_failed")
        self.assertEqual(episodes[0]["features"]["any_required_context_failed"], True)


if __name__ == "__main__":
    unittest.main()
