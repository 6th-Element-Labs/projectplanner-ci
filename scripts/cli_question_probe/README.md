# CLI question-event probe

Proves that each CLI coding agent surfaces "I need a human" as a **machine
event**, not terminal text. See `../../docs/CLI-QUESTION-EVENTS.md` for the full
write-up and per-runtime event schemas.

```bash
./run_probe.sh claude    # live — captures a real permission_request event
./run_probe.sh codex     # needs OPENAI_API_KEY (or `codex login`)
./run_probe.sh cursor    # needs CURSOR_API_KEY (or `cursor-agent login`)
```

Captured events land in `questions-queue.jsonl` — that file is a stand-in for the
operator question queue. `perm_mcp.py` is the 30-line permission gate a host
runner would own.
