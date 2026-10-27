import {
  api, icon, esc, $, $$, config, renderText, toast,
  fmtTime, isMarkHotkey,
} from "/static/common.js";
import { BookmarkOutbox, captureExposure } from "/static/bookmarks.js";
import { NativeVoiceClient } from "/static/voice.js";

const sid = new URLSearchParams(location.search).get("id");
let cfg, S, study;
let busy = false;
let pollTimer = null;
let eventStream = null;
let connecting = false;
let pendingMessage = null;
let workspaceRefresh = null;
let bookmarkOutbox = null;
let voiceClient = null;
let voiceStarting = false;
let voiceLocalStatus = null;
let voiceAutostartPending = false;
let voiceStartError = null;
const voiceAutostartKey = `voice-autostart:${sid}`;
const isAgentRuntime = () => ["codex","claude"].includes(S.runtime?.kind);
const isVoiceMode = () => S.runtime?.kind === "codex" && S.interaction_mode === "voice";
const agentName = () => S.runtime?.kind === "claude" ? "Claude" : "Codex";
const modelLabel = (model) => !model ? "Study model" : model === "sonnet" ? "Sonnet" : model === "opus" ? "Opus" : model === "haiku" ? "Haiku" : model === "TEST ONLY" ? "Test model" : model;
const PHASE_LABEL = { pre: "Before", chat: "Conversation", post: "After", review: "Review", done: "Done" };
const CHAT_ROLES = ["user", "assistant", "partner"];

const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  del: (k) => { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

async function load() {
  S = await api("GET", `/api/sessions/${sid}`);
  study = S.config_snapshot?.study || cfg.studies.find((s) => s.key === S.study);
}

// ------------------------------------------------------------------ shell

function renderShell() {
  const claude = S.runtime?.kind === "claude";
  document.body.dataset.agent = S.runtime?.kind || "human";
  $("#brand").innerHTML = claude ? `<span class="claude-mark" aria-hidden="true">✳</span><span>Claude Code <small>study session</small></span>` : `${icon("chat")}<span>Task session</span>`;
  $("#top-ic").innerHTML = icon("chat");
  $("#top-title").textContent = S.prompt.title;
  $("#fab-ic").innerHTML = icon("bulb");
  $("#task").innerHTML = `<details open><summary>Task brief</summary><div class="task-text">${esc(S.prompt.text)}</div></details>`;
  $("#foot").textContent = "";
  renderSteps();
}

function renderSteps() {
  const order = S.phase_order.filter((p) => p !== "done");
  const cur = S.phase_order.indexOf(S.phase);
  $("#steps").innerHTML = order.map((p, i) => {
    const state = i < cur ? "done" : i === cur ? "active" : "";
    const mins = S.phases[p]?.minutes;
    return `<div class="step ${state}" data-phase="${p}"><span class="dot">${i < cur ? icon("check") : i + 1}</span>
      ${PHASE_LABEL[p]}${mins && i >= cur ? `<span class="timer" data-timer="${p}">${mins}:00</span>` : ""}</div>`;
  }).join("");
}

let timesUpShown = {};
function tick() {
  const p = S.phase;
  const ph = S.phases[p];
  const el = $(`[data-timer="${p}"]`);
  if (!el || !ph?.minutes || !ph.started_at) return;
  const left = ph.minutes * 60 - (Date.now() - Date.parse(ph.started_at)) / 1000;
  const a = Math.abs(Math.round(left));
  el.textContent = `${left < 0 ? "+" : ""}${Math.floor(a / 60)}:${String(a % 60).padStart(2, "0")}`;
  el.classList.toggle("over", left < 0);
  if (left < 0 && !timesUpShown[p]) {
    timesUpShown[p] = true;
    banner(p === "chat" ? "Time's up for the conversation. Please wrap up and click <b>Finish conversation</b>." : "Time's up. Please finish your thoughts and submit.");
  }
}
setInterval(() => S && tick(), 1000);

function banner(html) { $("#banner").innerHTML = html ? `<div class="banner">${html}</div>` : ""; }

function route() {
  clearInterval(pollTimer);
  eventStream?.close(); eventStream = null;
  clearInterval(workspaceRefresh);
  banner("");
  renderSteps();
  $("#composer-wrap").classList.add("hidden");
  $("#composer-context").classList.add("hidden");
  $("#add-file").classList.add("hidden");
  $("#fab").classList.add("hidden");
  $("#top-actions").innerHTML = "";
  $("#top-runtime").innerHTML = "";
  if (S.phase !== "chat") { voiceClient?.closeLocal(); voiceClient = null; }
  voiceAutostartPending = false;
  voiceStartError = null;
  $("#voice-stage").classList.add("hidden");
  $("#voice-start").classList.add("hidden");
  $("#voice-enable-sound").classList.add("hidden");
  $("#voice-mute").classList.add("hidden");
  $("#voice-end").classList.add("hidden");
  document.body.dataset.voice = "off";
  document.body.dataset.voiceActive = "false";
  ({ pre: writingView, post: writingView, chat: S.external ? externalView : chatView, review: reviewView, done: doneView })[S.phase]();
  tick();
}

async function advance(text) {
  if (bookmarkOutbox && !await bookmarkOutbox.flush()) return toast("Bookmarks are still syncing. Reconnect before leaving this session.");
  await api("POST", `/api/sessions/${sid}/advance`, { phase: S.phase, text });
  await load();
  route();
  $("#content").scrollTop = 0;
}

// ------------------------------------------------------------------ writing (before / after)

function writingView() {
  const p = S.phase;
  const key = `draft:${sid}:${p}`;
  const instr = p === "pre" ? study.pre_instructions : study.post_instructions;
  $("#content").innerHTML = `<div class="narrow writing">
    <h1>${p === "pre" ? "Before the conversation" : "After the conversation"}</h1>
    <p class="instructions">${esc(instr)}</p>
    <div class="prompt-card"><div class="label">The question</div><p>${esc(S.prompt.text)}</p></div>
    ${p === "post" && S.writing.pre ? `<details class="box" style="margin-bottom:14px"><summary>Your earlier answer (for reference)</summary><div class="insp-text" style="margin-top:8px">${esc(S.writing.pre.text)}</div></details>` : ""}
    <textarea id="essay" placeholder="Write your position and reasons…">${esc(store.get(key) || "")}</textarea>
    <div class="row" style="margin-top:12px"><span class="muted small grow" id="wc"></span>
      <button class="btn btn-primary" id="submit">${p === "pre" ? "Continue to conversation" : "Submit final answer"}</button></div>
  </div>`;
  const ta = $("#essay");
  const wc = () => ($("#wc").textContent = `${(ta.value.trim().match(/\S+/g) || []).length} words`);
  ta.oninput = () => { store.set(key, ta.value); wc(); };
  wc();
  ta.focus();
  $("#submit").onclick = async () => {
    if ((ta.value.trim().match(/\S+/g) || []).length < 15 && !confirm("Your answer is quite short. Submit anyway?")) return;
    if (!confirm("Submit? You won't be able to edit this afterwards.")) return;
    await advance(ta.value);
    store.del(key);
  };
}

// ------------------------------------------------------------------ chat

function partnerName() { return S.condition === "human" ? "Partner" : isAgentRuntime() ? agentName() : S.runtime?.kind === "demo" ? "Scripted demo" : "AI"; }

function msgHTML(e) {
  const marks = S.marks.filter((m) => m.event_id === e.id && !m.deleted);
  const badges = marks.length ? `<span class="mark-badge">${icon("bookmark")}Marked${marks.length > 1 ? ` ×${marks.length}` : ""}</span>` : "";
  const btn = e.id === "pending" ? "" : `<button class="icon-btn mark-here" data-mark="${e.id}" title="Mark this message" aria-label="Mark this message">${icon("bulb")}</button>`;
  if (["tool_call", "tool_result"].includes(e.role)) {
    const label = e.role === "tool_result" ? "Tool result" : ({ commandExecution: "Command", fileChange: "File changes", webSearch: "Web search" }[e.tool] || e.tool || "Tool");
    return `<div class="msg msg-tool ${marks.length ? "marked" : ""}" data-id="${e.id}">${btn}
      <details data-tool="${e.id}"><summary>${esc(label)} <span class="small muted">${esc(e.status || "")}</span>${badges}</summary>
      <pre>${esc(e.text)}</pre>${e.output ? `<div class="small muted">Output</div><pre>${esc(e.output)}</pre>` : ""}</details></div>`;
  }
  if (e.role === "user") {
    return `<div class="msg msg-user ${marks.length ? "marked" : ""}" data-id="${e.id}">${btn}${renderText(e.text)}
      ${e.meta?.voice ? `<span class="small muted">Voice</span>` : ""}
      ${badges ? `<div class="mark-badges" style="margin-top:6px">${badges}</div>` : ""}</div>`;
  }
  return `<div class="msg msg-ai ${marks.length ? "marked" : ""}" data-id="${e.id}">${btn}
    <div class="who">${partnerName()}${e.meta?.voice ? `<span class="small muted">· voice</span>` : ""}<span class="mark-badges">${badges}</span></div>${renderText(e.text)}</div>`;
}

function renderThread() {
  const opened = new Set($$("details[open][data-tool]").map((el) => el.dataset.tool));
  const evs = S.events.filter((e) => !e.meta?.superseded && (CHAT_ROLES.includes(e.role) || isAgentRuntime() && ["tool_call", "tool_result"].includes(e.role)));
  const intro = S.condition === "human"
    ? "You're paired with another participant. Say hello and start discussing the question."
    : "Start by telling your partner what you think about the question, or ask it anything.";
  $("#thread").innerHTML = evs.length ? evs.map(msgHTML).join("") : `<div class="empty-chat">${esc(intro)}</div>`;
  $$("details[data-tool]").forEach((el) => { el.open = opened.has(el.dataset.tool); });
  $("#fab-count").textContent = S.marks.filter((m) => !m.deleted).length;
  paintBookmarkStatus();
}

function scrollBottom() { const c = $("#content"); c.scrollTop = c.scrollHeight; }

function chatView() {
  const claude = S.runtime?.kind === "claude";
  $("#content").innerHTML = `<div class="narrow"><p class="instructions small">${esc(study.chat_instructions)}</p>
    <div class="runtime-panel" id="runtime-panel"></div><div id="approvals"></div>
    ${isAgentRuntime() ? `<details class="workspace-panel"><summary>${claude ? "Project files" : "Task files"}</summary>
      <p class="small muted">These files are saved on the Mac running the server and shared with the agent. Existing files are never overwritten.</p>
      <label class="btn btn-sm">${icon("upload")} Add task file<input id="workspace-upload" type="file" hidden></label>
      <button class="btn btn-sm btn-ghost" id="refresh-files">Refresh files</button><div id="workspace-files"></div><pre id="file-preview" class="hidden"></pre></details>` : ""}
    <div class="thread" id="thread"></div><div id="typing"></div></div>`;
  $("#composer-wrap").classList.remove("hidden");
  if (isVoiceMode()) {
    document.body.dataset.voice = "enabled";
    voiceAutostartPending = !store.get(voiceAutostartKey) && !S.events.some(event => event.meta?.voice);
    $("#voice-start").onclick = startVoice;
    $("#voice-enable-sound").onclick = async () => {
      try { await $("#voice-audio").play(); voiceLocalStatus = null; renderVoice(); }
      catch (error) { toast(`Could not play voice audio: ${error.message}`); }
    };
    $("#voice-end").onclick = endVoice;
    $("#voice-mute").onclick = () => { voiceClient?.toggleMute(); renderVoice(); };
  }
  if (claude || isVoiceMode()) {
    $("#add-file").classList.remove("hidden");
    $("#add-file").onclick = () => $("#workspace-upload").click();
  }
  if (claude) {
    $("#composer-context").innerHTML = `<span>Local study server</span><span>Task workspace</span><span>${esc(modelLabel(S.runtime?.requested_model || S.runtime?.model))}</span><span>Accept edits · commands ask</span>`;
    $("#composer-context").classList.remove("hidden");
  }
  $("#fab").classList.remove("hidden");
  if (!bookmarkOutbox) bookmarkOutbox = new BookmarkOutbox(sid, (pending, mark) => {
    if (mark) {
      const current = S.marks.find(m => m.id === mark.id);
      if (current) Object.assign(current, mark); else S.marks.push(mark);
    }
    for (const row of pending) {
      const current = S.marks.find(m => m.id === row.body.id);
      if (current) Object.assign(current, row.body, { sync_state: row.state });
      else S.marks.push({ ...row.body, review: null, t: row.body.t ?? row.body.client_t / 1000, sync_state: row.state });
    }
    if (S.phase === "chat" && $("#thread")) renderThread();
  });
  $("#top-actions").innerHTML = `${isAgentRuntime() ? `<button class="btn btn-sm" id="connect-agent">Reconnect</button><button class="btn btn-sm hidden" id="stop-agent">${icon("stop")} Stop agent</button>` : ""}<button class="btn btn-sm" id="finish">Finish conversation</button>`;
  $("#finish").onclick = async () => {
    if (!confirm("Finish the conversation and move on? You won't be able to send more messages.")) return;
    await advance();
  };
  renderThread();
  scrollBottom();
  $("#input").focus();
  if (S.condition === "human") pollTimer = setInterval(pollPartner, 1500);
  if (isAgentRuntime()) {
    $("#connect-agent").onclick = connectAgent;
    $("#stop-agent").onclick = async () => {
      try { await api("POST", `/api/sessions/${sid}/runtime/interrupt`, {}); }
      catch (err) { toast(err.message); }
    };
    $("#workspace-upload").onchange = async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        await api("POST", `/api/sessions/${sid}/workspace/files`, file, { "X-Filename": file.name });
        await refreshFiles(); toast("Task file uploaded");
      } catch (err) { toast(err.message); }
      e.target.value = "";
    };
    $("#refresh-files").onclick = refreshFiles;
    refreshFiles();
    workspaceRefresh = setInterval(refreshFiles, 8000);
    startStream();
    connectAgent(); // initialization/resume only; never starts a model turn
  }
  renderRuntime();
  renderVoice();
  maybeAutostartVoice();
}

function maybeAutostartVoice() {
  if (!voiceAutostartPending || !isVoiceMode() || S.phase !== "chat" || connecting || S.runtime?.status !== "ready") return;
  voiceAutostartPending = false;
  store.set(voiceAutostartKey, "attempted");
  if (!["starting", "connecting", "active", "stopping"].includes(S.runtime.voice?.status)) void startVoice();
  else renderVoice();
}

function renderVoice() {
  if (!isVoiceMode() || S.phase !== "chat") return;
  const voice = S.runtime?.voice || { status: "inactive" };
  const state = voiceLocalStatus || voice.status || "inactive";
  const actualLive = ["starting", "connecting", "active", "stopping"].includes(voice.status) || voiceStarting;
  const live = actualLive || voiceAutostartPending;
  document.body.dataset.voiceActive = String(live);
  const labels = { inactive: "Ready", microphone: "Waiting for microphone", starting: "Starting", connecting: "Connecting",
    active: "Listening", stopping: "Ending", error: "Unavailable", failed: "Connection failed",
    "playback-blocked": "Tap to enable sound" };
  $("#voice-stage").classList.toggle("hidden", !live && !voiceStartError);
  $("#voice-stage").dataset.state = (voiceStartError || S.runtime?.status === "error") ? "error" : state;
  $("#voice-status").textContent = labels[state] || state;
  $("#voice-caption").textContent = voiceAutostartPending ?
    (S.runtime?.status === "error" ? S.runtime.error || "Could not connect to Codex. Reconnect to try again." : "Preparing voice chat") :
    voiceStartError ? "Voice couldn't start. Check microphone access, then retry." :
    state === "active" ? "" : voice.error || labels[state] || "";
  $("#voice-start").classList.toggle("hidden", live);
  $("#voice-start").innerHTML = icon("mic");
  $("#voice-start").setAttribute("aria-label", voiceStartError ? "Retry voice chat" : "Start voice chat");
  $("#voice-start").title = voiceStartError ? "Retry voice chat" : "Start voice chat";
  $("#voice-start").disabled = voiceStarting || S.runtime?.status !== "ready";
  $("#voice-enable-sound").classList.toggle("hidden", state !== "playback-blocked");
  $("#voice-mute").classList.toggle("hidden", !actualLive || !voiceClient?.stream);
  $("#voice-mute").innerHTML = icon(voiceClient?.muted ? "micOff" : "mic");
  $("#voice-mute").setAttribute("aria-label", voiceClient?.muted ? "Unmute microphone" : "Mute microphone");
  $("#voice-mute").setAttribute("aria-pressed", String(!!voiceClient?.muted));
  $("#voice-end").classList.toggle("hidden", !actualLive);
  $("#voice-end").innerHTML = icon("x");
  $("#voice-end").disabled = voice.status === "stopping";
  $("#send").classList.toggle("hidden", live);
  $("#input").placeholder = live ? "Type" : "Ask a follow-up…";
}

async function startVoice() {
  if (!isVoiceMode() || voiceStarting || voiceClient?.pc) return;
  voiceStarting = true;
  voiceStartError = null;
  voiceLocalStatus = "microphone";
  const client = new NativeVoiceClient(sid, $("#voice-audio"), state => {
    if (voiceClient !== client) return;
    voiceLocalStatus = state === "active" || state === "inactive" ? null : state;
    renderVoice();
  });
  voiceClient = client;
  renderVoice();
  try {
    await client.start();
  } catch (error) {
    client.closeLocal();
    if (voiceClient === client) voiceClient = null;
    voiceLocalStatus = null;
    try { await api("POST", `/api/sessions/${sid}/runtime/voice/stop`, {}); } catch { /* start may not have reached Codex */ }
    if (!client.cancelled) { voiceStartError = error.message; toast(error.message); }
  } finally {
    voiceStarting = false;
    renderRuntime();
  }
}

async function endVoice() {
  if (!isVoiceMode()) return;
  voiceStartError = null;
  try {
    if (voiceClient) await voiceClient.stop();
    else await api("POST", `/api/sessions/${sid}/runtime/voice/stop`, {});
  } catch (error) { toast(error.message); }
  finally {
    voiceClient?.closeLocal();
    voiceClient = null;
    voiceLocalStatus = null;
    renderRuntime();
  }
}

function renderRuntime() {
  const panel = $("#runtime-panel");
  const compact = $("#top-runtime");
  if (!panel) return;
  if (!isAgentRuntime()) {
    const mode = S.condition === "human" ? "Human partner" : S.runtime?.kind === "demo" ? "SCRIPTED DEMO — not a live model" : S.runtime?.kind === "legacy" ? "Legacy saved conversation — runtime configuration was not recorded" : "Plain LLM chat — no agent tools";
    panel.innerHTML = `<span class="sr-only">${esc(mode)}</span>`;
    compact.innerHTML = `<span class="runtime-compact"><i></i>${esc(mode)}</span>`;
    $("#send").disabled = busy;
    return;
  }
  const runtime = S.runtime;
  const active = ["starting", "running", "awaiting_approval"].includes(runtime.status);
  const status = connecting ? "connecting" : runtime.status || "disconnected";
  panel.innerHTML = `<span class="sr-only">${esc(status.replaceAll("_", " "))}</span>
      ${runtime.error ? `<div class="runtime-error" role="alert">${esc(runtime.error)}</div>` : ""}`;
  const statusClass = ["ready","running"].includes(status) ? "ok" : ["error","failed","interrupted"].includes(status) ? "bad" : "";
  compact.innerHTML = `<span class="runtime-compact ${statusClass}" title="${esc(agentName())} status"><i></i>${esc(status.replaceAll("_", " "))}</span>`;
  $("#send").disabled = busy || active || connecting || !["ready", "interrupted", "failed"].includes(runtime.status);
  const voiceActive = ["starting", "connecting", "active", "stopping"].includes(runtime.voice?.status) ||
    voiceStarting || (voiceAutostartPending && runtime.status !== "error");
  $("#send").disabled ||= voiceActive;
  $("#finish").disabled = busy || active || connecting || voiceActive;
  $("#stop-agent").classList.toggle("hidden", !active);
  $("#stop-agent").disabled = runtime.status === "starting";
  $("#connect-agent").disabled = active || connecting;
  $("#connect-agent").classList.toggle("hidden", runtime.status === "ready" && !runtime.error);
  $("#workspace-upload").disabled = active || connecting || voiceActive;
  $("#add-file").disabled = active || connecting || voiceActive;
  renderVoice();
  const approvals = runtime.approvals || [];
  const questionDrafts = new Map($$('[data-approval]').map(card=>[card.dataset.approval,
    Object.fromEntries($$('[data-answer]',card).map(input=>[input.dataset.answer,input.value]))]));
  $("#approvals").innerHTML = approvals.map((a) => {
    if (a.method === "claude/question") return `<div class="approval-card" data-approval="${a.id}"><h3>Claude has a question</h3>
      ${a.params.questions.map((q,i) => `<label class="field"><span>${esc(q.question)}</span>
        ${q.options?.length ? `<span class="small muted">${q.options.map(o=>esc(o.label)+': '+esc(o.description || '')).join(' · ')}</span>` : ''}
        <input type="text" data-answer="${i}" placeholder="Your answer (an option or your own words)"></label>`).join('')}
      <button class="btn btn-primary btn-sm" data-decision="accept">Send answers</button><button class="btn btn-sm" data-decision="decline">Decline</button></div>`;
    const item = S.events.find((e) => e.meta?.runtime_item_id === a.params.itemId);
    const description = a.params.command || item?.text || a.params.reason || "The agent requested permission.";
    const canAcceptForSession = (a.params.availableDecisions || []).includes("acceptForSession");
    return `<div class="approval-card" data-approval="${a.id}"><h3>Agent approval required</h3>
      <pre>${esc(description)}</pre>${a.params.reason ? `<p>${esc(a.params.reason)}</p>` : ""}
      <p class="small muted">This action is not covered by the session's automatic task-workspace permissions.</p>
      <button class="btn btn-primary btn-sm" data-decision="accept">Allow once</button>
      ${S.runtime.kind === "codex" && canAcceptForSession ? `<button class="btn btn-sm" data-decision="acceptForSession">Allow similar this session</button>` : ""}
      <button class="btn btn-sm" data-decision="decline">Decline</button></div>`;
  }).join("");
  $$('[data-approval]').forEach(card=>$$('[data-answer]',card).forEach(input=>{
    input.value=questionDrafts.get(card.dataset.approval)?.[input.dataset.answer] || '';
  }));
  $$("[data-approval]").forEach((card) => $$('[data-decision]', card).forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      const approval = approvals.find(a=>a.id === card.dataset.approval);
      const answers = approval.method === "claude/question" ? Object.fromEntries(approval.params.questions.map((q,i) =>
        [q.question, $(`[data-answer="${i}"]`,card).value])) : undefined;
      try { await api("POST", `/api/sessions/${sid}/runtime/approval`, { id: card.dataset.approval, decision: button.dataset.decision, answers }); }
      catch (err) { toast(err.message); button.disabled = false; }
    };
  }));
}

async function connectAgent() {
  if (connecting || S.phase !== "chat") return;
  connecting = true; renderRuntime();
  try { S.runtime = await api("POST", `/api/sessions/${sid}/runtime/connect`, {}); }
  catch (err) { S.runtime.status = "error"; S.runtime.error = err.message; }
  finally { connecting = false; renderRuntime(); maybeAutostartVoice(); }
}

function startStream() {
  eventStream?.close();
  eventStream = new EventSource(`/api/sessions/${sid}/stream`);
  eventStream.addEventListener("session", (event) => {
    if (S.phase !== "chat") return;
    const update = JSON.parse(event.data);
    const atBottom = $("#content").scrollHeight - $("#content").scrollTop - $("#content").clientHeight < 100;
    S.events = update.events;
    if (busy && pendingMessage && !S.events.some((e) => e.meta?.client_message_id === pendingMessage.id)) {
      S.events.push({ id: "pending", role: "user", text: pendingMessage.text });
    }
    S.runtime = update.runtime;
    S._revision = update.revision;
    if (isVoiceMode() && voiceClient?.pc && !voiceStarting &&
        ["inactive", "error"].includes(S.runtime.voice?.status)) {
      voiceClient.closeLocal();
      voiceClient = null;
      voiceLocalStatus = null;
    }
    renderThread(); renderRuntime(); renderVoice(); maybeAutostartVoice();
    if (atBottom) scrollBottom();
    $("#stream-status")?.remove();
  });
  eventStream.onerror = () => {
    if (!$("#stream-status")) panelNotice("Connection interrupted; reconnecting. Saved messages are retained.");
  };
}

function panelNotice(text) {
  const notice = document.createElement("p"); notice.id = "stream-status"; notice.className = "small muted";
  notice.textContent = text; $("#runtime-panel")?.appendChild(notice);
}

async function refreshFiles() {
  if (!$("#workspace-files") || !isAgentRuntime()) return;
  try {
    const files = await api("GET", `/api/sessions/${sid}/workspace/files`);
    $("#workspace-files").innerHTML = files.length ? files.map((file) => `<div class="workspace-file row">
      <button class="btn btn-sm btn-ghost grow" data-preview="${esc(file.path)}">${esc(file.path)}</button>
      <span class="small muted">${file.bytes} bytes</span><a class="btn btn-sm" href="/api/sessions/${sid}/workspace/file?path=${encodeURIComponent(file.path)}">Download</a></div>`).join("") : `<p class="small muted">No files yet.</p>`;
    $$('[data-preview]').forEach((button) => button.onclick = async () => {
      const response = await fetch(`/api/sessions/${sid}/workspace/file?path=${encodeURIComponent(button.dataset.preview)}`);
      if (!response.ok) return toast("Could not open that file");
      const type = response.headers.get("Content-Type") || "";
      if (!/text|json|csv|javascript|xml/.test(type)) return toast("Binary file: use Download to open it");
      $("#file-preview").textContent = (await response.text()).slice(0, 100000);
      $("#file-preview").classList.remove("hidden");
    });
  } catch (err) { $("#workspace-files").textContent = err.message; }
}

let composeStart = null;
$("#input").addEventListener("input", (e) => {
  if (!composeStart && e.target.value) composeStart = Date.now();
  e.target.style.height = "auto";
  e.target.style.height = Math.min(200, e.target.scrollHeight) + "px";
});
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (S?.phase === "chat" && isVoiceMode() && document.body.dataset.voiceActive === "true") {
      toast("Stop voice chat to send a typed message");
      return;
    }
    $("#composer").requestSubmit();
  }
});
$("#composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ta = $("#input");
  const text = ta.value.trim();
  if (!text || busy || $("#send").disabled) return;
  busy = true;
  $("#send").disabled = true;
  const compose_ms = composeStart ? Date.now() - composeStart : null;
  composeStart = null;
  const messageId = pendingMessage?.text === text ? pendingMessage.id : crypto.randomUUID();
  pendingMessage = { id: messageId, text };
  ta.value = "";
  ta.style.height = "auto";
  // optimistic render
  S.events.push({ id: "pending", role: "user", text });
  renderThread();
  if (S.condition !== "human" && !isAgentRuntime()) $("#typing").innerHTML = `<div class="msg msg-ai"><div class="who">${partnerName()}</div><span class="typing"><i></i><i></i><i></i></span></div>`;
  scrollBottom();
  try {
    const res = await api("POST", `/api/sessions/${sid}/message`, { text, compose_ms, client_message_id: messageId });
    const events = new Map(S.events.filter((x) => x.id !== "pending").map((x) => [x.id, x]));
    res.events.forEach((event) => { if (!events.has(event.id)) events.set(event.id, event); });
    S.events = [...events.values()];
    if (res.runtime) S.runtime = res.runtime;
    pendingMessage = null;
  } catch (err) {
    S.events = S.events.filter((x) => x.id !== "pending");
    ta.value = text;
    toast(err.message);
  }
  $("#typing").innerHTML = "";
  busy = false;
  $("#send").disabled = false;
  renderThread();
  renderRuntime();
  scrollBottom();
  ta.focus();
});

async function pollPartner() {
  if (S.phase !== "chat") return;
  try {
    const r = await api("GET", `/api/sessions/${sid}/poll?after=${S.events.length}`);
    if (r.events.length) {
      S.events.push(...r.events);
      const atBottom = $("#content").scrollHeight - $("#content").scrollTop - $("#content").clientHeight < 80;
      renderThread();
      if (atBottom) scrollBottom();
    }
  } catch { /* transient */ }
}

// ------------------------------------------------------------------ marking

function paintBookmarkStatus() {
  const pending = bookmarkOutbox?.pending() || [];
  let status = $("#bookmark-status");
  if (!status && $("#fab")) {
    status = document.createElement("span"); status.id = "bookmark-status"; status.className = "bookmark-status";
    status.setAttribute("aria-live", "polite"); $("#fab").after(status);
  }
  if (!status) return;
  status.textContent = pending.length ? bookmarkOutbox.storageAvailable
    ? `${pending.length} saved on this device · ${pending.some(r => r.state === "failed") ? "retrying sync" : "syncing"}`
    : "Not saved to this device. Keep this tab open while syncing." : S.marks.length && document.body.dataset.voiceActive !== "true" ? "Bookmarks saved" : "";
}

function createMark({ event_id = null, anchor = "timestamp", quote = null, source = "overlay" }) {
  if (!bookmarkOutbox) return;
  const payload = { event_id, anchor, quote, source, client_t: Date.now(), phase_at_click: S.phase,
                    draft_len: $("#input").value.length, exposure: captureExposure(S, $("#content"), event_id) };
  const m = bookmarkOutbox.create(payload);
  if (!S.marks.some(x => x.id === m.id)) S.marks.push(m);
  if ($("#thread")) renderThread();
  $("#fab-count").textContent = S.marks.filter((x) => !x.deleted).length;
  const fab = $("#fab");
  fab.classList.remove("pulse"); void fab.offsetWidth; fab.classList.add("pulse");
  const el = m.event_id && $(`.msg[data-id="${m.event_id}"]`);
  if (el) { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 900); }
  toast(bookmarkOutbox.storageAvailable ? "Moment saved on this device" : "Moment is syncing; keep this tab open");
  return m;
}

$("#fab").addEventListener("click", () => createMark({ anchor: "timestamp",
  source: isVoiceMode() && document.body.dataset.voiceActive === "true" ? "voice" : "overlay" }));
document.addEventListener("keydown", (e) => {
  if (isMarkHotkey(e) && S?.phase === "chat") { e.preventDefault(); createMark({ anchor: "hotkey" }); }
});
window.addEventListener("beforeunload", (e) => {
  if (bookmarkOutbox?.pending().length) { e.preventDefault(); e.returnValue = ""; }
});
window.addEventListener("pagehide", () => {
  if (!voiceClient?.pc) return;
  voiceClient.closeLocal();
  navigator.sendBeacon?.(`/api/sessions/${sid}/runtime/voice/stop`,
    new Blob(["{}"], { type: "application/json" }));
});
document.addEventListener("click", (e) => {
  const b = e.target.closest(".mark-here");
  if (b && S.phase === "chat") createMark({ event_id: b.dataset.mark, anchor: "message" });
});

// Select text inside a message -> "Mark this" chip, so the exact phrase is captured.
document.addEventListener("mouseup", () => setTimeout(() => {
  $(".sel-chip")?.remove();
  if (S?.phase !== "chat") return;
  const sel = window.getSelection();
  const text = sel?.toString().trim();
  if (!text || text.length < 3) return;
  const msg = sel.anchorNode?.parentElement?.closest(".msg");
  if (!msg || msg !== sel.focusNode?.parentElement?.closest(".msg")) return;
  const r = sel.getRangeAt(0).getBoundingClientRect();
  const chip = document.createElement("button");
  chip.className = "sel-chip";
  chip.innerHTML = `${icon("bulb")}Mark this`;
  chip.style.left = Math.min(window.innerWidth - 120, r.left + r.width / 2 - 45) + "px";
  chip.style.top = Math.max(8, r.top - 40) + "px";
  chip.onmousedown = (ev) => ev.preventDefault();
  chip.onclick = () => { chip.remove(); createMark({ event_id: msg.dataset.id, anchor: "selection", quote: text }); sel.removeAllRanges(); };
  document.body.appendChild(chip);
}, 0));

// ------------------------------------------------------------------ external agent session (Claude Code / Codex)

function externalView() {
  $("#content").innerHTML = `<div class="narrow">
    <h1>Agent session</h1>
    <p class="instructions">This session is recorded with the <b>Spark Recorder</b>. Open it on the participant's computer (Chrome or Edge recommended):</p>
    <div class="prompt-card" style="text-align:center"><div class="label">Recorder link</div>
      <p><a href="/record?session=${esc(sid)}">${esc(location.origin)}/record?session=${esc(sid)}</a></p></div>
    <p class="muted small">When the participant ends the session, the recording and their flags are saved here and the review opens automatically.</p>
    <a class="btn" href="/review?id=${sid}">Open review</a>
  </div>`;
}

// ------------------------------------------------------------------ review

function reviewView() {
  location.replace(`/review?id=${sid}`);
}

function doneView() {
  $("#content").innerHTML = `<div class="narrow" style="text-align:center;padding-top:80px">
    <div style="color:var(--accent)">${icon("check", "big")}</div>
    <h1>Thank you!</h1><p class="instructions">Your session is complete. Please let the researcher know you're finished.</p></div>`;
}

// ------------------------------------------------------------------ boot

cfg = await config();
await load();
renderShell();
route();
