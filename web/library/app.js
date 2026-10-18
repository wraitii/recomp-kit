import { platform } from "./platform.js";

const backend = platform();
const element = (id) => document.getElementById(id);
if (!navigator.platform.includes("Mac")) element("choose-source").textContent = "Choose game file";
const labels = { ready: "Ready to play", notInstalled: "Not installed", unavailable: "Unavailable", needsBuild: "Build needed", busy: "Installation in progress" };
const descriptions = {
  "ghost-recon": "Lead an elite squad through tense, tactical missions behind enemy lines.",
  "black-and-white": "Shape a world with your powers and raise a creature that learns from you.",
  "railroad-tycoon-3": "Build a railway network and grow your transport empire.",
};
const gigabytes = (bytes) => `${(bytes / 1e9).toFixed(1).replace(".", ",")} GB`;
let currentScreen = null;
let preparationRoute = false;
let hasInspection = false;
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
let candidateCanInstall = false;
let resumeJob = null;
let sourceKind = "";
let candidateSummary = "";
let candidateContentRoot = "";
let candidateExecutableBytes = 0;
let preflightReady = false;
let preflightRevision = 0;
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
  element("inspection-back").disabled = busy || checking;
  element("review-copy").disabled = busy || checking || inspecting || !inspectionReady || !candidateCanInstall || !candidateId;
  element("install").disabled = busy || checking || inspecting || !checksReady || !preflightReady || !inspectionReady || !candidateCanInstall || !candidateId || !source || !destination;
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
  panel.hidden = busy || currentScreen !== 1 || !jobs.length;
  for (const job of jobs) {
    const row = document.createElement("article");
    row.className = "recovery-job";
    const heading = document.createElement("strong");
    heading.textContent = job.mode === "assisted" ? "Preparation in progress" : "Installation interrupted";
    const status = document.createElement("p");
    const latestError = job.error || job.errors?.at(-1);
    row.classList.toggle("current-failure", Boolean(latestError || job.validationError));
    status.textContent = job.validationError || latestError?.message || (job.canResume
      ? "Continue from the saved files."
      : "This installation needs attention before it can continue.");
    if (job.mode === "assisted") status.textContent = job.validationError ||
      "Your preparation folder and copied files are still available.";
    const destinationPath = document.createElement("p");
    destinationPath.textContent = job.destination || "";
    row.append(heading, status, destinationPath);
    if (latestError) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Error details";
      const technical = document.createElement("pre");
      technical.textContent = `${latestError.code || "Error"} · ${latestError.stage || job.stage}\n${latestError.path ? `Path: ${latestError.path}\n` : ""}${JSON.stringify(latestError.details || {}, null, 2)}`;
      details.append(summary, technical);
      row.append(details);
    }
    const actions = document.createElement("div");
    actions.className = "buttons recovery-actions";
    const more = document.createElement("details");
    const moreLabel = document.createElement("summary");
    moreLabel.textContent = "More options";
    const secondary = document.createElement("div");
    secondary.className = "buttons recovery-actions";
    const savedSource = document.createElement("p");
    savedSource.textContent = `Source: ${job.source?.path || job.sourceReference?.path || "Not selected"}`;
    more.append(moreLabel, savedSource, secondary);
    const available = new Set(job.actions || []);
    const choices = [
      ["resume_preparation", "Resume preparation", () => resumePreparation(job)],
      ["retry_failed_stage", "Try again", () => retrySavedJob(job)],
      ["locate_missing_file", "Locate game files", () => locateSource(job)],
      ["recheck_preparation", "Check files", () => recheckPreparation(job)],
      ["choose_another_copy", "Choose other files", chooseAnotherCopy],
      ["change_destination", "Change install location", () => changeDestination(job)],
      ["open_log", "View log", () => openJobLog(job)],
      ["open_assisted_folder", "Open folder", () => backend.openAssistedFolder(selected.id, job.jobId)],
      ["cancel_assisted_job", "Discard preparation", () => cancelAssisted(job)],
    ];
    let hasPrimary = false;
    for (const [action, label, handler] of choices) {
      if (!available.has(action)) continue;
      const button = document.createElement("button");
      button.textContent = label;
      button.onclick = () => attempt(handler);
      if (!hasPrimary && action !== "cancel_assisted_job") {
        button.className = "primary";
        actions.append(button);
        hasPrimary = true;
      } else secondary.append(button);
    }
    if (secondary.childElementCount) row.append(more);
    row.append(actions);
    list.append(row);
  }
}

function renderInspection(result) {
  hasInspection = true;
  const target = element("inspection");
  target.replaceChildren();
  const candidates = result?.candidates || [];
  inspectionReady = Boolean(result?.scanComplete && candidates.length);
  const chosen = candidates.length === 1 ? candidates[0] : null;
  candidateId = chosen?.id || "";
  candidateCanInstall = Boolean(chosen?.installable);
  candidateContentRoot = chosen?.contentRoot || "";
  candidateExecutableBytes = chosen?.executable?.size || 0;
  candidateSummary = chosen ? `${chosen.version?.label || chosen.status || "Copy selected"}${chosen.version?.preparations?.length ? ` · preparation: ${chosen.version.preparations.join(", ")}` : ""}` : "";
  if (candidates.length > 1) { inspectionReady = false; candidateCanInstall = false; }
  const updateResult = (candidate) => {
    const ready = Boolean(result?.scanComplete && candidate?.installable);
    const media = ["inno", "unsupported_inno", "iso9660", "cabinet", "installshield_cab", "cue", "raw_cd"].includes(sourceKind);
    let title = "This copy isn’t ready to install";
    let help = `Choose a complete game folder containing ${selected.executable}, or prepare the required files using a checklist.`;
    if (ready) {
      title = "Your copy is supported";
      help = "Version verified. Next, review the install location and build tools.";
    } else if (candidates.length > 1 && !candidate) {
      title = "Choose a copy to use";
      help = "Multiple copies found. Select one to check.";
    } else if (media) {
      title = sourceKind.includes("inno") ? "This installer can’t be imported yet" : "This media can’t be imported yet";
      help = `Install or extract it outside this app, then choose the folder containing ${selected.executable}. Or collect the files using a preparation checklist.`;
    } else if (!candidates.length && sourceKind === "folder") {
      title = "No game found here";
      help = `${result.scanComplete ? "" : "The search stopped before all files were checked. "}Choose the installed game folder containing ${selected.executable}, or select that executable directly.`;
    } else if (!result?.scanComplete) {
      title = "These files couldn’t be fully checked";
      help = `Choose ${selected.executable} directly, or a folder containing just the installed game.`;
    } else if (!candidates.length) {
      title = "This file isn’t a supported game";
      help = `Choose the installed game folder, ${selected.executable}, or a ZIP of the game files.`;
    }
    element("copy-result").classList.toggle("inspection-failed", !ready && !(candidates.length > 1 && !candidate));
    element("copy-result-title").textContent = title;
    element("copy-result-help").textContent = help;
    element("review-copy").hidden = !ready;
    element("assisted-with-source").hidden = ready || !media && !candidate;
    element("assisted-with-source").classList.remove("primary");
    element("choose-another").classList.toggle("primary", !ready);
    element("choose-another").textContent = media ? "Choose extracted game files" : "Choose game files";
    const primary = ready ? "review-copy" : "choose-another";
    element("copy-result").querySelector(".actions").append(element(primary));
  };
  updateResult(chosen);
  const renderPreparation = (candidate) => {
    const guidance = target.querySelector("#preparation-guidance");
    guidance.replaceChildren();
    const preparations = candidate?.version?.preparationDetails || [];
    for (const preparation of preparations) {
      const section = document.createElement("section");
      const heading = document.createElement("strong");
      heading.textContent = `Preparation required: ${preparation.id}`;
      const explanation = document.createElement("p");
      explanation.textContent = preparation.explanation || "Prepare the listed files manually, then inspect the result again.";
      section.append(heading, explanation);
      for (const [label, items] of [["Required inputs", preparation.requiredInputs], ["Tools", preparation.requiredTools], ["Expected result", preparation.outputs]]) {
        if (!items?.length) continue;
        const row = document.createElement("p");
        row.textContent = `${label}: ${items.join(", ")}`;
        section.append(row);
      }
      guidance.append(section);
    }
  };
  for (const candidate of candidates) {
    const label = document.createElement("label");
    label.className = "candidate-choice";
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "candidate";
    input.value = candidate.id;
    input.dataset.unavailable = String(!result.scanComplete);
    input.disabled = !result.scanComplete || busy || checking;
    input.checked = candidate.id === candidateId;
    input.onchange = () => {
      candidateId = candidate.id;
      candidateContentRoot = candidate.contentRoot || "";
      candidateExecutableBytes = candidate.executable?.size || 0;
      candidateSummary = `${candidate.version?.label || candidate.status || "Copy selected"}${candidate.version?.preparations?.length ? ` · preparation: ${candidate.version.preparations.join(", ")}` : ""}`;
      if (resumeJob && candidate.id !== resumeJob.candidateId) resumeJob = null;
      inspectionReady = true;
      candidateCanInstall = Boolean(candidate.installable);
      updateResult(candidate);
      renderPreparation(candidate);
      preflightReady = false;
      if (!element("requirements").hidden) void updateSpaceEstimate();
      lock(false);
    };
    const detail = document.createElement("span");
    const relativeRoot = candidate.contentRootRelative && candidate.contentRootRelative !== "."
      ? ` · files in ${candidate.contentRootRelative}` : "";
    const candidateIssue = (candidate.diagnostics || []).find((item) => item.severity === "error");
    detail.textContent = `${candidate.version?.label || (candidate.installable ? "Supported version" : "Files need preparation")}${candidates.length > 1 ? ` · ${candidate.executable?.path || candidate.contentRootRelative || "Game copy"}` : relativeRoot}${candidateIssue ? ` · ${candidateIssue.message}` : ""}`;
    if (candidates.length > 1) label.append(input);
    label.append(detail);
    target.append(label);
  }
  const guidance = document.createElement("div");
  guidance.id = "preparation-guidance";
  target.append(guidance);
  renderPreparation(chosen);
  const diagnostics = result?.diagnostics || [];
  if (diagnostics.length) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Inspection details";
    details.append(summary);
    for (const diagnostic of diagnostics) {
      const note = document.createElement("p");
      note.textContent = diagnostic.message;
      details.append(note);
    }
    target.append(details);
  }
  target.hidden = false;
  lock(false);
}

async function inspectSource(path) {
  hasInspection = false;
  preparationRoute = false;
  const revision = ++inspectionRevision;
  const selectedGame = selected?.id;
  source = path;
  candidateId = "";
  preflightReady = false;
  preflightRevision++;
  inspectionReady = false;
  sourceKind = "";
  element("inspection").replaceChildren();
  element("inspection").hidden = true;
  element("selected-source").textContent = path;
  element("selected-source").hidden = false;
  element("copy-result").classList.remove("inspection-failed");
  element("copy-result-title").textContent = "Checking selected files…";
  element("copy-result-help").textContent = "Identifying the files and verifying the game version.";
  element("review-copy").hidden = true;
  element("assisted-with-source").hidden = true;
  message("");
  inspecting = true;
  step(4);
  lock(false);
  try {
    const result = await backend.query({ action: "inspect", game: selected.id, source });
    if (revision !== inspectionRevision || selectedGame !== selected?.id || source !== path) return null;
    sourceKind = result.source?.kind || "";
    renderInspection(result);
    element("selected-source").textContent = `Selected: ${source}`;
    element("selected-source").hidden = false;
    if (inspectionReady && candidateCanInstall && candidateId) void attempt(reviewInstallation);
    else { inspecting = false; step(4); }
    return result;
  } catch (error) {
    if (revision !== inspectionRevision || selectedGame !== selected?.id) return null;
    candidateId = "";
    inspectionReady = false;
    sourceKind = "";
    inspecting = false;
    step(4);
    element("copy-result").classList.add("inspection-failed");
    element("copy-result-title").textContent = "These files couldn’t be checked";
    element("copy-result-help").textContent = errorText(error);
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
  currentScreen = index;
  if (index === 1 || index === 4) preparationRoute = false;
  for (const [i, name] of ["requirements", "source", "job", "ready", "copy-result", "preparation-plan", "assisted"].entries()) element(name).hidden = i !== index;
  element("recovery").hidden = index !== 1 || busy || !selected?.recoverableJobs?.length;
  element("setup").hidden = true;
  element("steps").hidden = index === 3 || (index === 2 && ["play", "log"].includes(lastAction));
  const needsAttention = index === 4 && !inspecting;
  const timeline = preparationRoute
    ? [[1, "Choose copy"], [4, "Check copy"], [5, "Prepare files"], [6, "Collect files"], [2, "Install"]]
    : [[1, "Choose copy"], ...(needsAttention ? [[4, "Check copy"]] : []), [0, "Review"], [2, "Install"]];
  const activeScreen = index === 4 && inspecting ? 1 : index;
  const active = timeline.findIndex(([screen]) => screen === activeScreen);
  element("steps").replaceChildren(...timeline.map(([screen, label], i) => {
    const item = document.createElement("li");
    item.textContent = label;
    item.classList.toggle("active", screen === activeScreen);
    item.classList.toggle("done", i < active);
    if (screen === activeScreen) item.setAttribute("aria-current", "step");
    return item;
  }));
  element("source-selection").hidden = !source;
  element("source").classList.toggle("has-copy", Boolean(source));
  element("source-selection-path").textContent = source;
  element("source-selection-status").textContent = hasInspection ? candidateSummary || "Copy checked. Continue to view the result." : "This copy still needs to be checked.";
  element("choose-source").classList.toggle("primary", !source);
  element("choose-source").textContent = source ? "Change copy…" : navigator.platform.includes("Mac") ? "Choose files or folder" : "Choose game file";
  element("source-forward").hidden = !source;
  element("source-forward").textContent = hasInspection ? candidateCanInstall && inspectionReady ? "Continue to review" : "View copy details" : "Check this copy";
  element("create-preparation").textContent = assistedJob ? "Return to preparation folder" : "Create preparation folder";
  if (index === 3) {
    element("installed-location").textContent = `Installed in ${destination}`;
    element("update-note").hidden = !selected?.updateAvailable;
  }
  if (index === 1) {
    element(source ? "source-forward" : "choose-source").focus();
  } else if ([0, 4, 5, 6].includes(index)) {
    const heading = element(["requirements", "source", "job", "ready", "copy-result", "preparation-plan", "assisted"][index]).querySelector("h3");
    heading.tabIndex = -1;
    heading.focus();
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
    card.dataset.unavailable = String(game.state === "unavailable");
    card.querySelector(".detail").textContent = game.state === "unavailable" ? game.detail || "Support is not available yet." : descriptions[game.id] || "Install a native app from your game files.";
    card.querySelector(".card-action").textContent = game.state === "ready" ? "View game →" : game.state === "unavailable" ? "Not available yet" : "Set up game →";
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
  preparationRoute = false;
  hasInspection = false;
  stopAssistedPolling();
  assistedJob = null;
  assistedCanContinue = false;
  selected = game;
  failedJobId = "";
  activeJobId = "";
  checksReady = false;
  source = game.managedSource || "";
  candidateId = "";
  preflightReady = false;
  inspectionReady = Boolean(game.managedSource);
  resumeJob = null;
  destination = game.installation || "";
  element("destination").value = destination;
  element("pick-destination").disabled = false;
  element("install-title").textContent = game.title;
  element("selected-source").hidden = !source;
  element("selected-source").textContent = source ? `Using your library copy: ${source}` : "";
  element("installer").showModal();
  renderRecovery();
  message(game.maintenanceError || "");
  if (game.state === "ready") { step(3); return; }
  step(1);
  if (game.state === "unavailable") {
    element("checks").replaceChildren();
    element("check-status").textContent = game.detail || "This game is not available in this version of Recomp Toolkit.";
    element("recheck").hidden = true;
    return;
  }
  element("recheck").hidden = false;
  element("discovered").hidden = true;
  element("discovered").open = false;
  element("discovery").replaceChildren();
  discoverCopies(game.id);
}

async function discoverCopies(gameId) {
  const target = element("discovery");
  try {
    const result = await backend.query({ action: "discover", game: gameId });
    if (selected?.id !== gameId || !element("installer").open) return;
    target.replaceChildren();
    const found = result.candidates || [];
    element("discovered").hidden = !found.length;
    if (!found.some((candidate) => candidate.installable)) {
      target.textContent = result.scanComplete ? "No supported copies were found. Choose a folder or game file." : "Search was incomplete. Choose a folder or game file to inspect it directly.";
    }
    if (!found.length) return;
    const heading = document.createElement("p");
      heading.textContent = `Found ${found.length} ${found.length === 1 ? "candidate" : "candidates"}. Choose a supported copy to review.`;
    target.append(heading);
    for (const candidate of found) {
      const button = document.createElement("button");
      button.className = "discovery-choice";
      const version = typeof candidate.version === "object" ? candidate.version?.label : candidate.version;
      button.textContent = `${candidate.installable ? version || "Supported" : candidate.status || "Needs preparation"} · ${candidate.contentRoot || candidate.executable?.path || "Candidate location unavailable"}`;
      button.disabled = false;
      button.onclick = () => attempt(() => chooseDroppedPath(candidate.contentRoot || candidate.executable?.path));
      target.append(button);
    }
    lock(busy);
  } catch (error) {
    if (selected?.id === gameId) target.textContent = `Automatic search unavailable: ${errorText(error)} Choose a folder or game file.`;
  }
}

async function checkRequirements() {
  const revision = ++checkRevision;
  checking = true;
  checksReady = false;
  lock(false);
  element("checks").replaceChildren();
  element("check-status").textContent = "Checking your computer…";
  element("recheck").hidden = false;
  element("build-requirements").hidden = true;
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
    element("build-requirements").hidden = checksReady;
    element("build-requirements").open = !checksReady;
  } catch (error) {
    element("build-requirements").hidden = false;
    element("build-requirements").open = true;
    throw error;
  } finally {
    if (revision === checkRevision) {
      checking = false;
      lock(false);
    }
  }
}

async function chooseDroppedPath(path) {
  stopAssistedPolling();
  assistedJob = null;
  resumeJob = null;
  await inspectSource(path);
}

async function chooseSource() {
  const game = selected?.id;
  const path = await backend.pick("copy");
  if (path && selected?.id === game) await chooseDroppedPath(path);
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
}

function chooseAnotherCopy() {
  resumeJob = null;
  source = "";
  hasInspection = false;
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
  if (!inspectionReady && source && !await inspectSource(source)) return;
  message("This destination applies to a new job; the saved job remains unchanged.");
  step(0);
  await checkRequirements();
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
  preparationRoute = true;
  step(6);
  lock(false);
}

async function loadAssistedDetails(job, revision) {
  const details = await backend.query({ action: "assisted_path", game: selected.id, jobId: job.jobId });
  if (revision !== assistedRevision || assistedJob?.jobId !== job.jobId) return;
  element("assisted-path").textContent = details.dropFolder;
  element("assisted-checklist").textContent = details.checklistText || "The generated checklist is unavailable. Open the drop folder to inspect it.";
  if (details.validationError) {
    assistedCanContinue = false;
    element("assisted-summary").textContent = details.validationError;
    element("assisted-summary").classList.add("preparation-error");
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
        title.textContent = `${item.path} · ${item.status.replaceAll("_", " ")}${item.required ? "" : " (optional)"}`;
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
    const requiredVerified = itemList.filter((item) => item.required).every((item) => item.status === "verified");
    const candidateProblem = requiredVerified ? result.diagnostics?.find((item) => item.message)?.message : "";
    const verified = itemList.filter((item) => item.required && item.status === "verified").length;
    const required = itemList.filter((item) => item.required).length;
    const message = result.recipeChanged ? "Build tools changed. Choose Check files again in Preparation options." :
      result.canContinue ? "All required files are ready. You can build the game." :
      candidateProblem || `${verified} of ${required} required files ready. ${result.checking ? "Checking copied files…" : "Copy the remaining files into the folder above."}`;
    element("assisted-summary").classList.remove("preparation-error");
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
      element("assisted-summary").textContent = text;
      element("assisted-summary").classList.add("preparation-error");
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
  element("copy-assisted-path").textContent = "Copy path";
  element("assisted-summary").classList.remove("preparation-error");
  element("assisted-summary").textContent = "Checking files…";
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
    step(1);
  } catch (error) {
    if (selected?.id === game) await resumePreparation(previousJob || job);
    throw error;
  } finally {
    checking = false;
    lock(busy);
  }
}

async function openJobLog(job) {
  lastAction = "log";
  element("job").classList.remove("job-failed");
  const result = await backend.query({ action: "job_log", game: selected.id, jobId: job.jobId });
  lines = result.text.split("\n");
  element("log").textContent = `${result.truncated ? "Earlier log output omitted. Showing the latest 256 KiB.\n\n" : ""}${result.text}`;
  element("log").closest("details").open = true;
  element("job-back").hidden = false;
  element("job-duration").hidden = true;
  element("stage").textContent = "Installation log";
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
  element("job").classList.remove("job-failed");
  element("job-back").hidden = true;
  element("retry").textContent = "Try again";
  element("log").textContent = "Waiting for installation output…";
  element("stage").textContent = request.action === "play" ? "Launching your game…" : "Preparing installation…";
  element("stage-help").textContent = "";
  element("job-duration").hidden = request.action === "play";
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
    element("job").classList.add("job-failed");
    element("job-back").hidden = false;
    element("job-duration").hidden = true;
    element("progress").hidden = true;
    element("retry").classList.toggle("primary", selected?.state !== "ready");
    element("stage").textContent = "Couldn’t start";
    element("stage-help").textContent = errorText(error);
  }
}

function appendLog(text) {
  lines.push(text);
  if (lines.length > 2000) lines.shift();
  element("log").textContent = lines.join("\n");
}

function jobEvent(event) {
  if (event.kind === "stage") {
    if (event.jobId) {
      activeJobId = event.jobId;
      if (event.destination) destination = event.destination;
    }
    step(2, ["installing", "cleaning"].includes(event.stage));
    element("stage").textContent = event.message;
    appendLog(event.message);
    element("stage-help").textContent = {
      checking: "Checking build tools.",
      extracting: "Unpacking your copy without running the original installer.",
      validating: "Checking that your game version matches the supported release.",
      packaging: "Assembling the finished app and verifying its files before installation.",
      fingerprinting: "Verifying all game files. Large copies can take a while.",
      copying: "Copying your game files. Your originals stay untouched.",
      importing: "Copying your game files into the library. Your originals stay untouched.",
      building: "Compiling the app. This may take a while; compiler output appears below.",
      installing: `Saving the finished app in ${destination}.`,
      publishing: `Saving the finished app in ${destination}.`,
      cleaning: "Removing generated source, compiler output and build caches.",
    }[event.stage] || "";
    element("current").textContent = "";
    element("progress").removeAttribute("value");
  } else if (event.kind === "progress") {
    if (Number.isFinite(event.fraction)) element("progress").value = event.fraction;
    else element("progress").removeAttribute("value");
    const bytes = Number.isFinite(event.bytes) ? `${gigabytes(event.bytes)}${event.totalBytes ? ` / ${gigabytes(event.totalBytes)}` : ""} · ` : "";
    element("current").textContent = `${bytes}${event.current || ""}`;
  } else if (event.kind === "log") {
    appendLog(event.text);
  } else if (event.kind === "finished") {
    lock(false);
    element("cancel").hidden = true;
    const failed = event.outcome.kind === "error";
    if (failed) {
      failedJobId = event.outcome.diagnostic?.details?.jobId || activeJobId;
      element("stage").textContent = lastAction === "play" ? "Couldn’t launch the game" : "Installation stopped";
      element("job").classList.add("job-failed");
      element("job-back").hidden = false;
      element("job-duration").hidden = true;
      element("progress").hidden = true;
      element("current").hidden = true;
      element("stage-help").textContent = errorText(event.outcome.diagnostic || event.outcome.message);
      element("retry").textContent = lastAction === "assisted" ? "Return to preparation" : "Try again";
      element("retry").hidden = false;
      element("retry").classList.toggle("primary", selected?.state !== "ready");
      message("");
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
element("recheck").onclick = () => attempt(checkRequirements);
async function reviewInstallation() {
  step(0);
  await checkRequirements();
  if (currentScreen === 0) await updateSpaceEstimate();
}
element("review-copy").onclick = () => attempt(reviewInstallation);
async function updateSpaceEstimate() {
  const revision = ++preflightRevision;
  preflightReady = false;
  if (!destination) {
    element("space-summary").textContent = "Choose a location to check available space.";
    lock(false);
    return;
  }
  element("space-summary").textContent = "Checking available space…";
  lock(false);
  if (!["folder", "executable", "zip"].includes(sourceKind)) {
    element("space-summary").textContent = "Choose an installed folder, executable or ZIP to estimate space.";
    lock(false);
    return;
  }
  try {
    const preflightSource = sourceKind === "zip" ? source : candidateContentRoot || source;
    const estimate = await backend.query({ action: "preflight", source: preflightSource, sourceKind, destination });
    if (revision !== preflightRevision || currentScreen !== 0) return;
    const installedBytes = estimate.source.bytes + 40_000_000 + 10 * candidateExecutableBytes;
    element("space-summary").textContent = `About ${gigabytes(installedBytes)} installed · ${gigabytes(estimate.destination.requiredBytes)} free needed during the build.${estimate.destination.enough ? "" : ` Only ${gigabytes(estimate.destination.freeBytes)} available. Choose another location.`}`;
    preflightReady = estimate.destination.enough;
  } catch (error) {
    if (revision === preflightRevision) element("space-summary").textContent = `Space estimate failed: ${errorText(error)}`;
  }
  if (revision === preflightRevision) lock(false);
}
element("open-installation").onclick = () => attempt(() => backend.openFolder(destination));
element("choose-source").onclick = () => attempt(chooseSource);
element("choose-another").onclick = () => attempt(chooseSource);
element("source-forward").onclick = () => attempt(() => hasInspection ? candidateCanInstall && inspectionReady ? reviewInstallation() : step(4) : inspectSource(source));
element("inspection-back").onclick = () => {
  if (inspecting) {
    inspectionRevision++;
    inspecting = false;
  }
  message("");
  step(1);
  lock(false);
};
element("review-back").onclick = () => { message(""); step(1); };
element("preparation-back").onclick = () => { message(""); step(hasInspection ? 4 : 1); };
element("assisted-back").onclick = () => attempt(async () => {
  await pauseAssistedPolling();
  checking = false;
  message("");
  step(5);
  lock(false);
});
element("assisted-with-source").onclick = () => {
  preparationRoute = true;
  message("");
  step(5);
};
element("create-preparation").onclick = () => attempt(() => assistedJob ? resumePreparation(assistedJob) : startAssisted(source));
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
  try { await navigator.clipboard.writeText(value); element("copy-assisted-path").textContent = "Copied"; element("assisted-status").textContent = "Folder path copied."; }
  catch { message("Select the folder path above and copy it manually."); element("assisted-path").focus(); element("assisted-path").select?.(); }
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
  preflightReady = false;
  if (!element("requirements").hidden) await updateSpaceEstimate();
  else element("space-summary").textContent = "Destination changed. Review the copy again to check space.";
  resumeJob = null;
  lock(false);
});
for (const name of ["play", "play-existing"]) element(name).onclick = () => attempt(() => start({ action: "play", game: selected.id }));
element("retry").onclick = () => attempt(async () => {
  if (lastAction === "play") { message(""); step(3); return; }
  if (failedJobId) {
    const saved = selected?.recoverableJobs?.find((job) => job.jobId === failedJobId);
    if (saved?.mode === "assisted") return resumePreparation(saved);
    if (saved?.canResume) return retrySavedJob(saved);
    if (saved) throw new Error("This saved job needs a different recovery action. Choose it in Saved installation before retrying.");
  }
  source = selected.managedSource || source;
  if (selected.managedSource) destination = selected.installation;
  element("destination").value = destination;
  resumeJob = null;
  if (source) await inspectSource(source);
});
element("job-back").onclick = () => { message(""); step(lastAction === "play" ? 3 : 1); };
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
  for (const name of ["steps", "requirements", "source", "job", "ready", "copy-result", "preparation-plan"]) element(name).hidden = true;
  element("setup").hidden = false;
  element("assisted").hidden = true;
  element("recovery").hidden = true;
  message("");
};
element("installer").addEventListener("close", () => { stopAssistedPolling(); element("steps").hidden = false; });
element("save").onclick = () => attempt(async () => {
  await backend.save({ kit: element("kit").value.trim(), python: element("python").value.trim() });
  await refresh();
  close();
  message("Settings saved.");
});
for (const [button, field, kind] of [["pick-kit", "kit", "kit"], ["pick-python", "python", "python"]]) {
  element(button).onclick = () => attempt(async () => {
    const path = await backend.pick(kind);
    if (path) element(field).value = path;
  });
}

await attempt(async () => {
  await backend.listen(jobEvent);
  window.__TAURI__?.event.listen("tauri://drag-drop", ({ payload }) => {
    const path = payload?.paths?.[0];
    if (path && selected && element("installer").open && !busy && !checking && !inspecting && element("assisted").hidden) attempt(() => chooseDroppedPath(path));
  });
  const settings = await backend.settings();
  element("kit").value = settings.kit;
  element("python").value = settings.python;
  await refresh();
});
