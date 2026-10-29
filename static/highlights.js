// Open-ended highlights: exact spans with a note and free tags in the
// participant's words. Settings and wording come from the session's snapshot
// of config/annotation.json.
import { api, esc, $, $$, toast } from "/static/common.js";

export const targetKey = t => t.kind === "event" ? `ev:${t.event_id}:${t.field || "text"}` : `w:${t.writing}`;
const chars = text => Array.from(text || "");

// Paint possibly overlapping ranges ({start, end, cls, id}) over text, in code points.
export function paintRanges(text, ranges) {
  const cs = chars(text);
  const valid = ranges.filter(r => r.start >= 0 && r.end <= cs.length && r.end > r.start);
  if (!valid.length) return esc(text);
  const points = [...new Set([0, cs.length, ...valid.flatMap(r => [r.start, r.end])])].sort((a, b) => a - b);
  let html = "";
  for (let i = 0; i < points.length - 1; i++) {
    const [a, b] = [points[i], points[i + 1]];
    const piece = esc(cs.slice(a, b).join(""));
    const covering = valid.filter(r => r.start <= a && r.end >= b);
    if (!covering.length) { html += piece; continue; }
    const classes = [...new Set(covering.map(r => r.cls))].join(" ");
    const ids = covering.map(r => r.id).filter(Boolean).join(" ");
    html += `<mark class="${classes}"${ids ? ` data-hl="${esc(ids)}"` : ""}>${piece}</mark>`;
  }
  return html;
}

// Markdown markers in stored text, as ranges: the markers are hidden and the
// enclosed text styled, so the review reads like the chat while offsets stay
// those of the stored text.
export function markdownRanges(text) {
  const out = [], toPoints = i => chars(text.slice(0, i)).length;
  for (const [pattern, marker, cls] of [[/\*\*([^*\n]+)\*\*/g, 2, "md-strong"], [/`([^`\n]+)`/g, 1, "md-code"]]) {
    for (const m of text.matchAll(pattern)) {
      const start = toPoints(m.index), end = toPoints(m.index + m[0].length);
      out.push({ start, end: start + marker, cls: "md-mark" }, { start: start + marker, end: end - marker, cls }, { start: end - marker, end, cls: "md-mark" });
    }
  }
  return out;
}

// True when the stored text still reads `quote` at [start, end). Streaming text
// can be replaced when a turn completes, so stale offsets are never painted.
export function spanMatches(text, start, end, quote) {
  return chars(text).slice(start, end).join("") === quote;
}

// Locate a quote in stored text; only a unique match yields offsets.
export function locate(text, quote) {
  if (!text || !quote) return null;
  const at = text.indexOf(quote);
  if (at < 0 || text.indexOf(quote, at + 1) >= 0) return null;
  const start = chars(text.slice(0, at)).length;
  return { start, end: start + chars(quote).length };
}

export class Highlighter {
  constructor({ sid, getSession, researcher, describeTarget, moments, canEdit, allowStandalone, onChange }) {
    Object.assign(this, { sid, getSession, researcher, describeTarget, moments, canEdit, allowStandalone, onChange });
    this.cards = new Map();
    this.queues = new Map();
    this.timers = new Map();
    this.inflight = new Map();
    this.failed = new Map();
    // Server responses this tab has seen. Polling can return a snapshot taken
    // before a save landed; the higher version always wins.
    this.local = new Map();
    this.adjusting = null;
    this.suggestions = [];
    window.addEventListener("beforeunload", e => {
      if (this.timers.size || this.inflight.size || this.failed.size) { e.preventDefault(); e.returnValue = ""; }
    });
  }
  get S() { return this.getSession(); }
  get cfg() { return this.S.annotation_settings.highlights; }
  get words() { return this.S.annotation_settings.wording; }
  all() {
    const merged = new Map((this.S.highlights || []).map(h => [h.id, h]));
    for (const [id, h] of this.local) if (!merged.has(id) || merged.get(id).version < h.version) merged.set(id, h);
    return [...merged.values()];
  }
  live() { return this.all().filter(h => !h.deleted); }
  byId(id) { return this.all().find(h => h.id === id); }
  forTarget(key) { return this.live().filter(h => targetKey(h.target) === key); }
  forMark(markId) { return this.live().filter(h => h.mark_id === markId); }

  async loadSuggestions() {
    if (this.researcher || this.cfg.tag_suggestions === "off") return;
    try { this.suggestions = await api("GET", `/api/sessions/${this.sid}/tag-suggestions`); } catch { this.suggestions = []; }
    this.paintSuggestions();
  }
  paintSuggestions() {
    let list = $("#hl-tag-suggestions");
    if (!list) { list = document.createElement("datalist"); list.id = "hl-tag-suggestions"; document.body.appendChild(list); }
    list.innerHTML = this.suggestions.map(t => `<option value="${esc(t)}"></option>`).join("");
  }

  async create(selection, extra = {}) {
    const body = { id: "h_" + crypto.randomUUID().replaceAll("-", ""), target: selection.target,
                   start: selection.start, end: selection.end, text: selection.text, ...extra };
    const h = await api("POST", `/api/sessions/${this.sid}/highlights`, body);
    this.local.set(h.id, h);
    this.onChange(h);
    return h;
  }

  // Patches for one highlight run in order. `changes` may be a function of the
  // latest record, so quick successive tag edits never overwrite each other.
  patch(id, changes) {
    const run = async () => {
      const h = this.byId(id);
      const body = typeof changes === "function" ? changes(h) : changes;
      if (!h || !body) return true;
      this.inflight.set(id, (this.inflight.get(id) || 0) + 1);
      this.status(id, "Saving…");
      try {
        const saved = await api("PATCH", `/api/sessions/${this.sid}/highlights/${id}`, { ...body, expected_version: h.version });
        this.local.set(id, saved);
        this.failed.delete(id);
        this.status(id, "Saved");
        if (["mark_id", "start", "deleted"].some(k => k in body)) this.onChange(saved);
        return true;
      } catch (error) {
        toast(error.message);
        if (error.status === 409) await this.resync(id);  // show the newer version; nothing to retry
        else { this.failed.set(id, changes); this.status(id, `Not saved: ${error.message}`); }
        return false;
      } finally {
        const left = this.inflight.get(id) - 1;
        left ? this.inflight.set(id, left) : this.inflight.delete(id);
        const card = this.cards.get(id), current = this.byId(id);
        if (card && current) this.refreshCard(current, card);
      }
    };
    const queue = (this.queues.get(id) || Promise.resolve()).then(run);
    this.queues.set(id, queue);
    return queue;
  }

  async resync(id) {
    try {
      const fresh = (await api("GET", `/api/sessions/${this.sid}`)).highlights.find(h => h.id === id);
      if (fresh) this.local.set(id, fresh);
      this.failed.delete(id);
      this.status(id, "Updated from another tab");
      this.onChange(fresh);
    } catch { /* the next poll refreshes it */ }
  }

  // Saves pending notes and retries failed edits once; true when nothing is unsaved.
  async flush() {
    for (const [id, timer] of [...this.timers]) { clearTimeout(timer); this.timers.delete(id); this.saveNote(id); }
    for (const [id, changes] of [...this.failed]) this.patch(id, changes);
    await Promise.all(this.queues.values());
    return this.failed.size === 0;
  }

  saveNote(id) {
    const card = this.cards.get(id);
    if (!card) return;
    const note = $("[data-note]", card).value;
    this.patch(id, h => note === h.note ? null : { note });
  }

  status(id, text) { const el = this.cards.get(id) && $("[data-hl-status]", this.cards.get(id)); if (el) el.textContent = text; }

  // Incremental render: existing cards stay in place, so focus and unsaved
  // typing survive polling.
  render(container) {
    const words = this.words, live = this.live();
    if (!this.cfg.enabled) { container.innerHTML = ""; return; }
    if (!$(".hl-panel-head", container)) {
      container.innerHTML = `<div class="hl-panel-head"><h2>${esc(words.panel_title)}</h2>
        ${this.researcher ? "" : `<p class="small muted">${esc(words.panel_hint)}</p>`}</div>
        <p class="small muted hl-empty">${esc(words.panel_empty)}</p><div class="hl-list"></div>`;
    }
    $(".hl-empty", container).classList.toggle("hidden", live.length > 0);
    const list = $(".hl-list", container);
    for (const [id, card] of this.cards) {
      if (live.some(h => h.id === id) || this.timers.has(id)) continue;
      card.remove(); this.cards.delete(id);
    }
    for (const h of live) {
      let card = this.cards.get(h.id);
      if (!card) { card = this.buildCard(h); this.cards.set(h.id, card); }
      if (card.parentNode !== list) list.appendChild(card);
      this.refreshCard(h, card);
    }
  }

  buildCard(h) {
    const words = this.words, cfg = this.cfg;
    const card = document.createElement("div");
    card.className = "hl-card"; card.dataset.hlCard = h.id;
    card.innerHTML = `<blockquote class="hl-quote" data-jump role="button" tabindex="0" title="Show in context"></blockquote>
      <div class="small muted" data-where></div>
      <label class="field"><span>${esc(words.note_label)}</span>
        <textarea data-note maxlength="${cfg.note_max_chars}" placeholder="${esc(words.note_placeholder)}"></textarea></label>
      <div class="field"><span>${esc(words.tags_label)}</span>
        <div class="tag-box"><span data-tags></span><input data-tag-input list="hl-tag-suggestions" maxlength="${cfg.tag_max_chars}" aria-label="${esc(words.tags_label)}"></div>
        <span class="small muted">${esc(words.tags_hint)}</span></div>
      <label class="field"><span>${esc(words.link_label)}</span><select data-link></select></label>
      <div class="row hl-actions"><button class="btn btn-sm btn-ghost" data-adjust>${esc(words.adjust_button)}</button>
        <button class="btn btn-sm btn-ghost" data-remove>${esc(words.remove_button)}</button>
        <span class="small muted grow" data-hl-status></span></div>`;
    const id = h.id;
    const jump = () => {
      const el = document.querySelector(`mark[data-hl~="${id}"]`);
      el?.closest("details")?.setAttribute("open", "");
      el?.scrollIntoView({ block: "center", behavior: "smooth" });
      el?.classList.add("hl-flash"); setTimeout(() => el?.classList.remove("hl-flash"), 900);
    };
    $("[data-jump]", card).onclick = jump;
    $("[data-jump]", card).onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); jump(); } };
    $("[data-note]", card).addEventListener("input", () => {
      clearTimeout(this.timers.get(id));
      this.status(id, "Unsaved");
      this.timers.set(id, setTimeout(() => { this.timers.delete(id); this.saveNote(id); }, cfg.note_autosave_ms));
    });
    const input = $("[data-tag-input]", card);
    const addTags = () => {
      const fresh = input.value.split(",").map(t => t.trim()).filter(Boolean);
      input.value = "";
      if (!fresh.length) return;
      this.patch(id, current => {
        const added = fresh.filter(t => !current.tags.some(c => c.toLowerCase() === t.toLowerCase()));
        return added.length ? { tags: [...current.tags, ...added] } : null;
      }).then(ok => {
        if (!ok) return;
        fresh.forEach(t => { if (!this.suggestions.includes(t)) this.suggestions.push(t); });
        this.paintSuggestions();
      });
    };
    input.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addTags(); } });
    input.addEventListener("change", () => { if (input.value.trim()) addTags(); });
    $("[data-tags]", card).addEventListener("click", e => {
      const tag = e.target.closest("[data-remove-tag]")?.dataset.removeTag;
      if (tag != null) this.patch(id, current => ({ tags: current.tags.filter(t => t !== tag) }));
    });
    $("[data-link]", card).onchange = e => this.patch(id, { mark_id: e.target.value || null });
    $("[data-adjust]", card).onclick = () => {
      this.adjusting = this.adjusting === id ? null : id;
      this.cards.forEach((c, cid) => c.classList.toggle("adjusting", cid === this.adjusting));
      if (this.adjusting) toast(words.adjust_hint);
    };
    $("[data-remove]", card).onclick = () => this.patch(id, { deleted: true });
    return card;
  }

  refreshCard(h, card) {
    const editable = !this.researcher && this.canEdit(h);
    const words = this.words;
    $("[data-jump]", card).textContent = h.text;
    $("[data-where]", card).textContent = this.describeTarget(h);
    const note = $("[data-note]", card);
    if (document.activeElement !== note && !this.timers.has(h.id) && !this.inflight.has(h.id)) note.value = h.note || "";
    $("[data-tags]", card).innerHTML = h.tags.map(t => `<span class="tag-chip">${esc(t)}${editable ? `<button type="button" data-remove-tag="${esc(t)}" aria-label="Remove tag ${esc(t)}">×</button>` : ""}</span>`).join("");
    const select = $("[data-link]", card);
    if (document.activeElement !== select) {
      const moments = this.moments(h);
      const orphan = h.mark_id && !moments.some(m => m.id === h.mark_id);
      select.innerHTML = `<option value="" ${this.allowStandalone(h) ? "" : "disabled"}>${esc(words.link_none)}</option>` +
        moments.map(m => `<option value="${m.id}" ${m.disabled ? "disabled" : ""}>${esc(m.label)}</option>`).join("") +
        (orphan ? `<option value="${esc(h.mark_id)}" disabled>${esc(h.mark_id)}</option>` : "");
      select.value = h.mark_id || "";
    }
    $$("textarea,input,select,button[data-adjust],button[data-remove]", card).forEach(n => n.disabled = !editable);
    $(".hl-actions", card).classList.toggle("hidden", !editable);
    card.classList.toggle("read-only", !editable);
    card.classList.toggle("linked", !!h.mark_id);
  }
}
