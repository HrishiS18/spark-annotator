# Spark Annotator

A research prototype for studying **sparks**: participant-perceived valuable ideas or insights during an interaction. Perceived value, adoption, and eventual usefulness are distinct measurements.

## Integrated Codex study (v2)

The default in-browser AI partner is now a **real Codex app-server runtime**, not an Anthropic text-only call. The browser is our custom interface; Codex supplies its existing agent loop, command execution, and file-editing tools. The external recorder remains a separate, supported workflow below.

```bash
cd spark-annotator
python3 -m venv .venv
.venv/bin/python server.py
```

Open **http://127.0.0.1:8765**, expand **Integrated agent study**, and select a task. The session runs baseline → conversation → final answer → review. During conversation:

- Responses and tool activity stream into the interface.
- The task workspace supports file uploads, text previews, and artifact downloads.
- Command/file approvals are explicit; **Stop agent** interrupts a running turn.
- Bookmarks and their explanations use stable event IDs and survive refreshing.
- Reconnection resumes the saved thread without automatically starting inference.

Codex must be installed **on the execution host**. `codex login` can use an eligible ChatGPT account for this local prototype; API-key authentication is also supported. If `OPENAI_API_KEY` is configured securely in the server environment, the bridge can authenticate over its private subprocess pipe. Never paste keys into participant messages, study configuration, or the UI. The bundled Codex executable in the desktop app is detected on macOS when it is absent from PATH.

The optional **Plain LLM chat** and **Scripted demo** modes are explicitly labeled. Authentication or model failures never silently become demo replies. `SPARK_DEMO=1` only affects the legacy Anthropic integration; it does **not** disable inference in Codex sessions. Select **Scripted demo** explicitly for an offline demonstration.

### Storage and migration

`data/study.sqlite3` is the authoritative store. On first startup, existing `data/sessions/*.json` files are imported once; their bytes are not modified or deleted. Stop the old server before starting v2 so it cannot continue writing those legacy files.

- Separate records hold events, bookmarks, participant reviews, researcher codes, and automated predictions.
- SQLite transactions and revision checks reject stale overwrites.
- Event/review revisions and each analysis run are retained. Reruns do not reuse participant answers to earlier predictions.
- Re-imports preserve old evidence and annotation anchors rather than renumbering events.
- JSON exports include historical records, archived prediction responses, and runtime traces. CSVs remain current-view exports.
- `data/workspaces/<session-id>/` holds task files. Raw log uploads and video still live under `data/uploads/` and `data/recordings/`.

Back up SQLite using a SQLite-aware backup or with the server stopped. While running, its WAL file can contain uncheckpointed transactions; copying only the `.sqlite3` file is not a reliable backup. All `data/` is git-ignored.

### Local-prototype limits

This integration is currently **macOS execution host + localhost only**. A macOS privacy fence prevents the Codex subprocess tree from reading/writing study records or other sessions' task folders. Codex uses workspace-write command sandboxing, no command-network access by default, and explicit approvals. This does **not** constitute full host/credential isolation: tools may still read unrelated host paths outside the protected study-data directory, and approvals can authorize broader command access. Use synthetic, non-sensitive tasks during development.

Participant/researcher authentication, hosted execution isolation, resource limits, and production deployment are still outstanding. Do not expose this as a public study service. Hosted use must not reuse a researcher's ChatGPT login as a shared service credential.

The runtime was exercised with **codex-cli 0.162.0-alpha.2**. App-server is experimental; pin and test the runtime version before a study. The installed version, effective model, requested effort, sandbox, approval policy, and task/persona configuration are recorded with sessions. [Official app-server documentation](https://learn.chatgpt.com/docs/app-server).

Optional host settings: `SPARK_CODEX_BIN`, `SPARK_CODEX_MODEL`, `SPARK_CODEX_EFFORT` (default `medium`), `SPARK_TURN_TIMEOUT` (default 600 seconds), and `SPARK_DATA_DIR`. Changing host settings does not change the pinned model of an existing thread. These are server settings, not participant-controlled shell arguments.

### Verification

Unit/integration tests use temporary data and a fake runtime, with no model calls:

```bash
SPARK_QUIET=1 .venv/bin/python -m unittest discover -s tests -v
```

Optional headless browser QA requires Playwright and Chrome/Chromium:

```bash
node tests/browser_smoke.cjs
```

Set `SPARK_CHROME_PATH` if using a locally installed Chrome executable and `NODE_PATH` if Playwright is provided by a bundled runtime. The browser test covers baseline, streaming, approvals, tools, uploads, artifacts, bookmarking, refresh, review, and export.

A **live** smoke test is separately opt-in and consumes the host's configured Codex usage. It uses only synthetic temporary files and accepts only its narrowly specified test actions:

```bash
.venv/bin/python tests/live_smoke.py --run-live
```

## External-agent recorder (original workflow)

- **Record.** Participants work with their own AI agent (Codex, Claude Code, ChatGPT…) while their screen is recorded in the browser (video only, no audio). A small **Flag** button floats on top of every window; one press timestamps the moment, and they can pick a spark type or just bookmark it.
- **Review.** Afterwards they rewatch the recording with their flags on the timeline and annotate each one.
- **Analyse.** The agent's conversation log is parsed into a timestamped transcript and coded two ways: a deterministic **codebook parser** (no API) and an optional **LLM judge** (Claude). The dashboard compares flags, parser and judge side by side.

The recorder needs no participant-side installation. The study server and Codex bridge use Python's standard library; Codex is a separate host dependency. Anthropic is optional for plain chat and the judge.

---

## Setup

### 1. Requirements

| | |
|---|---|
| Server | Python **3.9+**. Recorder supports macOS, Windows or Linux; integrated Codex execution currently requires macOS. |
| Participants | **Chrome or Edge** on a desktop/laptop (Windows, macOS, Linux, ChromeOS) |
| LLM judge (optional) | An [Anthropic API key](https://console.anthropic.com/) and the `anthropic` package |

Check Python:

```bash
python3 --version
```

### 2. Get the code

```bash
git clone https://github.com/HrishiS18/spark-annotator.git
cd spark-annotator
```

### 3. Start the server

```bash
python3 server.py
```

Open **http://127.0.0.1:8765**. That's it for single-computer use, where the participant uses the same machine.

Optional demo data, so the dashboard isn't empty:

```bash
python3 tools/seed_demo.py
```

### 4. Lab setup: participants on other computers

```bash
python3 server.py --host 0.0.0.0 --port 8765
```

Participants then open `http://<server-ip>:8765/record`.

**Important:** browsers only allow screen recording on `https://` or `localhost`. For other computers, put the server behind HTTPS. The quickest way is [Caddy](https://caddyserver.com/):

```bash
caddy reverse-proxy --from your-hostname.example.org --to localhost:8765
```

Or run the server on each participant's own computer and use `http://localhost:8765/record`.

### 5. (Optional) Enable the LLM judge

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...            # Windows (PowerShell): $env:ANTHROPIC_API_KEY="sk-ant-..."
.venv/bin/python server.py
```

The **Run LLM judge** button in the dashboard becomes active. Only transcript text is sent to the API (never the video). Mention this in your consent form.

---

## Running a session

1. **Participant** opens `/record` (or a pre-made link from the home page: `/record?session=s_…`).
2. Enters their ID → **Share my screen & start recording** → chooses **Entire screen**.
3. Clicks **Pop out the Flag button**. It floats over all apps; drag it out of the way.
4. Works with their agent. Presses **Flag** whenever something shifts their thinking, then optionally picks a type (or keys `1`–`7`), **Just bookmark** (`B`), or **Undo**.
5. Clicks **End session** (■). The recording finishes uploading (it uploads continuously while recording).
6. Attaches their agent log: **Find my agent sessions** (pick the `~/.codex` or `~/.claude` folder) or **Choose log files**. Web-chat users click **Skip**.
7. The **review** opens: ▶ on each flag jumps to 20 s before it; they annotate each flag and click **Finish review**.

**Researcher:** open `/researcher` → pick a session.

| Button | What it does |
|---|---|
| **Recording review** | Video + flags + parser/judge finds + synced transcript |
| **Timestamped transcript** | Readable `[mm:ss]` transcript with ⚑ flags, ◆ codebook, ★ judge |
| **Run parser** | Re-runs the codebook parser (runs automatically after import) |
| **Run LLM judge** | Claude codes the transcript (shows cost first) |
| **Import log** | Attach or replace an agent log manually |
| `marks.csv` / `candidates.csv` / `parse.csv` | Exports for analysis |

Batch-judge a whole study:

```bash
.venv/bin/python tools/run_judge.py --all --dry-run   # cost estimate
.venv/bin/python tools/run_judge.py --all
```

Where agent logs live: Codex `~/.codex/sessions/…/rollout-*.jsonl`; Claude Code `~/.claude/projects/…/*.jsonl` (Windows: `C:\Users\<you>\.codex`, `.claude`). Zips from [agent-log-exporter](https://github.com/joebaumann/upskilling-agent-log-exporter) also work.

---

## How sparks are coded

**Spark types:** additive, representational, reflective, contrastive, counterfactual, associative, generative error (definitions and participant wording in `config/taxonomy.json`). Each find is also classed as *spark*, *persuasion* (adopts the agent's view without own reasoning) or *copying* (reuses its wording).

**Codebook parser** (`spark_parser.py`, rules in `config/spark_codebook.json`). It reads the transcript word by word (which terms the agent introduced and the participant later adopted), response by response (agent questions, hypotheticals, errors) and prompt by prompt (13 weighted phrase/structure rules). It's deterministic, and every find shows the rule and the exact words that fired. Edit the JSON to tune it.

**LLM judge** (`judge.py`). Claude reads the timestamped transcript and returns trigger and uptake events, type, mechanism, confidence, before→after, quotes and a rationale. It excludes participant flags and, by default, parser output; an optional hints mode is not independent. This judge and the codebook are unvalidated research baselines: agreement is not accuracy, and quote verification and spark definitions need further work. Model and effort: `SPARK_JUDGE_MODEL` (default `claude-opus-5-5`, availability not validated), `SPARK_JUDGE_EFFORT` (default `high`).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Share my screen" does nothing / is greyed out | Use Chrome or Edge, and open the page via `localhost` or `https://` |
| Flag button doesn't float over other apps | Needs Chrome/Edge 116+; other browsers open a normal small window instead |
| Can't see `.codex` / `.claude` in the folder picker | macOS: press `⌘⇧.` · Windows: paste `C:\Users\<you>\.codex` into the path bar · Linux: `Ctrl+H` |
| "Run LLM judge" is disabled | Install `anthropic`, set `ANTHROPIC_API_KEY`, restart the server |
| Port already in use | `python3 server.py --port 8800` |

App data is stored on the server host in `data/` (sessions in SQLite, recordings as MP4/WebM, task workspaces, and raw uploaded logs). Live Codex/Anthropic calls send relevant text and task context to their providers. Local app storage does not mean model inference is offline. Separately, Codex maintains runtime state under its configured Codex home. Include these data flows in study consent and retention decisions.

## Project layout

```
server.py              web server + API (sessions, flags, SSE, workspace, recording, import, exports)
storage.py             SQLite records, revisions, migration, runtime feed, full research exports
codex_runtime.py       stdio app-server bridge, per-session workspace, approvals, interrupt/resume
static/                pages: record (recorder), review, researcher (dashboard), index, session
parsers.py             agent-log → timestamped events (Claude Code, Codex, chat JSON, zips)
spark_parser.py        codebook parser          judge.py        LLM judge
transcript.py          timestamped transcript   detector.py     before/after tracing
config/                taxonomy, codebook rules, studies, personas
tools/                 seed_demo.py, run_judge.py, import_dilemmas.py
tests/                 storage/runtime/API regression tests, mock browser QA, opt-in live smoke
```

The integrated study is at `/session`; the runtime is selected explicitly when creating the session. The seven-category optional marker and the original post-study form remain in place; the larger annotation UX redesign is a subsequent milestone.
