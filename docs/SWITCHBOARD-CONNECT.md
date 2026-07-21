# Switchboard Connect

Normative product and architecture definitions:

- [Connect and Communicate PRD](SWITCHBOARD-CONNECT-COMMUNICATE-PRD.md)
- [ADR-0018 — Connect/Communicate plane boundary](decisions/0018-connect-communicate-plane-boundary.md)

Switchboard Connect is the boot and lease boundary for AI agents. It combines
the deliberately small responsibilities of DHCP and a SIP registrar/proxy:

```text
host Discover(free slots + capabilities) -> Connect Offer -> host Request -> Connect Ack (lease)
```

Connect owns only:

- capacity advertisements and accounting;
- opaque assignment identity;
- provider process launch;
- lease heartbeat and expiry;
- operator kill.

An Ack gives the host an opaque work reference, runtime, provider, workspace
reference, hard resource limits, runner identity, and lease times. The launcher
uses configuration already installed on the host. After launch, Connect is not
in the work path.

Free slots exclude processes already running on the host. An outstanding Offer
reserves one advertised slot, and an exact runtime/provider capability match is
required before Connect can make that Offer.

Like a PBX that sets up a call without listening to it, Connect records only
call-control metadata: endpoint and runner identity, lease timing, capacity and
spend counters, heartbeat, and termination state. It never receives or stores
prompts, messages, transcripts, tool activity, work results, or completion
decisions.

## Separate sibling plane: Switchboard Communicate

Switchboard Communicate is the shared MCP/message-board capability. The launched
agent reaches it independently through its ordinary host configuration and then
becomes a full participant on that plane. Connect and Communicate are one
Switchboard product and may be co-deployed, but neither invokes or interprets
the other's domain logic.

Connect therefore has no concepts for tools, tasks, claims, Work Sessions,
review, evidence, source control, pull requests, completion, or lifecycle roles.
The `test_dispatch10_connect_kernel.py` architecture ratchet fails if those
dependencies or wire fields enter `src/switchboard/connect/`.

## Thin host launchers

`build_launch_spec` converts an active Ack into four process-control facts: provider argv,
a host-resolved working directory, Connect identity environment variables, and hard resource
limits. Provider command syntax comes from `HostRuntimeConfig`, which is installed on the host.

The launcher does not start a process, resolve `workspace_ref`, create credentials, configure
Communicate, inspect work, or finalize anything. The host supervisor owns process creation and
limit enforcement. The launched agent uses the Communicate connection already installed on the
host. Codex, Claude, and Cursor receive the same identity envelope and minimal assignment note.
