"""Switchboard Connect: provider-neutral agent boot and lease control.

Connect is deliberately independent from every post-boot communication or work
workflow.  Its public vocabulary is exported here so callers do not need to
reach into implementation modules.
"""

from .contract import (
    Ack,
    Assignment,
    Discover,
    LeaseState,
    Offer,
    Request,
    ResourceLimits,
    RuntimeCapability,
)
from .kernel import ConnectKernel, ConnectRefused
from .launcher import (
    HostRuntimeConfig,
    LaunchRefused,
    LaunchSpec,
    assignment_note,
    build_launch_spec,
)

__all__ = [
    "Ack",
    "Assignment",
    "ConnectKernel",
    "ConnectRefused",
    "Discover",
    "LeaseState",
    "Offer",
    "Request",
    "ResourceLimits",
    "RuntimeCapability",
    "HostRuntimeConfig",
    "LaunchRefused",
    "LaunchSpec",
    "assignment_note",
    "build_launch_spec",
]
