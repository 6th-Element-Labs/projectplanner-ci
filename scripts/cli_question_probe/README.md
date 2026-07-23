# CLI question-event probe

Probes whether each CLI coding agent surfaces "I need a human" as a **machine
request with a reply path**, not terminal text. See
`../../docs/CLI-QUESTION-EVENTS.md` for the proven and unsupported results.

```bash
./run_probe.sh claude    # live — captures a real permission_request event
./run_probe.sh codex     # needs OPENAI_API_KEY (or `codex login`)
./run_probe.sh cursor    # lifecycle stream only; attention remains unsupported
```

Captured events land in runtime stream files. Only a proven request/reply event
may be copied into `questions-queue.jsonl`; Cursor events intentionally are not.
`perm_mcp.py` is the permission gate a Claude host runner would own.
