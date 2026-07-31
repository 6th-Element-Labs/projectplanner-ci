# ADR-0008/narration-event — Demoted to protocol specification

- **Status:** Historical decision record; normative detail moved on 2026-07-31
- **Original date:** 2026-07-11
- **Stable reference:** `ADR-0008/narration-event`
- **Current contract:** [Narration event contract and delivery semantics](../NARRATION-EVENT-CONTRACT.md)

The original decision established the transactional narration outbox, strict
`switchboard.narration_requested.v1` envelope, at-least-once delivery,
idempotent publication, project isolation, retry/dead-letter behavior, retention,
and recovery-only timer policy.

Those requirements are implemented and remain normative, but they are a product
protocol contract rather than a repository-wide architecture boundary. The full
text was moved without changing its technical requirements. This stable
filename remains so historical links and the `ADR-0008/narration-event`
canonical reference continue to resolve.

This record is unrelated to and does not amend
[ADR-0008/three-plane](0008-three-plane-separation.md).
