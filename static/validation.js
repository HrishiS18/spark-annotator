import { api, esc, icon, $, toast, fmtDate } from "/static/common.js";

const params = new URLSearchParams(location.search);
const sampleId = params.get('sample');
const token = params.get('token');
const researcher = params.get('view') === 'results';
const app = $('#app');
const pct = (n, d) => d ? `${Math.round(100 * n / d)}% (${n}/${d})` : '—';
const typeLabel = key => ({
  additive:'Additive', representational:'Representational', reflective:'Reflective', contrastive:'Contrastive',
  counterfactual:'Counterfactual', associative:'Associative', generative_error:'Generative error',
  unclear:'Unclear', none:'None / not applicable'
}[key] || key);
const roleLabel = (role, partner) => ({user:'Participant',assistant:partner === 'human' ? 'Partner' : 'Agent',partner:'Partner',
  tool_call:'Agent tool call',tool_result:'Tool result',system:'System'}[role] || role);

function header(title, subtitle='') {
  return `<header class="validation-head"><a href="/researcher">← Research dashboard</a><div>
    <h1>${esc(title)}</h1>${subtitle ? `<p class="muted">${esc(subtitle)}</p>` : ''}</div></header>`;
}

async function setupPage() {
  const [sessions, samples] = await Promise.all([api('GET','/api/sessions'), api('GET','/api/validations')]);
  const eligible = sessions.filter(s => s.events > 0 && (s.candidates > 0 || s.reviewed > 0));
  app.innerHTML = `${header('Validate the spark judge','Create a small blinded set for two independent coders.')}
  <div class="validation-two">
    <section class="card validation-setup">
      <h2>New validation set</h2>
      <p class="small muted">The sample mixes LLM candidates, independently confirmed participant episodes the judge did not overlap, and unflagged comparison passages. Their source is hidden from both coders.</p>
      <form id="create-validation">
        <label class="field"><span>Name</span><input name="name" value="Spark judge pilot validation" required></label>
        <div class="two"><label class="field"><span>Coder A</span><input name="coder_a" placeholder="Initials or study ID" required></label>
          <label class="field"><span>Coder B</span><input name="coder_b" placeholder="Initials or study ID" required></label></div>
        <div class="three"><label class="field"><span>Judge candidates</span><input name="candidate_n" type="number" min="0" max="100" value="10"></label>
          <label class="field"><span>Confirmed reports missed by judge</span><input name="report_n" type="number" min="0" max="100" value="5"></label>
          <label class="field"><span>Unflagged comparisons</span><input name="control_n" type="number" min="0" max="100" value="5"></label></div>
        <label class="field"><span>Sampling seed</span><input name="seed" placeholder="Leave blank to generate one"></label>
        <fieldset class="validation-sessions"><legend>Sessions to sample</legend>
          ${eligible.map(s => `<label><input type="checkbox" name="session_ids" value="${s.id}">
            <span><b>${esc(s.participant_id)}</b> · ${esc(s.prompt)}<small>${s.candidates} candidates · ${s.reviewed} reviewed bookmarks · ${fmtDate(s.created_at)}</small></span></label>`).join('') || '<p class="small muted">No sessions with judge candidates or reviewed bookmarks yet.</p>'}
        </fieldset>
        <button class="btn btn-primary" ${eligible.length ? '' : 'disabled'}>Create blinded set</button>
      </form>
    </section>
    <section class="card">
      <h2>Existing sets</h2>
      ${samples.map(s => `<a class="validation-sample" href="/validation?sample=${s.id}&view=results">
        <b>${esc(s.name)}</b><span>${s.items} items · ${esc(s.coders.map(c => `${c.name} ${s.progress[c.id]}/${s.items}`).join(' · '))}</span></a>`).join('') || '<p class="small muted">No validation sets yet.</p>'}
    </section>
  </div>`;
  $('#create-validation')?.addEventListener('submit', async event => {
    event.preventDefault();
    const fd = new FormData(event.currentTarget);
    const body = Object.fromEntries(fd);
    body.session_ids = fd.getAll('session_ids');
    if (!body.session_ids.length) return toast('Select at least one session');
    const button = event.currentTarget.querySelector('button');
    button.disabled = true; button.textContent = 'Creating…';
    try {
      const created = await api('POST','/api/validations',body);
      location.href = `/validation?sample=${created.id}&view=results`;
    } catch (error) { toast(error.message); button.disabled = false; button.textContent = 'Create blinded set'; }
  });
}

function option(value, label, saved) {
  return `<option value="${value}" ${saved === value ? 'selected' : ''}>${esc(label)}</option>`;
}

async function coderPage() {
  const data = await api('GET',`/api/validations/${sampleId}?token=${encodeURIComponent(token)}`);
  let index = Math.max(0, data.items.findIndex(item => !data.codes[item.id]));
  if (index < 0) index = 0;
  function render() {
    const item = data.items[index], saved = data.codes[item.id] || {};
    const complete = Object.keys(data.codes).length;
    const types = [...data.type_keys, 'unclear', 'none'];
    app.innerHTML = `${header(data.name,`${data.coder.name} · blinded independent coding`)}
      <div class="validation-progress"><span style="width:${100 * complete / data.items.length}%"></span></div>
      <div class="validation-coder-layout">
        <nav class="validation-nav"><b>${complete}/${data.items.length} saved</b>
          <div>${data.items.map((row,i) => `<button class="${i === index ? 'active' : ''} ${data.codes[row.id] ? 'done' : ''}" data-i="${i}">${i+1}</button>`).join('')}</div>
          <p class="small muted">The source of each passage is hidden. Do not infer that every passage contains a spark.</p>
        </nav>
        <section class="validation-item">
          <div class="card"><p class="eyebrow">Task</p><h2>${esc(item.task.title)}</h2><p>${esc(item.task.text)}</p>
            ${item.baseline ? `<details><summary>Participant's pre-conversation baseline</summary><p>${esc(item.baseline)}</p></details>` : ''}</div>
          <div class="validation-transcript">
            ${item.events.map(e => `<div class="validation-event ${e.target ? 'target' : 'context'}"><b>${esc(roleLabel(e.role,item.partner))}</b>${e.tool ? ` · ${esc(e.tool)}` : ''}<pre>${esc(e.text)}</pre></div>`).join('')}
          </div>
          <form id="validation-code" class="card">
            <h2>Code the highlighted episode</h2>
            <label class="field"><span>Transcript evidence of an insight</span><select name="evidence" required>
              <option value="">Choose…</option>${option('expressed','Expressed — participant articulates a new or changed understanding',saved.evidence)}
              ${option('possible','Possible — a plausible spark, but the participant does not clearly express the shift',saved.evidence)}
              ${option('insufficient','Insufficient — routine progress, repetition, or no meaningful insight evidence',saved.evidence)}</select></label>
            <label class="field"><span>Primary spark type</span><select name="primary_type" required><option value="">Choose…</option>
              ${types.map(k => option(k,typeLabel(k),saved.primary_type)).join('')}</select></label>
            <div class="two"><label class="field"><span>Downstream uptake</span><select name="uptake" required><option value="">Choose…</option>
              ${option('observed','Observed in this passage',saved.uptake)}${option('not_observed','Not observed',saved.uptake)}${option('unclear','Unclear',saved.uptake)}</select></label>
              <label class="field"><span>Proposed episode boundary</span><select name="span_quality" required><option value="">Choose…</option>
              ${option('adequate','Adequate',saved.span_quality)}${option('too_narrow','Too narrow',saved.span_quality)}${option('too_broad','Too broad',saved.span_quality)}${option('not_coherent','Not a coherent episode',saved.span_quality)}</select></label></div>
            <label class="field"><span>Your confidence</span><select name="confidence" required><option value="">Choose…</option>
              ${option('low','Low',saved.confidence)}${option('medium','Medium',saved.confidence)}${option('high','High',saved.confidence)}</select></label>
            <label class="field"><span>Brief rationale (optional)</span><textarea name="notes" rows="3">${esc(saved.notes || '')}</textarea></label>
            <div class="row"><button class="btn btn-primary">Save code</button>
              ${complete === data.items.length ? '<span class="ok-text">All items coded.</span>' : ''}</div>
          </form>
        </section>
      </div>`;
    document.querySelectorAll('[data-i]').forEach(b => b.onclick = () => { index = Number(b.dataset.i); render(); });
    $('#validation-code').onsubmit = async event => {
      event.preventDefault();
      const body = {...Object.fromEntries(new FormData(event.currentTarget)), item_id:item.id};
      const button = event.currentTarget.querySelector('button'); button.disabled = true;
      try {
        await api('POST',`/api/validations/${sampleId}/codes?token=${encodeURIComponent(token)}`,body);
        data.codes[item.id] = body;
        const next = data.items.findIndex((row,i) => i > index && !data.codes[row.id]);
        const any = data.items.findIndex(row => !data.codes[row.id]);
        index = next >= 0 ? next : any >= 0 ? any : index;
        render(); toast('Code saved');
      } catch (error) { button.disabled = false; toast(error.message); }
    };
  }
  render();
}

function metric(value) { return value == null ? '—' : Number(value).toFixed(2); }

async function resultsPage() {
  const data = await api('GET',`/api/validations/${sampleId}?view=researcher`);
  const total = data.total_items;
  const share = c => `${location.origin}/validation?sample=${sampleId}&token=${encodeURIComponent(c.token)}`;
  app.innerHTML = `${header(data.sample.name,'Researcher view · sources and model labels are unblinded here')}
    <div class="validation-results">
      <section class="card">
        <div class="row spread"><div><h2>Coding progress</h2><p class="small muted">Results are most interpretable after both coders finish.</p></div>
          <a class="btn btn-sm" href="/api/validations/${sampleId}/export.csv">Download CSV</a></div>
        ${data.coder_links.map(c => `<div class="share-link"><b>${esc(c.name)}</b><span>${data.progress[c.id]}/${total}</span><input readonly value="${esc(share(c))}"><button class="btn btn-sm" data-copy="${esc(share(c))}">Copy link</button></div>`).join('')}
      </section>
      <section class="card"><h2>Inter-coder agreement</h2>
        <p class="small muted">Raw agreement and Cohen's κ are shown separately. With a small pilot, κ is diagnostic—not a stable population estimate.</p>
        <table><thead><tr><th>Dimension</th><th>Double-coded n</th><th>Raw agreement</th><th>Cohen's κ</th></tr></thead><tbody>
          ${Object.entries(data.agreement).map(([field,m]) => `<tr><td>${esc(field.replace('_',' '))}</td><td>${m.n}</td><td>${metric(m.raw)}</td><td>${metric(m.cohen_kappa)}</td></tr>`).join('')}
        </tbody></table></section>
      <section class="card"><h2>Candidate support by hidden source</h2>
        <p class="small muted">“Supported” means both coders found expressed or possible transcript evidence. It does not establish participant-perceived value.</p>
        <table><thead><tr><th>Hidden source</th><th>Double-coded n</th><th>Supported by both</th><th>Both expressed</th><th>Evidence disagreements</th></tr></thead><tbody>
        ${Object.entries(data.candidate_support).map(([stratum,m]) => `<tr><td>${esc(stratum.replaceAll('_',' '))}</td><td>${m.double_coded_n}</td><td>${pct(m.both_transcript_supported,m.double_coded_n)}</td><td>${pct(m.both_expressed,m.double_coded_n)}</td><td>${m.evidence_disagreement}</td></tr>`).join('')}
        </tbody></table>
        <p class="small muted">Participant-confirmed independent reports overlapped by at least one judge passage: ${pct(data.reference.reports_with_passage_overlap,data.reference.confirmed_independent_reports)}. This is passage coverage, not recall.</p>
      </section>
      <section><h2>Unblinded item audit</h2>
        ${data.rows.map((row,i) => `<details class="card validation-audit"><summary>${i+1}. ${esc(row.stratum.replaceAll('_',' '))} · ${row.codes.length}/2 coded</summary>
          <p class="small muted">${esc(row.private.session_id)} · ${esc(row.private.start_event_id)} → ${esc(row.private.end_event_id)}</p>
          ${row.private.judge ? `<p><b>Judge:</b> ${esc(row.private.judge.insight_evidence)} · ${esc(typeLabel(row.private.judge.type))} · ${esc(row.private.judge.rationale || '')}</p>` : ''}
          ${row.codes.map(c => `<p><b>${esc(data.sample.coders.find(x => x.id === c.coder_id)?.name || c.coder_id)}:</b> ${esc(c.evidence)} · ${esc(typeLabel(c.primary_type))} · uptake ${esc(c.uptake)} · span ${esc(c.span_quality)}${c.notes ? `<br>${esc(c.notes)}` : ''}</p>`).join('') || '<p class="muted">Not coded yet.</p>'}
        </details>`).join('')}
      </section>
      <p class="small muted">${esc(data.method_note)} ${esc(data.interpretation)} This localhost researcher view is not an authorization boundary.</p>
    </div>`;
  document.querySelectorAll('[data-copy]').forEach(b => b.onclick = async () => {
    await navigator.clipboard.writeText(b.dataset.copy); toast('Coder link copied');
  });
}

try {
  if (!sampleId) await setupPage();
  else if (token) await coderPage();
  else if (researcher) await resultsPage();
  else location.href = '/validation';
} catch (error) {
  app.innerHTML = `${header('Validation workspace')}<div class="narrow"><div class="card"><h2>Could not load</h2><p>${esc(error.message)}</p></div></div>`;
}
