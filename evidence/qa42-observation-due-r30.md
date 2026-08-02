# QA-42 observation_due replay

- The mission cursor records the latest ordered mission event sequence handled by the workflow.
- Durable wake intents persist the requested next role before the implementation lease is surrendered.
- Terminal receipts finalize the claim boundary after the host acknowledges the handoff.
- The five-minute `observation_due` backstop is the only wake path after the mission enters waiting.
