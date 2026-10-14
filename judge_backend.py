"""Structured, text-only judging. No regex or canned prediction fallback."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import llm
from codex_runtime import privacy_command

DEFAULT_MODEL = "gpt-5.6-sol"  # explicitly pinned; verified with the local account


def binary():
    value = os.environ.get("SPARK_CODEX_BIN") or shutil.which("codex")
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    return value or (str(bundled) if bundled.is_file() else None)


def status():
    provider = os.environ.get("SPARK_JUDGE_PROVIDER", "codex")
    model = os.environ.get("SPARK_JUDGE_MODEL") or (DEFAULT_MODEL if provider == "codex" else llm.JUDGE_MODEL)
    if provider == "anthropic":
        value = llm.status()
        return {**value, "provider": provider, "model": model, "effort": llm.JUDGE_EFFORT}
    if provider != "codex":
        return {"live": False, "provider": provider, "model": model, "reason": "Unknown judge provider"}
    available = bool(binary() and Path('/usr/bin/sandbox-exec').exists())
    return {"live": available, "provider": provider, "model": model,
            "effort": os.environ.get("SPARK_JUDGE_EFFORT", "medium"),
            "reason": None if available else "Local Codex judging requires an installed Codex runtime and macOS",
            "authentication": "Existing Codex login; verified on run", "cost_note": "Consumes Codex account usage; no USD estimate"}


def call(system, prompt, schema, model=None):
    settings = status()
    if not settings["live"]:
        raise llm.LLMError(settings["reason"])
    if settings["provider"] == "anthropic":
        return llm.judge_call(system, prompt, schema, model=model or settings["model"], effort=settings["effort"])
    data = Path(os.environ.get("SPARK_DATA_DIR", Path(__file__).resolve().parent / "data")).resolve()
    data.mkdir(parents=True, exist_ok=True)
    selected = model or settings["model"]
    with tempfile.TemporaryDirectory(prefix='judge-', dir=data) as work:
        workspace = Path(work)
        schema_path = workspace / 'schema.json'
        result_path = workspace / 'result.json'
        schema_path.write_text(json.dumps(schema), encoding='utf-8')
        # Ignore personal/project config, disable tools/connectors/hooks, discard
        # session persistence, and fence off the actual study-data directory.
        command = privacy_command(binary(), data, workspace)[:3] + [binary(), 'exec', '--ignore-user-config',
            '--ignore-rules', '--ephemeral', '--skip-git-repo-check', '--json', '--color', 'never',
            '--sandbox', 'read-only', '--model', selected, '--output-schema', str(schema_path),
            '--output-last-message', str(result_path), '-c', f'model_reasoning_effort={json.dumps(settings["effort"])}',
            '-c', 'web_search="disabled"', '-c', 'project_doc_max_bytes=0']
        for feature in ('shell_tool','unified_exec','multi_agent','apps','plugins','browser_use','computer_use',
                        'memories','hooks','skill_search','image_generation','view_image','sleep_tool','goals',
                        'workspace_dependencies','remote_plugin'):
            command += ['--disable', feature]
        command += ['-']
        try:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, cwd=workspace, start_new_session=True) as process:
                try:
                    stdout, _ = process.communicate(system + '\n\n' + prompt,
                                                    timeout=int(os.environ.get('SPARK_JUDGE_TIMEOUT', '180')))
                except subprocess.TimeoutExpired as exc:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise llm.LLMError('LLM judge timed out. No heuristic result was substituted.') from exc
                returncode = process.returncode
        except OSError as exc:
            raise llm.LLMError('The configured Codex judge executable could not be started.') from exc
        usage = {}
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get('item', {})
            if item.get('type') in ('command_execution','mcp_tool_call','web_search','file_change'):
                raise llm.LLMError('The judge attempted a tool operation; its result was discarded.')
            if event.get('type') == 'turn.completed':
                usage = event.get('usage', {})
        if returncode or not result_path.is_file():
            # Never echo arbitrary stderr: it can contain account/config details.
            raise llm.LLMError('Codex judge failed. Check `codex login status` and the configured judge model. No regex fallback was used.')
        try:
            output = json.loads(result_path.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise llm.LLMError('The judge did not return valid structured JSON.') from exc
        version = subprocess.check_output([binary(), '--version'], text=True, timeout=5).strip()
        return output, {"provider": "codex", "model": selected, "effort": settings['effort'], "runtime_version": version,
                        "input_tokens": usage.get('input_tokens', 0), "output_tokens": usage.get('output_tokens', 0),
                        "cost_usd": None}
