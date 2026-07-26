# Needs-you push delivery

New durable provider questions and newly raised live-runner progress faults are delivered
through the existing project Communications `notify` configuration. The authoritative
Needs-you projection remains `/api/attention`; push stores no second queue or alert row.

Delivery is attempted synchronously before the producer call returns. Slack has a 20-second
HTTP timeout and SMTP has a 30-second connection timeout, so the documented upper bound is
50 seconds when both channels are attempted. Every attempt appends either
`attention.push_delivered` or `attention.push_missed` to project activity. Unconfigured
channels therefore produce an auditable miss rather than a silent drop.

Payloads include `?project=<project>&attention=<attention_id>#tab-needs`. The UI consumes the
`attention` parameter and selects that exact queue item. Delivery never changes, resolves, or
claims the queue item.

The MCP `notify` tool is project-scoped: its normal write authorization is evaluated against
the requested project, and email recipients resolve from Settings → Communications before the
global fallback.
