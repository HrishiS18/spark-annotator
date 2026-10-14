"""Private stdio bridge to the official Claude Agent SDK, not a custom agent loop."""
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, get_session_messages
from claude_agent_sdk.types import HookMatcher, PermissionResultAllow, PermissionResultDeny, ResultMessage


def encode(value):
    if dataclasses.is_dataclass(value):
        return {"_type": type(value).__name__, **{f.name: encode(getattr(value,f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value,dict):
        return {k:encode(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):
        return [encode(v) for v in value]
    return value


def emit(value):
    serialized = json.dumps(value,ensure_ascii=False)
    secret = os.environ.get("ANTHROPIC_API_KEY")
    if secret:
        serialized = serialized.replace(secret,"[REDACTED_API_KEY]")
    print(serialized,flush=True)


def allowed_path(root, value):
    path = (root / value).resolve()
    return path.is_relative_to(root) and not path.is_relative_to(root / ".runtime")


def allowed_tool(root, tool, inputs):
    if tool == "Bash":
        return not inputs.get("dangerouslyDisableSandbox")
    if tool not in ("Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep"):
        return True
    target = inputs.get("file_path") or inputs.get("notebook_path") or inputs.get("path") or "."
    if not isinstance(target, str) or not allowed_path(root, target):
        return False
    if tool == "Glob":
        pattern = inputs.get("pattern", "")
        if not isinstance(pattern, str) or ".." in Path(pattern).parts or ".runtime" in Path(pattern).parts:
            return False
        if Path(pattern).is_absolute() and not allowed_path(root, pattern):
            return False
    # Grep's pattern is a regular expression, not a filesystem path.
    return True


async def main(client_factory=ClaudeSDKClient):
    client = None
    running = None
    turn_id = None
    interrupted = False
    pending = {}
    config = {}
    def notice(method,params):
        emit({"method":method,"params":params})

    async def guard_tool(data, tool_use_id, context):
        tool, inputs = data["tool_name"], data.get("tool_input") or {}
        root = Path(config["cwd"]).resolve()
        if not allowed_tool(root, tool, inputs):
            return {"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
                     "permissionDecisionReason":"Use only task workspace files and sandboxed commands; internal runtime files are private."}}
        return {}

    async def permission(tool, inputs, context):
        aid = context.tool_use_id or os.urandom(12).hex()
        future = asyncio.get_running_loop().create_future()
        pending[aid] = future
        notice("sdk/permission",{"id":aid,"tool":tool,"input":inputs,"turn_id":turn_id})
        try:
            response = await future
            if response["decision"] != "accept":
                return PermissionResultDeny(message="Participant declined the action.")
            if tool == "AskUserQuestion":
                inputs = {**inputs,"answers":response.get("answers",{})}
            return PermissionResultAllow(updated_input=inputs)
        finally:
            pending.pop(aid,None)

    async def run_turn(params):
        nonlocal turn_id, interrupted
        turn_id, interrupted = params["turn_id"], False
        notice("sdk/turn_started",{"turn_id":turn_id})
        async def user_input():
            yield {"type":"user","uuid":params["user_uuid"],"session_id":config["session_id"],
                   "message":{"role":"user","content":params["text"]}}
        try:
            await client.query(user_input(),session_id=config["session_id"])
            async for message in client.receive_response():
                notice("sdk/message",{"turn_id":turn_id,"message":encode(message)})
                if isinstance(message,ResultMessage):
                    status = "interrupted" if interrupted else "failed" if message.is_error else "completed"
                    error = ("; ".join(message.errors or []) or message.result or message.subtype) if message.is_error else None
                    notice("sdk/turn_completed",{"turn_id":turn_id,"status":status,"error":error,
                           "usage":message.usage,"cost_usd":message.total_cost_usd})
                    return
            notice("sdk/turn_completed",{"turn_id":turn_id,"status":"interrupted" if interrupted else "failed",
                   "error":"Claude response ended without a result."})
        except Exception as exc:
            notice("sdk/turn_completed",{"turn_id":turn_id,"status":"interrupted" if interrupted else "failed",
                   "error":"Claude SDK generation failed ("+type(exc).__name__+")."})
        finally:
            for future in list(pending.values()):
                if not future.done(): future.set_result({"decision":"decline"})

    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line: break
            request = json.loads(line)
            method, params = request["method"], request.get("params") or {}
            rid = request.get("id")
            try:
                if method == "initialize":
                    config = params
                    root = Path(config["cwd"]).resolve()
                    config_dir = root / ".runtime" / "claude"
                    config_dir.mkdir(parents=True,exist_ok=True)
                    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
                    options = ClaudeAgentOptions(
                        cwd=root, model=config["model"], effort=config["effort"],
                        system_prompt={"type":"preset","preset":"claude_code","append":config["persona"] +
                          "\nWork on TASK.md in this workspace. Do not access study records, other sessions, credentials or .runtime."},
                        tools={"type":"preset","preset":"claude_code"}, disallowed_tools=["Agent","Task","Skill"],
                        setting_sources=[], skills=[], plugins=[], mcp_servers={}, strict_mcp_config=True,
                        permission_mode="default", can_use_tool=permission, include_partial_messages=True,
                        hooks={"PreToolUse":[HookMatcher(hooks=[guard_tool])]},
                        # The host applies a single Seatbelt boundary to this
                        # entire process tree; native Bash sandboxing cannot nest.
                        sandbox={"enabled":False},
                        settings=json.dumps({"permissions":{"ask":["Bash","Write","Edit","NotebookEdit","WebFetch","WebSearch"]}}),
                        max_budget_usd=config["max_budget_usd"], max_turns=40,
                        env={"CLAUDE_CONFIG_DIR":str(config_dir),"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1",
                             "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB":"1"},
                        resume=config["session_id"] if config.get("resume") else None,
                        session_id=None if config.get("resume") else config["session_id"],
                        stderr=lambda _:None)
                    client = client_factory(options=options)
                    await client.connect()  # Handshake only, no model prompt.
                    if config.get("resume"):
                        history = get_session_messages(config["session_id"],directory=str(root))
                        for message in history:
                            notice("sdk/replay",{"message":encode(message)})
                    result = {"session_id":config["session_id"]}
                elif method == "turn/start":
                    if running and not running.done():
                        raise ValueError("A Claude turn is already running")
                    running = asyncio.create_task(run_turn(params))
                    result = {"turn":{"id":params["turn_id"]}}
                elif method == "permission/respond":
                    future = pending.get(params["id"])
                    if not future or future.done():
                        raise ValueError("Permission request expired")
                    future.set_result(params)
                    result = {"ok":True}
                elif method == "turn/interrupt":
                    interrupted = True
                    for future in list(pending.values()):
                        if not future.done(): future.set_result({"decision":"decline"})
                    await client.interrupt()
                    result = {"ok":True}
                else:
                    raise ValueError("Unsupported bridge request")
                if rid is not None: emit({"id":rid,"result":result})
            except Exception as exc:
                if rid is not None:
                    emit({"id":rid,"error":{"message":"Claude SDK request failed ("+type(exc).__name__+")."}})
    finally:
        if running and not running.done():
            running.cancel()
            try: await running
            except asyncio.CancelledError: pass
        if client:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
