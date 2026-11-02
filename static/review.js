// One transcript-first review for integrated chat and external recordings.
import { api, icon, esc, $, $$, config, toast, fmtClock, plainText } from "/static/common.js";
import { annotationFormHTML, wireAnnotationForm, flushReviewDrafts } from "/static/review_forms.js";
import { BookmarkOutbox } from "/static/bookmarks.js";
import { Highlighter, paintRanges, locate, spanMatches, markdownRanges, targetKey } from "/static/highlights.js";

const params = new URLSearchParams(location.search), sid = params.get('id');
const researcher = params.get('mode') === 'researcher';
let highlighter, cfg, S, voice = null, step = null, video = null, t0 = 0, duration = 60, openId = null, selectedStart = null, outbox, finishing = false;
const forms = new Map();
const eventById = id => S.events.find(e => e.id === id);
const point = m => m.reference_t ?? m.t;
const offset = t => t == null ? 0 : t - t0;
const independentDone = () => !!S.independent_review;
const judgeBusy = () => ['queued','running'].includes(S.judge?.status);
const showsSuggestions = () => S.config_snapshot?.study?.show_candidates_in_review ?? cfg.studies.find(study => study.key === S.study)?.show_candidates_in_review;
const liveMarks = () => S.marks.filter(m => !m.deleted);
const reviewed = m => m.review?.status === 'submitted' && !m.review_dirty;
// Guided steps change layout only; every control and save path is the same as the single page.
const guided = () => !researcher && !!S.annotation_settings.review_steps?.enabled;
const stepWords = () => S.annotation_settings.review_steps.wording;
const voiceStep = () => S.interaction_mode === 'voice' && !!S.voice_settings?.enabled;
const stepKeys = () => ['moments', S.annotation_settings.highlights.enabled && 'highlights', showsSuggestions() && 'suggestions', voiceStep() && 'voice'].filter(Boolean);
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
  $('#brand').innerHTML = `${icon('bookmark')}<span>Session review</span>`;
  $('#title').textContent = S.prompt.title;
  $('#meta').textContent = researcher ? 'Researcher view' : S.phase === 'done' ? 'Session complete' : independentDone() ? 'Independent review saved' : 'Independent review · model suggestions hidden';
  $('#actions').innerHTML = researcher
    ? `<button class="btn btn-sm" id="run-judge" ${cfg.judge.live && !judgeBusy() ? '' : 'disabled'}>Re-run LLM judge</button><a class="btn btn-sm" href="/api/sessions/${sid}/export.json">Export</a><a class="btn btn-sm" href="/researcher#${sid}">Dashboard</a>`
    : guided() ? ''
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
  if (researcher || !independentDone() || guided() && S.phase !== 'done') { box.innerHTML = ''; return; }
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

function renderSteps() {
  const rv = $('#rv');
  if (!guided()) { delete rv.dataset.guided; return; }
  const words = stepWords(), keys = stepKeys(), locked = independentDone();
  if (!keys.includes(step)) step = keys[0];
  rv.dataset.guided = ''; rv.dataset.step = step;
  const done = {
    moments: locked || liveMarks().length > 0 && liveMarks().every(reviewed),
    highlights: locked,
    suggestions: locked && !visibleCandidates().length && !liveMarks().some(m => m.source === 'candidate_confirm' && !reviewed(m)),
    voice: S.phase === 'done',
  };
  $('#steps').classList.remove('hidden');
  $('#steps').innerHTML = keys.map((key, i) => `<button type="button" class="rv-step-tab ${key === step ? 'on' : ''} ${done[key] && key !== step ? 'done' : ''}" data-step="${key}" ${key === step ? 'aria-current="step"' : ''}>
    <b>${done[key] && key !== step ? icon('check') : i + 1}</b>${esc(words.steps[key].label)}</button>`).join('');
  $$('[data-step]', $('#steps')).forEach(b => b.onclick = () => setStep(b.dataset.step));
  const todo = S.phase === 'done' ? words.done
    : locked && ['moments', 'highlights'].includes(step) ? words.view_only
    : step === 'moments' && done.moments ? words.all_confirmed
    : words.steps[step].todo;
  $('#todo').classList.remove('hidden');
  $('#todo').innerHTML = `${icon('info')}<span>${esc(todo)}</span>`;
  $('#select-hint').textContent = words.select_hint;
  $('#select-hint').classList.toggle('hidden', locked || S.phase !== 'review');
  renderStepActions();
  renderMomentNav();
}
function setStep(key) {
  step = key;
  try { sessionStorage.setItem(`review-step:${sid}`, key); } catch {}
  if (key === 'moments' && !openId && liveMarks().length) openMoment((liveMarks().find(m => !reviewed(m)) || liveMarks()[0]).id);
  if (key === 'highlights') $('#writings details')?.setAttribute('open', '');
  renderSteps();
  $('.rv-right').scrollTop = 0;
}
function renderStepActions() {
  const box = $('#step-actions');
  if (!guided() || S.phase !== 'review') { box.innerHTML = ''; return; }
  const words = stepWords(), keys = stepKeys(), i = keys.indexOf(step), next = keys[i + 1], locked = independentDone();
  // The lock sits at the end of the last step before suggestions; after it, the
  // last step finishes the session. Both run the same finish() as before.
  const lockStep = keys.includes('highlights') ? 'highlights' : 'moments';
  let primary;
  if (!locked && step === lockStep) primary = `<button class="btn btn-primary btn-sm" id="finish" ${finishing ? 'disabled' : ''}>${esc(words.lock)}</button>`;
  else if (locked && !next) primary = `<button class="btn btn-primary btn-sm" id="finish" ${finishing ? 'disabled' : ''}>${esc(words.finish)}</button>`;
  else if (next) primary = `<button class="btn btn-sm" data-go="${next}">${esc(words.next_step.replace('{label}', words.steps[next].label))}</button>`;
  box.innerHTML = `${i > 0 ? `<button class="btn btn-sm btn-ghost" data-go="${keys[i - 1]}">${esc(words.back)}</button>` : '<span></span>'}${primary || ''}`;
  $$('[data-go]', box).forEach(b => b.onclick = () => setStep(b.dataset.go));
  if ($('#finish', box)) $('#finish', box).onclick = finish;
}
function renderMomentNav() {
  const box = $('#moment-nav');
  if (!guided()) { box.innerHTML = ''; return; }
  const words = stepWords(), marks = liveMarks(), i = marks.findIndex(m => m.id === openId);
  if (!marks.length) { box.innerHTML = `<p class="small muted">${esc(words.no_moments)}</p>`; return; }
  if (i < 0) { box.innerHTML = ''; return; }
  box.innerHTML = `<button class="btn btn-sm btn-ghost" data-moment-step="-1" ${i === 0 ? 'disabled' : ''}>${icon('chevLeft')}${esc(words.previous)}</button>
    <b class="grow">${esc(words.moment_of.replace('{i}', i + 1).replace('{n}', marks.length))}</b>
    <button class="btn btn-sm btn-ghost" data-moment-step="1" ${i === marks.length - 1 ? 'disabled' : ''}>${esc(words.next)}${icon('chevRight')}</button>`;
  $$('[data-moment-step]', box).forEach(b => b.onclick = () => { const m = marks[i + +b.dataset.momentStep]; if (m) openMoment(m.id); });
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
  highlighter?.forTarget(key).filter(h => spanMatches(text, h.start, h.end, h.text))
    .forEach(h => ranges.push({ start: h.start, end: h.end, cls: h.mark_id ? 'hl hl-moment' : 'hl hl-free', id: h.id }));
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
// Whether the study's settings allow a highlight record on this text.
function highlightable(target) {
  const settings = S.annotation_settings.highlights, allowed = settings.targets;
  if (!settings.enabled) return false;
  if (target.kind === 'writing') return allowed.writing.includes(target.writing);
  return allowed.event_fields.includes(target.field) && allowed.event_roles.includes(eventById(target.event_id)?.role);
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
  $('#transcript').innerHTML = events.map(e => {
    const textTarget = { kind: 'event', event_id: e.id, field: 'text' }, outputTarget = { kind: 'event', event_id: e.id, field: 'output' };
    const conversation = ['user','assistant','partner'].includes(e.role);
    if (guided() && conversation) return `<div class="tr-ev tr-bubble ${e.role}" data-ev="${e.id}">
    <div class="tr-meta"><button class="tr-time" data-seek="${e.id}">${fmtClock(offset(e.t))}</button><span class="r">${esc(label(e))}${e.meta?.voice ? ' · voice' : ''}${e.meta?.superseded ? ' · archived evidence' : ''}</span></div>${voiceChips(e)}
      <pre class="bubble" ${selectableAttrs(textTarget)}>${rangesFor(textTarget, e.text)}</pre>
      ${!independentDone() ? `<div class="row tr-actions"><button class="btn btn-sm btn-ghost" data-use="${e.id}">Use this message / add moment</button><button class="btn btn-sm btn-ghost" data-through="${e.id}">Extend passage through here</button></div>` : ''}
    </div>`;
    return `<div class="tr-ev ${e.role}" data-ev="${e.id}">
    <button class="tr-time" data-seek="${e.id}">${fmtClock(offset(e.t))}</button><div>
      <div class="r">${esc(label(e))}${e.meta?.voice ? ' · voice' : ''}${e.meta?.superseded ? ' · archived evidence' : ''}</div>${voiceChips(e)}
      <details class="full-event" ${opened.has(e.id) || !closed.has(e.id) && ['user','assistant','partner'].includes(e.role) ? 'open' : ''}><summary>${esc(plainText(e.text).slice(0, 90))}${plainText(e.text).length > 90 ? '…' : ''}</summary><pre ${selectableAttrs(textTarget)}>${rangesFor(textTarget, e.text)}</pre>${e.output ? `<pre ${highlightable(outputTarget) ? selectableAttrs(outputTarget) : ''}>${rangesFor(outputTarget, e.output)}</pre>` : ''}</details>
      ${!researcher && !independentDone() ? `<div class="row"><button class="btn btn-sm btn-ghost" data-use="${e.id}">Use this message / add moment</button><button class="btn btn-sm btn-ghost" data-through="${e.id}">Extend passage through here</button></div>` : ''}
    </div></div>`;
  }).join('') || `<p class="muted">No transcript. You can still annotate timestamped bookmarks using the recording.</p>`;
  $$('[data-seek]').forEach(b => b.onclick = () => seek(offset(eventById(b.dataset.seek).t) - 5));
  $$('[data-use]').forEach(b => b.onclick = async () => { selectedStart = b.dataset.use; if (!openId) await addMoment(selectedStart); forms.get(openId)?.selectPassage(selectedStart); });
  $$('[data-through]').forEach(b => b.onclick = () => { const start = forms.get(openId)?.startId(); if (!openId || !start) return toast('Select a starting message first'); forms.get(openId)?.selectPassage(start, b.dataset.through); });
}
// ---------------------------------------------------------------- how you spoke
// Every value is relative to the participant's own usual turn in this session.
async function loadVoice() {
  if (S.interaction_mode !== 'voice') return;
  try { voice = await api('GET', `/api/sessions/${sid}/voice-profile${researcher ? '?view=researcher' : ''}`); }
  catch (error) { voice = null; toast('Could not load voice measures: ' + error.message); }
  renderVoice(); renderTranscript();
}
const voiceWords = () => S.voice_settings.wording;
const flagText = f => (voiceWords().flags[f.key] || f.key).replace('{value}', f.value ?? '');
function voiceTurnFor(eventId) { return voice?.visible ? voice.turns.find(r => r.event_ids[0] === eventId) : null; }
function voiceChips(e) {
  const turn = voiceTurnFor(e.id);
  return turn?.flags.length ? `<div class="voice-chips">${turn.flags.map(f => `<span class="voice-chip">${esc(flagText(f))}</span>`).join('')}</div>` : '';
}
function jumpToTurn(turn) {
  const row = $(`.tr-ev[data-ev="${turn.event_ids[0]}"]`);
  row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  row?.classList.add('hl-flash'); setTimeout(() => row?.classList.remove('hl-flash'), 900);
}
const preview = text => { const n = S.voice_settings.display.preview_chars, plain = plainText(text); return plain.length > n ? plain.slice(0, n) + '…' : plain; };
function voiceChart() {
  const words = voiceWords(), turns = voice.turns, notable = new Set(voice.notable);
  const rows = Object.keys(words.rows).filter(key => turns.some(r => r.relative[key] != null));
  if (!rows.length) return '';
  const { column_px: col, gap_px: gap, row_px: rowH, tick_px: tickH } = S.voice_settings.display.chart;
  const width = turns.length * (col + gap), height = tickH + rows.length * rowH;
  // A moment is shown on the participant turn it fell in or, if the agent was
  // speaking, on the participant's reply right after it.
  const momentTurns = new Set(liveMarks().map(m => {
    const at = point(m) * 1000, i = turns.findIndex(r => r.end_ms >= at);
    return i < 0 ? null : i;
  }).filter(i => i != null));
  let svg = '';
  rows.forEach((key, ri) => {
    const values = turns.map(r => r.relative[key]), max = Math.max(...values.map(v => Math.abs(v ?? 0)), 1e-9);
    const mid = tickH + ri * rowH + rowH / 2, half = rowH / 2 - 3;
    svg += `<line x1="0" x2="${width}" y1="${mid}" y2="${mid}" class="vc-axis"/>`;
    values.forEach((v, i) => {
      if (v == null) return;
      const h = Math.max(1, Math.abs(v) / max * half), x = i * (col + gap);
      svg += `<rect x="${x}" y="${v > 0 ? mid - h : mid}" width="${col}" height="${h}" rx="2" class="${notable.has(i) ? 'vc-bar vc-strong' : 'vc-bar'}"/>`;
    });
  });
  momentTurns.forEach(i => { svg += `<rect x="${i * (col + gap) + col / 2 - 1.5}" y="0" width="3" height="${tickH - 2}" rx="1.5" class="vc-moment"/>`; });
  turns.forEach((r, i) => { svg += `<rect x="${i * (col + gap) - gap / 2}" y="0" width="${col + gap}" height="${height}" class="vc-hit" data-voice-turn="${i}"/>`; });
  return `<div class="vc"><div class="vc-labels" style="padding-top:${tickH}px">${rows.map(key => `<div style="height:${rowH}px">${esc(words.rows[key])}</div>`).join('')}</div>
    <div class="vc-plot"><svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(words.panel_title)}">${svg}</svg></div></div>
    <p class="small muted">${esc(words.chart_hint)}</p>`;
}
function turnTip(r) {
  const words = voiceWords(), parts = [];
  if (r.gap_s != null) parts.push(words.usual.gap_s.replace('{value}', r.gap_s));
  if (r.wpm != null) parts.push(words.usual.wpm.replace('{value}', Math.round(r.wpm)));
  if (r.acoustic?.pitch_hz) parts.push(words.usual.pitch_hz.replace('{value}', Math.round(r.acoustic.pitch_hz)));
  return `<b>${esc(words.turn_label)} ${r.index + 1} · ${fmtClock(offset(r.start_ms / 1000))}</b><br>${esc(parts.join(' · '))}${r.flags.length ? `<br>${esc(r.flags.map(flagText).join(' · '))}` : ''}<br><span class="muted">${esc(preview(r.text))}</span>`;
}
function renderVoice() {
  const box = $('#voice');
  if (!voice?.visible || !voice.available.timing) {
    box.innerHTML = guided() && voiceStep() && !independentDone() ? `<p class="small muted">${esc(stepWords().steps.voice.locked)}</p>` : '';
    return;
  }
  const words = voiceWords(), base = voice.baseline;
  const usual = [base.gap_s != null && words.usual.gap_s.replace('{value}', base.gap_s), base.wpm != null && words.usual.wpm.replace('{value}', Math.round(base.wpm)),
                 base.pitch_hz != null && words.usual.pitch_hz.replace('{value}', base.pitch_hz)].filter(Boolean);
  const notes = [!voice.available.acoustic && words.timing_only, !voice.enough_turns && words.not_enough,
                 voice.quality.echo_share > S.voice_settings.display.echo_warn_share && words.quality_echo].filter(Boolean);
  const notable = voice.notable.map(i => voice.turns[i]);
  box.innerHTML = `<h2>${esc(words.panel_title)}</h2><p class="small muted">${esc(words.panel_hint)}</p>
    ${notes.map(n => `<p class="small voice-note">${esc(n)}</p>`).join('')}
    ${usual.length ? `<p class="small"><b>${esc(words.usual_title)}:</b> ${esc(usual.join(' · '))}</p>` : ''}
    ${voice.enough_turns ? voiceChart() : ''}
    <h3>${esc(words.notable_title)}</h3>
    ${notable.length ? notable.map(r => `<button type="button" class="voice-turn" data-voice-jump="${r.index}"><span class="time-chip">${fmtClock(offset(r.start_ms / 1000))}</span><span class="grow">${esc(preview(r.text))}<span class="voice-chips">${r.flags.map(f => `<span class="voice-chip">${esc(flagText(f))}</span>`).join('')}</span></span></button>`).join('')
      : `<p class="small muted">${esc(words.notable_empty)}</p>`}`;
  $$('[data-voice-jump]', box).forEach(b => b.onclick = () => jumpToTurn(voice.turns[+b.dataset.voiceJump]));
  const tip = document.createElement('div'); tip.className = 'vc-tip hidden'; box.appendChild(tip);
  $$('[data-voice-turn]', box).forEach(hit => {
    const r = voice.turns[+hit.dataset.voiceTurn];
    hit.onmouseenter = () => { tip.innerHTML = turnTip(r); tip.classList.remove('hidden'); const a = hit.getBoundingClientRect(), b = box.getBoundingClientRect();
      tip.style.left = `${Math.max(0, Math.min(b.width - tip.offsetWidth, a.left - b.left - tip.offsetWidth / 2))}px`; tip.style.top = `${a.bottom - b.top + 6}px`; };
    hit.onmouseleave = () => tip.classList.add('hidden');
    hit.onclick = () => jumpToTurn(r);
  });
}
function renderWritings() {
  const settings = S.annotation_settings, words = settings.wording;
  const names = settings.highlights.targets.writing.filter(n => S.writing?.[n]?.text && highlightable({ kind: 'writing', writing: n }));
  const blocks = names.map(n => {
    const target = { kind: 'writing', writing: n };
    return `<div class="writing-block"><div class="r">${esc(words.writing_labels[n] || n)}</div><pre ${selectableAttrs(target)}>${rangesFor(target, S.writing[n].text)}</pre></div>`;
  }).join('');
  const open = $('#writings details')?.open;
  $('#writings').innerHTML = !names.length ? '' : guided()
    ? `<details ${open ? 'open' : ''}><summary>${esc(stepWords().answers_title)}</summary>${blocks}</details>`
    : `<h3>${esc(words.writing_title)}</h3>${blocks}`;
}
function sourceText(target) {
  return target.kind === 'event' ? eventById(target.event_id)?.[target.field] : S.writing?.[target.writing]?.text;
}
function selectedText() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return null;
  const range = selection.getRangeAt(0);
  const pre = (range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement)?.closest('pre[data-selectable-text]');
  if (!pre || !pre.contains(range.endContainer)) return null;
  const target = pre.dataset.kind === 'writing' ? { kind: 'writing', writing: pre.dataset.writing }
    : { kind: 'event', event_id: pre.dataset.eventId, field: pre.dataset.field };
  const source = sourceText(target);
  if (source == null) return null;
  const before = document.createRange(); before.selectNodeContents(pre); before.setEnd(range.startContainer, range.startOffset);
  const start = Array.from(before.toString()).length, end = start + Array.from(range.toString()).length;
  const text = Array.from(source).slice(start, end).join('');
  if (end <= start || end - start > S.annotation_settings.highlights.max_chars || text !== range.toString()) return null;
  return { target, start, end, text, rect: range.getBoundingClientRect() };
}
function focusHighlight(id) {
  const card = $(`[data-hl-card="${id}"]`);
  card?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  card?.classList.add('hl-flash'); setTimeout(() => card?.classList.remove('hl-flash'), 900);
  return card;
}
function selectionActions(selected) {
  const settings = S.annotation_settings, words = settings.wording, cfgH = settings.highlights;
  const locked = independentDone(), actions = [];
  const isEventText = selected.target.kind === 'event' && selected.target.field === 'text';
  const canHighlight = highlightable(selected.target);
  const adjusting = highlighter.adjusting && highlighter.byId(highlighter.adjusting);
  if (adjusting && targetKey(adjusting.target) === targetKey(selected.target))
    actions.push([words.adjust_action, async () => {
      await highlighter.patch(adjusting.id, { start: selected.start, end: selected.end, text: selected.text });
      highlighter.adjusting = null; $$('.hl-card.adjusting').forEach(c => c.classList.remove('adjusting'));
    }]);
  const open = openId && S.marks.find(m => m.id === openId && !m.deleted);
  const form = forms.get(openId);
  // A moment's first transcript quote stays its main quote (quote_selection);
  // further text becomes highlights linked to that moment.
  if (open && form && !locked && isEventText && (!canHighlight || !form.hasQuote()))
    actions.push([words.attach_action, () => form.selectQuote({ event_id: selected.target.event_id, start: selected.start, end: selected.end, text: selected.text })]);
  else if (canHighlight && open && (!locked || open.source === 'candidate_confirm'))
    actions.push([words.attach_action, () => highlighter.create(selected, { mark_id: open.id })]);
  if (canHighlight && cfgH.standalone && !locked)
    actions.push([words.highlight_action, async () => { const h = await highlighter.create(selected); $('[data-note]', focusHighlight(h.id))?.focus(); }]);
  if (!locked && (isEventText || canHighlight)) actions.push([words.new_moment_action, async () => {
    if (isEventText) { await addMoment(selected.target.event_id, null, null, { event_id: selected.target.event_id, start: selected.start, end: selected.end, text: selected.text }); return; }
    const mark = await addMoment(selected.target.event_id || null);
    if (mark) await highlighter.create(selected, { mark_id: mark.id });
  }]);
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
for (const id of ['#transcript', '#writings']) {
  $(id).addEventListener('mouseup', onSelect);
  $(id).addEventListener('click', e => {
    const mark = e.target.closest('mark[data-hl]');
    if (mark && window.getSelection()?.isCollapsed) focusHighlight(mark.dataset.hl.split(' ')[0]);
  });
}
function refreshLinked() {
  const words = S.annotation_settings.wording;
  $$('.flag-card').forEach(card => {
    const box = $('[data-linked-highlights]', card);
    if (!box) return;
    const linked = highlighter.forMark(card.dataset.flag);
    box.innerHTML = linked.length ? `<div class="small muted">${esc(words.linked_title)}</div>` + linked.map(h =>
      `<button type="button" class="linked-hl" data-focus-hl="${h.id}">${esc(h.text)}</button>`).join('') : '';
    $$('[data-focus-hl]', box).forEach(b => b.onclick = () => focusHighlight(b.dataset.focusHl));
  });
}
// The moment menu names each moment by number, time, and a few words of its
// quote or the participant's own description, matching what the cards show.
function momentLabel(m, i) {
  const words = (m.quote || (m.review_draft || m.review)?.after || eventById(m.event_id)?.text || '').replace(/\s+/g, ' ').trim();
  return `${S.annotation_settings.wording.moment_label} ${i + 1} · ${fmtClock(offset(point(m)))}${words ? ` · ${words.slice(0, 40)}${words.length > 40 ? '…' : ''}` : ''}`;
}
function renderHighlights() { highlighter.render($('#highlights')); refreshLinked(); }
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
  $('#intro').innerHTML = `<h2>${researcher ? 'Participant moments' : 'Your moments'}</h2><p class="small muted">${confirmed}/${marks.length} reviews confirmed. ${independentDone() ? 'The independent pass is preserved separately from any suggested moments below.' : 'It is fine if you did not use an idea, or if a bookmark was not an idea at all. Unsure and No are valid answers.'}</p>`;
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
      onSaved: m => { replace(m); updateIntro(); renderTrack(); renderTranscript(); advanceMoment(m); renderSteps(); }, onChanged: m => { m.review_dirty = true; replace(m); updateIntro(); } });
    forms.set(mark.id, form);
  }
  if (!openId && liveMarks().length && !independentDone()) openMoment(liveMarks().find(m => !m.review)?.id || liveMarks()[0].id);
  let add = $('#add-moment');
  if (!add && !researcher && !independentDone()) { add = document.createElement('button'); add.id = 'add-moment'; add.className = 'btn btn-sm'; add.textContent = 'Add a missed moment'; add.onclick = () => addMoment(null); $('#flags').after(add); }
  if (add) add.classList.toggle('hidden', independentDone());
  if (highlighter) refreshLinked();
}
function advanceMoment(saved) {
  if (!guided() || step !== 'moments' || saved.id !== openId) return;
  const marks = liveMarks(), i = marks.findIndex(m => m.id === saved.id);
  const next = [...marks.slice(i + 1), ...marks.slice(0, i)].find(m => !reviewed(m));
  if (next) openMoment(next.id);
}
function openMoment(id, jump = false) {
  openId = id; $$('.flag-card').forEach(card => card.classList.toggle('open', card.dataset.flag === id));
  const mark = S.marks.find(m => m.id === id); selectedStart = mark?.episode?.start_event_id || mark?.event_id || null;
  if (jump && mark) { seek(offset(point(mark)) - 15); $(`.flag-card[data-flag="${id}"]`)?.scrollIntoView({ block: 'nearest' }); }
  if (S && guided()) { if (jump && step !== 'moments' && !mark?.source?.startsWith('candidate')) setStep('moments'); renderMomentNav(); }
}
async function addMoment(eid, candidate = null, answer = null, quoteSelection = null) {
  if (independentDone() && !candidate) return;
  try {
    const mark = await api('POST', `/api/sessions/${sid}/marks`, { source: candidate ? 'candidate_confirm' : 'review_add', anchor: candidate ? 'suggested_passage' : eid ? 'review_passage' : 'review_timestamp', event_id: eid, client_t: Date.now(), reference_t: eid ? eventById(eid)?.t : video ? t0 + video.currentTime : null,
      ...(candidate ? { episode: { start_event_id: candidate.start_event_id || eid, end_event_id: candidate.end_event_id || eid } } : {}),
      ...(quoteSelection ? { quote_selection: { event_id: quoteSelection.event_id, start: quoteSelection.start, end: quoteSelection.end, text: quoteSelection.text } } : {}) });
    S.marks.push(mark); renderMoments(); openMoment(mark.id); if (quoteSelection) renderTranscript();
    if (candidate) await highlightCandidate(candidate, mark);
    if (candidate) { await api('PATCH', `/api/sessions/${sid}/candidates/${candidate.id}`, { participant_response: { answer, mark_id: mark.id } }); await load(); renderSuggestions(); renderTranscript(); renderHighlights(); }
    return mark;
  } catch (error) { toast(error.message); }
}
// A confirmed suggestion starts with the model's passages as highlights; the
// participant can adjust or remove them, and every boundary edit is kept.
async function highlightCandidate(candidate, mark) {
  const settings = S.annotation_settings.highlights;
  if (!settings.enabled || !settings.candidate_highlights) return;
  for (const [eid, quote] of [[candidate.trigger_event_id, candidate.trigger_quote], [candidate.uptake_event_id, candidate.uptake_quote]]) {
    const found = locate(eventById(eid)?.text, quote);
    if (!found) continue;
    try { await highlighter.create({ target: { kind: 'event', event_id: eid, field: 'text' }, ...found, text: quote },
                                   { mark_id: mark.id, candidate_id: candidate.id, source: 'candidate' }); }
    catch (error) { toast(error.message); }
  }
}
async function finish() {
  if (finishing) return;
  finishing = true;
  if ($('#finish')) $('#finish').disabled = true;
  try {
    if (!await outbox.flush() || !await flushReviewDrafts() || !await highlighter.flush()) throw new Error('Resolve unsynced drafts or bookmarks before continuing.');
    if (!independentDone()) {
      if (!confirm('Finish your independent review? These answers will be locked before you see model suggestions.')) return;
      await api('POST', `/api/sessions/${sid}/review/complete-independent`, {}); await load();
      for (const id of S.independent_review.mark_ids) forms.get(id)?.lock();
      openMoment(null);
      const after = guided() ? stepKeys().filter(k => ['suggestions', 'voice'].includes(k)) : showsSuggestions() ? ['suggestions'] : [];
      if (!after.length) {
        await completeSession(); return;
      }
      renderHeader(); renderNext(); renderTranscript(); renderWritings(); renderMoments(); renderSuggestions(); renderHighlights();
      await loadVoice();
      if (guided()) setStep(after[0]);
    } else {
      if (liveMarks().some(m => m.source === 'candidate_confirm' && (m.review_dirty || m.review?.status !== 'submitted'))) throw new Error('Confirm the suggested moments you added before finishing.');
      await completeSession();
    }
  } catch (error) { toast(error.message); }
  finally { finishing = false; renderHeader(); renderStepActions(); }
}
async function completeSession() {
  await api('POST', `/api/sessions/${sid}/advance`, { phase: 'review' });
  location.assign('/?completed=1');
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
highlighter = new Highlighter({ sid, researcher, getSession: () => S,
  describeTarget: h => h.target.kind === 'writing' ? S.annotation_settings.wording.writing_labels[h.target.writing] || h.target.writing
    : `${label(eventById(h.target.event_id) || { role: 'message' })}${h.target.field === 'output' ? ` · ${S.annotation_settings.wording.output_label}` : ''}`,
  moments: () => liveMarks().map((m, i) => ({ id: m.id, label: momentLabel(m, i),
    disabled: independentDone() ? m.source !== 'candidate_confirm' : m.source === 'candidate_confirm' })),
  allowStandalone: () => S.annotation_settings.highlights.standalone && !independentDone(),
  canEdit: h => {
    if (S.phase !== 'review') return false;
    if (!independentDone()) return true;
    if (S.independent_review.highlight_ids?.includes(h.id)) return false;
    return S.marks.find(m => m.id === h.mark_id)?.source === 'candidate_confirm';
  },
  onChange: () => { renderTranscript(); renderWritings(); renderHighlights(); } });
await highlighter.loadSuggestions();
try { step = sessionStorage.getItem(`review-step:${sid}`); } catch {}
if (!step && independentDone()) step = stepKeys().find(k => ['suggestions', 'voice'].includes(k)) || null;
renderHeader(); renderPlayer(); renderTrack(); $('#show-tools').checked = true; renderTranscript(); renderWritings(); renderMoments(); renderNext(); renderSuggestions(); renderHighlights();
await loadVoice();
renderVoice(); renderSteps();
if (guided() && step === 'moments' && !openId && liveMarks().length) setStep('moments');
$('#show-tools').onchange = renderTranscript;
setInterval(async () => { try { await load(); await prepareSuggestions(); renderHeader(); renderMoments(); renderNext(); renderSuggestions(); renderHighlights(); renderSteps(); } catch {} }, 3000);
