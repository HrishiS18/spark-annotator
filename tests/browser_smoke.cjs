// Optional headless CLI QA. Requires Playwright; never invokes a live model.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const root = path.resolve(__dirname, '..');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'spark-browser-qa-'));
  const port = Number(process.env.SPARK_TEST_PORT || 8876);
  const base = `http://127.0.0.1:${port}`;
  const python = process.env.SPARK_TEST_PYTHON || path.join(root, '.venv/bin/python');
  const processHandle = spawn(python, ['tests/run_mock_server.py', '--port', String(port)], {
    cwd: root, env: { ...process.env, SPARK_DATA_DIR: data, SPARK_QUIET: '1' }, stdio: ['ignore', 'pipe', 'pipe']
  });
  let output = '';
  processHandle.stderr.on('data', (chunk) => { output += chunk; });
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { const response = await fetch(base + '/api/config'); if (response.ok) { ready = true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert(ready, `Test server failed: ${output}`);
    browser = await chromium.launch({ headless: true, timeout: 30000,
      ...(process.env.SPARK_CHROME_PATH ? { executablePath: process.env.SPARK_CHROME_PATH } : {}) });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', err => errors.push(err.message));
    page.on('dialog', dialog => dialog.accept());
    await page.goto(base);
    await page.locator('#start button[type=submit]:enabled').waitFor();
    assert.equal(await page.locator('#tax, .tax-grid, #parser-pill, .cards').count(),0);
    assert.equal(await page.locator('details.home-settings').getAttribute('open'),null);
    assert(await page.locator('#interaction-field').isVisible());
    assert.equal(await page.getByRole('link',{name:'Screen recorder',exact:true}).count(),0);
    assert.equal(await page.getByRole('link',{name:'Researcher dashboard',exact:true}).count(),0);
    await page.locator('#start [name=condition]').selectOption('human');
    assert(await page.locator('#room-field').isVisible());
    assert(await page.locator('#runtime-field').isHidden());
    assert(await page.locator('#interaction-field').isHidden());
    assert(await page.locator('#claude-setup').isHidden());
    assert(await page.locator('#start [name=room]').evaluate(input=>input.required));
    await page.locator('#start [name=condition]').selectOption('ai');
    assert(await page.locator('#room-field').isHidden());
    assert(await page.locator('#start [name=room]').isDisabled());
    await page.locator('details.home-settings summary').click();
    assert.deepEqual(await page.locator('#start [name=study] option').evaluateAll(rows=>rows.map(r=>[r.value,r.textContent])),[['dilemma','Moral dilemma']]);
    assert(!(await page.locator('#start [name=persona] option').evaluateAll(rows=>rows.map(r=>r.value))).includes('trading'));
    assert.equal(await page.locator('#start [name=persona]').inputValue(),'partner');
    await page.locator('details.home-settings summary').click();
    await page.screenshot({path:path.join(data,'landing-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:path.join(data,'landing-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1280,height:900});
    const missingKeyConfig = async route => {
      const response=await route.fetch(); const cfg=await response.json();
      cfg.runtimes.claude={...cfg.runtimes.claude,installed:true,configured:false};
      await route.fulfill({response,json:cfg});
    };
    await page.route('**/api/config',missingKeyConfig);
    await page.reload();
    await page.locator('#start button[type=submit]:enabled').waitFor();
    await page.locator('#start [name=runtime]').selectOption('claude');
    assert(await page.locator('#claude-setup').isVisible());
    assert(await page.locator('#interaction-field').isHidden());
    assert((await page.locator('#claude-setup').textContent()).includes('Accept edits'));
    assert(await page.locator('#start button[type=submit]').isDisabled());
    assert((await page.locator('#runtime-status').textContent()).includes('Claude is installed'));
    assert((await page.locator('#runtime-status').textContent()).includes('ANTHROPIC_API_KEY'));
    await page.locator('#start [name=condition]').selectOption('human');
    assert(await page.locator('#start button[type=submit]').isEnabled());
    assert(await page.locator('#runtime-status').isHidden());
    await page.locator('#start [name=condition]').selectOption('ai');
    await page.locator('#start [name=runtime]').selectOption('codex');
    assert(await page.locator('#claude-setup').isHidden());
    assert(await page.locator('#interaction-field').isVisible());
    assert(await page.locator('#start button[type=submit]').isEnabled());
    await page.unroute('**/api/config',missingKeyConfig);
    await page.reload();
    await page.locator('#start button[type=submit]:enabled').waitFor();
    await page.locator('#start [name=participant_id]').fill('BROWSER-TEST');
    const runtime=process.env.SPARK_BROWSER_RUNTIME || 'codex';
    assert.deepEqual(await page.locator('#start [name=runtime] option').evaluateAll(rows=>rows.map(r=>r.value)),['codex','claude']);
    await page.locator('#start [name=runtime]').selectOption(runtime);
    assert.equal(await page.locator('#claude-setup').isVisible(), runtime === 'claude');
    if (runtime === 'claude') await page.screenshot({path:path.join(data,'claude-setup.png'),fullPage:true});
    await page.locator('#start button[type=submit]').click();
    await page.waitForURL(/\/session\?id=/);
    await page.locator('#essay').fill('My initial approach is to inspect the available evidence and identify the most important decision first.');
    await page.locator('#submit').click();
    await page.waitForFunction(() => document.querySelector('#runtime-panel')?.textContent.includes('ready'));
    assert.equal(await page.locator('body').getAttribute('data-agent'), runtime);
    assert.equal(await page.locator('#composer-context').isVisible(), runtime === 'claude');
    assert.equal(await page.locator('#add-file').isVisible(), runtime === 'claude');
    const sid = new URL(page.url()).searchParams.get('id');
    await page.locator('.workspace-panel summary').click();
    const upload = { name: 'input.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic task file') };
    if (runtime === 'claude') {
      const [chooser] = await Promise.all([page.waitForEvent('filechooser'), page.locator('#add-file').click()]);
      await chooser.setFiles(upload);
    } else {
      await page.locator('#workspace-upload').setInputFiles(upload);
    }
    await page.getByRole('button', { name: 'input.txt', exact: true }).waitFor();
    await page.locator('#input').fill('Inspect the task file and produce a short summary.');
    await page.locator('#send').click();
    await page.locator('.approval-card').waitFor();
    console.log('QA: streamed approval ready');
    if (runtime === 'codex') {
      assert.equal(await page.locator('.approval-card [data-decision=acceptForSession]').count(),1);
    } else {
      assert.equal(await page.locator('.approval-card [data-decision=acceptForSession]').count(),0);
    }
    assert(await page.locator('#send').isDisabled());
    await page.context().setOffline(true);
    assert.equal(await page.locator('.msg-ai .mark-here').first().getAttribute('title'),null);
    assert.equal((await page.locator('.msg-ai .mark-here-label').first().textContent()).trim(),'Mark entire message');
    assert.equal(await page.locator('.msg-ai .mark-here').first().getAttribute('aria-label'),'Mark this message');
    await page.locator('.msg-ai').first().hover();
    assert(await page.locator('.msg-ai .mark-here').first().evaluate(el=>{
      const icon=el.querySelector('.ic').getBoundingClientRect(), label=el.querySelector('.mark-here-label').getBoundingClientRect();
      return label.left>=icon.right && getComputedStyle(el.querySelector('.mark-here-label')).fontStyle==='italic';
    }));
    await page.locator('.msg-ai .mark-here').first().click();
    assert.equal(await page.locator('.msg-ai .mark-here').first().getAttribute('aria-label'),'Unmark this message');
    assert.equal((await page.locator('.msg-ai .mark-here-label').first().textContent()).trim(),'Unmark entire message');
    assert.equal(await page.locator('.mark-pop, [data-type]').count(),0);
    await page.waitForFunction(sid => JSON.parse(localStorage.getItem('spark-outbox:'+sid) || '{}') &&
      Object.keys(JSON.parse(localStorage.getItem('spark-outbox:'+sid) || '{}')).length === 1, sid);
    await page.context().setOffline(false);
    console.log('QA: offline bookmark queued');
    await page.reload();
    await page.locator('.approval-card').waitFor();
    await page.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks.length === 1, sid);
    const markedId = (await (await fetch(base+'/api/sessions/'+sid)).json()).marks[0].id;
    await page.locator('.msg-ai .mark-here[aria-pressed=true]').first().click();
    await page.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks[0].deleted, sid);
    assert.equal(await page.locator('#fab-count').textContent(), '0');
    await page.locator('.msg-ai .mark-here[aria-pressed=false]').first().click();
    await page.waitForFunction(async sid => !(await (await fetch('/api/sessions/'+sid)).json()).marks[0].deleted, sid);
    assert.equal(await page.locator('#fab-count').textContent(), '1');
    await page.locator('.bookmark-undo').click();
    await page.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks[0].deleted, sid);
    await page.locator('.msg-ai .mark-here[aria-pressed=false]').first().click();
    await page.waitForFunction(async sid => !(await (await fetch('/api/sessions/'+sid)).json()).marks[0].deleted, sid);
    assert.deepEqual((await (await fetch(base+'/api/sessions/'+sid)).json()).marks.map(m => m.id), [markedId]);
    await page.locator('.approval-card [data-decision=accept]').click();
    await page.waitForFunction(() => document.querySelector('#runtime-panel')?.textContent.includes('ready'));
    const before = await (await fetch(`${base}/api/sessions/${sid}`)).json();
    assert.equal(before.marks.length, 1);
    assert(before.events.some(event => event.role === 'tool_result'));
    assert(before.marks[0].exposure.events.length > 0);
    const exposure = before.marks[0].exposure.events[0].rendered_text;
    const anchor = before.marks[0].event_id;
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#runtime-panel')?.textContent.includes('ready'));
    const after = await (await fetch(`${base}/api/sessions/${sid}`)).json();
    assert.deepEqual(after.events.map(event => event.id), before.events.map(event => event.id));
    assert.equal(after.marks[0].event_id, anchor);
    assert.equal(after.marks[0].exposure.events[0].rendered_text, exposure);
    await page.locator('.workspace-panel summary').click();
    await page.getByRole('button', { name: 'summary.txt', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#file-preview')?.textContent.includes('Test-only artifact'));
    await page.screenshot({ path: path.join(data, 'agent-interface.png'), fullPage: true });
    await page.locator('#finish').click();
    await page.locator('#essay').waitFor();
    assert.equal(await page.getByText('Your earlier answer (for reference)').count(),0);
    assert(!(await page.locator('#content').textContent()).includes('My initial approach is to inspect the available evidence'));
    assert((await page.locator('#content').textContent()).includes('The question'));
    await page.locator('#essay').fill('I have now inspected the task file and used the resulting artifact to explain my final approach.');
    await page.locator('#submit').click();
    await page.waitForURL(/\/review\?id=/);
    await page.locator('[data-f=after]').first().waitFor();
    console.log('QA: review opened');
    assert.equal(await page.locator('#highlights, [data-linked-highlights]').count(), 0);
    assert.equal(await page.getByText('Your highlights').count(), 0);
    assert(await page.locator('.optional-review').first().isVisible());
    assert.equal(await page.locator('.optional-review summary').count(), 0);
    assert.deepEqual(await page.locator('.optional-review select[data-f] option:first-child').allTextContents(), Array(5).fill('Select answer'));
    assert(await page.locator('.annot .qlabel .required-mark').first().isVisible());
    assert(await page.locator('[data-after-required]').first().isHidden());
    assert.equal(await page.locator('[data-f=after]').first().evaluate(el => el.required), false);
    assert((await page.locator('.flag-card .mark-source').first().textContent()).includes('Message you marked during the conversation'));
    assert.equal(await page.getByText('What was rendered when you clicked').count(),0);
    assert.equal(await page.locator('.flag-card [data-adjust-passage]').first().getAttribute('open'),null);
    assert.equal(await page.getByText('Extend passage through here').count(),0);
    assert((await page.locator('.tr-ev').first().textContent()).includes('Add a moment here'));
    const divider = page.locator('#review-divider');
    assert.equal(await divider.getAttribute('title'),null);
    assert(await divider.evaluate(el=>{const style=getComputedStyle(el,'::before');return style.top==='0px' && style.bottom==='0px' && style.width==='2px';}));
    const initialRight = (await page.locator('.rv-right').boundingBox()).width;
    const handle = await divider.boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
    await page.mouse.down();
    await page.mouse.move(handle.x + handle.width / 2 - 110, handle.y + handle.height / 2, {steps:8});
    await page.mouse.up();
    const resizedRight = (await page.locator('.rv-right').boundingBox()).width;
    assert(resizedRight > initialRight + 80);
    await divider.focus();
    await page.keyboard.press('ArrowRight');
    const keyboardRight = (await page.locator('.rv-right').boundingBox()).width;
    assert(keyboardRight < resizedRight);
    await page.reload();
    await page.locator('[data-f=after]').first().waitFor();
    assert(Math.abs((await page.locator('.rv-right').boundingBox()).width - keyboardRight) < 3);
    await page.setViewportSize({width:800,height:900});
    assert(await divider.isHidden());
    await page.setViewportSize({width:1280,height:900});
    assert(await page.locator('#player').isHidden());
    assert((await page.locator('#suggestions').textContent()).includes('hidden'));
    const competing = await page.context().newPage();
    competing.setDefaultTimeout(15000); competing.on('dialog', dialog => dialog.accept());
    competing.on('pageerror', err => errors.push(err.message));
    await competing.goto(base+'/review?id='+sid);
    await competing.locator('[data-f=after]').first().waitFor();
    assert.notEqual(await page.evaluate(() => sessionStorage.getItem('spark-editor-tab')),
      await competing.evaluate(() => sessionStorage.getItem('spark-editor-tab')));
    let loseAck = true;
    await page.route('**/api/sessions/'+sid+'/marks/*',async route => {
      if (loseAck && route.request().method() === 'PATCH' && route.request().postDataJSON()?.review_draft) {
        loseAck = false; await route.fetch(); await route.abort('failed');
      } else await route.continue();
    });
    await page.locator('[data-f=after]').first().fill('Interrupted synthetic draft');
    await page.waitForFunction(() => document.querySelector('[data-save-state]')?.textContent.includes('Not synced'));
    await page.locator('[data-f=after]').first().fill('The suggestion helped me identify the first step.');
    await page.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks[0].review_draft?.after ===
      'The suggestion helped me identify the first step.', sid);
    await page.waitForFunction(() => document.querySelector('[data-save-state]')?.textContent.includes('Draft saved'));
    await page.unroute('**/api/sessions/'+sid+'/marks/*');
    await competing.locator('[data-f=after]').first().fill('Conflicting draft in another tab');
    await competing.locator('[data-replace]').first().waitFor();
    assert((await competing.locator('[data-save-state]').first().textContent()).includes('Conflict'));
    assert.equal((await (await fetch(base+'/api/sessions/'+sid)).json()).marks[0].review_draft.after,
      'The suggestion helped me identify the first step.');
    await competing.close();
    await page.reload();
    assert.equal(await page.locator('[data-f=after]').first().inputValue(),'The suggestion helped me identify the first step.');
    const userEvent = after.events.find(e => e.role === 'user').id;
    const endEvent = after.events.find(e => e.role === 'tool_result').id;
    const selectedText = after.events.find(e => e.id === userEvent).text.slice(0, 10);
    await page.locator(`.tr-ev[data-ev="${userEvent}"] pre[data-selectable-text]`).evaluate(pre => {
      const range = document.createRange(); range.setStart(pre.firstChild,0); range.setEnd(pre.firstChild,10);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      pre.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    });
    await page.locator('.review-selection-actions button').first().waitFor();
    assert.deepEqual(await page.locator('.review-selection-actions button').allTextContents(), ['Use for open moment', 'Mark as new moment']);
    await page.locator('.review-selection-actions button').first().click();
    assert(await page.locator('.flag-card [data-adjust-passage]').first().evaluate(el=>el.open));
    assert.equal(await page.locator('[data-quote-context] blockquote').first().textContent(),selectedText);
    await page.locator('[data-start]').first().selectOption(userEvent);
    await page.locator('[data-end]').first().selectOption(endEvent);
    await page.locator('[data-decision=confirmed]').first().click();
    assert(await page.locator('[data-after-required]').first().isVisible());
    assert.equal(await page.locator('[data-f=after]').first().evaluate(el => el.required), true);
    assert.equal(await page.locator('[data-type]').count(),0);
    await page.locator('[data-save]').first().click();
    await page.waitForFunction(() => document.querySelector('.review-card')?.classList.contains('saved'));
    const exported = await (await fetch(`${base}/api/sessions/${sid}/export.json`)).json();
    assert.equal(exported.marks[0].review.after, 'The suggestion helped me identify the first step.');
    assert.equal(exported.marks[0].original_event_id, anchor);
    assert.equal(exported.marks[0].original_anchor.event_id, anchor);
    assert.equal(exported.marks[0].original_anchor.episode,null);
    assert.equal(exported.marks[0].original_anchor.quote_selection,null);
    assert.equal(exported.marks[0].episode.start_event_id, userEvent);
    assert.equal(exported.marks[0].episode.end_event_id, endEvent);
    assert.deepEqual(exported.marks[0].quote_selection,{event_id:userEvent,start:0,end:10,text:selectedText});
    await page.locator('#add-moment').click();
    await page.waitForFunction(() => document.querySelectorAll('.flag-card').length === 2);
    const missed = page.locator('.flag-card').last();
    await missed.locator('[data-decision=rejected]').click();
    assert(await missed.locator('.insight-field').isHidden());
    assert(await missed.locator('.optional-review').isHidden());
    assert(await missed.locator('[data-adjust-passage]').isHidden());
    assert.equal(await missed.getByText('No rendered-text snapshot was captured').count(),0);
    await missed.locator('[data-save]').click();
    await page.waitForFunction(() => document.querySelectorAll('.review-card.saved').length === 2);
    assert.equal((await (await fetch(`${base}/api/sessions/${sid}`)).json()).marks.at(-1).review.after,'');
    // No researcher action: completion already started judging automatically.
    const dashboard = await browser.newPage();
    dashboard.setDefaultTimeout(15000);
    dashboard.on('pageerror', err => errors.push(err.message));
    dashboard.on('dialog', dialog => dialog.accept());
    await dashboard.goto(base+'/researcher#'+sid);
    await dashboard.waitForFunction(() => document.querySelector('#main')?.textContent.includes('SYNTHETIC_TEST_SUMMARY'));
    await dashboard.screenshot({path:path.join(data,'researcher-console.png'),fullPage:true});
    const stillBlind = await (await fetch(`${base}/api/sessions/${sid}`)).json();
    assert.deepEqual(stillBlind.candidates,[]);
    const auto = await (await fetch(`${base}/api/sessions/${sid}?view=researcher`)).json();
    assert.equal(auto.judge.requested_by,'automatic');
    assert.equal(auto.analysis_runs.length,1);
    await page.reload();
    assert.equal((await (await fetch(`${base}/api/sessions/${sid}?view=researcher`)).json()).analysis_runs.length,1);
    await page.screenshot({ path:path.join(data,'spark-review.png'),fullPage:true });
    await page.locator('#finish').click();
    console.log('QA: independent pass submitted');
    await page.waitForFunction(() => document.querySelector('#finish')?.textContent === 'Finish and return home');
    assert((await page.locator('#review-next').textContent()).includes('Suggested moments below are optional'));
    assert.equal(await page.locator('.flag-card.open').count(), 0);
    assert.equal(await page.locator('.flag-card.read-only').count(), 2);
    await page.screenshot({path:path.join(data,'review-after-independent.png'),fullPage:true});
    assert(await page.locator('[data-f=after]').first().isDisabled());
    await page.locator('[data-answer=retrospective]').first().click();
    await page.waitForFunction(() => document.querySelectorAll('.flag-card').length === 3);
    const suggested = page.locator('.flag-card').last();
    await suggested.locator('[data-decision=confirmed]').click();
    await suggested.locator('[data-f=after]').fill('Only during review did I recognize a new possible starting point.');
    await suggested.locator('[data-save]').click();
    await page.waitForFunction(() => document.querySelectorAll('.review-card.saved').length === 3);
    const archiveDownload = page.waitForEvent('download');
    await page.locator('#finish').click();
    const download = await archiveDownload;
    assert.equal(download.suggestedFilename(),`spark-session-${sid}.zip`);
    assert((await fs.promises.stat(await download.path())).size > 100);
    await page.waitForURL(new RegExp(`\\?completed=1&archive=${sid}$`));
    assert(await page.locator('#home-complete').isVisible());
    assert(await page.getByRole('link',{name:'Download ZIP'}).isVisible());
    const final = await (await fetch(`${base}/api/sessions/${sid}/export.json`)).json();
    assert.equal(final.phase,'done');
    assert.equal(final.independent_review.mark_ids.length,2);
    assert.equal(final.marks[2].source,'candidate_confirm');
    assert.equal(final.candidates[0].participant_response.answer,'retrospective');
    await page.goto(base+'/review?id='+sid);
    await page.waitForFunction(() => document.querySelector('#review-next')?.textContent.includes('Session complete'));
    assert(await page.locator('.flag-card').last().locator('[data-f=after]').isDisabled());
    // Distinguish missing transcript, completed empty result, and failed generation.
    const request = async (url,body) => {
      const result = await fetch(base+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      assert(result.ok); return result.json();
    };
    const liveSelectionSession = await request('/api/sessions',{
      study:'dilemma',participant_id:'TEST-LIVE-SELECTION',condition:'human',room:'live-selection-test'});
    await request('/api/sessions/'+liveSelectionSession.id+'/advance',{phase:'pre',text:'Synthetic baseline'});
    await request('/api/sessions/'+liveSelectionSession.id+'/message',{text:'alpha beta alpha beta'});
    await request('/api/sessions/'+liveSelectionSession.id+'/message',{text:'before **bold** after'});
    const selectionPage=await context.newPage(); selectionPage.setDefaultTimeout(15000);
    selectionPage.on('pageerror',err=>errors.push(err.message));
    await selectionPage.goto(base+'/session?id='+liveSelectionSession.id);
    await selectionPage.locator('.msg-user .msg-body').first().waitFor();
    const rowGeometry=await selectionPage.locator('.msg-user').first().evaluate(row=>{
      const outer=row.getBoundingClientRect(), bubble=row.querySelector('.msg-content').getBoundingClientRect();
      return {width:outer.width,threadWidth:row.parentElement.getBoundingClientRect().width,bubbleGap:bubble.left-outer.left};
    });
    assert(rowGeometry.width>=rowGeometry.threadWidth-2 && rowGeometry.bubbleGap>30);
    await selectionPage.locator('.msg-user').first().hover({position:{x:6,y:8}});
    await selectionPage.waitForFunction(()=>Number(getComputedStyle(document.querySelector('.msg-user .mark-here')).opacity)>.95);
    assert.equal(await selectionPage.locator('.msg-user .mark-here').first().getAttribute('title'),null);
    assert.equal((await selectionPage.locator('.msg-user .mark-here-label').first().textContent()).trim(),'Mark entire message');
    assert.equal(await selectionPage.locator('.msg-user .mark-here').first().getAttribute('aria-label'),'Mark this message');
    assert(await selectionPage.locator('.msg-user .mark-here').first().evaluate(el=>{
      const icon=el.querySelector('.ic').getBoundingClientRect(), label=el.querySelector('.mark-here-label').getBoundingClientRect();
      return label.right<=icon.left && getComputedStyle(el.querySelector('.mark-here-label')).fontStyle==='italic';
    }));
    await selectionPage.screenshot({path:path.join(data,'message-lightbulb.png'),fullPage:true});
    await selectionPage.setViewportSize({width:390,height:844});
    assert(await selectionPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await selectionPage.setViewportSize({width:1280,height:900});
    const touchContext=await browser.newContext({viewport:{width:390,height:844},hasTouch:true,isMobile:true});
    const touchPage=await touchContext.newPage(); touchPage.on('pageerror',err=>errors.push(err.message));
    await touchPage.goto(base+'/session?id='+liveSelectionSession.id);
    await touchPage.locator('.msg-user .mark-here').first().waitFor();
    assert(await touchPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert.equal(await touchPage.locator('.msg-user .mark-here').first().evaluate(el=>getComputedStyle(el).opacity),'1');
    assert(await touchPage.locator('.msg-user .mark-here').first().evaluate(el=>{
      const r=el.getBoundingClientRect(), target=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight && el.contains(target);
    }));
    await touchPage.screenshot({path:path.join(data,'message-lightbulb-mobile.png'),fullPage:true});
    await touchContext.close();
    await selectionPage.locator('.msg-user .msg-body').first().evaluate(body=>{
      const node=body.querySelector('p').firstChild;
      const range=document.createRange(); range.setStart(node,11); range.setEnd(node,21);
      const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    });
    await selectionPage.locator('.sel-chip').click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.msg-user .live-selection-highlight').length===1);
    assert.equal(await selectionPage.locator('.msg-user .live-selection-highlight').first().textContent(),'alpha beta');
    assert.equal(await selectionPage.locator('.msg-user.marked').count(),0);
    assert.equal(await selectionPage.locator('.msg-user .msg-body').first().textContent(),'alpha beta alpha beta');
    assert((await selectionPage.locator('.msg-user .mark-badge-action').first().textContent()).includes('tap to unmark'));
    await selectionPage.screenshot({path:path.join(data,'live-passage-highlight.png'),fullPage:true});
    await selectionPage.waitForFunction(async sid=>(await (await fetch('/api/sessions/'+sid)).json()).marks[0]?.quote_selection?.start===11,liveSelectionSession.id);
    await selectionPage.reload();
    assert.equal(await selectionPage.locator('.msg-user .live-selection-highlight').first().textContent(),'alpha beta');
    await selectionPage.locator('.msg-user .live-selection-highlight').first().hover();
    await selectionPage.locator('.unmark-preview').waitFor();
    assert((await selectionPage.locator('.unmark-preview').textContent()).includes('Unmark this'));
    await selectionPage.screenshot({path:path.join(data,'unmark-preview.png'),fullPage:true});
    await selectionPage.locator('.unmark-preview').click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.msg-user .live-selection-highlight').length===0);
    await selectionPage.waitForFunction(async sid=>(await (await fetch('/api/sessions/'+sid)).json()).marks[0]?.deleted,liveSelectionSession.id);
    await selectionPage.locator('.msg-user .msg-body').first().evaluate(body=>{
      const node=body.querySelector('p').firstChild;
      const range=document.createRange(); range.setStart(node,11); range.setEnd(node,21);
      const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    });
    assert.equal(await selectionPage.locator('.sel-chip').textContent(),'Mark this');
    await selectionPage.locator('.sel-chip').click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.msg-user .live-selection-highlight').length===1);
    // Wait for the optimistic bookmark to sync; its acknowledgement re-renders
    // the transcript and would otherwise clear the next synthetic selection.
    await selectionPage.waitForFunction(sid => Object.keys(JSON.parse(localStorage.getItem(`spark-outbox:${sid}`) || '{}')).length===0,
      liveSelectionSession.id);
    await selectionPage.locator('.msg-user .msg-body').first().evaluate(body=>{
      const p=body.querySelector('p');
      const range=document.createRange(); range.setStart(p.firstChild,6); range.setEnd(p.querySelector('mark').firstChild,5);
      const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    });
    await selectionPage.locator('.sel-chip').click();
    await selectionPage.locator('.live-selection-highlight[data-mark-ids*=" "]').first().click();
    assert.equal(await selectionPage.locator('.unmark-menu button').count(),2);
    await selectionPage.getByRole('button',{name:'Unmark “beta alpha”'}).click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.msg-user .live-selection-highlight').length===1);
    assert.equal(await selectionPage.locator('.msg-user .live-selection-highlight').first().textContent(),'alpha beta');
    await selectionPage.locator('.msg-user .live-selection-highlight').first().click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.msg-user .live-selection-highlight').length===0);
    await selectionPage.locator('.msg-user .msg-body').last().evaluate(body=>{
      const node=body.querySelector('strong').firstChild;
      const range=document.createRange(); range.selectNodeContents(node);
      const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    });
    await selectionPage.locator('.sel-chip').click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.msg-user strong .live-selection-highlight').length===1);
    assert.equal(await selectionPage.locator('.msg-user strong .live-selection-highlight').textContent(),'bold');
    await selectionPage.locator('.msg-user .mark-here').first().click();
    assert.equal(await selectionPage.locator('.msg-user.marked').count(),1);
    assert.equal(await selectionPage.locator('.msg-user .live-selection-highlight').count(),1);
    await selectionPage.locator('.msg-user .mark-badge-action').click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.msg-user .live-selection-highlight').length===0);
    await selectionPage.waitForFunction(sid=>Object.keys(JSON.parse(localStorage.getItem('spark-outbox:'+sid)||'{}')).length===0,liveSelectionSession.id);
    assert.equal(await selectionPage.locator('.msg-user.marked').count(),1);
    const exchangeIds=await selectionPage.locator('.msg-user').evaluateAll(rows=>rows.map(row=>row.dataset.id));
    assert.equal(await selectionPage.locator('#mark-exchange, #exchange-guide').count(),0);
    const dragPoints=await selectionPage.locator('.msg-user .msg-body').first().evaluate(body=>{
      const first=body.querySelector('p').firstChild;
      const second=document.querySelectorAll('.msg-user .msg-body')[1].querySelector('p').firstChild;
      const point=(node,offset)=>{const range=document.createRange();range.setStart(node,offset);range.setEnd(node,offset);
        const rect=range.getBoundingClientRect();return {x:rect.x+2,y:rect.y+rect.height/2};};
      return {start:point(first,0),end:point(second,6)};
    });
    await selectionPage.mouse.move(dragPoints.start.x,dragPoints.start.y);
    await selectionPage.mouse.down();
    await selectionPage.mouse.move(dragPoints.end.x,dragPoints.end.y,{steps:12});
    await selectionPage.mouse.up();
    assert((await selectionPage.locator('.sel-chip').textContent()).includes('Mark exchange'));
    await selectionPage.screenshot({path:path.join(data,'exchange-selection.png'),fullPage:true});
    await selectionPage.locator('.sel-chip').click();
    assert.equal(await selectionPage.locator('.exchange-marked').count(),2);
    assert.equal(await selectionPage.locator('[data-exchange-mark]').count(),1);
    await selectionPage.screenshot({path:path.join(data,'exchange-marked.png'),fullPage:true});
    await selectionPage.waitForFunction(async sid=>(await (await fetch('/api/sessions/'+sid)).json()).marks.some(m=>m.anchor==='exchange' && m.episode?.end_event_id),liveSelectionSession.id);
    const exchangeMark=(await (await fetch(base+'/api/sessions/'+liveSelectionSession.id)).json()).marks.find(m=>m.anchor==='exchange');
    assert.deepEqual(exchangeMark.episode,{start_event_id:exchangeIds[0],end_event_id:exchangeIds[1]});
    assert.equal(exchangeMark.exposure.events.length,2);
    await selectionPage.reload();
    assert.equal(await selectionPage.locator('.exchange-marked').count(),2);
    await selectionPage.locator('[data-exchange-mark]').click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.exchange-marked').length===0);
    await selectionPage.waitForFunction(async ({sid,id})=>(await (await fetch('/api/sessions/'+sid)).json()).marks.find(m=>m.id===id)?.deleted,{sid:liveSelectionSession.id,id:exchangeMark.id});
    await selectionPage.locator('.msg-user .msg-body').first().evaluate(body=>{
      const first=body.querySelector('p').firstChild;
      const second=document.querySelectorAll('.msg-user .msg-body')[1].querySelector('p').firstChild;
      const range=document.createRange(); range.setStart(first,0); range.setEnd(second,6);
      const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    });
    assert((await selectionPage.locator('.sel-chip').textContent()).includes('Mark exchange'));
    await selectionPage.locator('.sel-chip').click();
    await selectionPage.waitForFunction(()=>document.querySelectorAll('.exchange-marked').length===2);
    const reloadedExchange=(await (await fetch(base+'/api/sessions/'+liveSelectionSession.id)).json()).marks.filter(m=>m.anchor==='exchange');
    assert.equal(reloadedExchange.length,1);
    await request('/api/sessions/'+liveSelectionSession.id+'/advance',{phase:'chat'});
    await request('/api/sessions/'+liveSelectionSession.id+'/advance',{phase:'post',text:'Synthetic final answer'});
    await selectionPage.goto(base+'/review?id='+liveSelectionSession.id);
    await selectionPage.locator(`.flag-card[data-flag="${exchangeMark.id}"]`).waitFor();
    assert((await selectionPage.locator(`.flag-card[data-flag="${exchangeMark.id}"]`).textContent()).includes('Exchange marked during interaction'));
    assert.equal(await selectionPage.locator(`.flag-card[data-flag="${exchangeMark.id}"] [data-start]`).inputValue(),exchangeIds[0]);
    assert.equal(await selectionPage.locator(`.flag-card[data-flag="${exchangeMark.id}"] [data-end]`).inputValue(),exchangeIds[1]);
    assert.equal(await selectionPage.locator('.tr-ev.exchange-marked').count(),2);
    await selectionPage.screenshot({path:path.join(data,'exchange-review.png'),fullPage:true});
    await selectionPage.close();
    if (runtime==='claude') {
      const questionSession=await request('/api/sessions',{study:'dilemma',participant_id:'TEST-CLAUDE-QUESTION',runtime:'claude'});
      await request('/api/sessions/'+questionSession.id+'/advance',{phase:'pre',text:'Synthetic baseline'});
      const qpage=await context.newPage(); qpage.setDefaultTimeout(15000); qpage.on('pageerror',err=>errors.push(err.message));
      await qpage.goto(base+'/session?id='+questionSession.id);
      await qpage.waitForFunction(()=>document.querySelector('#runtime-panel')?.textContent.includes('ready'));
      await qpage.locator('#input').fill('[ASK] Make a file'); await qpage.locator('#send').click();
      await qpage.locator('.approval-card [data-answer="0"]').fill('Text, because I want a written explanation');
      // Force reconnect/SSE refresh while a native question is pending.
      await qpage.evaluate(()=>window.dispatchEvent(new Event('online')));
      await qpage.locator('.approval-card [data-decision=accept]').click();
      await qpage.waitForFunction(()=>document.querySelector('#runtime-panel')?.textContent.includes('ready'));
      const qexport=await (await fetch(base+'/api/sessions/'+questionSession.id+'/export.json')).json();
      assert(qexport.events.some(e=>e.meta?.source==='agent_question_answer' && e.text.includes('written explanation')));
      await qpage.reload();
      await qpage.waitForFunction(()=>document.querySelector('#runtime-panel')?.textContent.includes('ready'));
      const recovered=await (await fetch(base+'/api/sessions/'+questionSession.id)).json();
      assert.deepEqual(recovered.events.map(e=>e.id),qexport.events.map(e=>e.id));
    }
    async function stateSession(pid, withMessage) {
      const s = await request('/api/sessions',{study:'dilemma',participant_id:pid,condition:withMessage?'human':'ai',room:pid});
      await request('/api/sessions/'+s.id+'/advance',{phase:'pre',text:'Synthetic baseline'});
      if (withMessage) await request('/api/sessions/'+s.id+'/message',{text:'Synthetic participant message'});
      await request('/api/sessions/'+s.id+'/advance',{phase:'chat'});
      await request('/api/sessions/'+s.id+'/advance',{phase:'post',text:'Synthetic final position'});
      await request('/api/sessions/'+s.id+'/review/complete-independent',{});
      const p = await context.newPage(); p.setDefaultTimeout(15000); p.on('pageerror',err=>errors.push(err.message));
      await p.goto(base+'/review?id='+s.id);
      return {p,s};
    }
    const missing = await stateSession('TEST-NO-TRANSCRIPT',false);
    await missing.p.locator('[data-judge-status=waiting_for_transcript]').waitFor();
    assert((await missing.p.locator('#suggestions').textContent()).includes('No conversation messages were captured'));
    assert.equal((await (await fetch(base+'/api/sessions/'+missing.s.id+'?view=researcher')).json()).analysis_runs.length,0);
    const empty = await stateSession('TEST-JUDGE-EMPTY',true);
    await empty.p.locator('[data-judge-status=done]').waitFor();
    assert((await empty.p.locator('#suggestions').textContent()).includes('completed and found no candidate'));
    const failed = await stateSession('TEST-JUDGE-ERROR',true);
    await failed.p.locator('[data-judge-status=error]').waitFor();
    await failed.p.locator('#retry-judge').click();
    await failed.p.locator('[data-judge-status=done]').waitFor();
    assert.equal((await (await fetch(base+'/api/sessions/'+failed.s.id+'?view=researcher')).json()).analysis_runs.length,2);
    // A validation set must keep source/model/participant annotations blind to
    // both coders, persist their answers separately, then unblind researcher results.
    const validation = await request('/api/validations',{name:'Synthetic blinded validation',session_ids:[sid],
      candidate_n:1,report_n:0,control_n:0,seed:'browser-fixed',coder_a:'Coder A',coder_b:'Coder B'});
    assert.equal(validation.items,1);
    for (const coder of validation.coder_links) {
      const coderPage=await context.newPage(); coderPage.setDefaultTimeout(15000); coderPage.on('pageerror',err=>errors.push(err.message));
      await coderPage.goto(base+`/validation?sample=${validation.id}&token=${encodeURIComponent(coder.token)}`);
      await coderPage.locator('#validation-code').waitFor();
      const coderText=await coderPage.locator('body').textContent();
      assert(!coderText.includes('judge candidate')); assert(!coderText.includes('SYNTHETIC_TEST_SUGGESTION'));
      await coderPage.locator('[name=evidence]').selectOption('possible');
      await coderPage.locator('[name=primary_type]').selectOption('additive');
      await coderPage.locator('[name=uptake]').selectOption('unclear');
      await coderPage.locator('[name=span_quality]').selectOption('adequate');
      await coderPage.locator('[name=confidence]').selectOption('medium');
      await coderPage.locator('#validation-code button').click();
      await coderPage.waitForFunction(()=>document.querySelector('.validation-nav')?.textContent.includes('1/1 saved'));
      const blinded=await (await fetch(base+`/api/validations/${validation.id}?token=${encodeURIComponent(coder.token)}`)).json();
      assert(!JSON.stringify(blinded).includes('stratum')); assert.equal(Object.keys(blinded.codes).length,1);
      await coderPage.close();
    }
    const resultsPage=await context.newPage(); resultsPage.setDefaultTimeout(15000); resultsPage.on('pageerror',err=>errors.push(err.message));
    await resultsPage.goto(base+`/validation?sample=${validation.id}&view=results`);
    await resultsPage.getByText('Inter-coder agreement',{exact:true}).waitFor();
    const validationResult=await (await fetch(base+`/api/validations/${validation.id}?view=researcher`)).json();
    assert.equal(validationResult.complete_items,1); assert.equal(validationResult.agreement.evidence.cohen_kappa,1);
    assert(JSON.stringify(validationResult).includes('judge_candidate'));
    await resultsPage.screenshot({path:path.join(data,'judge-validation.png'),fullPage:true});
    if (runtime === 'codex') {
      const voicePage=await context.newPage(); voicePage.setDefaultTimeout(15000);
      voicePage.on('pageerror',err=>errors.push(err.message));
      voicePage.on('dialog',dialog=>dialog.accept());
      await voicePage.addInitScript(() => {
        const track={enabled:true,stop(){}};
        Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{
          getUserMedia:async()=>({getAudioTracks:()=>[track],getTracks:()=>[track]})}});
        window.RTCPeerConnection=class {
          constructor(){this.iceGatheringState='complete';this.signalingState='stable';this.connectionState='new';}
          addTrack(){} createDataChannel(){} addEventListener(){} removeEventListener(){}
          async createOffer(){return {type:'offer',sdp:'v=0\r\nmock-offer'};}
          async setLocalDescription(offer){this.localDescription=offer;}
          async setRemoteDescription(answer){assertAnswer(answer);this.connectionState='connected';this.onconnectionstatechange?.();}
          close(){this.signalingState='closed';this.connectionState='closed';}
        };
        function assertAnswer(answer){if(answer.sdp!=='v=0\r\nmock-answer')throw new Error('Unexpected mock SDP');}
      });
      await voicePage.goto(base);
      await voicePage.locator('#start button[type=submit]:enabled').waitFor();
      await voicePage.locator('#start [name=participant_id]').fill('TEST-VOICE');
      assert(await voicePage.locator('#interaction-field').isVisible());
      await voicePage.locator('#start [name=interaction_mode]').selectOption('voice');
      await voicePage.locator('#start button[type=submit]').click();
      await voicePage.waitForURL(/\/session\?id=/);
      const voiceSid=new URL(voicePage.url()).searchParams.get('id');
      await voicePage.locator('#essay').fill('I want to compare several options and understand their tradeoffs before deciding.');
      await voicePage.locator('#submit').click();
      await voicePage.locator('#voice-stage').waitFor({state:'visible'});
      assert(!(await voicePage.locator('#voice-start').isVisible()));
      await voicePage.waitForFunction(()=>document.querySelector('#runtime-panel')?.textContent.includes('ready'));
      assert(await voicePage.locator('#composer-wrap').isVisible());
      await voicePage.locator('.msg-ai').filter({hasText:'[TEST MOCK] Voice transcript.'}).waitFor();
      assert(await voicePage.locator('#voice-stage').isVisible());
      assert(!(await voicePage.locator('#voice-start').isVisible()));
      assert(await voicePage.locator('#composer-wrap').isVisible());
      assert(await voicePage.locator('.voice-orb').isVisible());
      assert((await voicePage.locator('#voice-status').textContent()).includes('Listening'));
      await voicePage.locator('#voice-mute').click();
      assert.equal(await voicePage.locator('#voice-mute').getAttribute('aria-pressed'),'true');
      await voicePage.locator('#fab').click();
      await voicePage.waitForFunction(async sid=>(await (await fetch('/api/sessions/'+sid)).json()).marks.length===1,voiceSid);
      assert.equal((await (await fetch(base+'/api/sessions/'+voiceSid)).json()).marks[0].source,'voice');
      await voicePage.waitForFunction(()=>{
        const toast=document.querySelector('.toast');
        return !toast || getComputedStyle(toast).opacity==='0';
      });
      await voicePage.screenshot({path:path.join(data,'voice-interface.png'),fullPage:true});
      await voicePage.setViewportSize({width:390,height:844});
      assert(await voicePage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      const orbBox=await voicePage.locator('.voice-orb').boundingBox();
      const markBox=await voicePage.locator('#fab').boundingBox();
      assert(orbBox && markBox && markBox.x >= orbBox.x + orbBox.width);
      assert(await voicePage.locator('#composer-wrap').isVisible());
      assert(await voicePage.locator('#voice-end').isVisible());
      await voicePage.screenshot({path:path.join(data,'voice-interface-mobile.png'),fullPage:true});
      assert(await voicePage.locator('#finish').isDisabled());
      await voicePage.locator('#voice-end').click();
      await voicePage.locator('#voice-start').waitFor({state:'visible'});
      await voicePage.reload();
      await voicePage.locator('#voice-start').waitFor({state:'visible'});
      assert(!(await voicePage.locator('#voice-stage').isVisible()));
      await voicePage.evaluate(()=>{navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('Permission denied','NotAllowedError');};});
      await voicePage.locator('#voice-start').click();
      await voicePage.getByText("Voice couldn't start. Check microphone access, then retry.").waitFor();
      assert.equal(await voicePage.locator('#voice-start').getAttribute('aria-label'),'Retry voice chat');
      const voiceExport=await (await fetch(base+'/api/sessions/'+voiceSid+'/export.json')).json();
      assert(voiceExport.events.some(e=>e.meta?.voice && e.text.includes('Voice transcript')));
      assert.equal(voiceExport.marks.length,1);
      await voicePage.close();
    }
    const reversible = await request('/api/sessions', {study:'dilemma',participant_id:'TEST-UNMARK',condition:'human',room:'UNMARK-ROOM'});
    await request(`/api/sessions/${reversible.id}/advance`, {phase:'pre',text:'I will consider both sides of the case.'});
    const markPage = await context.newPage(); markPage.setDefaultTimeout(15000); markPage.on('pageerror',err=>errors.push(err.message));
    await markPage.goto(base+'/session?id='+reversible.id);
    await markPage.locator('#fab').click();
    await markPage.locator('#fab').click();
    await markPage.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks.length === 1, reversible.id);
    assert.equal(await markPage.locator('#fab-count').textContent(), '1');
    await markPage.locator('.bookmark-undo').click();
    await markPage.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks[0].deleted, reversible.id);
    await markPage.locator('#fab').click();
    await markPage.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks.length === 2, reversible.id);
    await request(`/api/sessions/${reversible.id}/advance`, {phase:'chat'});
    await request(`/api/sessions/${reversible.id}/advance`, {phase:'post',text:'My final answer considers both sides.'});
    await markPage.goto(base+'/review?id='+reversible.id);
    await markPage.locator('.flag-card [data-remove-moment]').click();
    await markPage.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks.every(m => m.deleted), reversible.id);
    await markPage.waitForFunction(() => document.querySelectorAll('.flag-card').length === 0);
    await markPage.close();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, tests: ['create','stream','approval','tools','upload','artifact','offline bookmark recovery','exposure snapshot','lost save ACK','multi-tab conflict','draft autosave recovery','multi-event passage','missed moment','automatic judging','no duplicate calls on refresh','independent blinding','retrospective suggestion','final locking','export','blinded double-coder validation'],
      status_tests:['missing transcript','completed empty result','failed result and user retry'],screenshot: path.join(data, 'spark-review.png') }));
  } finally {
    if (browser) await browser.close();
    processHandle.kill('SIGINT');
    await new Promise(resolve => processHandle.once('exit', resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
