"""Synthetic SDK client: exercises our real bridge without provider calls."""
import asyncio
import json
from pathlib import Path

from claude_agent_sdk.types import (
    AssistantMessage, TextBlock, ToolUseBlock, ToolResultBlock, UserMessage,
    SystemMessage, ResultMessage, StreamEvent, ToolPermissionContext, PermissionResultAllow,
    SessionMessage,
)


class FakeClient:
    def __init__(self,options):
        self.options=options
        self.sid=options.resume or options.session_id
        self.queue=asyncio.Queue()
        self.generator=None
        self.counter=0
        self.root=Path(options.cwd)

    async def connect(self): pass

    async def query(self,prompt,session_id="default"):
        rows=[row async for row in prompt]
        self.counter+=1
        self.generator=asyncio.create_task(self.generate(rows[0]))

    async def generate(self,row):
        from claude_sdk_worker import encode
        history_path=self.root/".runtime"/"claude"/"fake-history.json"
        history=json.loads(history_path.read_text()) if history_path.exists() else []
        mid="msg_"+row["uuid"]
        history.append(encode(SessionMessage(type="user",uuid=row["uuid"],session_id=self.sid,message=row["message"])))
        await self.queue.put(SystemMessage(subtype="init",data={"session_id":self.sid,"model":"TEST ONLY Claude SDK"}))
        await self.queue.put(StreamEvent(uuid="s",session_id=self.sid,event={"type":"message_start","message":{"id":mid}}))
        await self.queue.put(StreamEvent(uuid="s",session_id=self.sid,event={"type":"content_block_start","index":0,
                                         "content_block":{"type":"text","text":""}}))
        await self.queue.put(StreamEvent(uuid="s",session_id=self.sid,event={"type":"content_block_delta","index":0,
                                         "delta":{"type":"text_delta","text":"[TEST MOCK] I will inspect the task workspace first."}}))
        if "[ASK]" in row["message"]["content"]:
            tool="AskUserQuestion"
            inputs={"questions":[{"question":"Which format should I use?","header":"Format","multiSelect":False,
                                  "options":[{"label":"Text","description":"A plain-text artifact"},{"label":"CSV","description":"Tabular output"}]}]}
        else:
            tool="Bash"; inputs={"command":"cat input.txt"}
        tool_id="tool_"+row["uuid"]
        blocks=[TextBlock(text="[TEST MOCK] I will inspect the task workspace first."),
                ToolUseBlock(id=tool_id,name=tool,input=inputs)]
        await self.queue.put(AssistantMessage(content=blocks,model="TEST ONLY Claude SDK",message_id=mid,uuid=mid))
        history.append(encode(SessionMessage(type="assistant",uuid=mid,session_id=self.sid,message={
            "id":mid,"role":"assistant","content":[{"type":"text","text":blocks[0].text},{"type":"tool_use","id":tool_id,"name":tool,"input":inputs}]})))
        history_path.write_text(json.dumps(history))
        response=await self.options.can_use_tool(tool,inputs,ToolPermissionContext(tool_use_id=tool_id))
        if isinstance(response,PermissionResultAllow):
            (self.root/"summary.txt").write_text("Test-only artifact created by mock Claude SDK.")
            output=json.dumps(response.updated_input.get("answers")) if tool=="AskUserQuestion" else "summary.txt saved"
        else:
            output="Participant declined the action."
        await self.queue.put(UserMessage(content=[ToolResultBlock(tool_use_id=tool_id,content=output)]))
        final="[TEST MOCK] Your test artifact is ready."
        await self.queue.put(AssistantMessage(content=[TextBlock(text=final)],model="TEST ONLY Claude SDK",message_id=mid+"_final",uuid=mid+"_final"))
        history.extend([
            encode(SessionMessage(type="user",uuid=mid+"_result",session_id=self.sid,message={"role":"user",
                "content":[{"type":"tool_result","tool_use_id":tool_id,"content":output}]})),
            encode(SessionMessage(type="assistant",uuid=mid+"_final",session_id=self.sid,message={"id":mid+"_final",
                "role":"assistant","content":[{"type":"text","text":final}]}))])
        history_path.write_text(json.dumps(history))
        await self.queue.put(ResultMessage(subtype="success",duration_ms=1,duration_api_ms=0,is_error=False,
                                          num_turns=1,session_id=self.sid,total_cost_usd=0))

    async def receive_response(self):
        while True:
            message=await self.queue.get()
            yield message
            if isinstance(message,ResultMessage): return

    async def interrupt(self):
        if self.generator:
            self.generator.cancel()
        await self.queue.put(ResultMessage(subtype="success",duration_ms=1,duration_api_ms=0,is_error=True,
                                          num_turns=1,session_id=self.sid,result="Synthetic interrupted turn"))

    async def disconnect(self):
        if self.generator: self.generator.cancel()


def history(session_id,directory=None):
    path=Path(directory)/".runtime"/"claude"/"fake-history.json"
    return [SessionMessage(**{k:v for k,v in row.items() if k!="_type"}) for row in json.loads(path.read_text())] if path.exists() else []
