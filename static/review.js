// Post-session review: the screen recording with the participant's flags on a timeline,
// a transcript synced to the video, and the annotation form for each flag.
import {
  api, icon, esc, $, $$, config, TYPE_COLOR, typeByKey, toast, fmtClock, annotationFormHTML, wireAnnotationForm,
} from "/static/common.js";

const params = new URLSearchParams(location.search);
const sid = params.get("id");
const researcher = params.get("mode") === "researcher";
const LEAD = 20; // seconds of lead-in when jumping to a flag
let cfg, S, video = null, t0 = 0, duration = 0, showParser = researcher, openId = null;

const off = (t) => (t == null ? null : t - t0);
const live = (m) => !m.deleted;
const flags = () => S.marks.filter(live).sort((a, b) => a.t - b.t);
const evById = (id) => S.events.find((e) => e.id === id);
const typeOf = (m) => m.review?.primary || m.quick_type || null;
const tLabel = (k) => typeByKey(cfg, k)?.label || (k === "other" ? "Something else" : null);
const agentName = () => (S.external ? "Agent" : S.condition === "human" ? "Partner" : "AI");

async function load() {
  S = await api("GET", `/api/sessions/${sid}`);
  const rec = S.recording || {};
  t0 = rec.started_t ?? S.events.find((e) => e.t)?.t ?? S.marks[0]?.t ?? 0;
  duration = rec.duration || Math.max(60, ...S.marks.map((m) => off(m.t) + 30), ...S.events.filter((e) => e.t).map((e) => off(e.t) + 30));
}

// ------------------------------------------------------------------ layout

function renderTop() {
  $("#brand").innerHTML = `${icon("bulb")}<span>Spark review</span>`;
  $("#title").textContent = `${S.participant_id} · ${S.prompt.title}`;
  const rec = S.recording;
  $("#meta").textContent = rec?.file ? `recording ${fmtClock(rec.duration)}` : rec?.status === "recording" ? "recording in progress" : "no recording";
  $("#actions").innerHTML = researcher
    ? `<button class="btn btn-sm" id="rerun">${icon("sparkle")}Re-run parser</button><a class="btn btn-sm" href="/api/sessions/${sid}/export.json">Export JSON</a><a class="btn btn-sm btn-ghost" href="/researcher#${sid}">Dashboard</a>`
    : S.phase === "review" ? `<button class="btn btn-primary btn-sm" id="finish">Finish review</button>` : "";
  $("#rerun") && ($("#rerun").onclick = async () => { await api("POST", `/api/sessions/${sid}/detect`); await load(); renderAll(); toast("Parser re-run"); });
  $("#finish") && ($("#finish").onclick = async () => {
    const todo = flags().filter((m) => !m.review).length;
    if (todo && !confirm(`${todo} flag${todo > 1 ? "s are" : " is"} not annotated yet. Finish anyway?`)) return;
    await api("POST", `/api/sessions/${sid}/advance`, { phase: "review" });
    await load(); renderAll();
    $("#intro").innerHTML = `<div class="banner" style="margin:0 0 14px">Thank you, your review is complete. Please let the researcher know.</div>`;
  });
}

function renderPlayer() {
  const rec = S.recording;
  if (rec?.file) {
    $("#player").innerHTML = `<video id="video" controls preload="metadata" src="/api/sessions/${sid}/recording"></video>`;
    video = $("#video");
    video.addEventListener("loadedmetadata", () => { if (isFinite(video.duration)) { duration = video.duration; renderTrack(); } });
    video.addEventListener("timeupdate", onTime);
    video.addEventListener("seeked", onTime);
    const jt = parseFloat(params.get("t"));
    if (!isNaN(jt)) video.addEventListener("loadedmetadata", () => (video.currentTime = Math.max(0, jt - LEAD)), { once: true });
  } else {
    video = null;
    $("#player").innerHTML = `<div class="player-empty">${icon("window", "big")}<div>${rec?.status === "recording"
      ? "The session is still being recorded. Flags appear here as they come in."
      : "No screen recording for this session yet. Flags and the transcript can still be reviewed."}</div></div>`;
  }
}

function seek(sec, play = true) {
  if (!video) return;
  video.currentTime = Math.max(0, Math.min(sec, (video.duration || duration) - 0.5));
  if (play) video.play().catch(() => {});
}
const now = () => (video ? video.currentTime : null);

// ------------------------------------------------------------------ timeline track

function visibleCandidates() {
  if (researcher) return S.candidates;
  // Participants see each unflagged moment once, whichever coder (codebook or judge) found it.
  const linked = new Set((S.analysis?.links || []).map((l) => l.candidate));
  const seen = new Set();
  return S.candidates
    .filter((c) => !linked.has(c.id) && c.mechanism === "spark" && (c.source === "judge" ? c.confidence >= 0.5 : c.rank === 1))
    .sort((a, b) => (b.source === "judge") - (a.source === "judge"))
    .filter((c) => !seen.has(c.trigger_event_id) && seen.add(c.trigger_event_id));
}

function renderTrack() {
  const pct = (sec) => `${Math.max(0, Math.min(100, (100 * sec) / (duration || 1)))}%`;
  const track = $("#track");
  $$(".pin,.pm,.tick", track).forEach((x) => x.remove());
  S.events.filter((e) => e.role === "user" && e.t).forEach((e) => {
    track.insertAdjacentHTML("beforeend", `<span class="tick" style="left:${pct(off(e.t))}" title="Prompt at ${fmtClock(off(e.t))}"></span>`);
  });
  if (showParser) {
    visibleCandidates().forEach((c) => {
      const e = evById(c.trigger_event_id);
      if (!e?.t) return;
      track.insertAdjacentHTML("beforeend", `<button class="pm ${researcher && c.source === "judge" ? "pm-judge" : ""}" data-cand="${c.id}" style="left:${pct(off(e.t))};--tc:${researcher ? TYPE_COLOR[c.type] : "var(--text-3)"}"
        title="Parser${researcher ? `: ${esc(tLabel(c.type))} (${c.score})` : ""} at ${fmtClock(off(e.t))}"></button>`);
    });
  }
  flags().forEach((m) => {
    const k = typeOf(m);
    track.insertAdjacentHTML("beforeend", `<button class="pin ${k ? "" : "bm"}" data-flag="${m.id}" style="left:${pct(off(m.t))};--tc:${TYPE_COLOR[k] || "var(--accent)"}"
      title="${fmtClock(off(m.t))} · ${esc(tLabel(k) || "Bookmarked")}">${icon(k ? typeByKey(cfg, k)?.icon || "sparkle" : "bookmark")}</button>`);
  });
  $$(".pin", track).forEach((p) => (p.onclick = (ev) => { ev.stopPropagation(); openFlag(p.dataset.flag, true); }));
  $$(".pm", track).forEach((p) => (p.onclick = (ev) => {
    ev.stopPropagation();
    const c = S.candidates.find((x) => x.id === p.dataset.cand);
    seek(off(evById(c.trigger_event_id).t) - 5);
    $(`[data-cand-card="${c.id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }));
  track.onclick = (ev) => {
    const r = track.getBoundingClientRect();
    seek(((ev.clientX - r.left) / r.width) * duration, false);
  };
  $("#legend").innerHTML = `<span><i class="lg pin-lg"></i>your flags</span><span><i class="lg tick-lg"></i>your prompts</span>${showParser ? `<span><i class="lg pm-lg"></i>found by parser</span>` : ""}
    <span class="muted grow" style="text-align:right">${fmtClock(0)} – ${fmtClock(duration)}</span>`;
  onTime();
}

// ------------------------------------------------------------------ transcript synced to video

function transcriptEvents() {
  const tools = $("#show-tools").checked;
  return S.events.filter((e) => ["user", "assistant", "partner"].includes(e.role) || (tools && ["tool_call", "tool_result"].includes(e.role)));
}

function renderTranscript() {
  const evs = transcriptEvents();
  const label = (e) => ({ user: "You", assistant: agentName(), partner: "Partner", tool_call: `Ran ${e.tool || "tool"}`, tool_result: "Tool output" }[e.role]);
  $("#transcript").innerHTML = evs.length ? evs.map((e) => `
    <div class="tr-ev ${e.role}" data-ev="${e.id}">
      <button class="tr-time" ${e.t && video ? "" : "disabled"}>${e.t ? fmtClock(off(e.t)) : ""}</button>
      <div><div class="r">${esc(label(e))}</div><div class="t">${esc(e.text.length > 900 ? e.text.slice(0, 900) + "…" : e.text)}</div></div>
    </div>`).join("")
    : `<p class="muted small">${S.external ? "No agent transcript yet. The participant attaches it at the end of the recording (or a researcher can import it from the dashboard)." : "No conversation yet."}</p>`;
  $$(".tr-ev", $("#transcript")).forEach((row) => ($(".tr-time", row).onclick = () => {
    const e = evById(row.dataset.ev);
    seek(off(e.t) - 2);
  }));
}

let lastActive = null;
function onTime() {
  const t = now() ?? 0;
  $("#playhead").style.left = `${Math.min(100, (100 * t) / (duration || 1))}%`;
  // transcript: highlight the last event at or before the playhead
  const evs = transcriptEvents().filter((e) => e.t);
  let active = null;
  for (const e of evs) { if (off(e.t) <= t + 0.5) active = e; else break; }
  if (active?.id !== lastActive) {
    $$(".tr-ev.active").forEach((x) => x.classList.remove("active"));
    const row = active && $(`.tr-ev[data-ev="${active.id}"]`);
    if (row) {
      row.classList.add("active");
      if ($("#follow").checked && video && !video.paused) {
        const box = $("#transcript");
        box.scrollTop = row.offsetTop - box.offsetTop - box.clientHeight / 3;
      }
    }
    lastActive = active?.id;
  }
  // flags near the playhead light up
  $$(".flag-card").forEach((c) => {
    const m = S.marks.find((x) => x.id === c.dataset.flag);
    const o = off(m.t);
    c.classList.toggle("live", video && t >= o - LEAD && t <= o + 5);
  });
  const add = $("#add-at");
  if (add) add.textContent = `Add a moment at ${fmtClock(t)}`;
}

// ------------------------------------------------------------------ flags

function contextAt(m) {
  const anchor = m.event_id ? evById(m.event_id) : null;
  const before = S.events.filter((e) => e.t && e.t <= m.t + 1);
  const agent = anchor && anchor.role !== "user" ? anchor : [...before].reverse().find((e) => ["assistant", "partner"].includes(e.role));
  const you = [...before].reverse().find((e) => e.role === "user" && (!agent || e.t <= agent.t));
  const cut = (s, n = 220) => esc(s.length > n ? s.slice(0, n) + "…" : s);
  if (!you && !agent) return "";
  return `<div class="flag-ctx">${you ? `<div><b>You:</b> ${cut(you.text)}</div>` : ""}${agent ? `<div><b>${agentName()}:</b> ${cut(agent.text)}</div>` : ""}</div>`;
}

function flagCardHTML(m, i) {
  const k = typeOf(m);
  const t = tLabel(k);
  const o = off(m.t);
  return `<div class="flag-card ${m.review ? "saved" : ""} ${openId === m.id ? "open" : ""}" data-flag="${m.id}">
    <div class="flag-head">
      <button class="time-chip" ${video ? "" : "disabled"} title="Watch from ${fmtClock(o - LEAD)}">${icon("play")}${fmtClock(o)}</button>
      <span class="type-chip ${k ? "" : "bm"}" style="--tc:${TYPE_COLOR[k] || "var(--accent)"}">${icon(k ? typeByKey(cfg, k)?.icon || "sparkle" : "bookmark")}${esc(t || "Bookmarked · explain later")}</span>
      <span class="grow"></span>
      <span class="status">${m.review ? `${icon("check")} Annotated` : `<span class="muted">Flag ${i + 1}</span>`}</span>
      <button class="icon-btn toggle" aria-label="Annotate">${icon("chev")}</button>
    </div>
    ${contextAt(m)}
    ${m.source && !["overlay", "hotkey"].includes(m.source) ? `<div class="small muted">${m.source === "review_add" ? "added during review" : m.source === "candidate_confirm" ? "confirmed from parser suggestion" : esc(m.source)}</div>` : ""}
    <div class="flag-form">${annotationFormHTML(cfg, m, { external: S.external })}</div>
  </div>`;
}

function renderFlags() {
  const list = flags();
  const done = list.filter((m) => m.review).length;
  $("#intro").innerHTML = researcher ? `<h2 style="margin:0 0 4px">Participant flags</h2><p class="small muted" style="margin-top:0">${list.length} flags · ${done} annotated</p>`
    : `<h2 style="margin:0 0 4px">Your flagged moments</h2>
      <p class="small muted" style="margin-top:0">${list.length ? `You flagged <b>${list.length}</b> moment${list.length > 1 ? "s" : ""} (${done} annotated). Press ▶ to rewatch the lead-up to each one, then tell us what happened.` : "You didn't flag any moments. You can scrub through the recording and add one if something stands out."}</p>`;
  if (!openId) openId = list.find((m) => !m.review)?.id || null;
  $("#flags").innerHTML = list.map(flagCardHTML).join("") +
    `<button class="btn btn-sm btn-accent" id="add-at" ${video ? "" : "disabled"} style="margin-top:6px">${icon("plus")}Add a moment</button>`;
  $$(".flag-card").forEach((card) => {
    const m = S.marks.find((x) => x.id === card.dataset.flag);
    $(".time-chip", card).onclick = () => { seek(off(m.t) - LEAD); openFlag(m.id); };
    $(".toggle", card).onclick = () => openFlag(openId === m.id ? null : m.id);
    wireAnnotationForm(card, m, {
      sid,
      onSaved: () => {
        const next = flags().find((x) => !x.review && x.id !== m.id);
        openId = next?.id || null;
        renderFlags(); renderTrack();
      },
      onRemoved: () => { openId = null; renderFlags(); renderTrack(); },
    });
  });
  $("#add-at").onclick = async () => {
    const m = await api("POST", `/api/sessions/${sid}/marks`, { t: t0 + now(), source: "review_add", anchor: "video" });
    S.marks.push(m);
    openId = m.id;
    renderFlags(); renderTrack();
  };
  onTime();
}

function openFlag(id, scroll = false) {
  openId = id;
  $$(".flag-card").forEach((c) => c.classList.toggle("open", c.dataset.flag === id));
  if (id && scroll) {
    const m = S.marks.find((x) => x.id === id);
    seek(off(m.t) - LEAD);
    $(`.flag-card[data-flag="${id}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// ------------------------------------------------------------------ parser results

function renderParser() {
  const box = $("#parser");
  const det = S.detection || {};
  if (!S.events.length) { box.innerHTML = ""; return; }
  if (!showParser) {
    box.innerHTML = `<div class="section-title">${icon("sparkle")}<h2 style="margin:0">Other moments</h2></div>
      <p class="small muted">When you've gone through your own flags, look at a few moments the conversation parser picked out.</p>
      <button class="btn btn-sm" id="show-parser">Show other moments</button>`;
    $("#show-parser").onclick = () => { showParser = true; renderParser(); renderTrack(); };
    return;
  }
  const list = visibleCandidates().filter((c) => researcher || !c.participant_response).slice(0, researcher ? 200 : 8);
  const head = researcher
    ? `<div class="section-title">${icon("sparkle")}<h2 style="margin:0">Parser candidates</h2></div>
       <p class="small muted" style="margin-top:0">◆ codebook v${esc(det.codebook_version || "?")}: ${S.analysis?.codebook || 0} · ★ LLM judge: ${S.judge?.status === "done" ? S.analysis?.judge || 0 : "not run"} · ${S.analysis?.matched_marks || 0}/${S.analysis?.marks || 0} flags matched</p>`
    : `<div class="section-title">${icon("sparkle")}<h2 style="margin:0">Other moments</h2></div>
       <p class="small muted" style="margin-top:0">Did any of these shift your thinking? Only say yes if it really did.</p>`;
  box.innerHTML = head + (list.length ? list.map((c) => {
    const tr = evById(c.trigger_event_id), up = evById(c.uptake_event_id);
    const o = tr?.t ? off(tr.t) : null;
    const linked = (S.analysis?.links || []).filter((l) => l.candidate === c.id).length;
    return `<div class="cand-card" data-cand-card="${c.id}">
      <div class="flag-head">
        <button class="time-chip" ${video && o != null ? "" : "disabled"}>${icon("play")}${fmtClock(o)}</button>
        ${researcher ? `<span class="tag">${c.source === "judge" ? "★ LLM judge" : "◆ codebook"}</span><span class="type-chip" style="--tc:${TYPE_COLOR[c.type]}">${icon(typeByKey(cfg, c.type)?.icon || "sparkle")}${esc(tLabel(c.type))}</span>
          <span class="tag">${esc(c.mechanism)}</span><span class="tag">${c.source === "judge" ? `${Math.round(c.confidence * 100)}%${c.quotes_verified ? "" : " · quote unverified"}` : `score ${c.score}`}</span>${linked ? `<span class="tag">matches a flag</span>` : ""}` : ""}
      </div>
      ${tr ? `<div class="flag-ctx"><div><b>${tr.role === "user" ? "You" : agentName()}:</b> ${esc(c.trigger_quote || tr.text.slice(0, 200))}</div>
        ${up && up.id !== tr.id ? `<div><b>Then you:</b> ${esc(c.uptake_quote || up.text.slice(0, 200))}</div>` : ""}</div>` : ""}
      ${researcher ? `<details class="small"><summary>Why</summary>${c.source === "judge" ? `<p style="margin:6px 0">${esc(c.rationale)}</p>${c.before ? `<p class="muted" style="margin:0">Before: ${esc(c.before)}<br>After: ${esc(c.after)}</p>` : ""}` : `<ul class="rules">${(c.rules || []).map((r) => `<li><code>${esc(r.rule)}</code> +${r.weight} ${esc(r.evidence)}</li>`).join("")}</ul>`}</details>
        ${c.participant_response ? `<div class="small muted">Participant said: ${esc(c.participant_response.answer)}</div>` : ""}`
      : `<div class="row" style="margin-top:8px"><span class="small grow">Did this shift your thinking?</span>
        <button class="btn btn-sm" data-ans="yes">Yes</button><button class="btn btn-sm" data-ans="no">No</button><button class="btn btn-sm btn-ghost" data-ans="unsure">Not sure</button></div>`}
    </div>`;
  }).join("") : `<p class="small muted">${researcher ? "No candidates. Run the parser after the transcript is attached." : "Nothing else to look at."}</p>`);
  $$("[data-cand-card]", box).forEach((el) => {
    const c = S.candidates.find((x) => x.id === el.dataset.candCard);
    const tr = evById(c.trigger_event_id);
    $(".time-chip", el).onclick = () => tr?.t && seek(off(tr.t) - 5);
    $$("[data-ans]", el).forEach((b) => (b.onclick = async () => {
      const answer = b.dataset.ans;
      let mark_id = null;
      if (answer === "yes") {
        const m = await api("POST", `/api/sessions/${sid}/marks`, { event_id: c.trigger_event_id, t: tr?.t, anchor: "candidate", source: "candidate_confirm" });
        S.marks.push(m);
        mark_id = m.id;
        openId = m.id;
      }
      c.participant_response = { answer, mark_id };
      await api("PATCH", `/api/sessions/${sid}/candidates/${c.id}`, { participant_response: { answer, mark_id } });
      renderFlags(); renderTrack(); renderParser();
    }));
  });
}

function renderAll() {
  renderTop();
  renderTrack();
  renderTranscript();
  renderFlags();
  renderParser();
}

// ------------------------------------------------------------------ boot

cfg = await config();
await load();
renderPlayer();
renderAll();
$("#show-tools").onchange = () => { renderTranscript(); lastActive = null; onTime(); };
// While the overlay is still recording, pick up new flags as they arrive.
setInterval(async () => {
  if (S.phase !== "chat" && S.recording?.file && S.detection?.status !== "running") return;
  const before = S.marks.length, hadFile = !!S.recording?.file, hadEvents = S.events.length, det = S.detection?.status;
  await load();
  if (!hadFile && S.recording?.file) { renderPlayer(); return renderAll(); }
  if (S.marks.length !== before || S.events.length !== hadEvents || S.detection?.status !== det) renderAll();
}, 4000);
