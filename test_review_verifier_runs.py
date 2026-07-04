import tempfile
from pathlib import Path

import review_verifier_runs as verifier


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


findings = [
    {
        "finding_id": "FORGE-DB-1",
        "dimension": "db-contract",
        "severity": "high",
        "description": "runtime table row is partial",
    },
    {
        "finding_id": "DOCS-1",
        "dimension": "docs",
        "severity": "low",
        "description": "docs typo",
    },
]

run = verifier.create_verifier_run(findings, run_id="forge-audit-1")
ok(run["schema"] == verifier.SCHEMA, "manifest has explicit schema")
ok(len(run["jobs"]) == 6, "three skeptic lenses are generated per finding")
ok(run["summary"]["status"] == "red", "new run fails closed until load-bearing finding is verified")

docs_only_gate = verifier.create_verifier_run(
    findings,
    run_id="forge-audit-docs-gate",
    required_dimensions=["docs"],
)
ok(
    docs_only_gate["summary"]["fail_closed"] is False,
    "required_dimensions can scope fail-closed load-bearing gates",
)

high_verify = next(
    job for job in run["jobs"]
    if job["finding_id"] == "FORGE-DB-1" and job["lens"] == "verify"
)
high_repro = next(
    job for job in run["jobs"]
    if job["finding_id"] == "FORGE-DB-1" and job["lens"] == "repro"
)
high_impact = next(
    job for job in run["jobs"]
    if job["finding_id"] == "FORGE-DB-1" and job["lens"] == "impact"
)
low_jobs = [job for job in run["jobs"] if job["finding_id"] == "DOCS-1"]

run = verifier.record_job_result(
    run,
    high_verify["job_id"],
    status="completed",
    result={"verdict": "confirmed"},
    transcript_path="transcripts/high-verify.jsonl",
)
run = verifier.record_job_failure(
    run,
    high_repro["job_id"],
    error="session token-limit reached while reading transcript",
    transcript_path="transcripts/high-repro.jsonl",
)
run = verifier.record_job_result(
    run,
    low_jobs[0]["job_id"],
    status="completed",
    result={"verdict": "confirmed"},
)

summary = verifier.summarize_verifier_run(run)
ok(summary["token_limit_jobs"] == 1, "token-limit failures are structured")
ok(summary["fail_closed"] is True, "load-bearing token-limit failure keeps report red")
ok(
    summary["unverified_load_bearing"][0]["missing_lenses"] == ["repro", "impact"],
    "fail-closed summary names missing skeptic lenses",
)

resume = verifier.resume_jobs(run)
resume_ids = {job["job_id"] for job in resume}
ok(high_verify["job_id"] not in resume_ids, "completed verifier job is skipped on resume")
ok(high_repro["job_id"] in resume_ids, "token-limit verifier job is retried on resume")
ok(high_impact["job_id"] in resume_ids, "never-run verifier job is scheduled on resume")

shards = verifier.shard_jobs(resume, shard_size=2)
ok(all(len(shard) <= 2 for shard in shards), "resume jobs can be sharded")
ok(sum(len(shard) for shard in shards) == len(resume), "sharding preserves every resume job")

with tempfile.TemporaryDirectory(prefix="switchboard-verifier-run-") as tmp:
    path = Path(tmp) / "checkpoint.json"
    verifier.save_checkpoint(run, path)
    loaded = verifier.load_checkpoint(path)
    fresh = verifier.create_verifier_run(findings, run_id="forge-audit-1")
    merged = verifier.merge_checkpoint(fresh, loaded)
    merged_jobs = {job["job_id"]: job for job in merged["jobs"]}
    ok(
        merged_jobs[high_verify["job_id"]]["status"] == "completed",
        "checkpoint merge preserves completed results",
    )
    ok(
        merged_jobs[high_repro["job_id"]]["status"] == "token_limit",
        "checkpoint merge preserves structured token-limit errors",
    )

run = verifier.record_job_result(run, high_repro["job_id"], status="completed")
run = verifier.record_job_result(run, high_impact["job_id"], status="completed")
summary = verifier.summarize_verifier_run(run)
ok(summary["fail_closed"] is False, "required load-bearing lenses clear fail-closed state")

header = verifier.format_verifier_summary(run)
ok("completion=" in header and "token_limit_jobs=" in header, "report header includes ratios and errors")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
