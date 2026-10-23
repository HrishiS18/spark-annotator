// Spark Recorder: records the participant's screen (video only) in the browser and
// floats an always-on-top Flag button over whatever app they use for their agent.
//
//   screen capture   navigator.mediaDevices.getDisplayMedia (all desktop browsers)
//   recording        MediaRecorder, uploaded to the server in 4 s chunks as it records
//   floating button  Document Picture-in-Picture (Chrome / Edge 116+ on Windows, macOS,
//                    Linux, ChromeOS); other browsers get a small popup window instead
import { api, icon, esc, $, $$, toast, fmtClock } from "/static/common.js";
import { BookmarkOutbox } from "/static/bookmarks.js";

const params = new URLSearchParams(location.search);
let S = null;
let stream = null, recorder = null, mime = "", t0 = null, seq = 0;
let uploadChain = Promise.resolve(), uploadedBytes = 0, uploadErrors = 0;
let flags = [], lastFlag = null, pip = null, sessionStart = null, finishing = false;
let bookmarkOutbox = null;

const support = {
  capture: !!navigator.mediaDevices?.getDisplayMedia,
  recorder: typeof MediaRecorder !== "undefined",
  pip: "documentPictureInPicture" in window,
  dirPicker: "showDirectoryPicker" in window,
};
const MIMES = ["video/mp4;codecs=avc1.640028", "video/mp4;codecs=avc1", "video/mp4",
  "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];
mime = support.recorder ? MIMES.find((m) => MediaRecorder.isTypeSupported(m)) || "" : "";

const live = () => flags.filter((f) => !f.deleted);
const elapsed = () => (t0 ? Date.now() / 1000 - t0 : 0);
function setState(text, cls = "") { const p = $("#state"); p.textContent = text; p.className = `status-pill ${cls}`; }

// ------------------------------------------------------------------ setup

function setupView() {
  setState("Not recording");
  const rows = [
    ["Screen capture", support.capture, "Use a desktop browser (Chrome, Edge, Firefox or Safari)."],
    ["Video recording", support.recorder && !!mime, "This browser can't record video."],
    ["Floating Flag button over other apps", support.pip, "Needs Chrome or Edge. Other browsers get a small separate window that does not stay on top."],
  ];
  const resume = S?.recording?.status === "recording";
  $("#view").innerHTML = `
    <p class="muted" style="margin-top:4px">While you work with your AI agent, this page records your screen (video only, no sound) and gives you a small <b>Flag</b> button that floats on top of your other windows. Press it when something feels worth revisiting; you can explain it after the session.</p>
    <div class="card">
      <h2>Before you start</h2>
      ${rows.map(([k, ok, why]) => `<div class="check-row ${ok ? "ok" : "bad"}">${icon(ok ? "check" : "x")}<b>${k}</b>${ok ? "" : `<span class="muted small">${why}</span>`}</div>`).join("")}
      <form id="setup" style="margin-top:16px">
        <label class="field"><span>Participant ID</span><input type="text" name="participant_id" placeholder="e.g. P014" value="${esc(S?.participant_id && S.participant_id !== "unassigned" ? S.participant_id : "")}"></label>
        <label class="field"><span>Session code <span class="muted">(optional: if the researcher gave you one)</span></span><input type="text" name="code" value="${esc(S?.id || "")}" placeholder="s_1a2b3c4d"></label>
        <label class="field"><span>What will you work on? <span class="muted">(optional)</span></span><input type="text" name="task" value=""></label>
        ${resume ? `<div class="banner" style="margin:0 0 12px">This session's recording was interrupted. You can finish it with what was saved, or start recording again (the earlier recording is replaced).</div>` : ""}
        <div class="row">
          <button class="btn btn-primary" type="submit" ${support.capture && mime ? "" : "disabled"}>${icon("window")}Share my screen &amp; start recording</button>
          ${resume ? `<button class="btn" type="button" id="recover">Finish with what was saved</button>` : ""}
        </div>
        <p class="small muted" style="margin-bottom:0">Your browser will ask what to share: choose <b>Entire screen</b>. Nothing is recorded until you confirm.</p>
      </form>
    </div>`;
  $("#setup").onsubmit = (e) => { e.preventDefault(); start(Object.fromEntries(new FormData(e.target))); };
  $("#recover") && ($("#recover").onclick = async () => {
    await api("POST", `/api/sessions/${S.id}/recording/finish`, { duration: S.recording.chunks * 4 });
    await api("POST", `/api/sessions/${S.id}/advance`, { phase: "chat" }).catch(() => {});
    location.href = `/review?id=${S.id}`;
  });
}

async function ensureSession(form) {
  const code = (form.code || "").trim();
  if (code) return api("GET", `/api/sessions/${code}`);
  if (!form.participant_id.trim()) throw new Error("Enter your participant ID (or a session code).");
  return api("POST", "/api/sessions", { study: "agentic", participant_id: form.participant_id.trim(), task: form.task });
}

async function start(form) {
  try {
    S = await ensureSession(form);
    if (S.phase !== "chat") throw new Error("This session has already finished recording.");
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "monitor", frameRate: { ideal: 15, max: 15 } },
      audio: false,
      selfBrowserSurface: "exclude",
      surfaceSwitching: "exclude",
      monitorTypeSurfaces: "include",
    });
  } catch (err) {
    if (err.name === "NotAllowedError") return toast("Screen sharing was cancelled. Press the button again when you're ready.");
    return toast(err.message);
  }
  const track = stream.getVideoTracks()[0];
  const st = track.getSettings();
  track.addEventListener("ended", () => finish("Screen sharing was stopped."));
  recorder = new MediaRecorder(stream, { mimeType: mime, videoBitsPerSecond: 2_500_000 });
  recorder.ondataavailable = (e) => { if (e.data && e.data.size) queueChunk(e.data); };
  recorder.onstart = async () => {
    t0 = Date.now() / 1000;
    sessionStart = new Date();
    await api("POST", `/api/sessions/${S.id}/recording/start`, {
      started_t: t0, mime, width: st.width, height: st.height, fps: 15, surface: st.displaySurface || "unknown",
      browser: navigator.userAgent,
    });
  };
  flags = (S.marks || []).filter((m) => !m.deleted).map((m) => ({ id: m.id, t: m.t, type: m.quick_type, synced: true }));
  bookmarkOutbox = new BookmarkOutbox(S.id, (pending, mark) => {
    if (mark) {
      const f = flags.find(x => x.id === mark.id);
      if (f) Object.assign(f, { synced: true, client_version: mark.client_version });
    }
    for (const row of pending) {
      let f = flags.find(x => x.id === row.body.id);
      if (!f) { f = { id: row.body.id, t: row.body.t, type: row.body.quick_type }; flags.push(f); }
      Object.assign(f, { synced: false, client_version: row.body.client_version, deleted: row.body.deleted, type: row.body.quick_type });
    }
    if ($("#view")) paintStatus();
  });
  recorder.start(4000);
  recordingView(st.displaySurface);
  addEventListener("beforeunload", warnLeave);
}

const warnLeave = (e) => { if (recorder && recorder.state !== "inactive" || bookmarkOutbox?.pending().length) { e.preventDefault(); e.returnValue = ""; } };

// ------------------------------------------------------------------ chunk upload

function queueChunk(blob) {
  const n = seq++;
  uploadChain = uploadChain.then(() => sendChunk(blob, n));
}

async function sendChunk(blob, n) {
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      const r = await fetch(`/api/sessions/${S.id}/recording/chunk?seq=${n}`, { method: "POST", body: blob });
      if (r.ok) { uploadedBytes += blob.size; uploadErrors = 0; paintStatus(); return; }
      if (r.status === 409) { const j = await r.json(); if (j.next > n) return; }
    } catch { /* network blip: retry */ }
    uploadErrors++;
    paintStatus();
    await new Promise((res) => setTimeout(res, 1000 * 2 ** attempt));
  }
  toast("Part of the recording could not be uploaded. Please tell the researcher.");
}

// ------------------------------------------------------------------ recording view (main tab)

function recordingView(surface) {
  setState("Recording", "rec");
  $("#view").innerHTML = `
    ${surface && surface !== "monitor" ? `<div class="banner">You're sharing a ${esc(surface)}, not your entire screen. Anything outside it won't be recorded. If that's not intended, end the session and start again choosing <b>Entire screen</b>.</div>` : ""}
    <div class="card rec-card">
      <div class="row"><span class="rec-dot"></span><b>Recording your screen</b><span class="muted" id="rec-time">0:00</span><span class="grow"></span><span class="small muted" id="rec-up"></span></div>
      <div class="pip-callout" id="pip-callout">
        <p style="margin:0 0 10px"><b>Next: pop out your Flag button.</b> It floats on top of your agent so you can flag moments without coming back to this tab.</p>
        <button class="btn btn-primary" id="pop">${icon("window")}Pop out the Flag button</button>
        ${support.pip ? "" : `<p class="small muted" style="margin-bottom:0">Your browser can't keep it on top of other apps; it opens as a small window instead. Chrome or Edge works best.</p>`}
      </div>
      <p class="small muted">Keep this tab open while you work. You can also flag from here:</p>
      <div class="row"><button class="mark-fab-inline" id="flag-here">${icon("bulb")}Flag <span class="count" id="count-here">0</span></button>
        <span class="grow"></span><button class="btn" id="end">${icon("stop")}End session</button></div>
      <div id="flag-list" class="flag-list"></div>
    </div>`;
  $("#pop").onclick = () => openPip();
  $("#flag-here").onclick = () => flag(null);
  $("#end").onclick = () => { if (confirm("End the session? This stops the screen recording.")) finish(); };
  setInterval(paintStatus, 1000);
  paintStatus();
}

function paintStatus() {
  const t = fmtClock(elapsed());
  $("#rec-time") && ($("#rec-time").textContent = t);
  $("#rec-up") && ($("#rec-up").textContent = `${(uploadedBytes / 1e6).toFixed(1)} MB saved${uploadErrors ? " · retrying upload…" : ""}`);
  $("#count-here") && ($("#count-here").textContent = live().length);
  pip && $(".pip-time", pip.document) && ($(".pip-time", pip.document).textContent = t);
  const list = $("#flag-list");
  if (list) list.innerHTML = live().slice().reverse().map((f) =>
    `<div class="small"><code>${fmtClock(f.t - t0)}</code> · bookmarked${f.synced ? "" : " <span class='muted'>(saving…)</span>"}</div>`).join("");
}

// ------------------------------------------------------------------ floating Flag button (Document PiP)

const PIP_SMALL = [330, 86];

async function openPip(asPopup = false) {
  if (pip && !pip.closed) return pip.focus();
  const floating = support.pip && !asPopup;
  try {
    if (floating) {
      const req = documentPictureInPicture.requestWindow({ width: PIP_SMALL[0], height: PIP_SMALL[1] });
      // Some embedded / locked-down browsers never open the window; offer a plain popup instead.
      const timer = setTimeout(() => { if (!pip) showPopupFallback(); }, 4000);
      pip = await req.finally(() => clearTimeout(timer));
    } else {
      pip = window.open("", "spark-flag", `width=${PIP_SMALL[0]},height=${PIP_SMALL[1]}`);
    }
  } catch (err) {
    pip = null;
    if (floating) showPopupFallback(err.message);
    else toast(err.message);
    return;
  }
  if (!pip) return toast("Your browser blocked the window. Allow pop-ups for this site and try again.");
  const d = pip.document;
  d.title = "Spark Flag";
  d.head.innerHTML = `<meta charset="utf-8"><link rel="stylesheet" href="${location.origin}/static/styles.css">`;
  d.body.className = "pip-body";
  d.body.innerHTML = `
    <div class="pip-bar">
      <span class="rec-dot" title="Recording your screen"></span><span class="pip-time">${fmtClock(elapsed())}</span>
      <button class="pip-flag" title="Flag this moment (F)">${icon("bulb")}Flag <span class="count">${live().length}</span></button>
      <button class="icon-btn pip-undo" title="Undo last flag" aria-label="Undo last flag">${icon("x")}</button>
      <button class="icon-btn pip-end" title="End session">${icon("stop")}</button>
    </div>`;
  $(".pip-flag", d).onclick = () => flag(null);
  $(".pip-end", d).onclick = () => { if (pip.confirm("End the session? This stops the screen recording.")) finish(); };
  $(".pip-undo", d).onclick = undo;
  d.addEventListener("keydown", (e) => {
    if (e.key === "f" || e.key === "F") flag(null);
    else if (e.key === "u" || e.key === "U") undo();
  });
  pip.setInterval(() => { const el = $(".pip-time", d); if (el) el.textContent = fmtClock(elapsed()); }, 1000);
  pip.addEventListener("pagehide", () => {
    pip = null;
    $("#pip-callout") && ($("#pip-callout").innerHTML = `<p style="margin:0 0 10px">The Flag button was closed.</p><button class="btn btn-primary" id="pop">${icon("window")}Pop it out again</button>`);
    $("#pop") && ($("#pop").onclick = () => openPip());
  });
  $("#pip-callout").innerHTML = `<p style="margin:0" class="small">${icon("check")} The Flag button is ${floating ? "floating on top of your other windows" : "open in its own window"}. Drag it wherever it's least in the way.</p>`;
}

function showPopupFallback(reason) {
  const box = $("#pip-callout");
  if (!box) return;
  if (reason) console.warn("Document Picture-in-Picture unavailable:", reason);
  box.innerHTML = `<p style="margin:0 0 10px">The floating button didn't open in this browser. You can use a small separate window instead (it won't stay on top of other apps), or switch to Chrome or Edge.</p>
    <button class="btn btn-primary" id="popup">${icon("window")}Open as a small window</button>`;
  $("#popup").onclick = () => openPip(true);
}

function paintPipCount() { pip && $(".pip-flag .count", pip.document) && ($(".pip-flag .count", pip.document).textContent = live().length); }

// ------------------------------------------------------------------ flags

const rid = () => "m_" + [...crypto.getRandomValues(new Uint8Array(6))].map((b) => b.toString(16).padStart(2, "0")).join("");

function flag() {
  if (!recorder || recorder.state === "inactive") return;
  const f = { id: rid(), t: Date.now() / 1000, synced: false };
  flags.push(f);
  lastFlag = f;
  bookmarkOutbox.create({ id: f.id, t: f.t, source: "overlay", anchor: "timestamp",
                         phase_at_click: "chat", exposure: { surface: "external" } });
  paintPipCount();
  paintStatus();
  if (!pip) toast(`Flagged at ${fmtClock(f.t - t0)}`);
}

function undo() {
  if (!lastFlag || lastFlag.deleted) return;
  lastFlag.deleted = true;
  lastFlag.synced = false;
  syncFlag(lastFlag);
  paintPipCount();
  paintStatus();
}

async function syncFlag(f) {
  bookmarkOutbox.update(f.id, { deleted: !!f.deleted },
    { id: f.id, t: f.t, source: "overlay", anchor: "timestamp", phase_at_click: "chat", client_version: f.client_version || 1,
      exposure: { surface: "external" } });
  await bookmarkOutbox.flush();
  paintStatus();
}

// ------------------------------------------------------------------ finish

async function finish(reason) {
  if (finishing) return;
  finishing = true;
  setState("Saving…");
  if (pip) { try { pip.close(); } catch { /* ignore */ } pip = null; }
  const duration = elapsed();
  if (recorder && recorder.state !== "inactive") {
    await new Promise((res) => { recorder.addEventListener("stop", res, { once: true }); recorder.stop(); });
  }
  stream?.getTracks().forEach((t) => t.stop());
  $("#view").innerHTML = `<div class="card"><h2>Saving your recording…</h2><p class="muted" id="save-msg">${reason ? esc(reason) + " " : ""}Uploading the last part.</p></div>`;
  await uploadChain;
  for (const f of flags.filter((x) => !x.synced)) await syncFlag(f);
  if (!await bookmarkOutbox.flush()) {
    finishing = false;
    $("#save-msg").textContent = "Bookmarks are stored on this device but not synced yet. Reconnect, then retry ending the session.";
    const retry = document.createElement("button"); retry.className = "btn"; retry.textContent = "Retry save";
    retry.onclick = () => finish(reason); $("#save-msg").after(retry); return;
  }
  try {
    await api("POST", `/api/sessions/${S.id}/recording/finish`, { duration });
  } catch (err) {
    $("#save-msg").textContent = `Could not finalise the recording: ${err.message}. Please tell the researcher.`;
    return;
  }
  removeEventListener("beforeunload", warnLeave);
  transcriptView();
}

// ------------------------------------------------------------------ attach the agent transcript

function transcriptView() {
  setState("Recording saved", "live");
  const os = /Win/.test(navigator.platform) ? "win" : /Mac/.test(navigator.platform) ? "mac" : "linux";
  const home = os === "win" ? "C:\\Users\\<you>" : "~";
  const sep = os === "win" ? "\\" : "/";
  $("#view").innerHTML = `
    <div class="card">
      <h2>${icon("check")} Recording saved · ${live().length} flag${live().length === 1 ? "" : "s"}</h2>
      <p>One last step: attach the conversation log from your coding agent, so your flags can be matched to exactly what it said. Only the session(s) you pick are uploaded.</p>
      ${support.dirPicker ? `<button class="btn btn-primary" id="scan">${icon("upload")}Find my agent sessions</button>
        <p class="small muted">Choose your <code>.codex</code> or <code>.claude</code> folder in your home folder (<code>${home}${sep}.codex</code>, <code>${home}${sep}.claude</code>). ${os === "mac" ? "In the folder picker press <kbd>⌘</kbd><kbd>⇧</kbd><kbd>.</kbd> to show hidden folders." : os === "win" ? "Paste the path into the address bar of the folder picker." : "Press <kbd>Ctrl</kbd>+<kbd>H</kbd> to show hidden folders."}</p>` : ""}
      <label class="btn ${support.dirPicker ? "" : "btn-primary"}">${icon("upload")}Choose log files…<input type="file" id="files" multiple accept=".jsonl,.json,.zip" hidden></label>
      <p class="small muted">Codex: <code>${home}${sep}.codex${sep}sessions${sep}…${sep}rollout-….jsonl</code> · Claude Code: <code>${home}${sep}.claude${sep}projects${sep}…${sep}&lt;session&gt;.jsonl</code> · or the zip from the agent-log exporter.</p>
      <div id="found" class="entry-list"></div>
      <div class="row" style="margin-top:16px"><span class="grow"></span>
        <button class="btn btn-ghost" id="skip">Skip (I used a web chat, e.g. ChatGPT)</button></div>
    </div>`;
  $("#skip").onclick = () => done();
  $("#files").onchange = (e) => attach([...e.target.files]);
  $("#scan") && ($("#scan").onclick = scan);
}

async function scan() {
  let dir;
  try { dir = await window.showDirectoryPicker({ id: "agent-logs", mode: "read" }); } catch { return; }
  const since = sessionStart.getTime() - 60_000;
  const found = [];
  async function walk(h, depth, path) {
    if (depth > 6) return;
    for await (const [name, child] of h.entries()) {
      if (child.kind === "directory" && name !== "subagents") await walk(child, depth + 1, `${path}/${name}`);
      else if (child.kind === "file" && name.endsWith(".jsonl")) {
        const file = await child.getFile();
        if (file.lastModified >= since) found.push({ file, path: `${path}/${name}` });
      }
    }
  }
  $("#found").innerHTML = `<p class="muted small">Looking for sessions active since you started…</p>`;
  await walk(dir, 0, dir.name);
  if (!found.length) { $("#found").innerHTML = `<p class="small">No agent sessions in <code>${esc(dir.name)}</code> were active during your recording. Try the other folder, or choose files manually.</p>`; return; }
  $("#found").innerHTML = `<p class="small muted">Sessions active during your recording (untick any you don't want to share):</p>` +
    found.map((x, i) => `<label class="entry"><input type="checkbox" data-i="${i}" checked> <b>${esc(x.path)}</b>
      <div class="meta">last updated ${new Date(x.file.lastModified).toLocaleTimeString()} · ${(x.file.size / 1024).toFixed(0)} KB</div></label>`).join("") +
    `<button class="btn btn-primary" id="send-found" style="margin-top:8px">Attach selected &amp; continue</button>`;
  $("#send-found").onclick = () => attach($$("#found input:checked").map((c) => found[+c.dataset.i].file));
}

async function attach(files) {
  if (!files.length) return;
  $("#found").innerHTML = `<p class="muted small">Attaching ${files.length} file${files.length > 1 ? "s" : ""}…</p>`;
  const start = sessionStart.getTime() - 600_000, end = Date.now() + 60_000;
  let first = true;
  try {
    for (const file of files) {
      const up = await api("POST", "/api/upload", file, { "X-Filename": file.name });
      let members = up.entries.map((x) => x.member);
      if (up.entries.length > 1) { // exporter zip: keep sessions that overlap this recording
        members = up.entries.filter((x) => x.start && Date.parse(x.end || x.start) >= start && Date.parse(x.start) <= end).map((x) => x.member);
      }
      if (!members.length) continue;
      await api("POST", `/api/sessions/${S.id}/import`, { upload_id: up.upload_id, members, append: !first });
      first = false;
    }
  } catch (err) {
    $("#found").innerHTML = `<p style="color:var(--danger)">${esc(err.message)}</p>`;
    return;
  }
  if (first) { $("#found").innerHTML = `<p class="small">Those files didn't contain a session from this recording.</p>`; return; }
  done();
}

async function done() {
  await api("POST", `/api/sessions/${S.id}/advance`, { phase: "chat" }).catch(() => {});
  location.href = `/review?id=${S.id}`;
}

// ------------------------------------------------------------------ boot

$("#logo").innerHTML = icon("bulb");
if (params.get("session")) S = await api("GET", `/api/sessions/${params.get("session")}`).catch(() => null);
setupView();
