// One transcript-first review for integrated chat and external recordings.
import { api, icon, esc, $, $$, config, TYPE_COLOR, toast, fmtClock } from "/static/common.js";
import { annotationFormHTML, wireAnnotationForm, flushReviewDrafts } from "/static/review_forms.js";
import { BookmarkOutbox } from "/static/bookmarks.js";

const params = new URLSearchParams(location.search), sid = params.get('id');
const researcher = params.get('mode') === 'researcher';
let cfg, S, video = null, t0 = 0, duration = 60, openId = null, selectedStart = null, outbox, finishing = false;
const forms = new Map();
const eventById = id => S.events.find(e => e.id === id);
const point = m => m.reference_t ?? m.t;
const offset = t => t == null ? 0 : t - t0;
const independentDone = () => !!S.independent_review;
const judgeBusy = () => ['queued','running'].includes(S.judge?.status);
const showsSuggestions = () => S.config_snapshot?.study?.show_candidates_in_review ?? cfg.studies.find(study => study.key === S.study)?.show_candidates_in_review;
const liveMarks = () => S.marks.filter(m => !m.deleted);
const label = e => ({ user: 'You', assistant: S.runtime?.kind === 'codex' ? 'Codex' : S.runtime?.kind === 'claude' ? 'Claude agent' : 'Agent', partner: 'Partner', tool_call: 'Tool call', tool_result: 'Tool output' }[e.role] || e.role);

async function load() {
  S = await api('GET', `/api/sessions/${sid}${researcher ? '?view=researcher' : ''}`);
  t0 = S.recording?.started_t ?? S.events.find(e => e.t)?.t ?? S.marks[0]?.t ?? 0;
  duration = S.recording?.duration || Math.max(60, ...S.events.filter(e => e.t).map(e => offset(e.t) + 20));
}

async function prepareSuggestions() {
  // Explicit idempotent action, not a GET side effect. Catches up unfinished
  // reviews created before automatic generation was introduced.
  if (!researcher && S.phase === 'review' && cfg.judge.automatic) {
    await api('POST', `/api/sessions/${sid}/judge/ensure`, {});
    await load();
  }
}
function renderHeader() {
  $('#brand').innerHTML = `${icon('bulb')}<span>Spark review</span>`;
  $('#title').textContent = S.prompt.title;
  $('#meta').textContent = researcher ? 'Researcher view' : S.phase === 'done' ? 'Session complete' : independentDone() ? 'Independent review saved' : 'Independent review · model suggestions hidden';
  $('#actions').innerHTML = researcher
    ? `<button class="btn btn-sm" id="run-judge" ${cfg.judge.live && !judgeBusy() ? '' : 'disabled'}>Re-run LLM judge</button><a class="btn btn-sm" href="/api/sessions/${sid}/export.json">Export</a><a class="btn btn-sm" href="/researcher#${sid}">Dashboard</a>`
    : S.phase === 'review' ? `<button class="btn btn-primary btn-sm" id="finish" ${finishing ? 'disabled' : ''}>${independentDone() ? 'Finish and return home' : 'Finish independent review'}</button>`
    : `<a class="btn btn-sm" href="/">Return home</a>`;
  if ($('#finish')) $('#finish').onclick = finish;
  if ($('#run-judge')) $('#run-judge').onclick = async () => {
    if (!confirm('Send this session’s transcript and baseline to the LLM judge? Participant bookmarks and reviews are excluded.')) return;
    try { await api('POST', `/api/sessions/${sid}/judge`, {}); toast('LLM judge started'); await load(); renderSuggestions(); }
    catch (error) { toast(error.message); }
  };
}
function renderNext() {
  const box = $('#review-next');
  if (researcher || !independentDone()) { box.innerHTML = ''; return; }
  if (S.phase === 'done') {
    box.innerHTML = `<div class="review-next"><h2>Session complete</h2><p>Your responses are saved. Thank you for taking part.</p><a class="btn btn-primary btn-sm" href="/">Return home</a></div>`;
    return;
  }
  const showingSuggestions = showsSuggestions();
  box.innerHTML = `<div class="review-next"><h2>Independent review saved</h2>
    <p>Your answers are locked so later suggestions cannot change them. ${showingSuggestions ? 'Suggested moments below are optional. Review any that appear, or finish now.' : 'You can now finish the session.'}</p>
    <button class="btn btn-primary btn-sm" id="finish-inline">Finish and return home</button></div>`;
  $('#finish-inline').onclick = finish;
}

function renderPlayer() {
  if (!S.recording?.file) { $('#player').classList.add('hidden'); $('#follow').closest('label').classList.add('hidden'); return; }
  $('#player').classList.remove('hidden');
  $('#player').innerHTML = `<video id="video" controls preload="metadata" src="/api/sessions/${sid}/recording"></video>`;
  video = $('#video'); video.ontimeupdate = onTime;
  video.onloadedmetadata = () => { if (isFinite(video.duration)) duration = video.duration; if (params.get('t')) video.currentTime = Math.max(0, +params.get('t') - 15); renderTrack(); };
}
function seek(t) { if (video) video.currentTime = Math.max(0, Math.min(t, duration)); }
function renderTrack() {
  const percent = t => Math.max(0, Math.min(100, offset(t) / duration * 100));
  $('#track').innerHTML = `<div class="track-line"></div><div class="playhead" id="playhead"></div>` + liveMarks().map(m =>
    `<button class="pin" data-pin="${m.id}" style="left:${percent(point(m))}%;--tc:${TYPE_COLOR[m.review?.primary || m.quick_type] || 'var(--accent)'}" title="Bookmark ${fmtClock(offset(point(m)))}">${icon('bookmark')}</button>`).join('');
  $$('[data-pin]').forEach(b => b.onclick = () => openMoment(b.dataset.pin, true));
  $('#track').onclick = e => { if (!e.target.closest('[data-pin]')) { const box = $('#track').getBoundingClientRect(); seek((e.clientX - box.left) / box.width * duration); } };
  $('#legend').innerHTML = `<span>Your bookmarks · original click times retained</span><span class="grow muted">${fmtClock(duration)}</span>`;
}
function renderTranscript() {
  const tools = $('#show-tools').checked;
  const events = S.events.filter(e => ['user','assistant','partner'].includes(e.role) || tools && ['tool_call','tool_result'].includes(e.role));
  $('#transcript').innerHTML = events.map(e => `<div class="tr-ev ${e.role}" data-ev="${e.id}">
    <button class="tr-time" data-seek="${e.id}">${fmtClock(offset(e.t))}</button><div>
      <div class="r">${esc(label(e))}${e.meta?.superseded ? ' · archived evidence' : ''}</div>
      <details class="full-event" ${['user','assistant','partner'].includes(e.role) ? 'open' : ''}><summary>${esc(e.text.slice(0, 90))}${e.text.length > 90 ? '…' : ''}</summary><pre>${esc(e.text)}</pre>${e.output ? `<pre>${esc(e.output)}</pre>` : ''}</details>
      ${!researcher && !independentDone() ? `<div class="row"><button class="btn btn-sm btn-ghost" data-use="${e.id}">Use this message / add moment</button><button class="btn btn-sm btn-ghost" data-through="${e.id}">Extend passage through here</button></div>` : ''}
    </div></div>`).join('') || `<p class="muted">No transcript. You can still annotate timestamped bookmarks using the recording.</p>`;
  $$('[data-seek]').forEach(b => b.onclick = () => seek(offset(eventById(b.dataset.seek).t) - 5));
  $$('[data-use]').forEach(b => b.onclick = async () => { selectedStart = b.dataset.use; if (!openId) await addMoment(selectedStart); forms.get(openId)?.selectPassage(selectedStart); });
  $$('[data-through]').forEach(b => b.onclick = () => { const start = forms.get(openId)?.startId(); if (!openId || !start) return toast('Select a starting message first'); forms.get(openId)?.selectPassage(start, b.dataset.through); });
}
function onTime() {
  if (!video) return;
  $('#playhead').style.left = `${video.currentTime / duration * 100}%`;
  let current; for (const event of S.events) if (event.t && offset(event.t) <= video.currentTime) current = event;
  $$('.tr-ev').forEach(row => row.classList.toggle('active', row.dataset.ev === current?.id));
  if ($('#follow').checked && !video.paused) $(`.tr-ev[data-ev="${current?.id}"]`)?.scrollIntoView({ block: 'nearest' });
}
function exposures(m) {
  if (!m.exposure?.events?.length) return '<p class="small muted">No rendered-text snapshot was captured for this bookmark.</p>';
  return `<details class="exposure"><summary>What was rendered when you clicked</summary><p class="small muted">This is a display snapshot, not proof you read it. It is not replaced by later agent output.</p>${m.exposure.events.map(e => `<pre>${esc(e.rendered_text)}${e.rendered_output ? '\n' + esc(e.rendered_output) : ''}</pre>`).join('')}</details>`;
}
function updateIntro() {
  const marks = liveMarks(), confirmed = marks.filter(m => m.review?.status === 'submitted' && !m.review_dirty).length;
  $('#intro').innerHTML = `<h2>${researcher ? 'Participant moments' : 'Your moments'}</h2><p class="small muted">${confirmed}/${marks.length} reviews confirmed. ${independentDone() ? 'The independent pass is preserved separately from any suggested moments below.' : 'An unused idea can still be a spark. Unsure and No are valid answers. No bookmarks does not establish that no sparks occurred.'}</p>`;
}
function renderMoments() {
  updateIntro();
  for (const mark of liveMarks()) {
    if (forms.has(mark.id)) continue;
    const card = document.createElement('div'); card.className = `flag-card review-card ${mark.review?.status === 'submitted' ? 'saved' : ''}`; card.dataset.flag = mark.id;
    const source = mark.source === 'candidate_confirm' ? 'Added after model suggestion' : mark.source === 'review_add' ? 'Added retrospectively during review' : 'Bookmarked during interaction';
    card.innerHTML = `<div class="flag-head"><button class="time-chip" data-open>${icon('bookmark')}${fmtClock(offset(point(mark)))}</button><span class="small muted grow">${source}</span><button class="icon-btn" data-toggle aria-label="Open moment">${icon('chev')}</button></div><div class="flag-form">${exposures(mark)}${annotationFormHTML(cfg, mark, S.events)}</div>`;
    $('#flags').appendChild(card);
    $('[data-open]', card).onclick = () => openMoment(mark.id, true); $('[data-toggle]', card).onclick = () => openMoment(openId === mark.id ? null : mark.id);
    const replace = m => { const i = S.marks.findIndex(v => v.id === m.id); if (i >= 0) S.marks[i] = m; };
    const form = wireAnnotationForm(card, mark, { sid, events: S.events, locked: researcher || S.phase === 'done' || !!S.independent_review?.mark_ids.includes(mark.id),
      onSaved: m => { replace(m); updateIntro(); renderTrack(); }, onChanged: m => { m.review_dirty = true; replace(m); updateIntro(); } });
    forms.set(mark.id, form);
  }
  if (!openId && liveMarks().length && !independentDone()) openMoment(liveMarks().find(m => !m.review)?.id || liveMarks()[0].id);
  let add = $('#add-moment');
  if (!add && !researcher && !independentDone()) { add = document.createElement('button'); add.id = 'add-moment'; add.className = 'btn btn-sm'; add.textContent = 'Add a missed moment'; add.onclick = () => addMoment(null); $('#flags').after(add); }
  if (add) add.classList.toggle('hidden', independentDone());
}
function openMoment(id, jump = false) {
  openId = id; $$('.flag-card').forEach(card => card.classList.toggle('open', card.dataset.flag === id));
  const mark = S.marks.find(m => m.id === id); selectedStart = mark?.episode?.start_event_id || mark?.event_id || null;
  if (jump && mark) { seek(offset(point(mark)) - 15); $(`.flag-card[data-flag="${id}"]`)?.scrollIntoView({ block: 'nearest' }); }
}
async function addMoment(eid, candidate = null, answer = null) {
  if (independentDone() && !candidate) return;
  try {
    const mark = await api('POST', `/api/sessions/${sid}/marks`, { source: candidate ? 'candidate_confirm' : 'review_add', anchor: candidate ? 'suggested_passage' : eid ? 'review_passage' : 'review_timestamp', event_id: eid, client_t: Date.now(), reference_t: eid ? eventById(eid)?.t : video ? t0 + video.currentTime : null,
      ...(candidate ? { episode: { start_event_id: candidate.start_event_id || eid, end_event_id: candidate.end_event_id || eid } } : {}) });
    S.marks.push(mark); renderMoments(); openMoment(mark.id);
    if (candidate) { await api('PATCH', `/api/sessions/${sid}/candidates/${candidate.id}`, { participant_response: { answer, mark_id: mark.id } }); await load(); renderSuggestions(); }
  } catch (error) { toast(error.message); }
}
async function finish() {
  if (finishing) return;
  finishing = true;
  $('#finish').disabled = true;
  try {
    if (!await outbox.flush() || !await flushReviewDrafts()) throw new Error('Resolve unsynced drafts or bookmarks before continuing.');
    if (!independentDone()) {
      if (!confirm('Finish your independent review? These answers will be locked before you see model suggestions.')) return;
      await api('POST', `/api/sessions/${sid}/review/complete-independent`, {}); await load();
      for (const id of S.independent_review.mark_ids) forms.get(id)?.lock();
      openMoment(null);
      if (!showsSuggestions()) {
        await completeSession(); return;
      }
      renderHeader(); renderNext(); renderTranscript(); renderMoments(); renderSuggestions();
    } else {
      if (liveMarks().some(m => m.source === 'candidate_confirm' && (m.review_dirty || m.review?.status !== 'submitted'))) throw new Error('Confirm the suggested moments you added before finishing.');
      await completeSession();
    }
  } catch (error) { toast(error.message); }
  finally { finishing = false; renderHeader(); }
}
async function completeSession() {
  await api('POST', `/api/sessions/${sid}/advance`, { phase: 'review' });
  location.assign('/?completed=1');
}
function renderSuggestions() {
  const box = $('#suggestions');
  if (!researcher && (S.phase === 'done' || !showsSuggestions())) { box.innerHTML = ''; return; }
  if (!researcher && !independentDone()) { box.innerHTML = `<p class="small muted suggestion-lock">${cfg.judge.automatic && S.runtime?.kind !== 'demo' ? 'Suggested moments are prepared automatically in the background. ' : ''}They stay hidden until your independent review is complete.</p>`; return; }
  const candidates = S.candidates.filter(c => c.source === 'judge' && !c.participant_response && (researcher ||
    ['expressed','possible'].includes(c.insight_evidence) && c.quotes_verified && c.start_event_id && c.end_event_id));
  const status = S.judge?.status || 'not_run';
  const message = {
    queued: 'Preparing suggested moments automatically — waiting for a judge slot…',
    running: 'Preparing suggested moments automatically. You do not need to ask a researcher to start it.',
    waiting_for_transcript: S.external ? 'Waiting for an agent transcript. Attach the log to generate suggested moments.' : 'No conversation messages were captured, so suggestions cannot be generated. Your bookmarks are still saved.',
    disabled: 'Automatic judging is disabled for this session.',
    unavailable: 'Suggestions could not be prepared: ' + (S.judge?.error || 'The judge is not configured.'),
    error: 'Suggestion generation failed: ' + (S.judge?.error || 'Unknown error.'),
    not_run: 'Suggested moments have not been generated yet.',
    done: candidates.length ? 'These are suggestions, not confirmed sparks. Keep your independent answers separate.' :
      S.judge?.n === 0 ? 'The judge completed and found no candidate episodes. This does not mean you had no sparks.' :
      S.candidates.some(c => c.participant_response) ? 'No suggested moments left to review.' : 'The judge completed, but no suggestions meet the evidence requirements for participant review.'
  }[status] || 'Suggestion status unavailable.';
  box.innerHTML = `<h2>${researcher ? 'LLM judge candidates' : 'Optional suggested moments'}</h2><p class="small muted" data-judge-status="${status}">${esc(message)}</p>${!researcher && ['error','unavailable'].includes(status) ? '<button class="btn btn-sm" id="retry-judge">Retry generation</button>' : ''}` + candidates.map(c => `<div class="cand-card" data-candidate="${c.id}"><p>${esc(c.trigger_quote || eventById(c.trigger_event_id)?.text || '')}</p>${c.uptake_quote ? `<p class="small">Then you: ${esc(c.uptake_quote)}</p>` : ''}${researcher ? `<p class="small muted">${esc(c.insight_evidence || c.mechanism)} · ${esc(c.type)} · ${esc(c.strength || 'uncalibrated')}</p><p>${esc(c.rationale)}</p>` : `<div class="row"><button class="btn btn-sm" data-answer="yes">I noticed this at the time</button><button class="btn btn-sm" data-answer="retrospective">Only now</button><button class="btn btn-sm" data-answer="unsure">Unsure</button><button class="btn btn-sm" data-answer="no">No</button></div>`}</div>`).join('');
  if ($('#retry-judge')) $('#retry-judge').onclick = async () => {
    $('#retry-judge').disabled = true;
    try { await api('POST', `/api/sessions/${sid}/judge`, {}); await load(); renderHeader(); renderSuggestions(); }
    catch (error) { toast(error.message); $('#retry-judge')?.removeAttribute('disabled'); }
  };
  $$('[data-candidate]').forEach(card => $$('[data-answer]', card).forEach(button => button.onclick = async () => {
    const candidate = candidates.find(c => c.id === card.dataset.candidate), answer = button.dataset.answer;
    if (['yes','retrospective'].includes(answer)) return addMoment(candidate.trigger_event_id, candidate, answer);
    try { await api('PATCH', `/api/sessions/${sid}/candidates/${candidate.id}`, { participant_response: { answer } }); await load(); renderSuggestions(); } catch (error) { toast(error.message); }
  }));
}
cfg = await config(); await load(); outbox = new BookmarkOutbox(sid, () => {}); await outbox.flush(); await load();
try { await prepareSuggestions(); } catch (error) { toast('Could not start suggestion generation: ' + error.message); }
renderHeader(); renderPlayer(); renderTrack(); $('#show-tools').checked = true; renderTranscript(); renderMoments(); renderNext(); renderSuggestions();
$('#show-tools').onchange = renderTranscript;
setInterval(async () => { try { await load(); await prepareSuggestions(); renderHeader(); renderMoments(); renderNext(); renderSuggestions(); } catch {} }, 3000);
