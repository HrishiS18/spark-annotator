# Spark Annotator

Local research prototype for **sparks**: participant-perceived valuable ideas or insights. Perceived value, adoption, persuasion, copying, and eventual usefulness are separate measurements.

## Start locally

```bash
cd spark-annotator
python3 -m venv .venv
.venv/bin/python server.py
```

Open http://127.0.0.1:8765. The two integrated AI choices are **Codex app-server** and **Claude Agent SDK**. Both use their native agent loops with streamed messages, command/file tools, approvals, a task workspace, and interrupt/resume controls. The default remains Codex.

Codex must be installed on the execution host and signed in (`codex login`). The macOS desktop-app executable is detected if absent from PATH. Existing Codex/ChatGPT login works for local development; API-key authentication is also supported by the agent bridge using server-side configuration. Never put keys in participant messages or configuration files.

Codex sessions use the built-in `:workspace` permission profile with `on-request` approvals: routine reads, edits, and commands inside the task workspace run automatically. Codex prompts only when an action must cross that boundary, and a participant can allow a matching request once or for the rest of the session. The setup page discloses this fixed study permission profile up front; participants do not choose different profiles within a condition. [Official permissions reference](https://learn.chatgpt.com/docs/permissions).

New participant sessions cannot select plain LLM chat or scripted demo. Existing legacy data and internal synthetic fixtures remain supported; no live failures become demo responses.

### Codex voice mode

On the main session setup form, choose **Codex → Conversation mode → Voice chat**. The browser keeps the transcript and composer visible, with a voice orb above the composer and microphone/stop controls inside it. The study's **Mark moment** control remains available. WebRTC connects to Codex's own thread-scoped realtime voice path, pinned to protocol v3 on the tested build. Codex still owns the voice model, agent tools, and handoffs; this app does not implement a separate speech agent or route voice through the text-chat endpoint. The app stores transcript segments and tool activity for review, but does not itself store microphone audio. Microphone access requires localhost or HTTPS. Voice is not available for Claude or human-partner sessions in this prototype.

This is a screenshot-informed visual simulation, not the Codex desktop frontend. In this prototype, typing while voice is active leaves a draft in the composer; stop voice before sending it. The app-server voice protocol is experimental, so test the exact installed Codex build and account before a study. The live smoke test below uses synthetic speech and a temporary study database. [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server), [ChatGPT Voice](https://learn.chatgpt.com/docs/features/voice).

### Claude agent setup

Install the pinned `claude-agent-sdk==0.2.165` from `requirements.txt` in the server virtual environment. It includes the Claude Code executable; participants do not need their own installation.

Configure `ANTHROPIC_API_KEY` in the execution host environment before starting the server. Never put it in task text, Git, browser forms, or a committed settings file. The adapter does not use Claude subscription/OAuth login. [Official SDK overview](https://code.claude.com/docs/en/agent-sdk/overview).

The participant setup echoes Claude Code Desktop's environment, project folder, model, and permission-mode summary, but these are fixed study settings rather than participant-selectable controls. Its `+` composer button uploads files into the task workspace; it does not expose Desktop's skills, connectors, or plugins. The app is a study interface backed by the Agent SDK, not the Claude Desktop app. [Claude Code Desktop session setup](https://code.claude.com/docs/en/desktop#start-a-session).

Claude uses `ClaudeSDKClient` in a private stdio subprocess, preserving the Claude Code system-prompt preset and appending the study persona. Native messages, tool calls/results, and questions become the same event format as Codex, with raw SDK evidence retained separately. Participant answers to native questions are saved before delivery to the agent. The baseline, flags and reviews are never passed to the task agent.

Runtime settings: `SPARK_CLAUDE_MODEL` (default native `sonnet` alias; effective model recorded), `SPARK_CLAUDE_EFFORT` (default medium), `SPARK_CLAUDE_MAX_BUDGET_USD` (default 2, SDK estimated per-query cap; not a guaranteed billing ceiling). A single macOS Seatbelt boundary covers the SDK and its tool subprocesses: study records/peer workspaces are inaccessible and writes are restricted to the task workspace and OS temporary directories. Native Bash sandboxing is explicitly off because Seatbelt cannot be nested; this is not an unsandboxed fallback. Workspace reads and edits proceed automatically; Bash and network tools still require participant approval because arbitrary commands cannot be safely scoped by the adapter. Direct file paths are checked, and the native subprocess credential scrub is enabled. Personal settings, skills, plugins, MCP connectors, and subagents are disabled in this controlled prototype. Approved commands can access the network; this is not network-isolated.

Each session persists native SDK state under `data/workspaces/<session-id>/.runtime/claude/`, separate from the app transcript. This folder is excluded from artifact browsing and direct agent file-tool access. Reconnect replays native history without inference, preserving event IDs and original click snapshots. This is not full host/credential isolation; do not deploy this using a shared researcher's key without proper isolation and quotas.

Verified locally with SDK **0.2.165**, Claude Code **2.1.294**, and effective model **claude-sonnet-5-5**. [Python SDK reference](https://code.claude.com/docs/en/agent-sdk/python).

## Participant flow

Integrated studies: **task → unaided baseline → interaction → unaided final write-up → independent review → optional model suggestions**.

The participant landing page is session setup only. Persona controls are under **Session settings**; recording and researcher tools remain at `/record` and `/researcher` rather than appearing in the participant flow. Spark categories are not shown to participants during interaction or review. Claude readiness distinguishes a missing SDK from a missing server API key, and unavailable agents cannot start a new session.

During interaction:

- One-click, taxonomy-free bookmarking. A live mark never opens a category menu.
- Stable IDs, original click timestamps, durable browser outbox, automatic retry, honest pending/saved feedback.
- An integrated bookmark captures displayed message/tool text at click, including partial output. It is a display snapshot, **not evidence the participant read it**.
- Timestamp-only clicks are not automatically attributed to the last assistant message.
- Explicit message/selection clicks preserve their original anchor. Later corrections do not rewrite the click or snapshot.

Review is the same transcript-first interface for integrated and external sessions:

- Confirm **Yes / Unsure / No**. A confirmed insight needs a brief explanation in the participant's words.
- Before-state, perceived value, uptake, later helpfulness, latency, and shift size are optional; the participant is not asked to choose a spark type. Historical category data remains in researcher exports.
- Select any ordered multi-event passage, including the participant's own messages. Highlight exact text in the transcript to attach it to an existing moment or create a new one; event ID and character offsets are retained and checked against the source text.
- Add missed moments without requiring a video.
- Drafts autosave to the server and recover locally after interruption. Final submission is distinct from draft saving.
- Revision checks reject stale edits from another tab; earlier answers remain in history.
- Finish the independent pass to lock its answers **before** seeing LLM suggestions.
- Suggested moments are recorded separately, distinguishing noticed-at-the-time from only-now recognition. Suggestions never overwrite the independent pass.

Existing legacy annotations remain preserved; they are not automatically converted into confirmed insights. Legacy model outputs remain researcher-visible but are not offered as participant suggestions; rerun the current judge for current-format suggestions.

## LLM judge (no regex spark detector)

The old lexical novelty/phrase rules, copying threshold, default-to-spark rule, automatic before/after tracing, and `/detect`, `/trace`, `parse.csv` workflows are removed. Historical results remain in research exports. `parsers.py` still normalizes Codex/Claude/chat log formats; it does **not** infer sparks.

Ending the conversation starts the judge automatically in the background, while participants write their final answer and independently annotate bookmarks. Suggestions remain hidden until the independent pass is locked. After locking, participants can review optional suggestions or finish immediately; finishing returns to the home page with a saved confirmation, even if judging is still in progress. External recordings wait for an imported transcript, then start automatically. Reopening an unfinished review catches up a missed job.

One automatic run per unchanged transcript/configuration; refreshes, flags, and annotations do not trigger repeat calls. Errors require an explicit participant/researcher retry. At most two judge jobs run concurrently. Participants may finish without waiting for suggestions; an unfinished judge run can still complete for researcher analysis. Missing transcript, queued/running, unavailable, failure, completed-empty, and all-suggestions-reviewed are distinct UI states.

Automatic judging transmits task/baseline/transcript to the configured provider and consumes account usage. Set `SPARK_AUTO_JUDGE=0` to require manual judging. Scripted demo mode never starts live judging automatically. Researcher buttons remain available for reruns. Startup never bulk-judges old sessions or resumes interrupted inference automatically.

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
- `SPARK_AUTO_JUDGE=1` (default; set `0` to disable automatic generation).
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

## Blinded judge validation

Open `/validation` from the research dashboard to create a reproducible small double-coded set. Select the source sessions, two coder IDs, a sampling seed, and counts for three hidden strata:

- LLM judge candidates (candidate-quality check).
- Independently participant-confirmed passages that do not overlap a judge candidate (coverage audit).
- Unflagged matched-length conversation passages (comparison windows, **not assumed negatives**).

Each coder receives a separate tokenized link. They see the same task, baseline, transcript context, and highlighted episode, but never see the sampling stratum, model output/rationale, participant report, session/participant identity, or the other coder's answers. Codes cover transcript evidence, provisional type, uptake, boundary quality, confidence, and rationale. Every edit is versioned.

The unblinded researcher view reports raw agreement and Cohen's κ, source-stratified transcript-support counts, and descriptive passage coverage of participant-confirmed episodes. It deliberately does not call model outputs ground truth, unflagged windows negatives, or passage overlap recall. Export the item-level data as CSV for analysis/adjudication. A small pilot is for codebook and workflow debugging; report uncertainty and do not treat its κ or support rate as a stable population estimate.

## External-agent recording

Open `/record` in desktop Chrome/Edge. Share screen (video only), pop out the optional always-on-top Flag button, work in Codex or Claude Code, then end recording and attach selected agent logs. Video is uploaded in 4-second chunks. Screen capture requires localhost or HTTPS; macOS screen-recording permission may also be needed.

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

**macOS execution host + localhost only for integrated agents.** Python 3.10+ is required for the Claude SDK. Codex uses its native `:workspace` permission profile; it is deliberately not wrapped in a second Seatbelt profile because nested macOS sandboxes fail and cause spurious approval prompts. Claude uses a single process-tree Seatbelt fence. These controls do not provide full hosted multi-tenant or credential isolation. Use synthetic, non-sensitive tasks during development.

Researcher/participant authentication, hosted execution isolation, quotas, resource limits, pairing/deployment hardening, formal adjudication, and adequately powered judge validation remain unfinished. The local tool now supports blinded double-coding, but tokenized coder links and UI/API-view hiding are workflow safeguards, **not an authorization boundary**; researcher/export routes are currently unauthenticated. Do not expose this publicly or share a researcher's ChatGPT login as a hosted service credential.

Task templates/personas are configurable in `config/`; a full factorial/counterbalanced study assignment system remains future work. Human-partner chat is a prototype comparison mode, not production participant matching.

Tested local text runtime: **codex-cli 0.162.0-alpha.2**; native voice was verified with **0.162.0-alpha.17.2**. Pin/test before a study. Session snapshots record task/persona/runtime configuration. [Official app-server docs](https://learn.chatgpt.com/docs/app-server), [CLI reference](https://learn.chatgpt.com/docs/cli/reference).

Agent settings: `SPARK_CODEX_BIN`, `SPARK_CODEX_MODEL`, `SPARK_CODEX_EFFORT`, `SPARK_TURN_TIMEOUT`, `SPARK_DATA_DIR`. Changing the default model does not alter an existing pinned thread.

## Verification

No model calls in unit/API or headless browser tests:

```bash
SPARK_QUIET=1 .venv/bin/python -m unittest discover -s tests -v
node tests/browser_smoke.cjs
SPARK_BROWSER_RUNTIME=claude node tests/browser_smoke.cjs
```

Browser QA requires Playwright plus Chrome/Chromium; optionally set `NODE_PATH` and `SPARK_CHROME_PATH`. Covers agent controls, offline bookmark recovery, exposure snapshots, autosave/reload, multi-event passages, missed moments, automatic suggestion generation without duplicate calls, blinding, failure/retry and missing/empty states, independent-pass locking, and exports.

Live tests are separately opt-in, **synthetic only**, and consume account usage:

```bash
.venv/bin/python tests/live_smoke.py --run-live
.venv/bin/python tests/live_judge_smoke.py --run
.venv/bin/python tests/live_claude_smoke.py --run-live --key-stdin
SPARK_LIVE_VOICE=1 SPARK_LIVE_VOICE_SPEECH=1 node tests/live_voice_smoke.cjs
```

## Code map

```text
server.py             API, streaming, recording, imports, exports
storage.py            transactional records and historical projections
codex_runtime.py      agent app-server bridge, approvals, workspace, resume
agent_runtime.py      shared provider dispatch (Codex / Claude only)
claude_runtime.py     Claude event/approval adapter, native-session recovery
claude_sdk_worker.py  isolated official SDK client with native tools/prompt
annotations.py        participant reviews, evidence passages, locking
judge.py              blinded LLM coding, quote/ID validation
judge_backend.py      restricted Codex judge / optional API adapter
judge_validation.py   blinded sampling, coder agreement, validation export
parsers.py            external log normalization (not inference)
transcript.py         full timestamped transcript rendering
static/bookmarks.js   durable retry outbox and click snapshots
static/review_forms.js autosave, conflict checks, optional review fields
static/               participant/researcher/recorder/review interfaces
config/               taxonomy, study templates, personas
tests/                synthetic unit/API/browser/live verification
```
