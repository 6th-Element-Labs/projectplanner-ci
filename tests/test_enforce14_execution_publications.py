import sqlite3

from path_setup import ROOT  # noqa: F401

from switchboard.application.commands import execution_context
from switchboard.storage.repositories.execution_publications import (
    ExecutionPublicationError,
    build_binding,
    get_for_task_in,
    persist_in,
    validate_event,
)


def context(project="atlas", task="BUILD-1", repo="acme/monorepo",
            default_branch="main", generation=3):
    value = {
        "schema": execution_context.SCHEMA,
        "project_id": project,
        "task_id": task,
        "repo_role": "canonical",
        "repository": repo,
        "default_branch": default_branch,
        "base_sha": "a" * 40,
        "workspace": {"isolation": "worktree", "repo_role": "canonical"},
        "runtime": {"requested": "codex", "registry_name": "codex"},
        "provider": {"connection_reference": "provider-atlas"},
        "scm": {"connection_reference": "scm-atlas"},
        "generation": generation,
        "authority_digest": "sha256:authority",
    }
    value["digest"] = execution_context._digest(value)
    return value


def database():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE execution_publications ("
        "publication_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "task_id TEXT NOT NULL, execution_id TEXT NOT NULL, "
        "execution_generation INTEGER NOT NULL, repo_role TEXT NOT NULL, "
        "repository TEXT NOT NULL, default_branch TEXT NOT NULL, "
        "base_sha TEXT NOT NULL, branch TEXT NOT NULL, head_sha TEXT NOT NULL, "
        "pr_number INTEGER NOT NULL, pr_url TEXT NOT NULL, "
        "scm_connection_ref TEXT NOT NULL, context_digest TEXT NOT NULL, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
        "UNIQUE(project_id, task_id, execution_id, head_sha), "
        "UNIQUE(project_id, repository, pr_number, head_sha))")
    return c


def binding(**overrides):
    values = {
        "project": "atlas",
        "task_id": "BUILD-1",
        "execution_id": "exec-atlas-3",
        "execution_context": context(),
        "branch": "codex/BUILD-1-work",
        "head_sha": "b" * 40,
        "pr_number": 17,
        "pr_url": "https://github.com/acme/monorepo/pull/17",
    }
    values.update(overrides)
    return build_binding(**values)


def raises_publication_error(fn):
    try:
        fn()
    except ExecutionPublicationError as caught:
        return caught
    raise AssertionError("expected ExecutionPublicationError")


def test_persists_exact_execution_and_scm_binding():
    c = database()
    saved = persist_in(c, binding())
    assert saved["project_id"] == "atlas"
    assert saved["execution_id"] == "exec-atlas-3"
    assert saved["base_sha"] == "a" * 40
    assert saved["scm_connection_ref"] == "scm-atlas"
    assert get_for_task_in(c, "atlas", "BUILD-1")["head_sha"] == "b" * 40


def test_publish_context_mismatches_fail_closed():
    mismatches = [
        ("project", "switchboard"),
        ("task_id", "OTHER-1"),
        ("pr_url", "https://github.com/acme/other/pull/17"),
        ("pr_number", 18),
    ]
    for field, value in mismatches:
        caught = raises_publication_error(
            lambda field=field, value=value: binding(**{field: value}))
        assert caught.code == "execution_publication_context_mismatch"


def test_event_is_fenced_to_owner_repo_branch_head_and_pr():
    value = binding()
    validate_event(
        value, project="atlas", repository="acme/monorepo", pr_number=17,
        branch="codex/BUILD-1-work", head_sha="b" * 40, base_branch="main")
    raises_publication_error(lambda:
        validate_event(
            value, project="switchboard", repository="acme/monorepo", pr_number=17,
            branch="codex/BUILD-1-work", head_sha="b" * 40))
    raises_publication_error(lambda:
        validate_event(
            value, project="atlas", repository="acme/monorepo", pr_number=17,
            branch="codex/BUILD-1-work", head_sha="c" * 40))


if __name__ == "__main__":
    test_persists_exact_execution_and_scm_binding()
    test_publish_context_mismatches_fail_closed()
    test_event_is_fenced_to_owner_repo_branch_head_and_pr()
