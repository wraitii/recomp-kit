export function platform() {
  const desktop = window.__TAURI__;
  if (!desktop) {
    const unavailable = async () => { throw new Error("Local compilation requires Recomp Toolkit. Browser play remains available through the kit’s web launcher."); };
    return { settings: unavailable, save: unavailable, pick: unavailable, query: unavailable,
      start: unavailable, cancel: unavailable, openFolder: unavailable, openAssistedFolder: unavailable, cancelAssisted: unavailable,
      open: (url) => window.open(url, "_blank", "noopener"),
      listen: async () => () => {} };
  }
  const invoke = desktop.core.invoke;
  return {
    settings: () => invoke("settings"),
    save: (value) => invoke("save_settings", { value }),
    pick: (kind) => invoke("pick", { kind }),
    query: (request) => invoke("query", { request }),
    start: (request) => invoke("start_job", { request }),
    cancel: () => invoke("cancel_job"),
    openFolder: (path) => invoke("open_folder", { path }),
    openAssistedFolder: (game, jobId) => invoke("open_assisted_folder", { game, jobId }),
    cancelAssisted: (game, jobId) => invoke("cancel_assisted", { game, jobId }),
    open: (url) => invoke("open_url", { url }),
    listen: (callback) => desktop.event.listen("library-job", ({ payload }) => callback(payload)),
  };
}
