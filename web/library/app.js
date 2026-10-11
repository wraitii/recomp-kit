import { platform } from "./platform.js";

const backend = platform();
const element = (id) => document.getElementById(id);
const labels = { ready: "Ready to play", notInstalled: "Not installed", unavailable: "Unavailable", needsBuild: "Build needed" };
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

function message(text) {
  const target = element(element("installer").open ? "install-message" : "message");
  target.textContent = text;
  target.hidden = !text;
}

async function attempt(action) {
  try { await action(); } catch (error) { message(String(error)); }
}

function lock(value) {
  busy = value;
  for (const button of document.querySelectorAll("button")) button.disabled = busy || checking;
  element("cancel").disabled = !busy;
  element("continue").disabled = busy || checking || !checksReady;
  element("install").disabled = busy || checking || !checksReady || !source || !destination;
  element("pick-destination").disabled = busy || checking || Boolean(selected?.managedSource);
}

function step(index, installing = false) {
  for (const [i, name] of ["requirements", "source", "job", "ready"].entries()) element(name).hidden = i !== index;
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
  element("play-existing").hidden = selected?.state !== "ready" || element("job").hidden || element("retry").hidden;
  if (selected && !element("ready").hidden) step(3);
  lock(busy);
}

async function openGame(game) {
  selected = game;
  checksReady = false;
  source = game.managedSource || "";
  destination = game.installation || "";
  element("destination").value = destination;
  element("pick-destination").disabled = Boolean(game.managedSource);
  element("install-title").textContent = game.title;
  element("setup").open = false;
  element("selected-source").hidden = !source;
  element("selected-source").textContent = source ? `Using your library copy: ${source}` : "";
  element("copy-help").textContent = `Use your supported copy containing ${game.executable}. We’ll verify the version before building.`;
  element("installer").showModal();
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
    const result = await backend.query({ action: "check", needsRust: selected?.needsRust ?? true, needsInnoextract: source.toLowerCase().endsWith(".exe") });
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
  source = path;
  element("selected-source").textContent = `Selected: ${source}`;
  element("selected-source").hidden = false;
  step(0);
  await checkRequirements();
  if (!element("continue").disabled) step(1);
}

async function start(request) {
  lock(true);
  message("");
  lines = [];
  lastAction = request.action;
  step(2);
  element("log").textContent = "";
  element("stage").textContent = request.action === "play" ? "Launching your game…" : "Preparing installation…";
  element("stage-help").textContent = "";
  element("current").textContent = "";
  element("progress").removeAttribute("value");
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
      element("stage").textContent = lastAction === "play" ? "Couldn’t launch the game" : "Installation stopped";
      element("stage-help").textContent = "Review the message above. You can check requirements and try again.";
      element("progress").value = 0;
      element("retry").hidden = false;
      message(event.outcome.message);
    } else {
      if (lastAction === "install") { selected.state = "ready"; selected.updateAvailable = false; }
      step(3);
      if (lastAction === "play") element("installer").close();
    }
    attempt(refresh);
  }
}

function close() {
  if (busy || checking) return;
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
element("install").onclick = () => attempt(() => start({ action: "install", game: selected.id, source, destination }));
element("pick-destination").onclick = () => attempt(async () => {
  const folder = await backend.pick("folder");
  if (!folder) return;
  const separator = folder.includes("\\") ? "\\" : "/";
  destination = `${folder.replace(/[\\/]+$/, "")}${separator}${selected.id}`;
  element("destination").value = destination;
  lock(false);
});
for (const name of ["play", "play-existing"]) element(name).onclick = () => attempt(() => start({ action: "play", game: selected.id }));
for (const name of ["retry", "rebuild"]) element(name).onclick = () => attempt(async () => {
  if (name === "retry" && lastAction === "play") { message(""); step(3); return; }
  source = selected.managedSource || source;
  if (selected.managedSource) destination = selected.installation;
  element("destination").value = destination;
  step(0);
  await checkRequirements();
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
element("installer").addEventListener("close", () => { element("steps").hidden = false; });
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
