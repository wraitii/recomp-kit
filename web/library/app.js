import { platform } from "./platform.js";

const backend = platform();
const element = (id) => document.getElementById(id);
const labels = { ready: "Ready to play", notInstalled: "Not installed", unavailable: "Unavailable", needsBuild: "Build needed", busy: "Installation in progress" };
const descriptions = {
  "ghost-recon": "Lead an elite squad through tense, tactical missions behind enemy lines.",
  "black-and-white": "Shape a world with your powers and raise a creature that learns from you.",
  "railroad-tycoon-3": "Build a railway network and grow your transport empire.",
};
let busy = false;
let checking = false;
let checksReady = false;
let selected = null;
let source = "";
let destination = "";
let lines = [];
let lastAction = "";
let checkRevision = 0;
let candidateId = "";
let inspectionReady = false;
let resumeJob = null;
let sourceKind = "";
let activeJobId = "";
let failedJobId = "";
let inspecting = false;
let inspectionRevision = 0;
let assistedJob = null;
let assistedSessionId = "";
let assistedPollTimer = null;
let assistedPollInFlight = false;
let assistedPollPromise = null;
let assistedRevision = 0;
let assistedAnnouncement = "";
let assistedItemSignature = "";
let assistedItemStates = new Map();
let assistedStatusText = "";
let assistedWasReady = false;
let assistedCanContinue = false;

function message(text) {
  const target = element(element("installer").open ? "install-message" : "message");
  target.textContent = text;
  target.hidden = !text;
}

function errorText(error) {
  if (typeof error === "string") {
    try { return errorText(JSON.parse(error)); } catch { return error; }
  }
  if (error && typeof error === "object") {
    const diagnostic = error.diagnostic;
    const result = diagnostic?.message || error.message;
    if (typeof result === "string") {
      try { return errorText(JSON.parse(result)); } catch { return result; }
    }
    return result || JSON.stringify(error);
  }
  return String(error);
}

function stageLabel(stage) {
  return ({ checking: "Checking", extracting: "Extracting copy", validating: "Validating files",
    importing: "Copying files", building: "Building app", publishing: "Publishing app",
    cleaning: "Cleaning up", failed: "Failed", complete: "Complete" })[stage] || "Installation";
}

async function attempt(action) {
  try { await action(); } catch (error) { message(errorText(error)); }
}

function lock(value) {
  busy = value;
  for (const button of document.querySelectorAll("button")) {
    button.disabled = busy || checking || inspecting || button.dataset.unavailable === "true";
  }
  for (const input of document.querySelectorAll(".candidate-choice input")) {
    input.disabled = busy || checking || inspecting || input.dataset.unavailable === "true";
  }
  element("cancel").disabled = !busy;
  element("continue").disabled = busy || checking || inspecting || !checksReady;
  element("install").disabled = busy || checking || inspecting || !checksReady || !inspectionReady || !candidateId || !source || !destination;
  element("pick-destination").disabled = busy || checking || inspecting;
  element("assisted-continue").disabled = busy || assistedPollInFlight || !assistedCanContinue;
  element("assisted-recheck").disabled = busy || !assistedJob;
  element("assisted-cancel").disabled = busy || !assistedJob;
}

function renderRecovery() {
  const jobs = selected?.recoverableJobs || [];
  const panel = element("recovery");
  const list = element("recovery-jobs");
  list.replaceChildren();
  panel.hidden = busy || !jobs.length;
  for (const job of jobs) {
    const row = document.createElement("article");
    row.className = "recovery-job";
    row.classList.toggle("current-failure", job.jobId === failedJobId);
    const heading = document.createElement("strong");
    heading.textContent = job.mode === "assisted" ? "Manual preparation · waiting for files" :
      `${stageLabel(job.stage)} · ${job.source?.kind === "folder" ? "Game folder" : "Game copy"}`;
    const status = document.createElement("p");
    const latestError = job.error || job.errors?.at(-1);
    status.textContent = job.validationError || latestError?.message || (job.canResume
      ? "The source and build recipe will be checked again before retrying."
      : "The library is recovering the saved publication state; refresh to see the result.");
    if (job.mode === "assisted") status.textContent = job.validationError ||
      "Continue preparing files in this job-owned folder. Nothing builds until you choose Continue.";
    if (job.mode === "assisted") {
      const sourcePath = document.createElement("p");
      sourcePath.textContent = `Reference: ${job.sourceReference?.path || "none"}\nDestination: ${job.destination}`;
      row.append(heading, status, sourcePath);
    } else if (job.source?.path) {
      const sourcePath = document.createElement("p");
      sourcePath.textContent = `Source: ${job.source.path}\nDestination: ${job.destination}`;
      row.append(heading, status, sourcePath);
    } else {
      row.append(heading, status);
    }
    if (latestError) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${latestError.code || "Error"} · ${latestError.stage || job.stage}`;
      const technical = document.createElement("pre");
      technical.textContent = `${latestError.path ? `Path: ${latestError.path}\n` : ""}${JSON.stringify(latestError.details || {}, null, 2)}`;
      details.append(summary, technical);
      row.append(details);
    }
    const actions = document.createElement("div");
    actions.className = "buttons recovery-actions";
    const add = (label, handler, disabled = false) => {
      const button = document.createElement("button");
      button.textContent = label;
      button.disabled = disabled;
      button.dataset.unavailable = String(disabled);
      button.onclick = () => attempt(handler);
      actions.append(button);
    };
    if ((job.actions || []).includes("retry_failed_stage")) add("Retry failed stage", () => retrySavedJob(job));
    if ((job.actions || []).includes("locate_missing_file")) add("Locate missing file", () => locateSource(job));
    if ((job.actions || []).includes("choose_another_copy")) add("Choose another copy", chooseAnotherCopy);
    if ((job.actions || []).includes("change_destination")) add("Change destination", () => changeDestination(job));
    if ((job.actions || []).includes("recheck_preparation")) add("Recheck preparation", () => recheckPreparation(job));
    if ((job.actions || []).includes("open_log")) add("Open log", () => openJobLog(job));
    if ((job.actions || []).includes("resume_preparation")) add("Resume preparation", () => resumePreparation(job));
    if ((job.actions || []).includes("open_assisted_folder")) add("Open drop folder", () => backend.openAssistedFolder(selected.id, job.jobId));
    if ((job.actions || []).includes("cancel_assisted_job")) add("Cancel and remove copied files", () => cancelAssisted(job));
    row.append(actions);
    list.append(row);
  }
}

function renderInspection(result) {
  const target = element("inspection");
  target.replaceChildren();
  const candidates = result?.candidates || [];
  const viable = candidates.filter((item) => item.installable);
  inspectionReady = Boolean(result?.scanComplete && viable.length);
  candidateId = viable.length === 1 ? viable[0].id : "";
  if (viable.length > 1) inspectionReady = false;
  for (const candidate of candidates) {
    const label = document.createElement("label");
    label.className = "candidate-choice";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "candidate";
    input.value = candidate.id;
    input.dataset.unavailable = String(!candidate.installable || !result.scanComplete);
    input.disabled = !candidate.installable || !result.scanComplete || busy || checking;
    input.checked = candidate.id === candidateId;
    input.onchange = () => {
      candidateId = candidate.id;
      if (resumeJob && candidate.id !== resumeJob.candidateId) resumeJob = null;
      inspectionReady = true;
      lock(false);
    };
    const detail = document.createElement("span");
    const relativeRoot = candidate.contentRootRelative && candidate.contentRootRelative !== "."
      ? ` · files in ${candidate.contentRootRelative}` : "";
    const candidateIssue = (candidate.diagnostics || []).find((item) => item.severity === "error");
    detail.textContent = `${candidate.installable ? "Supported copy" : candidate.status}: ${candidate.executable?.path || candidate.id}${relativeRoot}${candidateIssue ? ` · ${candidateIssue.message}` : ""}`;
    label.append(input, detail);
    target.append(label);
  }
  for (const diagnostic of result?.diagnostics || []) {
    const note = document.createElement("p");
    note.textContent = diagnostic.message;
    target.append(note);
  }
  if (!target.childElementCount) {
    const note = document.createElement("p");
    note.textContent = "No supported game copy was found in this source.";
    target.append(note);
  }
  target.hidden = false;
  lock(false);
}

async function inspectSource(path) {
  const revision = ++inspectionRevision;
  const selectedGame = selected?.id;
  source = path;
  candidateId = "";
  inspectionReady = false;
  sourceKind = "";
  element("inspection").replaceChildren();
  element("inspection").hidden = true;
  inspecting = true;
  lock(false);
  try {
    const result = await backend.query({ action: "inspect", game: selected.id, source });
    if (revision !== inspectionRevision || selectedGame !== selected?.id || source !== path) return null;
    sourceKind = result.source?.kind || "";
    renderInspection(result);
    element("selected-source").textContent = `Selected: ${source}`;
    element("selected-source").hidden = false;
    return result;
  } catch (error) {
    if (revision !== inspectionRevision || selectedGame !== selected?.id) return null;
    candidateId = "";
    inspectionReady = false;
    sourceKind = "";
    lock(false);
    throw error;
  } finally {
    if (revision === inspectionRevision) {
      inspecting = false;
      lock(false);
    }
  }
}

function step(index, installing = false) {
  for (const [i, name] of ["requirements", "source", "job", "ready"].entries()) element(name).hidden = i !== index;
  element("assisted").hidden = true;
  element("steps").hidden = index === 3 || (index === 2 && lastAction === "play");
  const active = installing ? 3 : index;
  for (const [i, item] of [...element("steps").children].entries()) {
    item.classList.toggle("active", i === active);
    item.classList.toggle("done", i < active);
    if (i === active) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  }
  if (index === 3) {
    element("installed-location").textContent = `Installed in ${destination}`;
    element("update-note").hidden = !selected?.updateAvailable;
    element("rebuild").textContent = selected?.updateAvailable ? "Update app" : "Rebuild app";
  }
}

async function refresh() {
  const catalog = await backend.query({ action: "catalog" });
  element("storage").textContent = `Library records: ${catalog.storage}`;
  element("games").replaceChildren();
  for (const game of catalog.games) {
    const card = element("card").content.firstElementChild.cloneNode(true);
    card.querySelector(".game-title").textContent = game.title;
    card.querySelector(".monogram").textContent = game.title.split(/\s+/).slice(0, 2).map((word) => word[0]).join("");
    card.querySelector(".state").textContent = game.updateAvailable ? "Update available" : labels[game.state];
    card.querySelector(".state").classList.toggle("ready", game.state === "ready");
    card.querySelector(".detail").textContent = descriptions[game.id] || "Install a native version from your game copy.";
    card.querySelector(".card-action").textContent = game.state === "ready" ? "Open game →" : "Set up game →";
    card.onclick = () => attempt(() => openGame(game));
    element("games").append(card);
  }
  if (selected) selected = catalog.games.find((game) => game.id === selected.id) || selected;
  renderRecovery();
  element("play-existing").hidden = selected?.state !== "ready" || element("job").hidden || element("retry").hidden;
  if (selected && !element("ready").hidden) step(3);
  lock(busy);
}

async function openGame(game) {
  stopAssistedPolling();
  assistedJob = null;
  assistedCanContinue = false;
  selected = game;
  failedJobId = "";
  activeJobId = "";
  checksReady = false;
  source = game.managedSource || "";
  candidateId = "";
  inspectionReady = Boolean(game.managedSource);
  resumeJob = null;
  destination = game.installation || "";
  element("destination").value = destination;
  element("pick-destination").disabled = false;
  element("install-title").textContent = game.title;
  element("setup").open = false;
  element("selected-source").hidden = !source;
  element("selected-source").textContent = source ? `Using your library copy: ${source}` : "";
  element("copy-help").textContent = `Use your supported copy containing ${game.executable}. We’ll verify the version before building.`;
  element("installer").showModal();
  renderRecovery();
  message(game.maintenanceError || "");
  if (game.state === "ready") { step(3); return; }
  step(0);
  if (game.state === "unavailable") {
    element("checks").replaceChildren();
    element("check-status").textContent = game.detail || "This game is not available in this version of recomp-kit.";
    element("continue").disabled = true;
    element("recheck").hidden = true;
    element("change-copy").hidden = true;
    return;
  }
  element("recheck").hidden = false;
  await checkRequirements();
}

async function checkRequirements() {
  const revision = ++checkRevision;
  checking = true;
  checksReady = false;
  lock(false);
  element("checks").replaceChildren();
  element("check-status").textContent = "Checking your computer…";
  element("recheck").hidden = false;
  element("change-copy").hidden = !source;
  try {
    const result = await backend.query({ action: "check", needsRust: selected?.needsRust ?? true, needsInnoextract: false });
    if (revision !== checkRevision) return;
    element("check-status").textContent = result.ready ? "All requirements are installed." : "Install the missing tools below, then choose Check again.";
    for (const check of result.checks) {
      const row = document.createElement("div");
      row.className = `check ${check.ok ? "passed" : "missing"}`;
      const title = document.createElement("strong");
      title.textContent = `${check.ok ? "✓" : "!"} ${check.label}`;
      row.append(title);
      if (!check.ok) {
        const options = document.createElement("div");
        options.className = "buttons dependency-options";
        options.setAttribute("role", "group");
        options.setAttribute("aria-label", `${check.label} setup options`);
        const content = document.createElement("div");
        const methods = check.methods || [{ label: "Manual", help: check.help || check.detail, command: check.command, url: check.url }];
        function showMethod(method) {
          content.replaceChildren();
          for (const [i, button] of [...options.children].entries()) button.setAttribute("aria-pressed", String(methods[i] === method));
          const help = document.createElement("p");
          help.textContent = method.help;
          content.append(help);
          if (method.command) {
            const command = document.createElement("pre");
            command.textContent = method.command;
            content.append(command);
          }
          if (method.url) {
            const link = document.createElement("a");
            link.href = method.url;
            link.textContent = method.label === "Download" ? "Open download page ↗" : "Open setup guide ↗";
            link.onclick = (event) => { event.preventDefault(); attempt(() => backend.open(method.url)); };
            content.append(link);
          }
        }
        for (const method of methods) {
          const button = document.createElement("button");
          button.textContent = method.label;
          button.onclick = () => showMethod(method);
          options.append(button);
        }
        row.append(options, content);
        showMethod(methods[0]);
      }
      element("checks").append(row);
    }
    checksReady = result.ready;
  } finally {
    if (revision === checkRevision) {
      checking = false;
      lock(false);
    }
  }
}

async function chooseSource(kind) {
  const path = await backend.pick(kind);
  if (!path) return;
  resumeJob = null;
  step(0);
  await inspectSource(path);
  await checkRequirements();
  if (checksReady) step(1);
}

async function retrySavedJob(job) {
  if (!job.canResume || !job.source?.path || !job.candidateId || !job.destination) {
    throw new Error("This saved job does not have enough validated information to retry.");
  }
  checking = true;
  lock(false);
  let checked;
  try {
    checked = await backend.query({ action: "check", needsRust: selected?.needsRust ?? true,
      needsInnoextract: false });
  } finally {
    checking = false;
    lock(false);
  }
  if (!checked.ready) {
    resumeJob = job;
    step(0);
    await checkRequirements();
    message("Install the missing build tools, then retry the saved job.");
    return;
  }
  resumeJob = job;
  source = job.source.path;
  candidateId = job.candidateId;
  destination = job.destination;
  element("destination").value = destination;
  await start({ action: "install", game: selected.id, source, candidateId, destination, jobId: job.jobId });
}

async function locateSource(job) {
  const kind = job.source?.kind === "folder" ? "folder" : "source";
  const path = await backend.pick(kind);
  if (!path) return;
  const result = await inspectSource(path);
  if (!result) return;
  const sameCandidate = result.candidates?.some((item) => item.installable && item.id === job.candidateId);
  resumeJob = sameCandidate ? job : null;
  if (!sameCandidate) {
    const candidate = result.candidates?.find((item) => item.id === job.candidateId);
    const issue = candidate?.diagnostics?.find((item) => item.severity === "error");
    message(`This copy does not match the saved job${issue ? `: ${issue.message}` : ""}. It remains untouched; choose an empty destination to start a new job.`);
  } else {
    source = path;
    candidateId = job.candidateId;
    destination = job.destination;
    element("destination").value = destination;
    message("The selected copy matches this saved job. Retry will recheck its full identity.");
  }
  await checkRequirements();
  step(1);
}

function chooseAnotherCopy() {
  resumeJob = null;
  source = "";
  candidateId = "";
  inspectionReady = false;
  element("selected-source").hidden = true;
  element("inspection").hidden = true;
  message("Choosing a different copy starts a new job. Choose an empty destination; saved jobs and their staged files remain untouched.");
  step(1);
  lock(false);
}

async function changeDestination(job) {
  if (job.source?.path && !source) source = job.source.path;
  const folder = await backend.pick("folder");
  if (!folder) return;
  resumeJob = null;
  const separator = folder.includes("\\") ? "\\" : "/";
  destination = `${folder.replace(/[\\/]+$/, "")}${separator}${selected.id}`;
  element("destination").value = destination;
  if (!inspectionReady && source) await inspectSource(source);
  message("This destination applies to a new job; the saved job remains unchanged.");
  step(0);
  await checkRequirements();
  if (checksReady) step(1);
  lock(false);
}

async function recheckPreparation(job) {
  const path = source || job.source?.path;
  if (!path) throw new Error("Choose the missing game source before checking preparation.");
  const result = await inspectSource(path);
  if (!result) return;
  if (job.candidateId && result.candidates?.some((item) => item.installable && item.id === job.candidateId)) {
    resumeJob = job;
    candidateId = job.candidateId;
    source = path;
    destination = job.destination;
    element("destination").value = destination;
  } else {
    resumeJob = null;
    const candidate = result.candidates?.find((item) => item.id === job.candidateId);
    const issue = candidate?.diagnostics?.find((item) => item.severity === "error");
    if (issue) message(issue.message);
  }
  step(0);
  await checkRequirements();
  if (checksReady) step(1);
}

function stopAssistedPolling() {
  assistedRevision++;
  assistedSessionId = "";
  assistedCanContinue = false;
  assistedPollInFlight = false;
  if (assistedPollTimer) clearTimeout(assistedPollTimer);
  assistedPollTimer = null;
}

async function pauseAssistedPolling() {
  const sessionId = assistedSessionId;
  const pending = assistedPollPromise;
  const wasBusy = busy;
  if (assistedPollTimer) clearTimeout(assistedPollTimer);
  assistedPollTimer = null;
  checking = true;
  lock(wasBusy);
  stopAssistedPolling();
  if (pending) await pending;
  return sessionId;
}

function showAssisted() {
  for (const name of ["requirements", "source", "job", "ready"]) element(name).hidden = true;
  element("steps").hidden = true;
  element("assisted").hidden = false;
  lock(false);
}

async function loadAssistedDetails(job, revision) {
  const details = await backend.query({ action: "assisted_path", game: selected.id, jobId: job.jobId });
  if (revision !== assistedRevision || assistedJob?.jobId !== job.jobId) return;
  element("assisted-path").textContent = details.dropFolder;
  element("assisted-checklist").textContent = details.checklistText || "The generated checklist is unavailable. Open the drop folder to inspect it.";
  if (details.validationError) {
    assistedCanContinue = false;
    element("assisted-status").textContent = details.validationError;
  }
}

async function pollAssisted(revision) {
  if (revision !== assistedRevision || !assistedJob || assistedPollInFlight || busy) return;
  assistedPollInFlight = true;
  lock(false);
  const requestSession = assistedSessionId;
  assistedPollPromise = (async () => { try {
    const result = await backend.query({ action: "assisted_status", game: selected.id,
      jobId: assistedJob.jobId, sessionId: requestSession });
    if (revision !== assistedRevision || requestSession !== assistedSessionId) return;
    assistedCanContinue = Boolean(result.canContinue);
    const itemList = result.items || [];
    const signature = JSON.stringify(itemList);
    if (signature !== assistedItemSignature) {
      const items = element("assisted-items");
      const expanded = new Set([...items.querySelectorAll("details[open]")].map((node) => node.dataset.itemId));
      items.replaceChildren();
      for (const item of itemList) {
        const row = document.createElement("article");
        const title = document.createElement("strong");
        title.textContent = `${item.required ? "Required" : "Optional"} · ${item.path} · ${item.status}`;
        const detail = document.createElement("p");
        detail.textContent = item.message;
        row.append(title, detail);
        if (item.diagnostic) {
          const error = document.createElement("details");
          error.dataset.itemId = item.id;
          error.open = expanded.has(item.id);
          const label = document.createElement("summary");
          label.textContent = item.diagnostic.code;
          const info = document.createElement("pre");
          info.textContent = JSON.stringify(item.diagnostic.details || {}, null, 2);
          error.append(label, info);
          row.append(error);
        }
        items.append(row);
      }
      assistedItemSignature = signature;
    }
    let itemAnnouncement = "";
    for (const item of itemList) {
      const old = assistedItemStates.get(item.id);
      if (item.status !== old && ["verified", "wrong_version", "unreadable"].includes(item.status)) {
        itemAnnouncement = `${item.path}: ${item.status.replace("_", " ")}.`;
        break;
      }
    }
    assistedItemStates = new Map(itemList.map((item) => [item.id, item.status]));
    const message = result.recipeChanged ? "The build recipe changed. Choose Recheck preparation to refresh this job." :
      result.canContinue ? "Required files are verified. Continue starts the build only after checking prerequisites again." :
      result.checking ? "Checking stable files…" : "Waiting for required files to be copied and settle.";
    element("assisted-summary").textContent = message;
    if (itemAnnouncement) {
      assistedStatusText = itemAnnouncement;
    } else if (result.recipeChanged && message !== assistedAnnouncement) {
      assistedStatusText = message;
    } else if (result.canContinue && !assistedWasReady) {
      assistedStatusText = message;
    }
    if (assistedStatusText && element("assisted-status").textContent !== assistedStatusText)
      element("assisted-status").textContent = assistedStatusText;
    assistedWasReady = Boolean(result.canContinue);
    assistedAnnouncement = message;
    if (result.checklist) element("assisted-checklist").textContent = result.checklist;
    lock(false);
  } catch (error) {
    if (revision === assistedRevision) {
      assistedCanContinue = false;
      const text = errorText(error);
      if (element("assisted-status").textContent !== text) element("assisted-status").textContent = text;
      lock(false);
    }
  } finally {
    if (revision === assistedRevision) {
      assistedPollInFlight = false;
      lock(false);
      if (assistedJob) assistedPollTimer = setTimeout(() => pollAssisted(revision), 1000);
    }
  } })();
  await assistedPollPromise;
  assistedPollPromise = null;
}

async function resumePreparation(job) {
  stopAssistedPolling();
  assistedJob = job;
  assistedSessionId = crypto.randomUUID();
  assistedAnnouncement = "";
  assistedItemSignature = "";
  assistedItemStates = new Map();
  assistedStatusText = "";
  assistedWasReady = false;
  assistedCanContinue = false;
  const revision = assistedRevision;
  showAssisted();
  element("assisted-items").replaceChildren();
  element("assisted-status").textContent = "Starting a fresh preparation check for this session…";
  await loadAssistedDetails(job, revision);
  void pollAssisted(revision);
}

async function startAssisted(reference = "") {
  if (busy || checking || inspecting) return;
  const selectedGame = selected?.id;
  if (!selectedGame) return;
  checking = true;
  lock(true);
  stopAssistedPolling();
  assistedJob = null;
  const request = { action: "assisted_start", game: selectedGame };
  if (reference) request.source = reference;
  if (destination) request.destination = destination;
  try {
    const result = await backend.query(request);
    if (selected?.id !== selectedGame) return;
    await refresh();
    const job = selected?.recoverableJobs?.find((item) => item.jobId === result.jobId)
      || { jobId: result.jobId, mode: "assisted", stage: "waiting_for_input", destination: result.destination };
    await resumePreparation(job);
  } finally {
    checking = false;
    lock(false);
  }
}

async function continueAssisted() {
  if (!assistedJob || !assistedCanContinue || !assistedSessionId) return;
  const job = assistedJob;
  const game = selected.id;
  const sessionId = await pauseAssistedPolling();
  if (selected?.id !== game || assistedJob?.jobId !== job.jobId) {
    checking = false;
    lock(busy);
    return;
  }
  checking = false;
  lock(busy);
  await start({ action: "install", mode: "assisted", game, jobId: job.jobId,
    continue: true, sessionId });
}

async function cancelAssisted(job = assistedJob) {
  if (!job) return;
  const game = selected.id;
  const previousJob = assistedJob;
  await pauseAssistedPolling();
  try {
    const result = await backend.cancelAssisted(game, job.jobId);
    if (selected?.id !== game) return;
    if (!result?.state) {
      await resumePreparation(previousJob || job);
      return;
    }
    if (previousJob && previousJob.jobId !== job.jobId) {
      await resumePreparation(previousJob);
      return;
    }
    assistedJob = null;
    assistedCanContinue = false;
    element("assisted").hidden = true;
    await refresh();
    message("The job-owned preparation folder was removed. The selected source was left unchanged.");
    step(0);
  } catch (error) {
    if (selected?.id === game) await resumePreparation(previousJob || job);
    throw error;
  } finally {
    checking = false;
    lock(busy);
  }
}

async function openJobLog(job) {
  const result = await backend.query({ action: "job_log", game: selected.id, jobId: job.jobId });
  lines = result.text.split("\n");
  element("log").textContent = `${result.truncated ? "Earlier log output omitted. Showing the latest 256 KiB.\n\n" : ""}${result.text}`;
  element("log").closest("details").open = true;
  element("stage").textContent = `Saved job log · ${job.stage}`;
  element("stage-help").textContent = `Job ${job.jobId}`;
  element("progress").hidden = true;
  element("current").hidden = true;
  element("cancel").hidden = true;
  element("retry").hidden = true;
  element("play-existing").hidden = selected?.state !== "ready";
  step(2);
  element("log").closest("details").querySelector("summary").focus();
}

async function start(request) {
  lock(true);
  message("");
  lines = [];
  lastAction = request.mode === "assisted" ? "assisted" : request.action;
  activeJobId = request.jobId || "";
  step(2);
  element("log").textContent = "";
  element("stage").textContent = request.action === "play" ? "Launching your game…" : "Preparing installation…";
  element("stage-help").textContent = "";
  element("current").textContent = "";
  element("progress").removeAttribute("value");
  element("progress").hidden = false;
  element("current").hidden = false;
  element("cancel").hidden = request.action === "play";
  element("retry").hidden = true;
  element("play-existing").hidden = true;
  try { await backend.start(request); } catch (error) {
    lock(false);
    element("cancel").hidden = true;
    element("retry").hidden = false;
    element("play-existing").hidden = selected?.state !== "ready";
    element("stage").textContent = "Couldn’t start";
    throw error;
  }
}

function jobEvent(event) {
  if (event.kind === "stage") {
    if (event.jobId) {
      activeJobId = event.jobId;
      if (event.destination) destination = event.destination;
    }
    step(2, ["installing", "cleaning"].includes(event.stage));
    element("stage").textContent = event.message;
    element("stage-help").textContent = {
      checking: "Making sure the tools are still ready.",
      extracting: "Unpacking your copy without running the original installer.",
      validating: "Checking that your game version matches the supported release.",
      importing: "Copying your game files into the library. Your originals stay untouched.",
      building: "Compiling a native app for this computer. Build output is shown in Installation details.",
      installing: `Saving the finished app in ${destination}.`,
      cleaning: "Removing generated source, compiler output and build caches.",
    }[event.stage] || "";
    element("current").textContent = "";
    element("progress").removeAttribute("value");
  } else if (event.kind === "progress") {
    element("progress").value = event.fraction;
    element("current").textContent = event.current;
  } else if (event.kind === "log") {
    lines.push(event.text);
    if (lines.length > 2000) lines.shift();
    element("log").textContent = lines.join("\n");
  } else if (event.kind === "finished") {
    lock(false);
    element("cancel").hidden = true;
    const failed = event.outcome.kind === "error";
    if (failed) {
      failedJobId = event.outcome.diagnostic?.details?.jobId || activeJobId;
      if (lastAction === "assisted") {
        element("retry").hidden = true;
        message(errorText(event.outcome.diagnostic || event.outcome.message));
        void attempt(async () => {
          await refresh();
          const saved = selected?.recoverableJobs?.find((job) => job.jobId === failedJobId);
          if (saved) await resumePreparation(saved);
        });
        return;
      }
      element("stage").textContent = lastAction === "play" ? "Couldn’t launch the game" : "Installation stopped";
      element("stage-help").textContent = "Review the message above. You can check requirements and try again.";
      element("progress").value = 0;
      element("retry").hidden = false;
      message(errorText(event.outcome.diagnostic || event.outcome.message));
    } else {
      if (lastAction === "install" || lastAction === "assisted") { selected.state = "ready"; selected.updateAvailable = false; }
      step(3);
      if (lastAction === "play") element("installer").close();
    }
    attempt(async () => {
      await refresh();
      if (failedJobId) resumeJob = selected?.recoverableJobs?.find((job) => job.jobId === failedJobId) || null;
      renderRecovery();
    });
  }
}

function close() {
  if (busy || checking || inspecting) return;
  stopAssistedPolling();
  checkRevision++;
  element("installer").close();
}

element("installer").addEventListener("cancel", (event) => { event.preventDefault(); close(); });
element("close").onclick = close;
element("refresh").onclick = () => attempt(refresh);
element("change-copy").onclick = () => { message(""); step(1); };
element("recheck").onclick = () => attempt(checkRequirements);
element("continue").onclick = () => step(1);
element("back").onclick = () => attempt(async () => { step(0); await checkRequirements(); });
element("folder").onclick = () => attempt(() => chooseSource("folder"));
element("archive").onclick = () => attempt(() => chooseSource("source"));
element("reference").onclick = () => attempt(async () => {
  const game = selected?.id;
  const path = await backend.pick("reference");
  if (path && selected?.id === game) {
    source = path;
    sourceKind = "reference";
    candidateId = "";
    inspectionReady = false;
    resumeJob = null;
    element("selected-source").textContent = `Reference only: ${path}`;
    element("selected-source").hidden = false;
    lock(false);
  }
});
element("assisted-no-source").onclick = () => attempt(() => startAssisted(""));
element("assisted-with-source").onclick = () => attempt(() => startAssisted(source));
element("assisted-recheck").onclick = () => attempt(async () => {
  if (!assistedJob) return;
  const job = assistedJob;
  const game = selected.id;
  await pauseAssistedPolling();
  try {
    await backend.query({ action: "assisted_recheck", game, jobId: job.jobId });
    if (selected?.id === game && assistedJob?.jobId === job.jobId) await resumePreparation(job);
  } catch (error) {
    if (selected?.id === game && assistedJob?.jobId === job.jobId) await resumePreparation(job);
    throw error;
  } finally {
    checking = false;
    lock(busy);
  }
});
element("assisted-cancel").onclick = () => attempt(() => cancelAssisted());
element("assisted-continue").onclick = () => attempt(continueAssisted);
element("open-assisted-folder").onclick = () => attempt(() => backend.openAssistedFolder(selected.id, assistedJob.jobId));
element("copy-assisted-path").onclick = () => attempt(async () => {
  const value = element("assisted-path").textContent;
  try { await navigator.clipboard.writeText(value); element("assisted-status").textContent = "Drop-folder path copied."; }
  catch { element("assisted-status").textContent = "Select the drop-folder path above and copy it."; element("assisted-path").focus(); element("assisted-path").select?.(); }
});
element("install").onclick = () => attempt(() => {
  const request = { action: "install", game: selected.id, source, candidateId, destination };
  if (resumeJob && source === resumeJob.source?.path && destination === resumeJob.destination && candidateId === resumeJob.candidateId) request.jobId = resumeJob.jobId;
  else resumeJob = null;
  return start(request);
});
element("pick-destination").onclick = () => attempt(async () => {
  const folder = await backend.pick("folder");
  if (!folder) return;
  const separator = folder.includes("\\") ? "\\" : "/";
  destination = `${folder.replace(/[\\/]+$/, "")}${separator}${selected.id}`;
  element("destination").value = destination;
  resumeJob = null;
  message("This destination applies to a new job; the saved job remains unchanged.");
  lock(false);
});
for (const name of ["play", "play-existing"]) element(name).onclick = () => attempt(() => start({ action: "play", game: selected.id }));
for (const name of ["retry", "rebuild"]) element(name).onclick = () => attempt(async () => {
  if (name === "retry" && lastAction === "play") { message(""); step(3); return; }
  if (name === "retry" && failedJobId) {
    const saved = selected?.recoverableJobs?.find((job) => job.jobId === failedJobId);
    if (saved?.canResume) return retrySavedJob(saved);
    if (saved) throw new Error("This saved job needs a different recovery action. Choose it in Saved installation before retrying.");
  }
  source = selected.managedSource || source;
  if (selected.managedSource) destination = selected.installation;
  element("destination").value = destination;
  resumeJob = null;
  if (source) await inspectSource(source);
  step(0);
  await checkRequirements();
  if (checksReady && inspectionReady) step(1);
});
element("cancel").onclick = () => attempt(async () => {
  await backend.cancel();
  element("cancel").disabled = true;
  element("stage").textContent = "Stopping installation…";
});
element("settings").onclick = () => {
  selected = null;
  source = "";
  element("install-title").textContent = "Library settings";
  element("installer").showModal();
  for (const name of ["steps", "requirements", "source", "job", "ready"]) element(name).hidden = true;
  element("setup").open = true;
  message("");
};
element("installer").addEventListener("close", () => { stopAssistedPolling(); element("steps").hidden = false; });
element("save").onclick = () => attempt(async () => {
  await backend.save({ kit: element("kit").value.trim(), python: element("python").value.trim() });
  message("Settings saved.");
  await refresh();
  if (selected && selected.state !== "ready" && selected.state !== "unavailable") { step(0); await checkRequirements(); }
});
for (const [button, field, kind] of [["pick-kit", "kit", "kit"], ["pick-python", "python", "python"]]) {
  element(button).onclick = () => attempt(async () => {
    const path = await backend.pick(kind);
    if (path) element(field).value = path;
  });
}

await attempt(async () => {
  await backend.listen(jobEvent);
  const settings = await backend.settings();
  element("kit").value = settings.kit;
  element("python").value = settings.python;
  await refresh();
});
