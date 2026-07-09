#!/usr/bin/env python3
"""UI-13 — multi-project email intake + per-project document corpus (de-Maxwell the pipeline).

Proves the acceptance criteria without a live mailbox or LLM gateway:
  1. corpus isolation  — a doc ingested on project X is searchable on X and INVISIBLE on Y;
  2. inbox isolation   — an inbox item added on X shows only on X (dedupe is per-board too);
  3. Maxwell unchanged — the default project still ingests/searches its own corpus;
  4. routing           — gmail_source._route resolves plus-address > sender-domain map >
                         global-allowlist fallback -> default project (today's behavior).

Each project is its own sqlite file (physical isolation, no project column) — the pattern
every other store uses. Run directly: `python test_ui13_multi_project_intake.py`.
"""
import hashlib
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="ui13-intake-")
os.environ["PM_DB_PATH"] = os.path.join(_TMP, "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = os.path.join(_TMP, "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = os.path.join(_TMP, "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = os.path.join(_TMP, "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import store          # noqa: E402
    import rag            # noqa: E402
    import gmail_source   # noqa: E402
except ModuleNotFoundError as exc:
    print(f"  SKIP  UI-13 intake smoke requires optional dependency: {exc.name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(0)

_FAILURES = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _FAILURES.append(msg)


def _fake_embed(texts):
    """Deterministic offline stand-in for the LLM gateway embedder — 16 dims, non-zero."""
    out = []
    for t in texts:
        h = hashlib.sha256((t or "").encode()).digest()
        out.append([b / 255.0 for b in h[:16]])
    return out


def setup():
    for p in ("maxwell", "helm", "switchboard"):
        store.init_db(p)
    # Offline embeddings; stub the static plan-docs index to empty so Maxwell search reflects
    # only what THIS test ingests (build_index() would otherwise hit the gateway).
    rag._embed = _fake_embed
    rag._index = []
    rag._dyn, rag._dyn_ver = {}, {}


def test_corpus_isolation():
    print("\n[1] per-project corpus isolation")
    n_helm = rag.add_document("transcript", "Helm sonar notes",
                              "the depth sounder pings at 200kHz off the transom", project="helm")
    n_sb = rag.add_document("transcript", "SB coordination notes",
                            "the switchboard wake substrate dispatches claim_next intents", project="switchboard")
    check(n_helm == 1 and n_sb == 1, f"add_document chunked both docs (helm={n_helm}, sb={n_sb})")

    helm_hits = rag.search("depth sounder", project="helm")
    sb_hits = rag.search("wake substrate", project="switchboard")
    mx_hits = rag.search("depth sounder", project="maxwell")

    helm_text = " ".join(h["text"] for h in helm_hits)
    sb_text = " ".join(h["text"] for h in sb_hits)
    check(len(helm_hits) == 1 and "depth sounder" in helm_text, "helm search finds the helm transcript")
    check("switchboard wake substrate" not in helm_text, "helm search does NOT see the switchboard transcript")
    check(len(sb_hits) == 1 and "wake substrate" in sb_text, "switchboard search finds the switchboard transcript")
    check("depth sounder" not in sb_text, "switchboard search does NOT see the helm transcript")
    check(mx_hits == [], "maxwell (default) corpus is empty — never sees helm/switchboard docs")


def test_maxwell_unchanged():
    print("\n[2] Maxwell (default project) still ingests + searches its own corpus")
    n = rag.add_document("note", "Maxwell TEEP note", "the barnett wells report methane flux weekly")
    hits = rag.search("methane flux")            # no project= -> default maxwell
    text = " ".join(h["text"] for h in hits)
    check(n == 1, "default add_document (no project arg) ingests into maxwell")
    check("methane flux" in text, "default search (no project arg) finds the maxwell doc")
    check("depth sounder" not in text and "wake substrate" not in text,
          "maxwell search still excludes other projects' corpora")


def test_inbox_isolation():
    print("\n[3] per-project inbox isolation")
    store.add_inbox_item("email", "helm-1", "skipper@boat.com", "Helm inbox subj", "s", {}, project="helm")
    store.add_inbox_item("email", "sb-1", "ops@6el.com", "SB inbox subj", "s", {}, project="switchboard")

    helm_box = store.list_inbox(project="helm")
    sb_box = store.list_inbox(project="switchboard")
    mx_box = store.list_inbox(project="maxwell")
    check([i["subject"] for i in helm_box] == ["Helm inbox subj"], "helm inbox shows only the helm item")
    check([i["subject"] for i in sb_box] == ["SB inbox subj"], "switchboard inbox shows only the switchboard item")
    check(mx_box == [], "maxwell inbox is empty — isolated from helm/switchboard items")
    check(store.inbox_pending_count(project="helm") == 1 and store.inbox_pending_count(project="maxwell") == 0,
          "inbox_pending_count is per-project")
    # dedupe key is scoped to the board, too
    check(store.inbox_exists("email", "helm-1", project="helm") is True, "dedupe sees the item on its own board")
    check(store.inbox_exists("email", "helm-1", project="switchboard") is False,
          "dedupe does NOT see the item across boards")


def test_routing():
    print("\n[4] gmail_source routing: plus-address > domain map > allowlist fallback")
    os.environ["PM_INBOX_ROUTES"] = "totalenergy.com=maxwell, acme.com=helm"
    os.environ["PM_INBOX_ALLOWLIST"] = ""   # empty -> accept-all for unmapped senders (today's default)

    check(gmail_source._route("Anyone <x@random.com>", "plan+switchboard@taikunai.com") == (True, "switchboard"),
          "plus-address plan+switchboard@ routes to switchboard (accepts)")
    check(gmail_source._route("Bob <bob@acme.com>", "plan@taikunai.com") == (True, "helm"),
          "sender-domain acme.com routes to helm (accepts)")
    check(gmail_source._route("bob@mail.acme.com", "plan@taikunai.com") == (True, "helm"),
          "subdomain of a mapped domain routes to helm")
    check(gmail_source._route("j@totalenergy.com", "plan@taikunai.com") == (True, "maxwell"),
          "sender-domain totalenergy.com routes to maxwell")
    check(gmail_source._route("bob@acme.com", "plan+switchboard@taikunai.com") == (True, "switchboard"),
          "plus-address WINS over the sender-domain map")
    check(gmail_source._route("bob@random.com",
                              "plan@taikunai.com, plan+helm@taikunai.com") == (True, "helm"),
          "plus-address is found even when it is not the first recipient (comma list)")
    check(gmail_source._route("stranger@nowhere.com", "plan@taikunai.com") == (True, "maxwell"),
          "unmapped sender falls back to default project (accept-all allowlist)")
    check(gmail_source._route("x@random.com", "plan+bogus@taikunai.com") == (True, "maxwell"),
          "unknown plus-tag is ignored -> falls back to default project")

    os.environ["PM_INBOX_ALLOWLIST"] = "knownpartner.com"
    check(gmail_source._route("y@knownpartner.com", "plan@taikunai.com") == (True, "maxwell"),
          "allowlisted unmapped sender is accepted -> default project")
    check(gmail_source._route("z@stranger.com", "plan@taikunai.com") == (False, "maxwell"),
          "non-allowlisted unmapped sender is REJECTED (unchanged allowlist gate)")


def main():
    setup()
    test_corpus_isolation()
    test_maxwell_unchanged()
    test_inbox_isolation()
    test_routing()
    shutil.rmtree(_TMP, ignore_errors=True)
    print()
    if _FAILURES:
        print(f"FAILED — {len(_FAILURES)} check(s):")
        for f in _FAILURES:
            print("  - " + f)
        sys.exit(1)
    print("PASSED — UI-13 multi-project intake + corpus isolation + routing")


if __name__ == "__main__":
    main()
