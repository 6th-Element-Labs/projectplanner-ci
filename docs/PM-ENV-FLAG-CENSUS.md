# PM environment flag census

ARCH-MS-10 turns ADR-0007's one-off `PM_*` count into an executable deletion gate.

The authoritative command is:

```bash
python3 scripts/pm_env_flag_census.py --check
```

It inventories literal `PM_*` names in tracked text files and distinguishes:

- runtime references, including adapter and operational-script consumers;
- tracked declarations in `.env.example` and systemd `Environment=` entries;
- documentation/test-only names, which are examples or deletion tombstones rather than live flags;
- dynamic families whose literal prefix ends in `_`;
- unread declarations: tracked configuration with no runtime defender.

`--check` fails when an unread declaration exists. Delete that declaration and any obsolete
documentation in the same change. Do not make the census green by adding a comment-only runtime
reference; the conservative scanner is a tripwire, and review still verifies the named consumer.

At the ARCH-MS-10 baseline, every tracked deployment declaration has a runtime defender. The four
known unread names identified during CONSOL-9 (`PM_OPERATOR_TOKEN`, `PM_SYSTEM_TOKEN`,
`PM_WAKE_ID`, and `PM_WEBHOOK_SECRET`) were already deleted from their documentation declarations
in PR #297; `test_consol9_h2_census.py` keeps them as deletion tombstones.

| Baseline measure | Count |
|---|---:|
| Literal names tracked across code, configuration, tests, and docs | 194 |
| Runtime-referenced names and dynamic-family prefixes | 183 |
| Deployment declarations | 27 |
| Unread deployment declarations | 0 |
| Documentation/test-only tombstones or examples | 11 |

Generate the current detailed table without editing a checked-in snapshot:

```bash
python3 scripts/pm_env_flag_census.py --format markdown
```

The live command output is the census. Keeping the report generated avoids a stale second source
of truth while the CI test ensures new declarations cannot silently become unread.
