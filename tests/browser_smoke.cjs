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
    assert.equal(await page.getByRole('link',{name:'Screen recorder',exact:true}).count(),0);
    assert.equal(await page.getByRole('link',{name:'Researcher dashboard',exact:true}).count(),0);
    await page.locator('#start [name=condition]').selectOption('human');
    assert(await page.locator('#room-field').isVisible());
    assert(await page.locator('#runtime-field').isHidden());
    assert(await page.locator('#claude-setup').isHidden());
    assert(await page.locator('#start [name=room]').evaluate(input=>input.required));
    await page.locator('#start [name=condition]').selectOption('ai');
    assert(await page.locator('#room-field').isHidden());
    assert(await page.locator('#start [name=room]').isDisabled());
    await page.locator('details.home-settings summary').click();
    await page.locator('#start [name=study]').selectOption('trading');
    assert.equal(await page.locator('#start [name=persona]').inputValue(),'trading');
    await page.locator('#start [name=study]').selectOption('dilemma');
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
    await page.locator('.msg-ai .mark-here').first().click();
    await page.locator('.mark-pop').waitFor({ timeout: 2000 }).catch(() => {});
    await page.keyboard.press('Escape');
    await page.waitForFunction(sid => JSON.parse(localStorage.getItem('spark-outbox:'+sid) || '{}') &&
      Object.keys(JSON.parse(localStorage.getItem('spark-outbox:'+sid) || '{}')).length === 1, sid);
    await page.context().setOffline(false);
    console.log('QA: offline bookmark queued');
    await page.reload();
    await page.locator('.approval-card').waitFor();
    await page.waitForFunction(async sid => (await (await fetch('/api/sessions/'+sid)).json()).marks.length === 1, sid);
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
    await page.locator('#essay').fill('I have now inspected the task file and used the resulting artifact to explain my final approach.');
    await page.locator('#submit').click();
    await page.waitForURL(/\/review\?id=/);
    await page.locator('[data-f=after]').first().waitFor();
    console.log('QA: review opened');
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
    await page.locator('[data-start]').first().selectOption(userEvent);
    await page.locator('[data-end]').first().selectOption(endEvent);
    await page.locator('[data-decision=confirmed]').first().click();
    await page.locator('.optional-review summary').first().click();
    await page.locator('[data-type=additive]').first().click();
    await page.locator('[data-save]').first().click();
    await page.waitForFunction(() => document.querySelector('.review-card')?.classList.contains('saved'));
    const exported = await (await fetch(`${base}/api/sessions/${sid}/export.json`)).json();
    assert.equal(exported.marks[0].review.after, 'The suggestion helped me identify the first step.');
    assert.equal(exported.marks[0].original_event_id, anchor);
    assert.equal(exported.marks[0].episode.start_event_id, userEvent);
    assert.equal(exported.marks[0].episode.end_event_id, endEvent);
    await page.locator('#add-moment').click();
    await page.waitForFunction(() => document.querySelectorAll('.flag-card').length === 2);
    const missed = page.locator('.flag-card').last();
    await missed.locator('[data-decision=rejected]').click();
    await missed.locator('[data-save]').click();
    await page.waitForFunction(() => document.querySelectorAll('.review-card.saved').length === 2);
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
    await page.waitForFunction(() => document.querySelector('#finish')?.textContent === 'Finish session');
    assert(await page.locator('[data-f=after]').first().isDisabled());
    await page.locator('[data-answer=retrospective]').first().click();
    await page.waitForFunction(() => document.querySelectorAll('.flag-card').length === 3);
    const suggested = page.locator('.flag-card').last();
    await suggested.locator('[data-decision=confirmed]').click();
    await suggested.locator('[data-f=after]').fill('Only during review did I recognize a new possible starting point.');
    await suggested.locator('[data-save]').click();
    await page.waitForFunction(() => document.querySelectorAll('.review-card.saved').length === 3);
    await page.locator('#finish').click();
    await page.waitForFunction(() => !document.querySelector('#finish'));
    const final = await (await fetch(`${base}/api/sessions/${sid}/export.json`)).json();
    assert.equal(final.phase,'done');
    assert.equal(final.independent_review.mark_ids.length,2);
    assert.equal(final.marks[2].source,'candidate_confirm');
    assert.equal(final.candidates[0].participant_response.answer,'retrospective');
    assert(await suggested.locator('[data-f=after]').isDisabled());
    // Distinguish missing transcript, completed empty result, and failed generation.
    const request = async (url,body) => {
      const result = await fetch(base+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      assert(result.ok); return result.json();
    };
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
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, tests: ['create','stream','approval','tools','upload','artifact','offline bookmark recovery','exposure snapshot','lost save ACK','multi-tab conflict','draft autosave recovery','multi-event passage','missed moment','automatic judging','no duplicate calls on refresh','independent blinding','retrospective suggestion','final locking','export','blinded double-coder validation'],
      status_tests:['missing transcript','completed empty result','failed result and user retry'],screenshot: path.join(data, 'spark-review.png') }));
  } finally {
    if (browser) await browser.close();
    processHandle.kill('SIGINT');
    await new Promise(resolve => processHandle.once('exit', resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
