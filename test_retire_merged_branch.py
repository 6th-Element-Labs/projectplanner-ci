"""BUG-29: retire (archive+delete) a merged PR head branch. Unit tests, no network."""
import os
import store


def _reset():
    os.environ.pop("PM_RETIRE_MERGED_BRANCHES", None)


def test_disabled_by_default():
    _reset()
    assert store.retire_merged_branch("owner/repo", "claude/FOO-1-x", "sha") == {
        "retired": False, "reason": "disabled"}


def test_archive_then_delete():
    os.environ["PM_RETIRE_MERGED_BRANCHES"] = "1"
    calls = []

    def fake_write(method, repo, path, token="", data=None):
        calls.append((method, repo, path, data))
        return (201 if method == "POST" else 204), None

    orig_w, orig_t = store._github_write, store._github_token
    store._github_write, store._github_token = fake_write, (lambda: "tok")
    try:
        r = store.retire_merged_branch("owner/repo", "claude/FOO-1-x", "deadbeef")
    finally:
        store._github_write, store._github_token = orig_w, orig_t
        _reset()
    assert r["retired"] is True and r["archived"] is True, r
    posts = [c for c in calls if c[0] == "POST"]
    dels = [c for c in calls if c[0] == "DELETE"]
    assert posts and posts[0][3]["ref"] == "refs/tags/archive/claude/FOO-1-x", posts
    assert dels and dels[0][2] == "git/refs/heads/claude/FOO-1-x", dels
    assert calls.index(posts[0]) < calls.index(dels[0]), ("archive must precede delete", calls)


def test_no_delete_when_archive_fails():
    os.environ["PM_RETIRE_MERGED_BRANCHES"] = "1"
    methods = []

    def fake_write(method, repo, path, token="", data=None):
        methods.append(method)
        return (403, {"error": "forbidden"}) if method == "POST" else (204, None)

    orig_w, orig_t = store._github_write, store._github_token
    store._github_write, store._github_token = fake_write, (lambda: "tok")
    try:
        r = store.retire_merged_branch("owner/repo", "b", "sha")
    finally:
        store._github_write, store._github_token = orig_w, orig_t
        _reset()
    assert r["retired"] is False and "archive_failed" in r.get("error", ""), r
    assert "DELETE" not in methods, ("must NOT delete when archive fails", methods)


def test_already_gone_is_success():
    os.environ["PM_RETIRE_MERGED_BRANCHES"] = "1"

    def fake_write(method, repo, path, token="", data=None):
        return (201 if method == "POST" else 404), None  # branch already deleted

    orig_w, orig_t = store._github_write, store._github_token
    store._github_write, store._github_token = fake_write, (lambda: "tok")
    try:
        r = store.retire_merged_branch("owner/repo", "b", "sha")
    finally:
        store._github_write, store._github_token = orig_w, orig_t
        _reset()
    assert r["retired"] is True and r.get("already_gone") is True, r


def test_no_token_is_visible_noop():
    os.environ["PM_RETIRE_MERGED_BRANCHES"] = "1"
    orig_t = store._github_token
    store._github_token = (lambda: "")
    try:
        r = store.retire_merged_branch("owner/repo", "b", "sha")
    finally:
        store._github_token = orig_t
        _reset()
    assert r == {"retired": False, "reason": "no_github_token"}, r


if __name__ == "__main__":
    test_disabled_by_default()
    test_archive_then_delete()
    test_no_delete_when_archive_fails()
    test_already_gone_is_success()
    test_no_token_is_visible_noop()
    print("test_retire_merged_branch: PASS")
