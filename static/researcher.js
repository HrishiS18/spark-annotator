import { api, icon, esc, $, $$, config, toast, TYPE_COLOR, typeByKey, fmtDate, fmtTime } from "/static/common.js";

const cfg = await config();
let sessions = [];
let S = null;
let sel = null; // selected event id
const show = { tools: false, thinking: false, signalsOnly: false };
const coder = (() => { try { return localStorage.getItem("coder") || ""; } catch { return ""; } })();

$("#brand").innerHTML = `${icon("bulb")}<span>Spark research</span>`;

async function refreshList() {
  sessions = await api("GET", "/api/sessions");
  renderList();
}
function renderList() {
  const q = $("#filter").value.toLowerCase();
  $("#list").innerHTML = sessions.filter((s) => !q || JSON.stringify(s).toLowerCase().includes(q)).map((s) => `
    <div class="rs-item ${S?.id === s.id ? "active" : ""}" data-id="${s.id}">
      <div><b>${esc(s.participant_id)}</b> · ${esc(s.prompt)}</div>
      <div class="l2">${esc(s.study)} · ${esc(s.study === "agentic" ? "external agent" : s.condition === "human" ? "human partner" : s.persona)} · ${esc(s.phase)}</div>
      <div class="l2">${s.marks} marks (${s.reviewed} reviewed) · ${s.candidates} candidates · ${fmtDate(s.created_at)}</div>
    </div>`).join("") || `<p class="muted small" style="padding:8px">No sessions yet.</p>`;
  $$(".rs-item").forEach((el) => (el.onclick = () => openSession(el.dataset.id)));
}
$("#filter").oninput = renderList;

async function openSession(id, keepSel = false) {
  S = await api("GET", `/api/sessions/${id}`);
  if (!keepSel) sel = null;
  history.replaceState(null, "", `#${id}`);
  renderList();
  render();
}

// ------------------------------------------------------------------ derived lookups

const marksOn = (eid) => S.marks.filter((m) => !m.deleted && m.event_id === eid);
const src = (c) => c.source || "codebook";
const candsOn = (eid, source) => S.candidates.filter((c) => c.trigger_event_id === eid && (!source || src(c) === source));
const uptakeOn = (eid) => S.candidates.filter((c) => c.uptake_event_id === eid && c.trigger_event_id !== eid);
const traced = (eid) => (S.trace?.units || []).filter((u) => u.source_event_ids.includes(eid));
const markType = (m) => m.review?.primary || m.quick_type;
const tLabel = (k) => typeByKey(cfg, k)?.label || (k === "other" ? "Other" : k || "untagged");
const roleLabel = (r) => ({ user: "participant", assistant: S.external ? "agent" : "AI", partner: "partner", tool_call: "tool call", tool_result: "tool result", thinking: "reasoning", system: "system" }[r] || r);

// ------------------------------------------------------------------ render

const pct = (n, d) => (d ? `${Math.round((100 * n) / d)}%` : "–");

let judgePoll = null;
function render() {
  clearInterval(judgePoll);
  if (S.judge?.status === "running") judgePoll = setInterval(async () => {
    const r = await api("GET", `/api/sessions/${S.id}/poll?after=${S.events.length}`).catch(() => null);
    if (r?.judge?.status !== "running") { clearInterval(judgePoll); openSession(S.id, true); }
  }, 3000);
  const a = S.analysis;
  const det = S.detection || {};
  const jd = S.judge || {};
  const jcfg = cfg.judge || {};
  const pre = S.writing.pre?.text, post = S.writing.post?.text;
  $("#main").innerHTML = `
    <div class="rs-head">
      <div class="row"><h1 style="margin:0">${esc(S.participant_id)} · ${esc(S.prompt.title)}</h1></div>
      <div class="row">
        <span class="tag">${esc(S.study)}</span><span class="tag">${S.external ? `external agent${S.imports.length ? ` · ${esc(S.imports.at(-1).meta?.agent || "")}` : " · no log yet"}` : S.condition === "human" ? `human partner · room ${esc(S.room || "?")}` : `persona: ${esc(S.persona)}`}</span>
        <span class="tag">phase: ${esc(S.phase)}</span><span class="tag">${S.events.length} events</span>
        <span class="tag">parser: ${esc(det.method === "codebook" ? `codebook v${det.codebook_version}` : det.method || "not run")}${det.status === "running" ? " (running…)" : ""}</span>
        <span class="tag">judge: ${jd.status === "running" ? "running…" : jd.status === "done" ? `${esc(jd.model || "")} · ${jd.n} found · $${jd.cost_usd ?? "?"}` : jd.status === "error" ? "failed" : "not run"}</span>
        ${S.recording ? `<span class="tag">recording: ${S.recording.file ? `${Math.round((S.recording.duration || 0) / 60)} min${S.recording.synthetic ? " (synthetic demo)" : ""}` : esc(S.recording.status)}</span>` : ""}
        <span class="grow"></span>
        <button class="btn btn-sm" id="b-detect">${icon("sparkle")}${det.method ? "Re-run" : "Run"} parser</button>
        <button class="btn btn-sm" id="b-judge" ${jcfg.live && S.events.length && jd.status !== "running" ? "" : "disabled"}
          title="${esc(jcfg.live ? (S.events.length ? `Claude (${jcfg.model}) reads the whole timestamped transcript and codes sparks` : "Attach the agent transcript first") : `Needs an Anthropic API key on the server (${jcfg.reason || ""})`)}">${icon("eye")}${jd.status === "done" ? "Re-run" : "Run"} LLM judge</button>
        <a class="btn btn-sm" href="/api/sessions/${S.id}/transcript.md" target="_blank">Timestamped transcript</a>
        ${post ? `<button class="btn btn-sm" id="b-trace">Trace before → after</button>` : ""}
        <label class="btn btn-sm">${icon("upload")}Import log<input type="file" id="b-import" accept=".zip,.jsonl,.json" hidden></label>
        ${S.recording?.file ? `<a class="btn btn-sm btn-accent" href="/review?id=${S.id}&mode=researcher">${icon("play")}Recording review</a>` : ""}
        <a class="btn btn-sm" href="/api/sessions/${S.id}/export.json">Export JSON</a>
        <a class="btn btn-sm btn-ghost" href="/session?id=${S.id}" target="_blank">Participant view</a>
      </div>
      ${det.error ? `<div class="small" style="color:var(--danger);margin-top:6px">Parser error: ${esc(det.error)}</div>` : ""}
      ${jd.status === "error" ? `<div class="small" style="color:var(--danger);margin-top:6px">LLM judge failed: ${esc(jd.error)}</div>` : ""}
      ${jd.summary ? `<details class="small" style="margin-top:6px"><summary><b>Judge's summary</b> of how the participant's thinking evolved</summary><p style="margin:6px 0 0">${esc(jd.summary)}</p></details>` : ""}
      <div id="import-list"></div>
    </div>
    <div class="stats">
      <div class="stat"><div class="n">${a.marks}</div><div class="k">participant marks</div></div>
      <div class="stat"><div class="n">${a.codebook} · ${a.judge}</div><div class="k">candidates: codebook · LLM judge</div></div>
      <div class="stat"><div class="n">${pct(a.marks_found_by.codebook, a.marks)} · ${pct(a.marks_found_by.judge, a.marks)}</div><div class="k">flags also found by codebook · judge (±${a.tolerance_events} event)</div></div>
      <div class="stat"><div class="n">${pct(a.judge_vs_codebook.judge_with_codebook_nearby, a.judge)}</div><div class="k">judge finds also near a codebook find (${pct(a.judge_vs_codebook.same_type, a.judge)} same type)</div></div>
      <div class="stat"><div class="n">${S.candidates.filter((c) => c.participant_response?.answer === "yes").length}/${S.candidates.filter((c) => c.participant_response).length}</div><div class="k">candidates confirmed in review</div></div>
      <div class="stat" style="grid-column: span 2">
        <table class="type-table"><tr><th>type</th><th>flags</th><th>codebook</th><th>judge</th></tr>
        ${Object.entries(a.by_type).map(([k, v]) => `<tr style="--tc:${TYPE_COLOR[k]}"><td><span class="bar" style="width:8px"></span> ${esc(tLabel(k))}</td>
          <td><span class="bar" style="width:${v.marks * 10}px"></span> ${v.marks}</td><td><span class="bar" style="width:${v.codebook * 10}px;opacity:.55"></span> ${v.codebook}</td>
          <td><span class="bar" style="width:${v.judge * 10}px;opacity:.35"></span> ${v.judge}</td></tr>`).join("")}</table>
      </div>
    </div>
    ${pre || post ? diffHTML(pre, post) : ""}
    <div class="rs-body">
      <section class="timeline">
        <div class="tl-ctrl">
          <label><input type="checkbox" data-show="signalsOnly" ${show.signalsOnly ? "checked" : ""}> only events with signals</label>
          <label><input type="checkbox" data-show="tools" ${show.tools ? "checked" : ""}> tool calls/results</label>
          <label><input type="checkbox" data-show="thinking" ${show.thinking ? "checked" : ""}> agent reasoning</label>
          <span class="muted">● participant flag &nbsp; ■ codebook &nbsp; ★ LLM judge (hollow = persuasion/copying) &nbsp; ◆ used in final answer</span>
        </div>
        <div class="lane-head"><span title="Participant flags">P</span><span title="Codebook parser">C</span><span title="LLM judge">J</span><span title="Traced into final answer">D</span><span></span></div>
        <div id="tl"></div>
      </section>
      <aside class="inspector" id="insp"></aside>
    </div>`;
  wireHead();
  renderTimeline();
  renderInspector();
}

function diffHTML(pre, post) {
  const t = S.trace;
  return `<div class="diff"><details class="box" ${t ? "open" : ""}><summary>Before / after write-ups${t ? ` · shift: ${esc(t.position_shift)} · ${esc(t.method)}` : ""}</summary>
    <div class="diff-cols" style="margin-top:10px">
      <div><div class="small muted">Before (no AI)</div><div class="insp-text">${esc(pre || "—")}</div></div>
      <div><div class="small muted">After (no AI)</div><div class="insp-text">${esc(post || "—")}</div></div>
    </div>
    ${t ? `<p class="small" style="margin:12px 0 4px">${esc(t.summary)}</p>
      ${t.units.map((u) => `<div class="unit"><span class="st ${u.status}">${u.status}</span><span class="grow">${esc(u.text)}
        <span class="muted small"> · ${esc(u.mechanism)}${u.spark_type && u.spark_type !== "none" ? ` (${esc(tLabel(u.spark_type))})` : ""}${u.copy_overlap ? ` · verbatim overlap ${Math.round(u.copy_overlap * 100)}%` : ""}</span></span>
        <span>${u.source_event_ids.map((e) => `<span class="evlink" data-goto="${e}">${e}</span>`).join(" ")}</span></div>`).join("")}
      ${t.dropped?.length ? `<div class="small muted" style="margin-top:8px">Dropped: ${t.dropped.map((d) => esc(d.pre_quote)).join(" · ")}</div>` : ""}` : `<p class="small muted">Click “Trace before → after” to attribute each part of the final answer to conversation turns.</p>`}
  </details></div>`;
}

function wireHead() {
  $("#b-detect").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Detecting…";
    try { await api("POST", `/api/sessions/${S.id}/detect`); } catch (err) { toast(err.message); }
    openSession(S.id, true);
  };
  $("#b-judge").onclick = async (e) => {
    const est = await api("GET", `/api/sessions/${S.id}/judge/estimate`);
    if (!confirm(`Run the LLM judge on this session?\n\nThis sends the session's transcript (not the video) to the Anthropic API.\nModel: ${est.model} · about ${est.input_tokens.toLocaleString()} input tokens · roughly $${est.approx_cost_usd}.\n\nThe judge does not see the participant's flags or the codebook results.`)) return;
    e.target.disabled = true;
    try { await api("POST", `/api/sessions/${S.id}/judge`, { hints: false }); toast("Judge started; this can take a minute or two"); }
    catch (err) { toast(err.message); }
    openSession(S.id, true);
  };
  $("#b-trace") && ($("#b-trace").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Tracing…";
    try { await api("POST", `/api/sessions/${S.id}/trace`); } catch (err) { toast(err.message); }
    openSession(S.id, true);
  });
  $("#b-import").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const up = await api("POST", "/api/upload", await f.arrayBuffer(), { "X-Filename": f.name });
    $("#import-list").innerHTML = `<div class="entry-list">${up.entries.map((x, i) => `<button class="entry" data-i="${i}"><b>${esc(x.agent)}</b> · ${esc(x.opening || "")}
      <div class="meta">${x.start ? new Date(x.start).toLocaleString() : ""} · ${x.minutes ?? "?"} min · ${x.prompts} prompts · ${x.events} events · ${esc(x.member)}</div></button>`).join("") || "No transcripts found."}</div>`;
    $$("#import-list .entry").forEach((b) => (b.onclick = async () => {
      if (S.events.length && !confirm("Replace this session's events with the imported log? Marks will be re-aligned by timestamp.")) return;
      const r = await api("POST", `/api/sessions/${S.id}/import`, { upload_id: up.upload_id, member: up.entries[+b.dataset.i].member });
      toast(`Imported ${r.events} events`);
      openSession(S.id);
    }));
  };
  $$("[data-show]").forEach((c) => (c.onchange = () => { show[c.dataset.show] = c.checked; renderTimeline(); }));
  $$("[data-goto]").forEach((l) => (l.onclick = () => select(l.dataset.goto, true)));
}

function visible(e) {
  if (e.role === "thinking" && !show.thinking) return false;
  if ((e.role === "tool_call" || e.role === "tool_result") && !show.tools && !marksOn(e.id).length && !candsOn(e.id).length) return false;
  if (show.signalsOnly && !(marksOn(e.id).length || candsOn(e.id).length || uptakeOn(e.id).length || traced(e.id).length)) return false;
  return true;
}

function renderTimeline() {
  const tl = $("#tl");
  const evs = S.events.filter(visible);
  tl.innerHTML = evs.map((e) => {
    const P = marksOn(e.id).map((m) => `<span class="d" style="--tc:${TYPE_COLOR[markType(m)] || "var(--text-3)"}" title="mark: ${esc(tLabel(markType(m)))}"></span>`).join("");
    const L = candsOn(e.id, "codebook").map((c) => `<span class="d cand ${c.mechanism}" style="--tc:${TYPE_COLOR[c.type]};opacity:${0.35 + 0.65 * c.confidence}" title="codebook: ${esc(tLabel(c.type))} · ${esc(c.mechanism)} · score ${c.score}"></span>`).join("");
    const J = candsOn(e.id, "judge").map((c) => `<span class="d judge ${c.mechanism}" style="--tc:${TYPE_COLOR[c.type]};opacity:${0.35 + 0.65 * c.confidence}" title="judge: ${esc(tLabel(c.type))} · ${esc(c.mechanism)} · ${Math.round(c.confidence * 100)}%">${icon("star")}</span>`).join("");
    const D = traced(e.id).length ? `<span class="d trace" title="used in final answer"></span>` : "";
    return `<div class="tl-row ${e.role} ${sel === e.id ? "sel" : ""}" data-id="${e.id}">
      <div class="lane">${P}</div><div class="lane">${L}</div><div class="lane">${J}</div><div class="lane">${D}</div>
      <div class="tl-text"><span class="r ${e.role}">${esc(roleLabel(e.role))}${e.tool ? ` · ${esc(e.tool)}` : ""}</span><span class="muted small">${e.id} · ${fmtTime(e.ts)}</span>
        <div class="body">${esc(e.text.slice(0, 600))}</div></div></div>`;
  }).join("") || `<p class="muted">No events${S.external ? " yet: import the participant's agent log" : ""}.</p>`;
  $$(".tl-row", tl).forEach((r) => (r.onclick = () => select(r.dataset.id)));
}

function select(id, scroll = false) {
  sel = id;
  $$(".tl-row").forEach((r) => r.classList.toggle("sel", r.dataset.id === id));
  renderInspector();
  if (scroll) {
    let row = $(`.tl-row[data-id="${id}"]`);
    if (!row) { show.tools = true; show.thinking = true; show.signalsOnly = false; render(); row = $(`.tl-row[data-id="${id}"]`); }
    row?.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

const LABELS = [...cfg.taxonomy.types.map((t) => [t.key, t.label]), ["persuasion", "Persuasion (not a spark)"], ["copying", "Copying (not a spark)"], ["not_spark", "No shift / not a spark"], ["unsure", "Unsure"]];
function codeForm(kind, target) {
  const cur = S.codes[`${kind}:${target}`] || {};
  return `<div class="code-form" data-kind="${kind}" data-target="${target}">
    <select><option value="">Researcher code…</option>${LABELS.map(([k, l]) => `<option value="${k}" ${cur.label === k ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
    <input type="text" placeholder="note" value="${esc(cur.notes || "")}" style="padding:5px 8px;font-size:13px">
    <button class="btn btn-sm">Save</button></div>`;
}

function renderInspector() {
  const box = $("#insp");
  if (!box) return;
  if (!sel) {
    box.innerHTML = `<p class="muted small">Click an event to inspect its flags, codebook and judge candidates and attributions, and to code it.</p>
      <div class="insp-block"><h3>How to read the lanes</h3>
      <div class="small"><b>P</b>: participant flags (colour = type they chose).<br><b>C</b>: codebook-parser candidates on the <i>trigger</i> event (opacity = score).<br><b>J</b>: LLM-judge candidates on the trigger event (opacity = confidence). Hollow = persuasion/copying.<br><b>D</b>: the event was traced into the final write-up.<br>Two items agree if their trigger/uptake events are within ±${S.analysis.tolerance_events} event.</div></div>`;
    return;
  }
  const e = S.events.find((x) => x.id === sel);
  const ms = marksOn(sel), cs = candsOn(sel), ups = uptakeOn(sel), tr = traced(sel);
  const links = S.analysis.links;
  box.innerHTML = `
    <div class="insp-block"><h3>${esc(roleLabel(e.role))}${e.tool ? ` · ${esc(e.tool)}` : ""} <span class="muted">${e.id} · ${fmtTime(e.ts)}</span>
      ${S.recording?.file && e.t ? `<a class="btn btn-sm" style="margin-left:auto" href="/review?id=${S.id}&mode=researcher&t=${Math.round(e.t - S.recording.started_t)}">${icon("play")}Watch</a>` : ""}</h3><div class="insp-text">${esc(e.text)}</div></div>
    ${ms.map((m) => {
      const r = m.review || {};
      const matched = links.filter((l) => l.mark === m.id).map((l) => l.candidate);
      return `<div class="insp-block" style="border-color:${TYPE_COLOR[markType(m)] || "var(--border)"}">
        <h3>${icon("bulb")} Participant mark <span class="muted">${fmtTime(m.created_at)}</span></h3>
        <div class="kv"><div>source</div><div>${esc(m.source)} · ${esc(m.anchor)}${m.aligned ? " · aligned by time" : ""}</div>
        <div>quick tag</div><div>${esc(m.quick_type ? tLabel(m.quick_type) : "none (bookmark)")}</div>
        ${m.quote ? `<div>selected</div><div>“${esc(m.quote)}”</div>` : ""}
        <div>review</div><div>${m.review ? `${(r.types || []).map((t) => esc(tLabel(t))).join(", ")}${r.primary ? ` (main: ${esc(tLabel(r.primary))})` : ""}` : "<i>not reviewed</i>"}</div>
        ${m.review ? `<div>before</div><div>${esc(r.before || "—")}</div><div>changed</div><div>${esc(r.after || "—")}</div>
        <div>uptake</div><div>${esc(r.uptake || "—")}</div><div>clicked</div><div>${esc(r.latency || "—")}</div><div>size</div><div>${r.intensity ?? "—"}/5</div>` : ""}
        <div>found by</div><div>${matched.length ? links.filter((l) => l.mark === m.id).map((l) => `${l.source} ${l.candidate}`).join(", ") : "neither codebook nor judge"}</div></div>
        ${codeForm("mark", m.id)}</div>`;
    }).join("")}
    ${cs.map((c) => `<div class="insp-block" style="border-style:dashed;border-color:${TYPE_COLOR[c.type]}">
      <h3>${icon(src(c) === "judge" ? "star" : "sparkle")} ${src(c) === "judge" ? "LLM judge" : "Codebook"} · ${esc(tLabel(c.type))} <span class="muted">${src(c) === "judge" ? `${Math.round(c.confidence * 100)}% confident` : `score ${c.score}`}</span></h3>
      <div class="kv"><div>mechanism</div><div>${esc(c.mechanism)}</div>
      ${src(c) === "judge" ? `<div>quotes</div><div>${c.quotes_verified ? `${icon("check")} found verbatim in the transcript` : `<span style="color:var(--danger)">not found verbatim, check carefully</span>`}</div>
        ${c.before || c.after ? `<div>before → after</div><div>${esc(c.before)} → ${esc(c.after)}</div>` : ""}` : ""}
      ${c.secondary_type && c.secondary_type !== "none" ? `<div>secondary</div><div>${esc(tLabel(c.secondary_type))}</div>` : ""}
      <div>trigger</div><div>“${esc(c.trigger_quote)}”</div>
      <div>uptake</div><div>${c.uptake_event_id ? `<span class="evlink" data-goto="${c.uptake_event_id}">${c.uptake_event_id}</span> ` : ""}“${esc(c.uptake_quote)}”</div>
      <div>why</div><div>${c.rules ? `<ul class="rules">${c.rules.map((r) => `<li><code>${esc(r.rule)}</code> +${r.weight}: ${esc(r.evidence)}</li>`).join("")}</ul>` : esc(c.rationale)}</div>
      <div>participant</div><div>${c.participant_response ? esc(c.participant_response.answer) : "not shown / not answered"}</div>
      <div>mark match</div><div>${links.filter((l) => l.candidate === c.id).map((l) => l.mark).join(", ") || "none"}</div></div>
      ${codeForm("candidate", c.id)}</div>`).join("")}
    ${ups.length ? `<div class="insp-block"><h3>Uptake evidence for</h3>${ups.map((c) => `<div class="small"><span class="evlink" data-goto="${c.trigger_event_id}">${c.trigger_event_id}</span> · ${esc(tLabel(c.type))} (${esc(c.mechanism)})</div>`).join("")}</div>` : ""}
    ${tr.length ? `<div class="insp-block"><h3>Shaped the final answer</h3>${tr.map((u) => `<div class="small" style="margin-bottom:6px"><b>${esc(u.status)}</b> · ${esc(u.mechanism)}: ${esc(u.text)}</div>`).join("")}</div>` : ""}
    ${!ms.length && !cs.length ? `<div class="insp-block"><h3>Add a researcher-identified spark</h3>${codeForm("event", e.id)}</div>` : ""}`;
  $$("[data-goto]", box).forEach((l) => (l.onclick = () => select(l.dataset.goto, true)));
  $$(".code-form", box).forEach((f) => ($("button", f).onclick = async () => {
    let name = coder;
    if (!name) { name = prompt("Your initials (saved for future codes):") || ""; try { localStorage.setItem("coder", name); } catch { /* ignore */ } }
    const res = await api("POST", `/api/sessions/${S.id}/code`, { kind: f.dataset.kind, target: f.dataset.target, label: $("select", f).value, notes: $("input", f).value, coder: name });
    S.codes[`${f.dataset.kind}:${f.dataset.target}`] = res;
    toast("Code saved");
  }));
}

await refreshList();
const initial = location.hash.slice(1) || sessions[0]?.id;
if (initial) openSession(initial);
window.addEventListener("hashchange", () => location.hash.length > 1 && openSession(location.hash.slice(1)));
setInterval(refreshList, 10000);
