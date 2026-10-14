"""Provider-neutral dispatch; both adapters own their native agent loops."""
from codex_runtime import RuntimeManager as CodexRuntimeManager
from claude_runtime import ClaudeRuntimeManager

AGENT_KINDS = {"codex","claude"}


class RuntimeManager:
    def __init__(self,store,data_dir,codex=None,claude=None):
        self.store = store
        self.adapters = {"codex":codex or CodexRuntimeManager(store,data_dir),
                         "claude":claude or ClaudeRuntimeManager(store,data_dir)}

    def adapter(self,sid):
        kind = self.store.load(sid).get("runtime",{}).get("kind")
        if kind not in self.adapters: raise ValueError("This session does not use an integrated agent")
        return self.adapters[kind]

    def get(self,sid): return self.adapter(sid).get(sid)
    def workspace(self,sid): return self.adapter(sid).workspace(sid)
    def files(self,sid): return self.adapter(sid).files(sid)
    def file(self,sid,path): return self.adapter(sid).file(sid,path)
    def upload(self,sid,name,data): return self.adapter(sid).upload(sid,name,data)
    def status(self): return self.adapters["codex"].status()  # compatibility with older clients
    def statuses(self): return {kind:manager.status() for kind,manager in self.adapters.items()}
    def close(self):
        for manager in self.adapters.values(): manager.close()
