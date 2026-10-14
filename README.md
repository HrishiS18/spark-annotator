# Spark Annotator

Local research prototype for **sparks**: participant-perceived valuable ideas or insights. Perceived value, adoption, persuasion, copying, and eventual usefulness are separate measurements.

## Start locally

```bash
cd spark-annotator
python3 -m venv .venv
.venv/bin/python server.py
```

Open http://127.0.0.1:8765. The default integrated partner is a **real Codex app-server agent** with streamed messages, command/file tools, explicit approvals, a task workspace, and interrupt/resume controls.

Codex must be installed on the execution host and signed in (`codex login`). The macOS desktop-app executable is detected if absent from PATH. Existing Codex/ChatGPT login works for local development; API-key authentication is also supported by the agent bridge using server-side configuration. Never put keys in participant messages or configuration files.

Optional **Plain LLM chat** (Anthropic, no tools) and **Scripted demo** are explicitly selected alternatives. Live failures do not silently become demo responses. `SPARK_DEMO=1` does not disable Codex inference.

## Participant flow

Integrated studies: **task → unaided baseline → interaction → unaided final write-up → independent review → optional model suggestions**.

During interaction:

- One-click bookmarking; optional plain-language taxonomy tags.
- Stable IDs, original click timestamps, durable browser outbox, automatic retry, honest pending/saved feedback.
- An integrated bookmark captures displayed message/tool text at click, including partial output. It is a display snapshot, **not evidence the participant read it**.
- Timestamp-only clicks are not automatically attributed to the last assistant message.
- Explicit message/selection clicks preserve their original anchor. Later corrections do not rewrite the click or snapshot.

Review is the same transcript-first interface for integrated and external sessions:

- Confirm **Yes / Unsure / No**. A confirmed insight needs a brief explanation in the participant's words.
- Categories, before-state, perceived value, uptake, later helpfulness, latency, and shift size are optional.
- Select any ordered multi-event passage, including the participant's own messages; read full expandable messages/tool output.
- Add missed moments without requiring a video.
- Drafts autosave to the server and recover locally after interruption. Final submission is distinct from draft saving.
- Revision checks reject stale edits from another tab; earlier answers remain in history.
- Finish the independent pass to lock its answers **before** seeing LLM suggestions.
- Suggested moments are recorded separately, distinguishing noticed-at-the-time from only-now recognition. Suggestions never overwrite the independent pass.

Existing legacy annotations remain preserved; they are not automatically converted into confirmed insights. Legacy model outputs remain researcher-visible but are not offered as participant suggestions; rerun the current judge for validated suggestions.

## LLM judge (no regex spark detector)

The old lexical novelty/phrase rules, copying threshold, default-to-spark rule, automatic before/after tracing, and `/detect`, `/trace`, `parse.csv` workflows are removed. Historical results remain in research exports. `parsers.py` still normalizes Codex/Claude/chat log formats; it does **not** infer sparks.

Run the judge explicitly from the researcher dashboard after conversation completion. Importing a log or ending a conversation does not start paid inference.

The local default uses your existing **Codex login**, model `gpt-5.6-sol`, in a separate ephemeral, read-only process with tools/connectors disabled and study records fenced off. The judge sees only supplied task, baseline, and visible transcript, not bookmarks, reviews, exposure snapshots, prior predictions, researcher codes, or hidden reasoning. Local inference still sends this text to the provider and consumes account usage.

Each candidate includes:

- Ordered start/end/trigger/participant-evidence event IDs.
- Fully verified verbatim quotes; invalid IDs, chronology, or quotations are rejected and retained as rejected predictions.
- Insight evidence: **expressed / possible / insufficient**; type may be unclear.
- Independent **persuasion, copying, uptake** labels; these can coexist with an insight.
- Qualitative strength, rationale, and before/after interpretation—not a calibrated probability.

Zero candidates is valid. Missing participant evidence is uncertainty, not proof that no spark occurred. Neither model predictions nor passage overlap constitute ground truth or accuracy.

Long transcripts use explicit overlapping windows; reports record whether scope was whole-session or windowed. Oversized individual events/context fail explicitly rather than silently truncating. Prompt, schema, model/effort, input hash/text, usage, rejected predictions, and every run are saved. Errors never fall back to heuristics.

Settings:

- `SPARK_JUDGE_PROVIDER=codex` (default; local macOS prototype).
- `SPARK_JUDGE_MODEL`, `SPARK_JUDGE_EFFORT` (default medium).
- `SPARK_JUDGE_MAX_INPUT_TOKENS` (default 60000), `SPARK_JUDGE_TIMEOUT` (default 180 seconds).
- Optional legacy API adapter: `SPARK_JUDGE_PROVIDER=anthropic`, install `requirements.txt`, configure credentials and an available model on the host. This adapter has not been live-validated with the current account.

Batch judging is an explicit inference action:

```bash
.venv/bin/python tools/run_judge.py --all --dry-run
.venv/bin/python tools/run_judge.py --session s_12345678
.venv/bin/python tools/run_judge.py --all --skip-done
```

`--all` selects ended conversations with transcripts. No fabricated USD estimate is displayed.

## External-agent recording

Open `/record` in desktop Chrome/Edge. Share screen (video only), pop out the optional always-on-top Flag button, work in Codex/Claude Code/etc., then end recording and attach selected agent logs. Video is uploaded in 4-second chunks. Screen capture requires localhost or HTTPS; macOS screen-recording permission may also be needed.

Logs: Codex `~/.codex/sessions/.../rollout-*.jsonl`, Claude Code `~/.claude/projects/.../*.jsonl`. JSON, JSONL, and exporter ZIPs are supported. Web-chat users can skip logs and review timestamped bookmarks from video.

Imported timestamp alignment supplies contextual suggestions, not confirmed causal anchors. External flags do not have an integrated rendered-text snapshot.

## Storage / exports

`data/study.sqlite3` is authoritative. Existing `data/sessions/*.json` files are imported once and never modified/deleted. Stop the old server before starting this version.

- Separate, versioned events, bookmarks, drafts, submitted reviews, researcher codes, and model predictions.
- SQLite transactions and revision checks; full historical analysis runs and archived responses.
- Re-import preserves event identities, prior evidence, and annotations.
- Workspaces: `data/workspaces/<session-id>/`; uploaded logs: `data/uploads/`; video: `data/recordings/`.
- Full session JSON includes research history/runtime traces; CSV exports contain current participant annotations and LLM candidates. CSV text fields are abbreviated; use JSON/full transcript for full evidence.
- Browser localStorage temporarily contains unsynced bookmarks and review drafts. Consider shared-device cleanup and retention in the study protocol.

Back up SQLite using a SQLite-aware backup or with the server stopped. Copying the database without its active WAL is not a reliable backup. All `data/` is git-ignored.

Study data is local to the execution host, **not model inference**. Codex also maintains agent runtime state under its configured Codex home. Include provider transmission, recordings, full judge inputs, browser drafts, runtime logs, and retention in consent decisions.

## Prototype limits

**macOS execution host + localhost only for integrated Codex.** The macOS privacy fence protects study records and other sessions' workspaces. Workspace-write sandboxing and explicit approvals do not provide full host/credential isolation. Use synthetic, non-sensitive tasks during development.

Researcher/participant authentication, hosted execution isolation, quotas, resource limits, pairing/deployment hardening, multi-coder blind adjudication, and validated judge evaluation remain unfinished. UI/API-view hiding of suggestions is a study-flow safeguard, **not an authorization boundary**; researcher/export routes are currently unauthenticated. Do not expose this publicly or share a researcher's ChatGPT login as a hosted service credential.

Task templates/personas are configurable in `config/`; a full factorial/counterbalanced study assignment system remains future work. Human-partner chat is a prototype comparison mode, not production participant matching.

Tested local runtime: **codex-cli 0.162.0-alpha.2**. Pin/test before a study. Session snapshots record task/persona/runtime configuration. [Official app-server docs](https://learn.chatgpt.com/docs/app-server), [CLI reference](https://learn.chatgpt.com/docs/cli/reference).

Agent settings: `SPARK_CODEX_BIN`, `SPARK_CODEX_MODEL`, `SPARK_CODEX_EFFORT`, `SPARK_TURN_TIMEOUT`, `SPARK_DATA_DIR`. Changing the default model does not alter an existing pinned thread.

## Verification

No model calls in unit/API or headless browser tests:

```bash
SPARK_QUIET=1 .venv/bin/python -m unittest discover -s tests -v
node tests/browser_smoke.cjs
```

Browser QA requires Playwright plus Chrome/Chromium; optionally set `NODE_PATH` and `SPARK_CHROME_PATH`. Covers agent controls, offline bookmark recovery, exposure snapshots, autosave/reload, multi-event passages, missed moments, suggestion blinding, independent-pass locking, and exports.

Live tests are separately opt-in, **synthetic only**, and consume account usage:

```bash
.venv/bin/python tests/live_smoke.py --run-live
.venv/bin/python tests/live_judge_smoke.py --run
```

## Code map

```text
server.py             API, streaming, recording, imports, exports
storage.py            transactional records and historical projections
codex_runtime.py      agent app-server bridge, approvals, workspace, resume
annotations.py        participant reviews, evidence passages, locking
judge.py              blinded LLM coding, quote/ID validation
judge_backend.py      restricted Codex judge / optional API adapter
parsers.py            external log normalization (not inference)
transcript.py         full timestamped transcript rendering
static/bookmarks.js   durable retry outbox and click snapshots
static/review_forms.js autosave, conflict checks, optional review fields
static/               participant/researcher/recorder/review interfaces
config/               taxonomy, study templates, personas
tests/                synthetic unit/API/browser/live verification
```
