import { api, icon, esc, $, $$, config, toast, fmtDate } from "/static/common.js";

const cfg = await config();
let sessions = [], S, judgePoll;
$('#brand').innerHTML = icon('bulb') + '<span>Spark research</span>';
const label = key => cfg.taxonomy.types.find(t => t.key === key)?.label || key || 'Untagged';
const marks = () => S.marks.filter(m => !m.deleted);
const candidates = () => S.candidates.filter(c => c.source === 'judge');
async function refreshList() { sessions = await api('GET', '/api/sessions'); renderList(); }
function renderList() {
  const q = $('#filter').value.toLowerCase();
  $('#list').innerHTML = sessions.filter(s => !q || JSON.stringify(s).toLowerCase().includes(q)).map(s => `
    <button class="rs-item ${S?.id === s.id ? 'active' : ''}" data-session="${s.id}">
      <b>${esc(s.participant_id)}</b> · ${esc(s.prompt)}
      <div class="l2">${esc(s.study)} · ${esc(s.condition)} · ${esc(s.phase)}</div>
      <div class="l2">${s.marks} bookmarks · ${s.candidates} LLM suggestions · ${fmtDate(s.created_at)}</div>
    </button>`).join('') || '<p class="small muted">No sessions yet.</p>';
  $$('[data-session]').forEach(b => b.onclick = () => openSession(b.dataset.session));
}
$('#filter').oninput = renderList;
async function openSession(id) {
  S = await api('GET', `/api/sessions/${id}?view=researcher`);
  sessions = sessions.map(s => s.id === id ? {...s,marks:marks().length,candidates:candidates().length,phase:S.phase} : s);
  history.replaceState(null, '', '#' + id); renderList(); render();
}
function codeForm(kind, id) {
  const saved = S.codes[`${kind}:${id}`] || {};
  return `<details class="small"><summary>Researcher code (separate from participant report)</summary>
    <form class="code-form" data-kind="${kind}" data-target="${id}">
      <label class="field"><span>Coder ID</span><input name="coder" required value="${esc(saved.coder || '')}"></label>
      <label class="field"><span>Code</span><select name="label">${['uncoded','spark_candidate','not_spark','uncertain'].map(k => `<option value="${k}" ${saved.label === k ? 'selected' : ''}>${k}</option>`).join('')}</select></label>
      <label class="field"><span>Notes</span><textarea name="notes">${esc(saved.notes || '')}</textarea></label>
      <button class="btn btn-sm">Save researcher code</button>
    </form></details>`;
}
function render() {
  clearInterval(judgePoll);
  const independent = marks().filter(m => m.source !== 'candidate_confirm');
  const counts = ['confirmed','unsure','rejected'].map(d => independent.filter(m => !m.review_dirty && m.review?.status === 'submitted' && m.review?.decision === d).length);
  const jd = S.judge || {};
  $('#main').innerHTML = `<div class="rs-head">
    <h1>${esc(S.participant_id)} · ${esc(S.prompt.title)}</h1>
    <p class="small muted">${esc(S.study)} · ${esc(S.runtime?.kind || S.condition)} · ${esc(S.phase)} · ${S.events.length} events</p>
    <div class="row">
      <button class="btn btn-sm" id="run-judge" ${!cfg.judge.live || ['pre','chat'].includes(S.phase) || !S.events.length || jd.status === 'running' ? 'disabled' : ''}>${jd.status === 'running' ? 'Judge running…' : 'Run LLM judge'}</button>
      <a class="btn btn-sm" href="/review?id=${S.id}&mode=researcher">Transcript / recording review</a>
      <a class="btn btn-sm" href="/api/sessions/${S.id}/transcript.md">Readable transcript</a>
      <a class="btn btn-sm" href="/api/sessions/${S.id}/export.json">Full JSON + history</a>
      <label class="btn btn-sm">Import agent log<input type="file" id="import-log" accept=".json,.jsonl,.zip" hidden></label>
    </div>
    <p class="small muted">Judge: ${esc(jd.status || 'not run')} · ${esc(jd.model || cfg.judge.model)}. ${esc(cfg.judge.cost_note || 'Provider billing applies.')}</p>
    ${jd.error ? `<p class="error">${esc(jd.error)}</p>` : ''}
    ${jd.summary ? `<details><summary>Model interpretation (not ground truth)</summary><p>${esc(jd.summary)}</p></details>` : ''}
    <p class="small muted">Independent pass: ${S.independent_review ? 'locked' : 'not completed'}. This localhost console has no researcher authentication yet.</p>
  </div>
  <div class="stats">
    <div class="stat"><div class="n">${independent.length}</div><div class="k">independent bookmarks (not spark count)</div></div>
    <div class="stat"><div class="n">${counts.join(' / ')}</div><div class="k">participant confirmed / unsure / rejected</div></div>
    <div class="stat"><div class="n">${marks().length - independent.length}</div><div class="k">suggestion-prompted reports</div></div>
    <div class="stat"><div class="n">${candidates().length}</div><div class="k">LLM candidates, not confirmed sparks</div></div>
  </div>
  <p class="small muted" style="padding:0 24px">Passage overlap: ${S.analysis.matched_marks} bookmarks overlap a model passage. This is descriptive overlap, not accuracy, recall, or independent confirmation.</p>
  <div class="rs-body">
    <section class="timeline"><h2>Full visible transcript</h2>
      ${['pre','post'].map(k => S.writing[k] ? `<details><summary>${k === 'pre' ? 'Baseline' : 'Final write-up'}</summary><pre class="full-event">${esc(S.writing[k].text)}</pre></details>` : '').join('')}
      ${S.events.filter(e => e.role !== 'thinking' && !e.meta?.superseded).map(e => `<details id="${e.id}" class="insp-block" ${['user','assistant','partner'].includes(e.role) ? 'open' : ''}>
        <summary>${esc(e.role)} · ${esc(e.id)} · ${esc(e.meta?.status || '')}</summary><pre class="full-event">${esc(e.text)}</pre>${codeForm('event', e.id)}</details>`).join('') || '<p>No transcript attached.</p>'}
    </section>
    <aside class="inspector"><h2>Participant reports</h2>
      ${marks().map(m => {
        const r = m.review || {}, ep = m.episode || {};
        return `<div class="insp-block"><h3>${esc(r.decision || 'Unreviewed bookmark')} · ${esc(label(r.primary || m.quick_type))}</h3>
          <p class="small muted">${esc(m.source)} · ${esc(r.status || 'not submitted')}${m.review_dirty ? ' · changed draft' : ''}</p>
          <p>${esc(r.after || '')}</p><p class="small">Before: ${esc(r.before || 'Not reported')}</p>
          <p class="small">Use: ${esc(r.uptake || 'Not reported')} · value: ${esc(r.perceived_value ?? 'Not reported')} · later help: ${esc(r.outcome || 'Not reported')}</p>
          <p class="small muted">Passage: ${esc(ep.start_event_id || m.event_id || 'Timestamp only')} → ${esc(ep.end_event_id || '')}. Original click: ${esc(m.created_at)}.</p>
          ${codeForm('mark',m.id)}</div>`;
      }).join('') || '<p class="small muted">No bookmarks. This does not establish an absence of insights.</p>'}
      <h2>LLM suggestions</h2>
      ${candidates().map(c => `<div class="insp-block"><h3>${esc(label(c.type))} · ${esc(c.insight_evidence || 'legacy output')}</h3>
        <p class="small muted">Qualitative strength: ${esc(c.strength || 'not recorded')}; not a probability.</p>
        <p>“${esc(c.trigger_quote)}”</p>${c.uptake_quote ? `<p>Participant evidence: “${esc(c.uptake_quote)}”</p>` : '<p class="small muted">No cited participant evidence.</p>'}
        <p>${esc(c.rationale)}</p>
        <p class="small">Persuasion: ${esc(c.persuasion || 'not recorded')} · copying: ${esc(c.copying || 'not recorded')} · uptake: ${esc(c.uptake || 'not recorded')}</p>
        <p class="small">Participant response: ${esc(c.participant_response?.answer || 'not shown / unanswered')}</p>${codeForm('candidate',c.id)}</div>`).join('') || '<p class="small muted">Not run, failed, or no candidate found. These are distinct states.</p>'}
      ${jd.rejected_candidates?.length ? `<details><summary>${jd.rejected_candidates.length} predictions rejected by evidence validation</summary><pre class="full-event">${esc(JSON.stringify(jd.rejected_candidates,null,2))}</pre></details>` : ''}
    </aside>
  </div>`;
  $('#run-judge').onclick = async () => {
    try {
      const est = await api('GET', `/api/sessions/${S.id}/judge/estimate`);
      if (!confirm(`Send task, baseline and visible transcript to the LLM judge?\nProvider: ${est.provider} · model: ${est.model}\n~${est.input_tokens} input tokens. ${est.cost_note}\nFlags, annotations, and researcher codes are excluded.`)) return;
      await api('POST', `/api/sessions/${S.id}/judge`, {}); await openSession(S.id);
    } catch (error) { toast(error.message); }
  };
  $('#import-log').onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    try {
      const uploaded = await api('POST','/api/upload',file,{'X-Filename':file.name});
      await api('POST',`/api/sessions/${S.id}/import`,{upload_id:uploaded.upload_id}); await openSession(S.id);
    } catch (error) { toast(error.message); }
  };
  $$('.code-form').forEach(f => f.onsubmit = async e => {
    e.preventDefault();
    try { await api('POST',`/api/sessions/${S.id}/code`,{...Object.fromEntries(new FormData(f)),kind:f.dataset.kind,target:f.dataset.target}); toast('Researcher code saved'); }
    catch (error) { toast(error.message); }
  });
  if (jd.status === 'running') judgePoll = setInterval(async () => {
    try { const fresh = await api('GET',`/api/sessions/${S.id}/poll?after=${S.events.length}`); if (fresh.judge?.status !== 'running') await openSession(S.id); } catch {}
  },3000);
}
await refreshList();
const initial = location.hash.slice(1) || sessions[0]?.id;
if (initial) await openSession(initial);
window.addEventListener('hashchange', () => location.hash.length > 1 && openSession(location.hash.slice(1)));
setInterval(() => refreshList().catch(() => {}),10000);
