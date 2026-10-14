// Durable browser outbox. Retries the same bookmark ID, never a new moment.
import { api } from "/static/common.js";

export class BookmarkOutbox {
  constructor(sid, onChange = () => {}) {
    this.sid = sid; this.onChange = onChange; this.key = `spark-outbox:${sid}`;
    this.rows = {}; this.running = new Map(); this.storageAvailable = true;
    try { this.rows = JSON.parse(localStorage.getItem(this.key) || "{}"); } catch { this.storageAvailable = false; }
    this.online = () => this.flush();
    window.addEventListener("online", this.online);
    this.timer = setInterval(() => this.flush(), 6000);
    this.flush();
  }
  persist() {
    try { localStorage.setItem(this.key, JSON.stringify(this.rows)); this.storageAvailable = true; }
    catch { this.storageAvailable = false; }
    this.onChange(this.pending());
  }
  pending() { return Object.values(this.rows); }
  create(payload) {
    const id = payload.id || "m_" + crypto.randomUUID().replaceAll("-", "");
    const body = { ...payload, id, client_version: 1 };
    const row = { body, state: "pending", error: null };
    this.rows[id] = row; this.persist(); this.sync(id);
    return { ...body, t: body.t ?? body.client_t / 1000, review: null, sync_state: "pending" };
  }
  update(id, patch, original) {
    const previous = this.rows[id]?.body || original;
    if (!previous) return;
    const body = { ...previous, ...patch, id, client_version: (previous.client_version || 1) + 1 };
    this.rows[id] = { body, state: "pending", error: null }; this.persist(); this.sync(id);
  }
  async sync(id) {
    if (this.running.has(id)) return this.running.get(id);
    const operation = (async () => {
      while (this.rows[id]) {
        const body = structuredClone(this.rows[id].body);
        const abort = new AbortController();
        const timeout = setTimeout(() => abort.abort(), 5000);
        try {
          const mark = await api("POST", `/api/sessions/${this.sid}/marks`, body, {}, { signal: abort.signal });
          if (this.rows[id]?.body.client_version === body.client_version) delete this.rows[id];
          this.persist(); this.onChange(this.pending(), mark);
        } catch (error) {
          if (this.rows[id]) { this.rows[id].state = "failed"; this.rows[id].error = error.message; }
          this.persist(); break;
        } finally { clearTimeout(timeout); }
      }
    })();
    this.running.set(id, operation);
    await operation;
    this.running.delete(id);
  }
  async flush() { await Promise.all(Object.keys(this.rows).map(id => this.sync(id))); return this.pending().length === 0; }
  close() { clearInterval(this.timer); window.removeEventListener("online", this.online); }
}

export function captureExposure(session, container, anchorId = null) {
  const bounds = container?.getBoundingClientRect();
  const rows = [];
  for (const node of container?.querySelectorAll(".msg[data-id]") || []) {
    const rect = node.getBoundingClientRect();
    const anchored = node.dataset.id === anchorId;
    if (!anchored && bounds && (rect.bottom <= bounds.top || rect.top >= bounds.bottom)) continue;
    const event = session.events.find(e => e.id === node.dataset.id);
    if (!event || event.id === "pending") continue;
    const tool = ["tool_call", "tool_result"].includes(event.role);
    const toolOpen = !!node.querySelector("details[open]");
    rows.push({ event_id: event.id, rendered_text: !tool || toolOpen ? event.text : node.querySelector("summary")?.textContent || "",
      rendered_output: toolOpen ? event.output || "" : "", status: event.status || "", tool_open: toolOpen,
      partially_in_view: !!bounds && (rect.top < bounds.top || rect.bottom > bounds.bottom) });
  }
  const relevant = anchorId ? rows.filter(r => r.event_id === anchorId) : rows.slice(-6);
  return { surface: "integrated", events: relevant };
}
