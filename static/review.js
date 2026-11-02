// One transcript-first review for integrated chat and external recordings.
import { api, icon, esc, $, $$, config, toast, fmtClock, plainText } from "/static/common.js";
import { annotationFormHTML, wireAnnotationForm, flushReviewDrafts } from "/static/review_forms.js";
import { BookmarkOutbox } from "/static/bookmarks.js";
import { paintRanges, locate, spanMatches, markdownRanges, targetKey } from "/static/highlights.js";

const params = new URLSearchParams(location.search), sid = params.get('id');
const researcher = params.get('mode') === 'researcher';
let cfg, S, video = null, t0 = 0, duration = 60, openId = null, outbox, finishing = false;
const forms = new Map();
const eventById = id => S.events.find(e => e.id === id);
const point = m => m.reference_t ?? m.t;
const offset = t => t == null ? 0 : t - t0;
const independentDone = () => !!S.independent_review;
const judgeBusy = () => ['queued','running'].includes(S.judge?.status);
const showsSuggestions = () => S.config_snapshot?.study?.show_candidates_in_review ?? cfg.studies.find(study => study.key === S.study)?.show_candidates_in_review;
const liveMarks = () => S.marks.filter(m => !m.deleted);
const label = e => ({ user: 'You', assistant: S.runtime?.kind === 'codex' ? 'Codex' : S.runtime?.kind === 'claude' ? 'Claude agent' : 'Agent', partner: 'Partner', tool_call: 'Tool call', tool_result: 'Tool output' }[e.role] || e.role);

function setupPanelResize() {
  const layout = $('.rv-body'), divider = $('#review-divider');
  const key = `spark-review-panel-width:${sid}`;
  const minLeft = 360, minRight = 320, defaultRight = 420;
  let desiredWidth = defaultRight, dragging = null;
  try { desiredWidth = Number(localStorage.getItem(key)) || defaultRight; } catch { /* private mode */ }

  function paint() {
    const maxRight = Math.max(minRight, layout.clientWidth - divider.offsetWidth - minLeft);
    const right = Math.round(Math.min(Math.max(desiredWidth, minRight), maxRight));
    const left = layout.clientWidth - divider.offsetWidth - right;
    layout.style.setProperty('--review-right-width', `${right}px`);
    divider.setAttribute('aria-valuemin', String(minLeft));
    divider.setAttribute('aria-valuemax', String(layout.clientWidth - divider.offsetWidth - minRight));
    divider.setAttribute('aria-valuenow', String(left));
    divider.setAttribute('aria-valuetext', `Transcript ${left} pixels, reflections ${right} pixels`);
    return right;
  }
  function save() {
    desiredWidth = paint();
    try { localStorage.setItem(key, String(desiredWidth)); } catch { /* private mode */ }
  }
  divider.addEventListener('pointerdown', e => {
    if (e.button !== 0 || getComputedStyle(divider).display === 'none') return;
    dragging = { x: e.clientX, width: paint() };
    divider.setPointerCapture(e.pointerId);
    document.body.classList.add('review-resizing');
    e.preventDefault();
  });
  divider.addEventListener('pointermove', e => {
    if (!dragging) return;
    desiredWidth = dragging.width + dragging.x - e.clientX;
    paint();
  });
  const stop = () => {
    if (!dragging) return;
    dragging = null;
    document.body.classList.remove('review-resizing');
    save();
  };
  divider.addEventListener('pointerup', stop);
  divider.addEventListener('pointercancel', stop);
  divider.addEventListener('lostpointercapture', stop);
  divider.addEventListener('keydown', e => {
    const step = e.shiftKey ? 60 : 20;
    if (e.key === 'ArrowLeft') desiredWidth = paint() + step;
    else if (e.key === 'ArrowRight') desiredWidth = paint() - step;
    else if (e.key === 'Home') desiredWidth = layout.clientWidth - divider.offsetWidth - minLeft;
    else if (e.key === 'End') desiredWidth = minRight;
    else return;
    e.preventDefault(); save();
  });
  divider.addEventListener('dblclick', () => { desiredWidth = defaultRight; save(); });
  new ResizeObserver(paint).observe(layout);
  paint();
}

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
  $('#brand').innerHTML = `${icon('bookmark')}<span>Session review</span>`;
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
    box.innerHTML = `<div class="review-next"><h2>Session complete</h2><p>Your responses are saved. Thank you for taking part.</p><a class="btn btn-sm" href="/api/sessions/${sid}/export.zip" download>Download session ZIP</a> <a class="btn btn-primary btn-sm" href="/">Return home</a></div>`;
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
    `<button class="pin" data-pin="${m.id}" style="left:${percent(point(m))}%;--tc:var(--accent)" title="Bookmark ${fmtClock(offset(point(m)))}">${icon('bookmark')}</button>`).join('');
  $$('[data-pin]').forEach(b => b.onclick = () => openMoment(b.dataset.pin, true));
  $('#track').onclick = e => { if (!e.target.closest('[data-pin]')) { const box = $('#track').getBoundingClientRect(); seek((e.clientX - box.left) / box.width * duration); } };
  $('#legend').innerHTML = `<span>Your bookmarks · original click times retained</span><span class="grow muted">${fmtClock(duration)}</span>`;
}
function rangesFor(target, text) {
  const ranges = [];
  if (target.kind === 'event' && target.field === 'text')
    liveMarks().forEach(m => { const q = m.quote_selection;
      if (q?.event_id === target.event_id && spanMatches(text, q.start, q.end, q.text)) ranges.push({ start: q.start, end: q.end, cls: 'hl review-highlight' }); });
  if (target.kind === 'event' && target.field === 'text' && ['user','assistant','partner'].includes(eventById(target.event_id)?.role))
    ranges.push(...markdownRanges(text));
  const key = targetKey(target);
  candidateRanges().filter(r => r.key === key).forEach(r => ranges.push(r));
  return paintRanges(text, ranges);
}
function candidateRanges() {
  if (!researcher && !independentDone()) return [];
  return visibleCandidates().flatMap(c => [[c.trigger_event_id, c.trigger_quote], [c.uptake_event_id, c.uptake_quote]].map(([eid, quote]) => {
    const found = locate(eventById(eid)?.text, quote);
    return found && { key: targetKey({ kind: 'event', event_id: eid, field: 'text' }), ...found, cls: 'hl hl-candidate' };
  }).filter(Boolean));
}
const selectableAttrs = target => target.kind === 'event'
  ? `data-selectable-text data-kind="event" data-event-id="${esc(target.event_id)}" data-field="${target.field}"`
  : `data-selectable-text data-kind="writing" data-writing="${esc(target.writing)}"`;
function renderTranscript() {
  const tools = $('#show-tools').checked;
  // Re-rendering must not collapse events the participant opened.
  const opened = new Set($$('#transcript .tr-ev').filter(row => $('details', row)?.open).map(row => row.dataset.ev));
  const closed = new Set($$('#transcript .tr-ev').filter(row => $('details', row) && !$('details', row).open).map(row => row.dataset.ev));
  const events = S.events.filter(e => ['user','assistant','partner'].includes(e.role) || tools && ['tool_call','tool_result'].includes(e.role));
  const exchangeIds = new Set();
  for (const mark of liveMarks().filter(m => m.anchor === 'exchange' && m.episode)) {
    const first = events.findIndex(e => e.id === mark.episode.start_event_id);
    const last = events.findIndex(e => e.id === mark.episode.end_event_id);
    if (first >= 0 && last >= first) events.slice(first, last + 1).filter(e => !e.meta?.superseded).forEach(e => exchangeIds.add(e.id));
  }
  $('#transcript').innerHTML = events.map(e => {
    const textTarget = { kind: 'event', event_id: e.id, field: 'text' }, outputTarget = { kind: 'event', event_id: e.id, field: 'output' };
    return `<div class="tr-ev ${e.role}${exchangeIds.has(e.id) ? ' exchange-marked' : ''}" data-ev="${e.id}">
    <button class="tr-time" data-seek="${e.id}">${fmtClock(offset(e.t))}</button><div>
      <div class="r">${esc(label(e))}${e.meta?.voice ? ' · voice' : ''}${e.meta?.superseded ? ' · archived evidence' : ''}</div>
      <details class="full-event" ${opened.has(e.id) || !closed.has(e.id) && ['user','assistant','partner'].includes(e.role) ? 'open' : ''}><summary>${esc(plainText(e.text).slice(0, 90))}${plainText(e.text).length > 90 ? '…' : ''}</summary><pre ${selectableAttrs(textTarget)}>${rangesFor(textTarget, e.text)}</pre>${e.output ? `<pre>${rangesFor(outputTarget, e.output)}</pre>` : ''}</details>
      ${!researcher && !independentDone() ? `<div class="row"><button class="btn btn-sm btn-ghost" data-add-here="${e.id}">Add a moment here</button></div>` : ''}
    </div></div>`;
  }).join('') || `<p class="muted">No transcript. You can still annotate timestamped bookmarks using the recording.</p>`;
  $$('[data-seek]').forEach(b => b.onclick = () => seek(offset(eventById(b.dataset.seek).t) - 5));
  $$('[data-add-here]').forEach(b => b.onclick = () => addMoment(b.dataset.addHere));
}
function renderWritings() {
  const words = S.annotation_settings.wording;
  const names = ['pre', 'post'].filter(n => S.writing?.[n]?.text);
  $('#writings').innerHTML = names.length ? `<h3>${esc(words.writing_title)}</h3>` + names.map(n => {
    const target = { kind: 'writing', writing: n };
    return `<div class="writing-block"><div class="r">${esc(words.writing_labels[n] || n)}</div><pre>${rangesFor(target, S.writing[n].text)}</pre></div>`;
  }).join('') : '';
}
function selectedText() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return null;
  const range = selection.getRangeAt(0);
  const pre = (range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement)?.closest('pre[data-selectable-text]');
  if (!pre || !pre.contains(range.endContainer)) return null;
  const target = { kind: 'event', event_id: pre.dataset.eventId, field: 'text' };
  const source = eventById(target.event_id)?.text;
  if (source == null) return null;
  const before = document.createRange(); before.selectNodeContents(pre); before.setEnd(range.startContainer, range.startOffset);
  const start = Array.from(before.toString()).length, end = start + Array.from(range.toString()).length;
  const text = Array.from(source).slice(start, end).join('');
  if (end <= start || end - start > 2000 || text !== range.toString()) return null;
  return { target, start, end, text, rect: range.getBoundingClientRect() };
}
function selectionActions(selected) {
  if (independentDone()) return [];
  const actions = [];
  const open = openId && S.marks.find(m => m.id === openId && !m.deleted);
  const form = forms.get(openId);
  const quote = { event_id: selected.target.event_id, start: selected.start, end: selected.end, text: selected.text };
  if (open && form) actions.push(['Use for open moment', () => form.selectQuote(quote)]);
  actions.push(['Mark as new moment', () => addMoment(selected.target.event_id, null, null, quote)]);
  return actions;
}
function onSelect() {
  setTimeout(() => {
    $('.review-selection-actions')?.remove();
    if (researcher || S.phase !== 'review') return;
    const selected = selectedText();
    if (!selected) return;
    const actions = selectionActions(selected);
    if (!actions.length) return;
    const box = document.createElement('div'); box.className = 'review-selection-actions';
    for (const [text, run] of actions) {
      const button = document.createElement('button'); button.className = 'btn btn-sm'; button.textContent = text;
      button.onmousedown = e => e.preventDefault();
      button.onclick = async () => { box.remove(); window.getSelection()?.removeAllRanges(); try { await run(); } catch (error) { toast(error.message); } };
      box.appendChild(button);
    }
    document.body.appendChild(box);
    box.style.left = `${Math.max(10, Math.min(window.innerWidth - box.offsetWidth - 10, selected.rect.left))}px`;
    box.style.top = `${Math.max(10, selected.rect.top - box.offsetHeight - 8)}px`;
  }, 0);
}
$('#transcript').addEventListener('mouseup', onSelect);
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
function originalMark(m) {
  const original = m.original_anchor || m;
  const brief = text => { const value = plainText(text || '').replace(/\s+/g, ' ').trim(); return esc(value.slice(0, 180)) + (value.length > 180 ? '…' : ''); };
  const sourceLabel = (live, review) => m.source === 'candidate_confirm' ? 'Suggested passage added during review' : m.source === 'review_add' ? review : live;
  const selected = original.quote_selection?.text || original.quote;
  if (selected) return `<div class="mark-source"><div class="small muted">${sourceLabel('Text you marked during the conversation', 'Text selected during review')}</div><blockquote>${brief(selected)}</blockquote></div>`;
  const episode = original.episode;
  if (episode) {
    const a = S.events.findIndex(e => e.id === episode.start_event_id), b = S.events.findIndex(e => e.id === episode.end_event_id);
    const count = a >= 0 && b >= a ? S.events.slice(a, b + 1).filter(e => !e.meta?.superseded && ['user', 'assistant', 'partner', 'tool_call', 'tool_result'].includes(e.role)).length : 0;
    const first = eventById(episode.start_event_id), last = eventById(episode.end_event_id);
    return `<div class="mark-source"><div class="small muted">${sourceLabel('Exchange you marked during the conversation', 'Exchange selected during review')}${count ? ` · ${count} messages` : ''}</div>
      <p>From “${brief(first?.text)}”${first?.id !== last?.id ? ` through “${brief(last?.text)}”` : ''}</p></div>`;
  }
  const event = eventById(original.event_id);
  if (event) return `<div class="mark-source"><div class="small muted">${sourceLabel('Message you marked during the conversation', 'Message selected during review')}</div><blockquote>${brief(event.text)}</blockquote></div>`;
  return `<div class="mark-source"><div class="small muted">${m.source === 'review_add' ? 'Moment added during review' : 'Point bookmarked during the conversation'}</div>
    <p>No specific message was selected. You can leave it that way.</p></div>`;
}
function updateIntro() {
  const marks = liveMarks(), confirmed = marks.filter(m => m.review?.status === 'submitted' && !m.review_dirty).length;
  $('#intro').innerHTML = `<h2>${researcher ? 'Participant moments' : 'Your moments'}</h2><p class="small muted">${confirmed}/${marks.length} reviews confirmed. ${independentDone() ? 'The independent pass is preserved separately from any suggested moments below.' : 'It is fine if you did not use an idea, or if a bookmark was not an idea at all. Unsure and No are valid answers.'}</p>`;
}
function renderMoments() {
  updateIntro();
  const activeIds = new Set(liveMarks().map(m => m.id));
  for (const [id, form] of forms) {
    if (activeIds.has(id)) continue;
    form.dispose(); forms.delete(id);
    $(`.flag-card[data-flag="${id}"]`)?.remove();
    if (openId === id) openId = null;
  }
  for (const mark of liveMarks()) {
    if (forms.has(mark.id)) continue;
    const card = document.createElement('div'); card.className = `flag-card review-card ${mark.review?.status === 'submitted' ? 'saved' : ''}`; card.dataset.flag = mark.id;
    const source = mark.anchor === 'exchange' ? 'Exchange marked during interaction' : mark.source === 'candidate_confirm' ? 'Added after model suggestion' : mark.source === 'review_add' ? 'Added retrospectively during review' : 'Bookmarked during interaction';
    card.innerHTML = `<div class="flag-head"><button class="time-chip" data-open>${icon('bookmark')}${fmtClock(offset(point(mark)))}</button><span class="small muted grow">${source}</span>${!researcher && S.phase === 'review' && !independentDone() ? '<button class="btn btn-sm btn-ghost" data-remove-moment>Unmark</button>' : ''}<button class="icon-btn" data-toggle aria-label="Open moment">${icon('chev')}</button></div><div class="flag-form">${originalMark(mark)}${researcher ? exposures(mark) : ''}${annotationFormHTML(cfg, mark, S.events)}</div>`;
    $('#flags').appendChild(card);
    $('[data-open]', card).onclick = () => openMoment(mark.id, true); $('[data-toggle]', card).onclick = () => openMoment(openId === mark.id ? null : mark.id);
    if ($('[data-remove-moment]', card)) $('[data-remove-moment]', card).onclick = () => removeMoment(mark.id);
    const replace = m => { const i = S.marks.findIndex(v => v.id === m.id); if (i >= 0) S.marks[i] = m; };
    const form = wireAnnotationForm(card, mark, { sid, events: S.events, locked: researcher || S.phase === 'done' || !!S.independent_review?.mark_ids.includes(mark.id),
      onSaved: m => { replace(m); updateIntro(); renderTrack(); renderTranscript(); }, onChanged: m => { m.review_dirty = true; replace(m); updateIntro(); } });
    forms.set(mark.id, form);
  }
  if (!openId && liveMarks().length && !independentDone()) openMoment(liveMarks().find(m => !m.review)?.id || liveMarks()[0].id);
  let add = $('#add-moment');
  if (!add && !researcher && !independentDone()) { add = document.createElement('button'); add.id = 'add-moment'; add.className = 'btn btn-sm'; add.textContent = 'Add a missed moment'; add.onclick = () => addMoment(null); $('#flags').after(add); }
  if (add) add.classList.toggle('hidden', independentDone());
}
function openMoment(id, jump = false) {
  openId = id; $$('.flag-card').forEach(card => card.classList.toggle('open', card.dataset.flag === id));
  const mark = S.marks.find(m => m.id === id);
  if (jump && mark) { seek(offset(point(mark)) - 15); $(`.flag-card[data-flag="${id}"]`)?.scrollIntoView({ block: 'nearest' }); }
}
async function removeMoment(id) {
  if (researcher || independentDone() || S.phase !== 'review') return;
  try {
    if (forms.get(id) && !await forms.get(id).flush()) throw new Error('Save or resolve this moment’s draft before unmarking it.');
    await api('PATCH', `/api/sessions/${sid}/marks/${id}`, { deleted: true });
    await load();
    renderTrack(); renderTranscript(); renderWritings(); renderMoments();
    toast('Moment unmarked');
  } catch (error) { toast(error.message); }
}
async function addMoment(eid, candidate = null, answer = null, quoteSelection = null) {
  if (independentDone() && !candidate) return;
  if (candidate?.trigger_event_id && candidate.trigger_quote) {
    const span = locate(eventById(candidate.trigger_event_id)?.text, candidate.trigger_quote);
    if (span) quoteSelection = { event_id: candidate.trigger_event_id, ...span, text: candidate.trigger_quote };
  }
  if (!candidate && eid) {
    const existing = liveMarks().find(m => {
      if (m.event_id !== eid) return false;
      const prior = m.quote_selection;
      if (quoteSelection) return prior && prior.start === quoteSelection.start && prior.end === quoteSelection.end;
      return !prior && ['message', 'review_passage'].includes(m.anchor);
    });
    if (existing) { openMoment(existing.id, true); toast('This passage is already marked'); return existing; }
  }
  try {
    const mark = await api('POST', `/api/sessions/${sid}/marks`, { source: candidate ? 'candidate_confirm' : 'review_add', anchor: candidate ? 'suggested_passage' : eid ? 'review_passage' : 'review_timestamp', event_id: eid, client_t: Date.now(), reference_t: eid ? eventById(eid)?.t : video ? t0 + video.currentTime : null,
      ...(candidate ? { episode: { start_event_id: candidate.start_event_id || eid, end_event_id: candidate.end_event_id || eid } } : {}),
      ...(quoteSelection ? { quote_selection: { event_id: quoteSelection.event_id, start: quoteSelection.start, end: quoteSelection.end, text: quoteSelection.text } } : {}) });
    S.marks.push(mark); renderMoments(); openMoment(mark.id); if (quoteSelection) renderTranscript();
    if (candidate) { await api('PATCH', `/api/sessions/${sid}/candidates/${candidate.id}`, { participant_response: { answer, mark_id: mark.id } }); await load(); renderSuggestions(); renderTranscript(); }
    return mark;
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
      renderHeader(); renderNext(); renderTranscript(); renderWritings(); renderMoments(); renderSuggestions();
    } else {
      if (liveMarks().some(m => m.source === 'candidate_confirm' && (m.review_dirty || m.review?.status !== 'submitted'))) throw new Error('Confirm the suggested moments you added before finishing.');
      await completeSession();
    }
  } catch (error) { toast(error.message); }
  finally { finishing = false; renderHeader(); }
}
async function completeSession() {
  await api('POST', `/api/sessions/${sid}/advance`, { phase: 'review' });
  location.assign(`/?completed=1&archive=${encodeURIComponent(sid)}`);
}
function visibleCandidates() {
  return S.candidates.filter(c => c.source === 'judge' && !c.participant_response && (researcher ||
    ['expressed','possible'].includes(c.insight_evidence) && c.quotes_verified && c.start_event_id && c.end_event_id));
}
function renderSuggestions() {
  const box = $('#suggestions');
  if (!researcher && (S.phase === 'done' || !showsSuggestions())) { box.innerHTML = ''; return; }
  if (!researcher && !independentDone()) { box.innerHTML = `<p class="small muted suggestion-lock">${cfg.judge.automatic && S.runtime?.kind !== 'demo' ? 'Suggested moments are prepared automatically in the background. ' : ''}They stay hidden until your independent review is complete.</p>`; return; }
  const candidates = visibleCandidates();
  const status = S.judge?.status || 'not_run';
  const message = {
    queued: 'Preparing suggested moments automatically — waiting for a judge slot…',
    running: 'Preparing suggested moments automatically. You do not need to ask a researcher to start it.',
    waiting_for_transcript: S.external ? 'Waiting for an agent transcript. Attach the log to generate suggested moments.' : 'No conversation messages were captured, so suggestions cannot be generated. Your bookmarks are still saved.',
    disabled: 'Automatic judging is disabled for this session.',
    unavailable: 'Suggestions could not be prepared: ' + (S.judge?.error || 'The judge is not configured.'),
    error: 'Suggestion generation failed: ' + (S.judge?.error || 'Unknown error.'),
    not_run: 'Suggested moments have not been generated yet.',
    done: candidates.length ? 'These are possible moments, not confirmed insights. Keep your independent answers separate.' :
      S.judge?.n === 0 ? 'The judge completed and found no candidate episodes. This does not mean no meaningful moments occurred.' :
      S.candidates.some(c => c.participant_response) ? 'No suggested moments left to review.' : 'The judge completed, but no suggestions meet the evidence requirements for participant review.'
  }[status] || 'Suggestion status unavailable.';
  box.innerHTML = `<h2>${researcher ? 'LLM judge candidates' : 'Optional suggested moments'}</h2><p class="small muted" data-judge-status="${status}">${esc(message)}</p>${!researcher && ['error','unavailable'].includes(status) ? '<button class="btn btn-sm" id="retry-judge">Retry generation</button>' : ''}` + candidates.map(c => `<div class="cand-card" data-candidate="${c.id}"><p>${esc(plainText(c.trigger_quote || eventById(c.trigger_event_id)?.text || ''))}</p>${c.uptake_quote ? `<p class="small">Then you: ${esc(plainText(c.uptake_quote))}</p>` : ''}${researcher ? `<p class="small muted">${esc(c.insight_evidence || c.mechanism)} · ${esc(c.type)} · ${esc(c.strength || 'uncalibrated')}</p><p>${esc(c.rationale)}</p>` : `<div class="row"><button class="btn btn-sm" data-answer="yes">I noticed this at the time</button><button class="btn btn-sm" data-answer="retrospective">Only now</button><button class="btn btn-sm" data-answer="unsure">Unsure</button><button class="btn btn-sm" data-answer="no">No</button></div>`}</div>`).join('');
  if ($('#retry-judge')) $('#retry-judge').onclick = async () => {
    $('#retry-judge').disabled = true;
    try { await api('POST', `/api/sessions/${sid}/judge`, {}); await load(); renderHeader(); renderSuggestions(); }
    catch (error) { toast(error.message); $('#retry-judge')?.removeAttribute('disabled'); }
  };
  $$('[data-candidate]').forEach(card => $$('[data-answer]', card).forEach(button => button.onclick = async () => {
    const candidate = candidates.find(c => c.id === card.dataset.candidate), answer = button.dataset.answer;
    if (['yes','retrospective'].includes(answer)) return addMoment(candidate.trigger_event_id, candidate, answer);
    try { await api('PATCH', `/api/sessions/${sid}/candidates/${candidate.id}`, { participant_response: { answer } }); await load(); renderSuggestions(); renderTranscript(); } catch (error) { toast(error.message); }
  }));
}
cfg = await config(); await load(); outbox = new BookmarkOutbox(sid, () => {}); await outbox.flush(); await load();
try { await prepareSuggestions(); } catch (error) { toast('Could not start suggestion generation: ' + error.message); }
setupPanelResize();
renderHeader(); renderPlayer(); renderTrack(); $('#show-tools').checked = true; renderTranscript(); renderWritings(); renderMoments(); renderNext(); renderSuggestions();
$('#show-tools').onchange = renderTranscript;
setInterval(async () => { try { await load(); await prepareSuggestions(); renderHeader(); renderMoments(); renderNext(); renderSuggestions(); } catch {} }, 3000);
