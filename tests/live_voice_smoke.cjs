// Opt-in: exercises Codex's native voice transport with a silent synthetic mic.
// Uses a temporary study database and the signed-in host Codex account.
if (process.env.SPARK_LIVE_VOICE !== '1') {
  console.error('Set SPARK_LIVE_VOICE=1 to run a real Codex voice connection test.');
  process.exit(2);
}

const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const root = path.resolve(__dirname, '..');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'spark-live-voice-'));
  const port = Number(process.env.SPARK_TEST_PORT || 8889);
  const base = `http://127.0.0.1:${port}`;
  const python = process.env.SPARK_TEST_PYTHON || path.join(root, '.venv/bin/python');
  const server = spawn(python, ['server.py', '--port', String(port)], {
    cwd: root, env: { ...process.env, SPARK_DATA_DIR: data, SPARK_AUTO_JUDGE: '0', SPARK_QUIET: '1' },
    stdio: ['ignore', 'pipe', 'pipe']
  });
  let serverError = '';
  server.stderr.on('data', chunk => { serverError += chunk.toString(); });
  let browser;
  try {
    let speechFile;
    if (process.env.SPARK_LIVE_VOICE_SPEECH === '1') {
      const aiff = path.join(data, 'synthetic-prompt.aiff');
      speechFile = path.join(data, 'synthetic-prompt.wav');
      execFileSync('/usr/bin/say', ['-o', aiff, 'What is this task about? Please answer in one short sentence.']);
      execFileSync('/usr/bin/afconvert', ['-f', 'WAVE', '-d', 'LEI16', aiff, speechFile]);
    }
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(base + '/api/config')).ok) { ready = true; break; } } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert(ready, `Study server did not start: ${serverError.slice(-500)}`);
    browser = await chromium.launch({ headless: true,
      ...(process.env.SPARK_CHROME_PATH ? { executablePath: process.env.SPARK_CHROME_PATH } : {}),
      args: ['--autoplay-policy=no-user-gesture-required'] });
    const page = await browser.newPage();
    page.setDefaultTimeout(45000);
    page.on('dialog', dialog => dialog.accept());
    if (speechFile) await page.route('**/__spark_speech.wav', route => route.fulfill({
      body: fs.readFileSync(speechFile), contentType: 'audio/wav' }));
    await page.addInitScript(() => {
      const native = navigator.mediaDevices;
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
        ...native,
        getUserMedia: async () => {
          const audio = new AudioContext();
          const destination = audio.createMediaStreamDestination();
          window.__voiceSmokeAudio = { audio, destination };
          return destination.stream;
        }
      } });
    });
    await page.goto(base);
    await page.locator('#start button[type=submit]:enabled').waitFor();
    await page.locator('[name=participant_id]').fill('SYNTHETIC-LIVE-VOICE');
    await page.locator('details.home-settings summary').click();
    await page.locator('[name=interaction_mode]').selectOption('voice');
    await page.locator('#start button[type=submit]').click();
    await page.waitForURL(/\/session\?id=/);
    const sid = new URL(page.url()).searchParams.get('id');
    await page.locator('#essay').fill('Synthetic connection test. No participant data or task work is used.');
    await page.locator('#submit').click();
    await page.waitForFunction(() => document.querySelector('#runtime-panel')?.textContent.includes('ready'));
    await page.locator('#voice-start').click();
    await page.waitForFunction(() => document.querySelector('#voice-status')?.textContent === 'Listening', null, { timeout: 70000 });
    const started = await (await fetch(`${base}/api/sessions/${sid}`)).json();
    assert.equal(started.runtime.voice.status, 'active');
    if (speechFile) {
      await page.evaluate(async () => {
        const { audio, destination } = window.__voiceSmokeAudio;
        await audio.resume();
        const response = await fetch('/__spark_speech.wav');
        const clip = await audio.decodeAudioData(await response.arrayBuffer());
        const source = audio.createBufferSource();
        source.buffer = clip;
        source.connect(destination);
        source.start();
        await new Promise(resolve => { source.onended = resolve; });
      });
      let roundTrip = false;
      const deadline = Date.now() + 55000;
      while (Date.now() < deadline) {
        const session = await (await fetch(`${base}/api/sessions/${sid}`)).json();
        roundTrip = ['user', 'assistant'].every(role => session.events.some(event =>
          event.meta?.voice && event.role === role && event.status === 'completed' && event.text.trim()));
        if (roundTrip) break;
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      assert(roundTrip, 'Native voice did not produce both user and assistant transcript segments.');
    }
    await page.locator('#voice-end').click();
    await page.waitForFunction(() => document.querySelector('#voice-status')?.textContent === 'Ready');
    console.log(JSON.stringify({ passed: true, codexVersion: started.runtime.version,
      realtimeVersion: started.runtime.voice.version, model: started.runtime.model,
      transport: 'WebRTC', spokenRoundTrip: !!speechFile, studyData: data }));
  } catch (error) {
    const activePage = browser?.contexts()[0]?.pages()[0];
    const state = activePage ? await activePage.locator('#voice-caption').textContent().catch(() => '') : '';
    console.error(JSON.stringify({ error: error.message, voiceCaption: state, serverError: serverError.slice(-500), studyData: data }));
    process.exitCode = 1;
  } finally {
    await browser?.close();
    server.kill('SIGINT');
    if (server.exitCode === null) await new Promise(resolve => server.once('exit', resolve));
  }
})();
