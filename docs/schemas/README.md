# Event JSON Schema registry

Checked-in `switchboard.*.v1` JSON Schemas generated from
`src/switchboard/contracts` (ARCH-MS-42).

## Layout

- `schemas/manifest.json` — ordered index of registered v1 `$id`s and file paths
- `schemas/switchboard.<name>.v1.json` — one JSON Schema document per contract

Unlike `openapi/switchboard.openapi.json` (ARCH-MS-41), these files **keep**
the short `$id` (for example `switchboard.task.create_command.v1`) so event
producers and consumers can resolve the same contracts used by REST and MCP.

Non-v1 registrations (for example `switchboard.project.v2`) stay in the
in-process registry but are not exported here.

## Regenerate / drift gate

```bash
python scripts/generate_schemas.py          # rewrite schemas/
python scripts/generate_schemas.py --check  # exit 1 on drift
python tests/test_arch_ms42_schema_registry.py
```

Golden wire instances for compatibility checks live under
`fixtures/contracts/`.
