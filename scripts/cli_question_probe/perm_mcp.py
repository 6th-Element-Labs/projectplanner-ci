#!/usr/bin/env python3
# Minimal MCP stdio server exposing ONE tool: approval_prompt.
# Claude Code calls this whenever it needs permission for a tool it isn't
# pre-allowed to use. That call — with the exact tool + input — is the
# machine event we're proving. We log it, then answer "deny" so the agent
# stays parked (the "waiting on a human" state).
import sys, json, datetime
import os
LOG = os.environ.get("QUESTION_QUEUE", os.path.join(os.path.dirname(__file__), "questions-queue.jsonl"))
def send(obj): sys.stdout.write(json.dumps(obj)+"\n"); sys.stdout.flush()
def main():
    for line in sys.stdin:
        line=line.strip()
        if not line: continue
        msg=json.loads(line)
        mid=msg.get("id"); method=msg.get("method")
        if method=="initialize":
            send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
                "capabilities":{"tools":{}},"serverInfo":{"name":"perm-gate","version":"1.0"}}})
        elif method=="notifications/initialized":
            pass
        elif method=="tools/list":
            send({"jsonrpc":"2.0","id":mid,"result":{"tools":[{
                "name":"approval_prompt","description":"Permission gate",
                "inputSchema":{"type":"object","properties":{
                    "tool_name":{"type":"string"},"input":{"type":"object"}}}}]}})
        elif method=="tools/call":
            args=msg.get("params",{}).get("arguments",{})
            # THIS is the machine event: the agent is asking to do something.
            row={"ts":datetime.datetime.utcnow().isoformat()+"Z","kind":"permission_request",
                 "tool_name":args.get("tool_name"),"input":args.get("input")}
            open(LOG,"a").write(json.dumps(row)+"\n")
            sys.stderr.write("QUESTION RECEIVED -> "+json.dumps(row)+"\n"); sys.stderr.flush()
            decision={"behavior":"deny","message":"parked: waiting on operator (test)"}
            send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps(decision)}]}})
        elif mid is not None:
            send({"jsonrpc":"2.0","id":mid,"result":{}})
main()
