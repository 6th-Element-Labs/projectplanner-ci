#!/usr/bin/env python3
"""ARCH-MS-17: task MCP tools live behind the package adapter seam."""
from pathlib import Path

from path_setup import ROOT, entrypoint_source
from switchboard.mcp.tools import tasks as task_tools


TASK_TOOLS = (
    "search_tasks", "get_task", "update_task", "create_task", "add_comment",
    "archive_task", "move_task", "add_dependency", "remove_dependency",
)


def ok(condition, message):
    if not condition:
        raise AssertionError(message)
    print("  PASS  " + message)


server_source = entrypoint_source("mcp_server")
adapter_path = ROOT / "src/switchboard/mcp/tools/tasks.py"
adapter_source = adapter_path.read_text(encoding="utf-8")

ok(adapter_path.is_file(), "task MCP adapter exists in the target package")
ok("task_tools.register_task_tools(" in server_source,
   "mcp_server registers the packaged task tool set")

for name in TASK_TOOLS:
    ok(f"def {name}(" not in server_source,
       f"{name} implementation left the MCP monolith")
    ok(f"def {name}(" in adapter_source,
       f"{name} implementation lives in the task adapter")

for shared_call in (
    "create_task_command.execute_mapping_result",
    "update_task_command.execute_mapping_result",
    "get_task_query.execute_for",
):
    ok(shared_call in adapter_source,
       f"task adapter preserves shared application seam: {shared_call}")

ok("TASK_TOOL_NAMES" in adapter_source and "register_task_tools" in adapter_source,
   "task adapter exposes an explicit registration contract for later MCP modules")


class FakeMCP:
    def __init__(self):
        self.names = []

    def tool(self):
        def register(function):
            self.names.append(function.__name__)
            return function
        return register


fake_mcp = FakeMCP()
registered = task_tools.register_task_tools(
    fake_mcp,
    task_tools.TaskToolServices(
        dumps=lambda value: str(value),
        require_write=lambda *args, **kwargs: {},
        resolve_write_actor=lambda *args, **kwargs: {},
        write_binding_comment=lambda *args, **kwargs: None,
    ),
)
ok(tuple(fake_mcp.names) == TASK_TOOLS,
   "registration publishes every task tool exactly once")
ok(tuple(registered) == TASK_TOOLS,
   "registration returns compatibility aliases for the monolith host")

print("ARCH-MS-17 MCP task-tool extraction checks passed")
