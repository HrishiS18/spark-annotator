# Spark Annotator

A research tool for finding **sparks**: moments in a human–AI agent interaction that shift or reframe the participant's thinking, context or goal.

- **Record.** Participants work with their own AI agent (Codex, Claude Code, ChatGPT…) while their screen is recorded in the browser (video only, no audio). A small **Flag** button floats on top of every window; one press timestamps the moment, and they can pick a spark type or just bookmark it.
- **Review.** Afterwards they rewatch the recording with their flags on the timeline and annotate each one.
- **Analyse.** The agent's conversation log is parsed into a timestamped transcript and coded two ways: a deterministic **codebook parser** (no API) and an optional **LLM judge** (Claude). The dashboard compares flags, parser and judge side by side.

Nothing to install on participants' computers. The server is plain Python (standard library only).

---

## Setup

### 1. Requirements

| | |
|---|---|
| Server | Python **3.9+** (macOS, Windows or Linux). No packages needed. |
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

**LLM judge** (`judge.py`). Claude reads the whole timestamped transcript and returns trigger and uptake events, type, mechanism, confidence, before→after, quotes and a rationale. It never sees the participant's flags or the parser output, so agreement is meaningful. Quotes are checked against the transcript and unverifiable ones are marked. Model and effort: `SPARK_JUDGE_MODEL` (default `claude-opus-5-5`), `SPARK_JUDGE_EFFORT` (default `high`).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Share my screen" does nothing / is greyed out | Use Chrome or Edge, and open the page via `localhost` or `https://` |
| Flag button doesn't float over other apps | Needs Chrome/Edge 116+; other browsers open a normal small window instead |
| Can't see `.codex` / `.claude` in the folder picker | macOS: press `⌘⇧.` · Windows: paste `C:\Users\<you>\.codex` into the path bar · Linux: `Ctrl+H` |
| "Run LLM judge" is disabled | Install `anthropic`, set `ANTHROPIC_API_KEY`, restart the server |
| Port already in use | `python3 server.py --port 8800` |

Data is stored locally in `data/` (sessions as JSON, recordings as MP4/WebM). It is git-ignored; back it up.

## Project layout

```
server.py              web server + API (sessions, flags, recording upload/streaming, import, exports)
static/                pages: record (recorder), review, researcher (dashboard), index, session
parsers.py             agent-log → timestamped events (Claude Code, Codex, chat JSON, zips)
spark_parser.py        codebook parser          judge.py        LLM judge
transcript.py          timestamped transcript   detector.py     before/after tracing
config/                taxonomy, codebook rules, studies, personas
tools/                 seed_demo.py, run_judge.py, import_dilemmas.py
```

An optional in-browser dilemma/trading chat study also exists (`/session`; its AI partner needs the API key).
