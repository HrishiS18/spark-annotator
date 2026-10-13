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
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
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
    assert(await page.locator('#send').isDisabled());
    await page.locator('.msg-ai .mark-here').first().click();
    await page.locator('.mark-pop').waitFor({ timeout: 2000 }).catch(() => {});
    await page.keyboard.press('Escape');
    await page.locator('.approval-card [data-decision=accept]').click();
    await page.waitForFunction(() => document.querySelector('#runtime-panel')?.textContent.includes('ready'));
    const before = await (await fetch(`${base}/api/sessions/${sid}`)).json();
    assert.equal(before.marks.length, 1);
    assert(before.events.some(event => event.role === 'tool_result'));
    const anchor = before.marks[0].event_id;
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#runtime-panel')?.textContent.includes('ready'));
    const after = await (await fetch(`${base}/api/sessions/${sid}`)).json();
    assert.deepEqual(after.events.map(event => event.id), before.events.map(event => event.id));
    assert.equal(after.marks[0].event_id, anchor);
    await page.locator('.workspace-panel summary').click();
    await page.getByRole('button', { name: 'summary.txt', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#file-preview')?.textContent.includes('Test-only artifact'));
    await page.screenshot({ path: path.join(data, 'agent-interface.png'), fullPage: true });
    await page.locator('#finish').click();
    await page.locator('#essay').fill('I have now inspected the task file and used the resulting artifact to explain my final approach.');
    await page.locator('#submit').click();
    await page.locator('[data-f=after]').first().fill('The suggestion helped me identify the first step.');
    await page.locator('[data-type=additive]').first().click();
    await page.locator('[data-save]').first().click();
    await page.waitForFunction(() => document.querySelector('.review-card')?.classList.contains('saved'));
    const exported = await (await fetch(`${base}/api/sessions/${sid}/export.json`)).json();
    assert.equal(exported.marks[0].review.after, 'The suggestion helped me identify the first step.');
    assert.equal(exported.marks[0].event_id, anchor);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, tests: ['create', 'baseline', 'stream', 'approval', 'tools', 'upload', 'artifact', 'bookmark', 'refresh', 'review', 'export'], screenshot: path.join(data, 'agent-interface.png') }));
  } finally {
    if (browser) await browser.close();
    processHandle.kill('SIGINT');
    await new Promise(resolve => processHandle.once('exit', resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
