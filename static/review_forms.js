import { api, esc, $, $$ } from "/static/common.js";

const controllers = new Set();
const readLocal = key => { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch { return null; } };
const writeLocal = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; } };
const removeLocal = key => { try { localStorage.removeItem(key); } catch {} };
// Stable on reload, different in independently opened tabs. Per-tab draft copies
// keep a competing tab's local edits recoverable even before server sync.
const tabId = (() => {
  try { const id = sessionStorage.getItem('spark-editor-tab') || crypto.randomUUID(); sessionStorage.setItem('spark-editor-tab',id); return id; }
  catch { return crypto.randomUUID(); }
})();

export function annotationFormHTML(cfg, m, events) {
  const draft = m.review_draft || m.review || {};
  const select = (field, label, rows) => `<label class="field"><span>${label}</span><select data-f="${field}">
    <option value="">Optional / unsure</option>${rows.map(([key, title]) => `<option value="${key}" ${String(draft[field]) === String(key) ? "selected" : ""}>${esc(title)}</option>`).join("")}</select></label>`;
  const options = `<option value="">Timestamp only / while composing</option>` + events.filter(e => e.role !== "thinking").map((e, i) =>
    `<option value="${e.id}">${i + 1}. ${esc(e.role)} — ${esc(e.text.slice(0, 65))}</option>`).join("");
  return `<div class="annot">
    <div class="qlabel">Did anything about this moment change how you thought about the task?</div>
    <div class="chips" data-group="decision">
      <button class="chip" data-decision="confirmed">Yes</button><button class="chip" data-decision="unsure">Unsure</button>
      <button class="chip" data-decision="rejected">No / just a bookmark</button>
    </div>
    <label class="field insight-field"><span>What changed, if anything? <span class="muted">Briefly, in your own words.</span></span>
      <textarea data-f="after" maxlength="6000" placeholder="I noticed… / I reconsidered…">${esc(draft.after || "")}</textarea></label>
    <div class="passage-picker"><div class="qlabel">Which passage does this relate to? <span class="muted">Optional; can include your own message. Select exact text in the transcript to highlight it.</span></div>
      <div class="two"><label class="field"><span>From</span><select data-start>${options}</select></label>
      <label class="field"><span>Through</span><select data-end>${options}</select></label></div>
      <div class="quote-context" data-quote-context></div>
      <button class="btn btn-sm btn-ghost hidden" data-clear-quote>Clear highlighted text</button>
      <div class="passage-context" data-context></div>
    </div>
    <details class="optional-review"><summary>Optional follow-up details</summary>
      <label class="field"><span>Before this moment, what were you thinking?</span><textarea data-f="before" maxlength="6000">${esc(draft.before || "")}</textarea></label>
      ${select("perceived_value", "How valuable did it feel at the time?", [[1,"1 — little value"],[2,"2"],[3,"3"],[4,"4"],[5,"5 — very valuable"]])}
      ${select("uptake", "Did you use the idea?", [["used","I used or built on it"],["not_used","I did not use it"],["unclear","Not sure"]])}
      ${select("outcome", "Did it seem to help later in the session?", [["helped","It seemed to help"],["did_not_help","It did not seem to help"],["not_yet_known","Too early to tell"],["unclear","Not sure"]])}
      ${select("latency", "When did it click?", [["immediate","At the time"],["delayed","Later in the conversation"],["review","Only now, during review"],["unclear","Not sure"]])}
      ${select("intensity", "How much did it change your thinking?", [[1,"1 — small shift"],[2,"2"],[3,"3"],[4,"4"],[5,"5 — large shift"]])}
    </details>
    <div class="row review-save-row"><span class="small grow" data-save-state aria-live="polite">Drafts save automatically</span>
      <button class="btn btn-primary btn-sm" data-save>Confirm review</button><button class="btn btn-sm hidden" data-retry>Retry save</button>
      <button class="btn btn-sm hidden" data-replace>Replace saved version with my draft</button></div>
  </div>`;
}

export function wireAnnotationForm(root, m, { sid, events, locked = false, onSaved = () => {}, onChanged = () => {} }) {
  const sharedKey = `spark-review-draft:${sid}:${m.id}`, key = sharedKey + ':' + tabId;
  const ownLocal = readLocal(key), local = ownLocal || readLocal(sharedKey);
  let version = m.review_version || 0;
  let state = structuredClone((local?.dirty || local?.baseVersion === version ? local?.state : null) || m.review_draft || m.review || { types: m.quick_type ? [m.quick_type] : [] });
  let editorId = ownLocal?.editorId || tabId;
  let seq = local?.seq || 0, timer, operation = null, blocked = false, dirty = !!local?.dirty;
  if (local?.dirty && m.review?.editor_id === editorId && m.review.editor_seq >= seq && !m.review_dirty) {
    state = structuredClone(m.review); dirty = false; removeLocal(key);
    if (readLocal(sharedKey)?.editorId === editorId) removeLocal(sharedKey);
  } else if (local?.dirty && (!ownLocal || local.baseVersion !== version && m.review_draft?.editor_id !== editorId)) blocked = true;
  let start = local?.start ?? m.episode?.start_event_id ?? m.event_id ?? "";
  let end = local?.end ?? m.episode?.end_event_id ?? m.event_id ?? "";
  let quoteSelection = local?.quoteSelection ?? m.quote_selection ?? null;
  let quoteDirty = !!local?.quoteDirty;
  const paint = () => {
    $$('[data-decision]', root).forEach(b => b.classList.toggle("on", b.dataset.decision === state.decision));
  };
  const status = message => { $('[data-save-state]', root).textContent = message; };
  const remember = () => {
    const value = { state, editorId, seq, baseVersion: version, start, end, quoteSelection, quoteDirty, dirty };
    const ok = writeLocal(key, value);
    writeLocal(sharedKey, value);
    if (!ok) status("Device storage unavailable — keep this tab open until saved");
    return ok;
  };
  const clearLocal = () => { removeLocal(key); if (readLocal(sharedKey)?.editorId === editorId) removeLocal(sharedKey); };
  const context = () => {
    const a = events.findIndex(e => e.id === start), b = events.findIndex(e => e.id === end);
    const selected = a >= 0 && b >= a ? events.slice(a, b + 1) : [];
    $('[data-context]', root).innerHTML = selected.map(e => `<details ${selected.length === 1 ? "open" : ""}><summary>${esc(e.role)} · ${esc(e.text.slice(0, 80))}</summary><pre>${esc(e.text)}</pre></details>`).join("") || `<p class="small muted">Choose the relevant message or passage on the left, or leave this as a timestamp-only moment.</p>`;
    $('[data-quote-context]', root).innerHTML = quoteSelection
      ? `<span class="small muted">Highlighted text from ${esc(events.find(e => e.id === quoteSelection.event_id)?.role || 'message')}:</span><blockquote>${esc(quoteSelection.text)}</blockquote>`
      : m.quote && !quoteDirty ? `<span class="small muted">Text selected during the conversation:</span><blockquote>${esc(m.quote)}</blockquote>` : '';
    $('[data-clear-quote]', root).classList.toggle('hidden', !quoteSelection && !(m.quote && !quoteDirty));
  };
  $('[data-start]', root).value = start; $('[data-end]', root).value = end;
  for (const field of ['before','after','perceived_value','uptake','outcome','latency','intensity']) {
    const node = $(`[data-f="${field}"]`, root);
    if (node) node.value = state[field] ?? "";
  }
  function capture() {
    for (const field of ['before','after','uptake','outcome','latency']) state[field] = $(`[data-f="${field}"]`, root).value || null;
    for (const field of ['perceived_value','intensity']) state[field] = +$(`[data-f="${field}"]`, root).value || null;
    start = $('[data-start]', root).value; end = $('[data-end]', root).value;
    state.primary = state.types?.[0] || null;
    state.editor_id = editorId; state.editor_seq = seq;
  }
  function changed() {
    if (locked) return;
    seq++; capture(); dirty = true; const cached = remember(); root.closest('.review-card')?.classList.remove('saved');
    status(!cached ? "Device storage unavailable — syncing; keep this tab open" : blocked ? "Another tab changed this review. Your local draft is retained." : "Saved on this device · syncing draft…");
    onChanged(m, state); clearTimeout(timer); if (!blocked) timer = setTimeout(() => save(false), 600);
  }
  async function save(final) {
    clearTimeout(timer);
    if (locked || blocked) return false;
    if (operation) { await operation; if (blocked) return false; }
    capture();
    if (final && (!state.decision || state.decision === 'confirmed' && !String(state.after || '').trim())) {
      status("Choose Yes / Unsure / No, and briefly describe a confirmed change."); $('[data-f="after"]', root).focus(); return false;
    }
    if (final) { seq++; capture(); }
    const sentSeq = seq, payload = structuredClone(state);
    const body = { expected_review_version: version, [final ? "review" : "review_draft"]: payload };
    if (quoteDirty) body.quote_selection = quoteSelection;
    if (start && end) body.episode = { start_event_id: start, end_event_id: end };
    else if (!start && !end) body.episode = null;
    status(final ? "Confirming…" : "Saving draft…");
    operation = (async () => {
      try {
        const saved = await api("PATCH", `/api/sessions/${sid}/marks/${m.id}`, body);
        version = saved.review_version; Object.assign(m, saved);
        if (sentSeq === seq) quoteDirty = false;
        if (sentSeq === seq) {
          dirty = false;
          if (final) { clearLocal(); status("Review confirmed"); root.closest('.review-card')?.classList.add('saved'); onSaved(m); }
          else { remember(); status("Draft saved · confirm when ready"); }
        } else { remember(); timer = setTimeout(() => save(false), 300); }
        $('[data-retry]', root).classList.add('hidden'); return true;
      } catch (error) {
        if (error.message.includes("Another tab")) {
          try {
            const fresh = await api("GET", `/api/sessions/${sid}`);
            const record = fresh.marks.find(mark => mark.id === m.id);
            const previous = record?.review_draft || record?.review;
            if (previous?.editor_id === editorId && !fresh.independent_review?.mark_ids.includes(m.id) && fresh.phase !== 'done') {
              version = record.review_version; Object.assign(m, record);
              if (final && previous.status === 'submitted' && previous.editor_seq === sentSeq && sentSeq === seq) {
                dirty = false; clearLocal(); status("Review confirmed"); root.closest('.review-card')?.classList.add('saved'); onSaved(m); return true;
              }
              // Same editor's earlier save reached the server but its ACK was
              // lost. Retry current text with the acknowledged revision.
              remember(); status("Recovering interrupted save…");
              timer = setTimeout(() => save(final && sentSeq === seq), 150); return false;
            }
          } catch {}
        }
        blocked = error.message.includes("Another tab") || error.message.includes("locked");
        status(blocked ? "Conflict — your draft is retained on this device." : "Not synced — draft retained on this device. Retry or reconnect.");
        $('[data-retry]', root).classList.toggle('hidden', blocked);
        $('[data-replace]', root).classList.toggle('hidden', !blocked);
        remember(); return false;
      } finally { operation = null; }
    })();
    return operation;
  }
  $$('[data-decision]', root).forEach(b => b.onclick = () => { state.decision = b.dataset.decision; paint(); changed(); });
  $('[data-clear-quote]', root).onclick = () => { quoteSelection = null; quoteDirty = true; changed(); context(); };
  $$('[data-f]', root).forEach(node => node.addEventListener(node.tagName === 'SELECT' ? 'change' : 'input', changed));
  $('[data-start]', root).onchange = () => {
    const chosen = $('[data-start]', root).value;
    if (!end || !chosen || events.findIndex(e => e.id === chosen) > events.findIndex(e => e.id === end)) $('[data-end]', root).value = chosen;
    changed(); context();
  };
  $('[data-end]', root).onchange = () => { changed(); context(); };
  $('[data-save]', root).onclick = () => save(true);
  $('[data-retry]', root).onclick = () => save(false);
  $('[data-replace]', root).onclick = async () => {
    if (!confirm("Replace this moment's saved draft with your local draft? Earlier versions remain in the research history.")) return;
    const fresh = await api("GET", `/api/sessions/${sid}`);
    const record = fresh.marks.find(mark => mark.id === m.id);
    if (fresh.independent_review?.mark_ids.includes(m.id)) return status("Independent review is locked; its original answers cannot be replaced.");
    version = record.review_version || 0; editorId = crypto.randomUUID(); blocked = false; seq++;
    $('[data-replace]', root).classList.add('hidden'); await save(false);
  };
  const controller = {
    mark: m,
    async flush() { if (operation) await operation; return !blocked && (!dirty || await save(false)); },
    submit: () => save(true),
    needsSave: () => !locked && (blocked || !!operation || dirty),
    selectPassage(a, b = a) { if (locked) return; start = a; end = b; $('[data-start]', root).value = a; $('[data-end]', root).value = b; changed(); context(); },
    selectQuote(selection) { if (locked) return; quoteSelection = selection; quoteDirty = true; start = end = selection.event_id;
      $('[data-start]', root).value = start; $('[data-end]', root).value = end; changed(); context(); },
    startId: () => start,
    lock() { locked = true; clearTimeout(timer); root.closest('.review-card')?.classList.add('read-only'); $$('.annot button,.annot input,.annot textarea,.annot select', root).forEach(n => n.disabled = true); status('Review locked'); },
    dispose() { clearTimeout(timer); controllers.delete(controller); }
  };
  controllers.add(controller); context(); paint();
  if (locked) controller.lock();
  else if (blocked) { status("A different tab has newer answers. Your draft is retained."); $('[data-replace]', root).classList.remove('hidden'); }
  else if (dirty) timer = setTimeout(() => save(false), 300);
  else if (m.review?.status === 'submitted' && !m.review_dirty) status('Review confirmed');
  return controller;
}

export async function flushReviewDrafts() { return (await Promise.all([...controllers].map(c => c.flush()))).every(Boolean); }
window.addEventListener('beforeunload', e => { if ([...controllers].some(c => c.needsSave())) { e.preventDefault(); e.returnValue = ''; } });
window.addEventListener('online', () => flushReviewDrafts());
