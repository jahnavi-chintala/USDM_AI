// Modal dialogs on the native <dialog> element (focus trapping, Escape and the backdrop come with it).

import { h, icon, uid } from "./dom.js";
import { isOffline, on, state } from "./state.js";

/**
 * A confirmation dialog. Resolves {ok, checks: {name: bool}, values: {name: string}}.
 * checkboxes: [{name, label, checked, help}]; danger: red confirm button and the focus starts on Cancel.
 */
export function confirmDialog({ title, body, confirmLabel = "Continue", cancelLabel = "Cancel", danger = false,
  checkboxes = [], iconName, wide = false } = {}) {
  return new Promise((resolve) => {
    const titleId = uid("dlg-title");
    const checks = checkboxes.map((c) => {
      const input = h("input", { type: "checkbox", name: c.name, checked: Boolean(c.checked) });
      return { c, input, el: h("div", { class: "stack-sm" },
        h("label", { class: "check" }, input, h("span", null, c.label)),
        c.help ? h("div", { class: "field-help" }, c.help) : null) };
    });
    const cancel = h("button", { type: "button", class: "btn", value: "cancel" }, cancelLabel);
    const confirm = h("button", { type: "button", class: ["btn", danger ? "btn-danger-solid" : "btn-primary"],
      value: "ok" }, confirmLabel);
    const dialog = h("dialog", { class: ["dialog", wide ? "wide" : null], "aria-labelledby": titleId },
      h("div", { class: "dialog-inner" },
        h("div", { class: "dialog-head" }, iconName ? icon(iconName) : null, h("h2", { id: titleId }, title)),
        h("div", { class: "dialog-body" }, body, checks.map((c) => c.el)),
        h("div", { class: "dialog-foot" }, cancel, confirm)));
    let settled = false;
    const finish = (ok) => { // once, from a button, Escape ("cancel") or any other close
      if (settled) return;
      settled = true;
      const out = { ok, checks: {} };
      for (const c of checks) out.checks[c.c.name] = c.input.checked;
      if (dialog.open) dialog.close();
      dialog.remove();
      resolve(out);
    };
    cancel.addEventListener("click", () => finish(false));
    confirm.addEventListener("click", () => finish(true));
    dialog.addEventListener("cancel", () => finish(false));
    dialog.addEventListener("close", () => finish(false));
    document.body.appendChild(dialog);
    dialog.showModal();
    (danger ? cancel : confirm).focus();
  });
}

/** A dialog that only shows content (image viewer, details). */
export function showDialog({ title, body, wide = true, closeLabel = "Close" }) {
  const titleId = uid("dlg-title");
  const close = h("button", { type: "button", class: "btn" }, closeLabel);
  const dialog = h("dialog", { class: ["dialog", wide ? "wide" : null], "aria-labelledby": titleId },
    h("div", { class: "dialog-inner" },
      h("div", { class: "dialog-head" }, h("h2", { id: titleId, class: "break" }, title)),
      h("div", { class: "dialog-body" }, body),
      h("div", { class: "dialog-foot" }, close)));
  const finish = () => {
    if (dialog.open) dialog.close();
    dialog.remove();
  };
  close.addEventListener("click", finish);
  dialog.addEventListener("click", (e) => { if (e.target === dialog) finish(); }); // the backdrop
  dialog.addEventListener("cancel", finish);
  dialog.addEventListener("close", finish);
  document.body.appendChild(dialog);
  dialog.showModal();
  close.focus();
  return dialog;
}

// ---------------------------------------------------------------- OpenRouter: badge and confirmation

const IDENTITY_WHAT = "the text of the first three pages and the running headers/footers (one request, for the identity check)";

const WHAT = {
  upload: "figure images (PNG crops) for the figure descriptions and the text of every chunk for the embeddings",
  reprocess: "figure images not yet in the figure cache and chunk texts not yet in the embedding cache (a reprocess of an unchanged PDF usually finds everything cached and sends nothing)",
  search: "the query text (query embedding) and the query with the candidate chunk texts (reranking), unless this exact query is cached",
};

function models() {
  const cfg = (state.version && state.version.config) || {};
  return cfg;
}

/** The visible marker on every action that may call OpenRouter; it follows the service's offline flag. */
export function openRouterBadge(kind = "search") {
  const el = h("span", { class: "badge badge-violet openrouter-badge", tabindex: "0", role: "note" });
  const paint = () => {
    const offline = isOffline();
    const label = offline ? "OpenRouter: offline" : "Calls OpenRouter";
    const identity = kind !== "search" && models().identity_model ? `; ${IDENTITY_WHAT}` : "";
    const title = offline
      ? "The service runs with OPENROUTER_OFFLINE=1: nothing is sent to OpenRouter."
      : `May send to OpenRouter (zero-data-retention routing): ${WHAT[kind] || WHAT.search}${identity}.`;
    el.classList.toggle("is-offline", Boolean(offline));
    el.title = title;
    el.setAttribute("aria-label", `${label}. ${title}`);
    el.replaceChildren(icon("cloud"), label);
  };
  paint();
  let seen = false;
  const off = on("version", () => {
    if (el.isConnected) seen = true;
    else if (seen) { off(); return; }
    paint();
  });
  return el;
}

/**
 * Ask before an action that may call OpenRouter. Resolves true when confirmed (or when the service is offline and
 * `skipWhenOffline`). `extra` is shown below the statement; checkboxes as in confirmDialog (returned in .checks).
 */
export async function confirmOpenRouter(kind, { title, action, extra, checkboxes = [], skipWhenOffline = true,
  confirmLabel } = {}) {
  const offline = isOffline();
  if (offline && skipWhenOffline && !checkboxes.length) return { ok: true, checks: {} };
  const cfg = models();
  const body = [];
  if (offline) {
    body.push(h("div", { class: "alert alert-ok" }, icon("check"), h("div", { class: "alert-body" },
      h("div", { class: "alert-title" }, "The service is offline"),
      "OPENROUTER_OFFLINE is set: nothing is sent to OpenRouter. Figures are not described, embeddings are skipped "
      + "and search answers only cached queries.")));
  } else {
    const identity = kind !== "search" && cfg.identity_model;
    const modelLine = kind === "search"
      ? `${cfg.embed_model || "embedding model"} (query embedding) and ${cfg.rerank_model || "reranker"} (reranking)`
      : `${cfg.figure_model || "figure model"} (figure descriptions)`
        + (identity ? `, ${cfg.identity_model} (identity check)` : "")
        + ` and ${cfg.embed_model || "embedding model"} (embeddings)`;
    const what = (WHAT[kind] || WHAT.search) + (identity ? `; ${IDENTITY_WHAT}` : "");
    body.push(h("p", { class: "mt-0" }, action || "This action may call OpenRouter."));
    body.push(h("div", { class: "alert alert-violet" }, icon("cloud"), h("div", { class: "alert-body" },
      h("div", { class: "alert-title" }, "Sent to OpenRouter, with zero-data-retention routing"),
      h("div", null, `What: ${what}.`),
      h("div", { class: "small muted" }, `Models: ${modelLine}. Every request carries provider data_collection "deny", `
        + "zdr true and no fallbacks. The PDF itself, file names and whole documents are never sent."))));
    if (state.version === null) {
      body.push(h("div", { class: "small muted" }, "The service's offline flag is unknown (GET /version failed): "
        + "assuming it is online."));
    }
  }
  if (extra) body.push(extra);
  return confirmDialog({ title: title || "Confirm", body, confirmLabel: confirmLabel || (offline ? "Continue" : "Send and continue"),
    checkboxes, iconName: offline ? null : "cloud" });
}
