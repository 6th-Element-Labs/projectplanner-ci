#!/usr/bin/env python3
"""HARDEN-68: unit tests for the auto-update-PRs selection logic (no network)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
import auto_update_prs as bot  # noqa: E402

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


def pr(number, **kw):
    base = {"number": number, "isDraft": False, "baseRefName": "master",
            "mergeStateStatus": "BEHIND", "behind_by": 3, "conflicting": False, "autoMerge": False}
    base.update(kw)
    return base


try:
    # 1. a behind, clean PR is selected.
    ok(bot.select_prs_to_update([pr(1)]) == [1],
       "a PR behind master and not conflicting is selected for update")

    # 2. an already-current PR is skipped (no wasted gate run).
    ok(bot.select_prs_to_update([pr(2, behind_by=0, mergeStateStatus="CLEAN")]) == [],
       "a PR already current is not updated")

    # 3. a conflicting PR is never touched (update-branch can't resolve it).
    ok(bot.select_prs_to_update([pr(3, mergeStateStatus="DIRTY")]) == []
       and bot.select_prs_to_update([pr(3, conflicting=True, mergeStateStatus="BEHIND")]) == [],
       "a conflicting PR is skipped (left for the agent to resolve)")

    # 4. drafts and non-master PRs are skipped.
    ok(bot.select_prs_to_update([pr(4, isDraft=True), pr(5, baseRefName="release")]) == [],
       "draft PRs and PRs targeting another base are skipped")

    # 5. auto-merge-armed PRs are ordered first (updating them is what lands them).
    order = bot.select_prs_to_update([pr(10, autoMerge=False), pr(11, autoMerge=True)])
    ok(order == [11, 10], "auto-merge-armed PRs are updated first")

    # 6. behind detection works via behind_by even when mergeStateStatus isn't BEHIND
    #    (strict:false branch protection reports CLEAN for behind PRs).
    ok(bot.select_prs_to_update([pr(6, mergeStateStatus="CLEAN", behind_by=5)]) == [6],
       "behind_by drives selection even when mergeStateStatus is not BEHIND (strict:false)")

    # 7. the cap bounds one run to a slice of a large backlog.
    many = [pr(n) for n in range(100, 130)]
    sel = bot.select_prs_to_update(many, max_updates=5)
    ok(len(sel) == 5, "max_updates caps a run so a big backlog is drained a slice at a time")

except Exception as exc:  # pragma: no cover
    import traceback
    traceback.print_exc()
    ok(False, f"unexpected exception: {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
