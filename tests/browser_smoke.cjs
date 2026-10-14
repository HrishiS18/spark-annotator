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
    await page.locator('#start [name=participant_id]').fill('BROWSER-TEST');
    assert.equal(await page.locator('#start [name=runtime]').inputValue(), 'codex');
    await page.locator('#start button[type=submit]').click();
    await page.waitForURL(/\/session\?id=/);
    await page.locator('#essay').fill('My initial approach is to inspect the available evidence and identify the most important decision first.');
    await page.locator('#submit').click();
    await page.waitForFunction(() => document.querySelector('#runtime-panel')?.textContent.includes('ready'));
    const sid = new URL(page.url()).searchParams.get('id');
    await page.locator('.workspace-panel summary').click();
    await page.locator('#workspace-upload').setInputFiles({ name: 'input.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic task file') });
    await page.getByRole('button', { name: 'input.txt', exact: true }).waitFor();
    await page.locator('#input').fill('Inspect the task file and produce a short summary.');
    await page.locator('#send').click();
    await page.locator('.approval-card').waitFor();
    console.log('QA: streamed approval ready');
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
    // Researcher inference can run first, but must remain hidden from the independent pass.
    const dashboard = await browser.newPage();
    dashboard.setDefaultTimeout(15000);
    dashboard.on('pageerror', err => errors.push(err.message));
    dashboard.on('dialog', dialog => dialog.accept());
    await dashboard.goto(base+'/researcher#'+sid);
    await dashboard.locator('#run-judge').click();
    await dashboard.waitForFunction(() => document.querySelector('#main')?.textContent.includes('SYNTHETIC_TEST_SUMMARY'));
    await dashboard.screenshot({path:path.join(data,'researcher-console.png'),fullPage:true});
    const stillBlind = await (await fetch(`${base}/api/sessions/${sid}`)).json();
    assert.deepEqual(stillBlind.candidates,[]);
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
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, tests: ['create','stream','approval','tools','upload','artifact','offline bookmark recovery','exposure snapshot','lost save ACK','multi-tab conflict','draft autosave recovery','multi-event passage','missed moment','independent blinding','researcher judge','retrospective suggestion','final locking','export'],
      screenshot: path.join(data, 'spark-review.png') }));
  } finally {
    if (browser) await browser.close();
    processHandle.kill('SIGINT');
    await new Promise(resolve => processHandle.once('exit', resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
