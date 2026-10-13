import {
  api, icon, esc, $, $$, config, renderText, openMarkPopover, closeMarkPopover, toast, TYPE_COLOR, typeByKey,
  fmtTime, isMarkHotkey, annotationFormHTML, wireAnnotationForm,
} from "/static/common.js";

const sid = new URLSearchParams(location.search).get("id");
let cfg, S, study;
let busy = false;
let pollTimer = null;
let eventStream = null;
let connecting = false;
let pendingMessage = null;
let workspaceRefresh = null;
const isCodex = () => S.runtime?.kind === "codex";
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
  $("#brand").innerHTML = `${icon("bulb")}<span>Spark study</span>`;
  $("#top-ic").innerHTML = icon("chat");
  $("#top-title").textContent = S.prompt.title;
  $("#fab-ic").innerHTML = icon("bulb");
  $("#task").innerHTML = `<h3>${esc(S.prompt.title)}</h3><div class="muted">${esc(S.prompt.text)}</div>`;
  $("#foot").textContent = `${S.participant_id} · ${S.id}`;
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
  closeMarkPopover();
  banner("");
  renderSteps();
  $("#composer-wrap").classList.add("hidden");
  $("#fab").classList.add("hidden");
  $("#top-actions").innerHTML = "";
  ({ pre: writingView, post: writingView, chat: S.external ? externalView : chatView, review: reviewView, done: doneView })[S.phase]();
  tick();
}

async function advance(text) {
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

function partnerName() { return S.condition === "human" ? "Partner" : isCodex() ? "Codex" : S.runtime?.kind === "demo" ? "Scripted demo" : "AI"; }

function msgHTML(e) {
  const marks = S.marks.filter((m) => m.event_id === e.id && !m.deleted);
  const groups = {};
  marks.forEach((m) => (groups[m.quick_type || ""] = (groups[m.quick_type || ""] || 0) + 1));
  const badges = Object.entries(groups).map(([k, n]) => {
    const t = k && typeByKey(cfg, k);
    return `<span class="mark-badge" title="${esc(t ? t.participant : "Marked")}">${icon(t ? t.icon : "bulb")}${t ? esc(t.label) : "Marked"}${n > 1 ? ` ×${n}` : ""}</span>`;
  }).join("");
  const btn = e.id === "pending" ? "" : `<button class="icon-btn mark-here" data-mark="${e.id}" title="Mark this message" aria-label="Mark this message">${icon("bulb")}</button>`;
  if (["tool_call", "tool_result"].includes(e.role)) {
    const label = e.role === "tool_result" ? "Tool result" : ({ commandExecution: "Command", fileChange: "File changes", webSearch: "Web search" }[e.tool] || e.tool || "Tool");
    return `<div class="msg msg-tool ${marks.length ? "marked" : ""}" data-id="${e.id}">${btn}
      <details data-tool="${e.id}"><summary>${esc(label)} <span class="small muted">${esc(e.status || "")}</span>${badges}</summary>
      <pre>${esc(e.text)}</pre>${e.output ? `<div class="small muted">Output</div><pre>${esc(e.output)}</pre>` : ""}</details></div>`;
  }
  if (e.role === "user") {
    return `<div class="msg msg-user ${marks.length ? "marked" : ""}" data-id="${e.id}">${btn}${renderText(e.text)}
      ${badges ? `<div class="mark-badges" style="margin-top:6px">${badges}</div>` : ""}</div>`;
  }
  return `<div class="msg msg-ai ${marks.length ? "marked" : ""}" data-id="${e.id}">${btn}
    <div class="who">${partnerName()}<span class="mark-badges">${badges}</span></div>${renderText(e.text)}</div>`;
}

function renderThread() {
  const opened = new Set($$("details[open][data-tool]").map((el) => el.dataset.tool));
  const evs = S.events.filter((e) => !e.meta?.superseded && (CHAT_ROLES.includes(e.role) || isCodex() && ["tool_call", "tool_result"].includes(e.role)));
  const intro = S.condition === "human"
    ? "You're paired with another participant. Say hello and start discussing the question."
    : "Start by telling your partner what you think about the question, or ask it anything.";
  $("#thread").innerHTML = evs.length ? evs.map(msgHTML).join("") : `<div class="empty-chat">${esc(intro)}</div>`;
  $$("details[data-tool]").forEach((el) => { el.open = opened.has(el.dataset.tool); });
  $("#fab-count").textContent = S.marks.filter((m) => !m.deleted).length;
}

function scrollBottom() { const c = $("#content"); c.scrollTop = c.scrollHeight; }

function chatView() {
  $("#content").innerHTML = `<div class="narrow"><p class="instructions small">${esc(study.chat_instructions)}</p>
    <div class="runtime-panel" id="runtime-panel"></div><div id="approvals"></div>
    ${isCodex() ? `<details class="workspace-panel"><summary>Task workspace <span class="small muted">files live on the execution host</span></summary>
      <p class="small muted">Only upload files you intend to share with the agent. Existing files are never overwritten.</p>
      <label class="btn btn-sm">${icon("upload")} Add task file<input id="workspace-upload" type="file" hidden></label>
      <button class="btn btn-sm btn-ghost" id="refresh-files">Refresh files</button><div id="workspace-files"></div><pre id="file-preview" class="hidden"></pre></details>` : ""}
    <div class="thread" id="thread"></div><div id="typing"></div></div>`;
  $("#composer-wrap").classList.remove("hidden");
  $("#fab").classList.remove("hidden");
  $("#top-actions").innerHTML = `${isCodex() ? `<button class="btn btn-sm" id="connect-agent">Reconnect</button><button class="btn btn-sm hidden" id="stop-agent">${icon("stop")} Stop agent</button>` : ""}<button class="btn btn-sm" id="finish">Finish conversation</button>`;
  $("#finish").onclick = async () => {
    if (!confirm("Finish the conversation and move on? You won't be able to send more messages.")) return;
    await advance();
  };
  renderThread();
  scrollBottom();
  $("#input").focus();
  if (S.condition === "human") pollTimer = setInterval(pollPartner, 1500);
  if (isCodex()) {
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
}

function renderRuntime() {
  const panel = $("#runtime-panel");
  if (!panel) return;
  if (!isCodex()) {
    const mode = S.condition === "human" ? "Human partner" : S.runtime?.kind === "demo" ? "SCRIPTED DEMO — not a live model" : S.runtime?.kind === "legacy" ? "Legacy saved conversation — runtime configuration was not recorded" : "Plain LLM chat — no agent tools";
    panel.innerHTML = `<span class="status-pill">${esc(mode)}</span>`;
    $("#send").disabled = busy;
    return;
  }
  const runtime = S.runtime;
  const active = ["starting", "running", "awaiting_approval"].includes(runtime.status);
  const status = connecting ? "connecting" : runtime.status || "disconnected";
  panel.innerHTML = `<div class="row"><span class="status-pill">Codex agent · ${esc(status.replaceAll("_", " "))}</span>
      <span class="small muted">${esc(runtime.model || "Configured model")} · ${esc(runtime.version || cfg.runtime.version || "")}</span></div>
      <p class="small muted">Local development runtime. Commands operate on its task workspace, not automatically on your personal files.</p>
      ${runtime.error ? `<div class="runtime-error" role="alert">${esc(runtime.error)}</div>` : ""}`;
  $("#send").disabled = busy || active || connecting || !["ready", "interrupted", "failed"].includes(runtime.status);
  $("#finish").disabled = busy || active || connecting;
  $("#stop-agent").classList.toggle("hidden", !active);
  $("#stop-agent").disabled = runtime.status === "starting";
  $("#connect-agent").disabled = active || connecting;
  $("#connect-agent").classList.toggle("hidden", runtime.status === "ready" && !runtime.error);
  $("#workspace-upload").disabled = active || connecting;
  const approvals = runtime.approvals || [];
  $("#approvals").innerHTML = approvals.map((a) => {
    const item = S.events.find((e) => e.meta?.runtime_item_id === a.params.itemId);
    const description = a.params.command || item?.text || a.params.reason || "The agent requested permission.";
    return `<div class="approval-card" data-approval="${a.id}"><h3>Agent approval required</h3>
      <pre>${esc(description)}</pre>${a.params.reason ? `<p>${esc(a.params.reason)}</p>` : ""}
      <p class="small muted">Approval can authorize access beyond the command's normal sandbox. Decline anything unrelated to the task.</p>
      <button class="btn btn-primary btn-sm" data-decision="accept">Allow once</button>
      <button class="btn btn-sm" data-decision="decline">Decline</button></div>`;
  }).join("");
  $$("[data-approval]").forEach((card) => $$('[data-decision]', card).forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      try { await api("POST", `/api/sessions/${sid}/runtime/approval`, { id: card.dataset.approval, decision: button.dataset.decision }); }
      catch (err) { toast(err.message); button.disabled = false; }
    };
  }));
}

async function connectAgent() {
  if (connecting || S.phase !== "chat") return;
  connecting = true; renderRuntime();
  try { S.runtime = await api("POST", `/api/sessions/${sid}/runtime/connect`, {}); }
  catch (err) { S.runtime.status = "error"; S.runtime.error = err.message; }
  finally { connecting = false; renderRuntime(); }
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
    renderThread(); renderRuntime();
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
  if (!$("#workspace-files") || !isCodex()) return;
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
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
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
  if (S.condition !== "human" && !isCodex()) $("#typing").innerHTML = `<div class="msg msg-ai"><div class="who">${partnerName()}</div><span class="typing"><i></i><i></i><i></i></span></div>`;
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

async function createMark({ event_id = null, anchor = "latest", quote = null, source = "overlay", popAnchor }) {
  const m = await api("POST", `/api/sessions/${sid}/marks`, {
    event_id, anchor, quote, source, client_t: Date.now(), draft_len: $("#input").value.length,
  });
  S.marks.push(m);
  if ($("#thread")) renderThread();
  $("#fab-count").textContent = S.marks.filter((x) => !x.deleted).length;
  const fab = $("#fab");
  fab.classList.remove("pulse"); void fab.offsetWidth; fab.classList.add("pulse");
  const el = m.event_id && $(`.msg[data-id="${m.event_id}"]`);
  if (el) { el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 900); }
  openMarkPopover({
    anchor: popAnchor || fab, cfg,
    onPick: async (type) => {
      if (!type) return;
      m.quick_type = type;
      await api("PATCH", `/api/sessions/${sid}/marks/${m.id}`, { quick_type: type });
      if ($("#thread")) renderThread();
      toast(`Tagged: ${typeByKey(cfg, type).label}`);
    },
  });
  return m;
}

$("#fab").addEventListener("click", () => createMark({ anchor: "latest" }));
document.addEventListener("keydown", (e) => {
  if (isMarkHotkey(e) && S?.phase === "chat") { e.preventDefault(); createMark({ anchor: "hotkey" }); }
});
document.addEventListener("click", (e) => {
  const b = e.target.closest(".mark-here");
  if (b && S.phase === "chat") createMark({ event_id: b.dataset.mark, anchor: "message", popAnchor: b });
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
  chip.onclick = () => { chip.remove(); createMark({ event_id: msg.dataset.id, anchor: "selection", quote: text, popAnchor: chip }); sel.removeAllRanges(); };
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

const reviewable = () => S.events.filter((e) => CHAT_ROLES.includes(e.role) || ((S.external || isCodex()) && e.role !== "thinking"));

function contextHTML(m) {
  const evs = reviewable();
  let i = evs.findIndex((e) => e.id === m.event_id);
  if (i < 0) return `<div class="ctx muted">This mark isn't attached to a message.</div>`;
  const slice = evs.slice(Math.max(0, i - 2), i + 3);
  const label = (e) => ({ user: "You", assistant: S.external ? "Agent" : partnerName(), partner: "Partner", tool_call: `Agent ran ${e.tool || "a tool"}`, tool_result: "Tool output", system: "System" }[e.role] || e.role);
  return `<div class="ctx">${slice.map((e) => {
    const anchor = e.id === m.event_id;
    let t = esc(anchor ? e.text : e.text.slice(0, 420) + (e.text.length > 420 ? "…" : ""));
    if (anchor && m.quote) { const q = esc(m.quote); t = t.includes(q) ? t.replace(q, `<mark class="q">${q}</mark>`) : t; }
    return `<div class="ctx-ev ${anchor ? "anchor" : ""}"><div class="r">${esc(label(e))}${anchor ? " · marked" : ""}</div><div class="t">${t}</div></div>`;
  }).join("")}</div>`;
}

function reviewCardHTML(m, n) {
  return `<div class="review-card ${m.review ? "saved" : ""}" data-mark="${m.id}">
    <div class="review-head"><h3 style="margin:0">Moment ${n}</h3><span class="when">${fmtTime(m.created_at)}${m.source === "candidate_confirm" ? " · added during review" : ""}</span>
      <span class="status">${m.review ? `${icon("check")} Saved` : ""}</span></div>
    ${contextHTML(m)}
    <div class="row small muted" style="margin-top:-6px">Not the right message?
      <button class="btn btn-sm btn-ghost" data-move="-1">← Earlier</button><button class="btn btn-sm btn-ghost" data-move="1">Later →</button></div>
    ${annotationFormHTML(cfg, m, { external: S.external })}
  </div>`;
}

function wireCard(card, m) {
  $$("[data-move]", card).forEach((b) => (b.onclick = async () => {
    const evs = reviewable();
    const i = evs.findIndex((e) => e.id === m.event_id) + +b.dataset.move;
    if (i < 0 || i >= evs.length) return;
    m.event_id = evs[i].id;
    await api("PATCH", `/api/sessions/${sid}/marks/${m.id}`, { event_id: m.event_id });
    card.querySelector(".ctx").outerHTML = contextHTML(m);
  }));
  wireAnnotationForm(card, m, {
    sid,
    onSaved: () => { card.classList.add("saved"); $(".status", card).innerHTML = `${icon("check")} Saved`; },
    onRemoved: () => card.remove(),
  });
}

function reviewView() {
  if (S.external || S.recording?.file) return location.replace(`/review?id=${sid}`);
  const evIdx = Object.fromEntries(S.events.map((e, i) => [e.id, i]));
  const marks = S.marks.filter((m) => !m.deleted).sort((a, b) => (evIdx[a.event_id] ?? 1e9) - (evIdx[b.event_id] ?? 1e9));
  $("#content").innerHTML = `<div class="narrow">
    <h1>Review your moments</h1>
    <p class="instructions">${marks.length
      ? `You marked <b>${marks.length}</b> moment${marks.length > 1 ? "s" : ""}. For each one, tell us briefly what happened. The surrounding messages are shown so you don't have to reread the whole conversation.`
      : "You didn't mark any moments during the conversation. That's fine. Below are a few moments you can look at."}</p>
    <div id="cards"></div>
    <div id="cands"></div>
    <div class="row" style="margin-top:28px"><span class="grow muted small">Unsaved changes on a card are lost when you finish.</span><button class="btn btn-primary" id="done">Finish review</button></div>
  </div>`;
  marks.forEach((m, i) => addCard(m, i + 1));
  $("#done").onclick = async () => {
    const unsaved = $$(".review-card:not(.saved)").length;
    if (unsaved && !confirm(`${unsaved} moment${unsaved > 1 ? "s are" : " is"} not saved yet. Finish anyway?`)) return;
    await advance();
  };
  renderCandidates();
}

function addCard(m, n) {
  const wrap = document.createElement("div");
  wrap.innerHTML = reviewCardHTML(m, n);
  const card = wrap.firstElementChild;
  $("#cards").appendChild(card);
  wireCard(card, m);
  return card;
}

function renderCandidates() {
  const det = S.detection;
  const box = $("#cands");
  if (!study.show_candidates_in_review) return;
  if (!det || det.status === "running") {
    box.innerHTML = `<div class="section-title">${icon("sparkle")}<h2 style="margin:0">Other moments you might have noticed</h2></div><p class="muted">Looking through the conversation…</p>`;
    clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      const r = await api("GET", `/api/sessions/${sid}/poll?after=${S.events.length}`).catch(() => null);
      if (r?.detection && r.detection.status !== "running") {
        clearInterval(pollTimer);
        const fresh = await api("GET", `/api/sessions/${sid}`);
        S.candidates = fresh.candidates; S.detection = fresh.detection; S.analysis = fresh.analysis;
        renderCandidates();
      }
    }, 2000);
    return;
  }
  const linked = new Set((S.analysis?.links || []).map((l) => l.candidate));
  const minConf = det.method === "llm" ? 0.4 : 0;
  const list = S.candidates.filter((c) => !linked.has(c.id) && c.confidence >= minConf && !c.participant_response)
    .sort((a, b) => b.confidence - a.confidence).slice(0, 6);
  if (!list.length) { box.innerHTML = ""; return; }
  const evs = Object.fromEntries(S.events.map((e) => [e.id, e]));
  const short = (t, n = 380) => esc(t.length > n ? t.slice(0, n) + "…" : t);
  box.innerHTML = `<div class="section-title">${icon("sparkle")}<h2 style="margin:0">Other moments you might have noticed</h2></div>
    <p class="instructions small">These are moments where your thinking <i>might</i> have shifted. Only say yes if it really did.</p>
    ${list.map((c) => {
      const tr = evs[c.trigger_event_id], up = evs[c.uptake_event_id];
      return `<div class="cand-card" data-cand="${c.id}">
        ${tr ? `<div class="ctx-ev anchor"><div class="r">${tr.role === "user" ? "You" : S.external ? "Agent" : partnerName()}</div><div class="t">${short(tr.text)}</div></div>` : ""}
        ${up && up.id !== tr?.id ? `<div class="ctx-ev"><div class="r">Then you said</div><div class="t">${short(up.text, 260)}</div></div>` : ""}
        <div class="row" style="margin-top:8px"><span class="small grow">Did this shift your thinking?</span>
          <button class="btn btn-sm" data-ans="yes">Yes</button><button class="btn btn-sm" data-ans="no">No</button><button class="btn btn-sm btn-ghost" data-ans="unsure">Not sure</button></div>
      </div>`;
    }).join("")}`;
  $$("[data-cand]", box).forEach((el) => $$("[data-ans]", el).forEach((b) => (b.onclick = async () => {
    const c = S.candidates.find((x) => x.id === el.dataset.cand);
    const answer = b.dataset.ans;
    let mark_id = null;
    if (answer === "yes") {
      const m = await api("POST", `/api/sessions/${sid}/marks`, { event_id: c.trigger_event_id, anchor: "candidate", source: "candidate_confirm" });
      S.marks.push(m);
      mark_id = m.id;
      const card = addCard(m, $$(".review-card").length + 1);
      card.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    c.participant_response = { answer, mark_id };
    await api("PATCH", `/api/sessions/${sid}/candidates/${c.id}`, { participant_response: { answer, mark_id } });
    el.classList.add("answered");
    $$("button", el).forEach((x) => (x.disabled = true));
  })));
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
