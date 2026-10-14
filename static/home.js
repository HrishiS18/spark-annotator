import { api, config, esc, $ } from "/static/common.js";

const form = $("#start");
const button = form.querySelector('button[type="submit"]');
const status = $("#runtime-status");
const permissionNote = $("#permission-note");
let starting = false;

function notice(text, error = false) {
  status.textContent = text;
  status.classList.toggle("hidden", !text);
  status.setAttribute("role", error ? "alert" : "status");
}

async function setup() {
  const cfg = await config();
  const studies = cfg.studies.filter(study => !study.external);
  if (!studies.length) throw new Error("No studies are configured. Ask the researcher to add a task.");
  form.study.innerHTML = studies.map(study => `<option value="${esc(study.key)}">${esc(study.label)}</option>`).join("");
  form.persona.innerHTML = cfg.personas.map(persona => `<option value="${esc(persona.key)}">${esc(persona.label)}</option>`).join("");

  function syncRuntime() {
    if (form.condition.value === "human") {
      permissionNote.textContent = "";
      permissionNote.classList.add("hidden");
      notice(""); button.disabled = starting; return;
    }
    const kind = form.runtime.value;
    permissionNote.classList.remove("hidden");
    permissionNote.textContent = kind === "codex"
      ? "Agent access: routine reading, editing, and commands inside the task workspace run without interruption. Codex asks before crossing that boundary."
      : "Agent access: reading and editing inside the task workspace run without interruption. Claude may ask before commands or network access.";
    const runtime = cfg.runtimes?.[kind] || (kind === "codex" ? cfg.runtime : null);
    let message = "";
    if (!runtime?.installed) {
      message = `${kind === "claude" ? "Claude agent" : "Codex"} is not installed on the server. Ask the researcher to finish setup.`;
    } else if (kind === "claude" && !runtime.configured) {
      message = "Claude is installed, but the server needs an Anthropic API key. Configure ANTHROPIC_API_KEY on the Mac running the server, then restart it.";
    }
    notice(message);
    button.disabled = starting || !!message;
  }

  function syncStudy() {
    const study = studies.find(study => study.key === form.study.value);
    form.prompt_id.innerHTML = study.prompts.map(prompt => `<option value="${esc(prompt.id)}">${esc(prompt.title)}</option>`).join("");
    form.persona.value = study.default_persona;
  }

  function syncPartner() {
    const human = form.condition.value === "human";
    $("#room-field").classList.toggle("hidden", !human);
    $("#runtime-field").classList.toggle("hidden", human);
    $("#persona-field").classList.toggle("hidden", human);
    $("#partner-fields").classList.toggle("human-partner", human);
    form.room.required = human;
    form.room.disabled = !human;
    form.runtime.disabled = human;
    form.persona.disabled = human;
    syncRuntime();
  }

  form.study.onchange = syncStudy;
  form.condition.onchange = syncPartner;
  form.runtime.onchange = syncRuntime;
  syncStudy(); syncPartner();
  form.onsubmit = async event => {
    event.preventDefault();
    if (starting || button.disabled) return;
    starting = true; button.disabled = true; button.textContent = "Starting…";
    try {
      const session = await api("POST", "/api/sessions", Object.fromEntries(new FormData(form)));
      location.href = `/session?id=${session.id}`;
    } catch (error) {
      starting = false; button.textContent = "Start session"; syncRuntime();
      notice(error.message, true);
    }
  };
}

setup().catch(error => notice(`Could not load session setup. ${error.message}`, true));
