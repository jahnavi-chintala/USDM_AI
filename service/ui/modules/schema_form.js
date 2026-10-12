// A form generated from a tool's JSON schema (input_schema): string, integer (minimum / maximum), boolean, enum,
// required. Optional fields left empty are omitted, so the tool applies its own defaults.

import { h, uid } from "./dom.js";

const LONG_TEXT = new Set(["quote", "query", "text"]);

/**
 * Returns {el, getArgs(): {args, errors}, setValues(values), focusFirst()}.
 * omit: property names not shown (e.g. doc_id, filled from the document picker).
 */
export function schemaForm(schema, { omit = [], values = {}, compact = false } = {}) {
  const props = (schema && schema.properties) || {};
  const required = new Set((schema && schema.required) || []);
  const fields = [];
  const grid = h("div", { class: "form-grid" });

  for (const [name, spec] of Object.entries(props)) {
    if (omit.includes(name)) continue;
    const id = uid(`f-${name}`);
    const helpId = `${id}-help`;
    const errId = `${id}-err`;
    const isReq = required.has(name);
    const type = spec.type || "string";
    let control;
    let kind = type;
    if (Array.isArray(spec.enum)) {
      kind = "enum";
      control = h("select", { id, class: "input", name, "aria-describedby": helpId });
      if (!isReq) control.append(h("option", { value: "" }, spec.default !== undefined ? `default (${spec.default})` : "— not set —"));
      for (const v of spec.enum) control.append(h("option", { value: String(v) }, String(v)));
    } else if (type === "boolean") {
      control = h("input", { id, type: "checkbox", name, checked: spec.default === true, "aria-describedby": helpId });
    } else if (type === "integer" || type === "number") {
      control = h("input", { id, type: "number", class: "input", name, inputmode: "numeric",
        step: type === "integer" ? "1" : "any", min: spec.minimum, max: spec.maximum,
        placeholder: spec.default !== undefined ? `default ${spec.default}` : "", "aria-describedby": helpId });
    } else if (LONG_TEXT.has(name)) {
      control = h("textarea", { id, class: "input", name, rows: compact ? "2" : "3", "aria-describedby": helpId,
        spellcheck: "false" });
    } else {
      control = h("input", { id, type: "text", class: "input", name, autocomplete: "off", spellcheck: "false",
        placeholder: spec.default !== undefined ? `default ${spec.default}` : "", "aria-describedby": helpId });
    }
    if (isReq && kind !== "boolean") control.required = true;

    const typeHint = [type, spec.minimum !== undefined ? `≥ ${spec.minimum}` : null,
      spec.maximum !== undefined ? `≤ ${spec.maximum}` : null].filter(Boolean).join(" ");
    const help = h("div", { class: "field-help", id: helpId },
      spec.description || (Array.isArray(spec.enum) ? `One of ${spec.enum.length} values.` : ""));
    const error = h("div", { class: "field-error", id: errId, hidden: true });
    let wrap;
    if (kind === "boolean") {
      wrap = h("div", { class: "field" },
        h("label", { class: "check" }, control, h("span", { class: "field-label" }, name,
          h("span", { class: "field-type" }, `boolean${spec.default !== undefined ? `, default ${spec.default}` : ""}`))),
        help, error);
    } else {
      wrap = h("div", { class: ["field", LONG_TEXT.has(name) || kind === "enum" && spec.enum.length > 12 ? "span-all" : null] },
        h("label", { class: "field-label", for: id }, name, isReq ? h("span", { class: "req", "aria-hidden": "true" }, "*") : null,
          isReq ? h("span", { class: "visually-hidden" }, "(required)") : null, h("span", { class: "field-type" }, typeHint)),
        control, help, error);
    }
    if (LONG_TEXT.has(name)) wrap.classList.add("span-all");
    grid.append(wrap);
    fields.push({ name, spec, kind, control, error, isReq });
  }
  if (!fields.length) grid.append(h("div", { class: "field-help span-all" }, "This tool takes no arguments."));

  function setValues(vals) {
    for (const f of fields) {
      if (!(f.name in vals)) continue;
      const v = vals[f.name];
      if (f.kind === "boolean") f.control.checked = v === true || v === "true";
      else f.control.value = v === null || v === undefined ? "" : String(v);
    }
  }

  function getArgs() {
    const args = {};
    const errors = [];
    for (const f of fields) {
      f.error.hidden = true;
      f.control.removeAttribute("aria-invalid");
      let fail = null;
      if (f.kind === "boolean") {
        const def = f.spec.default;
        if (def === undefined || f.control.checked !== def) args[f.name] = f.control.checked;
      } else {
        const raw = f.control.value;
        if (raw === "" || (f.kind !== "enum" && raw.trim() === "")) {
          if (f.isReq) fail = "required";
        } else if (f.kind === "integer" || f.kind === "number") {
          const n = Number(raw);
          if (!Number.isFinite(n) || (f.kind === "integer" && !Number.isInteger(n))) fail = `must be ${f.kind === "integer" ? "an integer" : "a number"}`;
          else if (f.spec.minimum !== undefined && n < f.spec.minimum) fail = `must be ≥ ${f.spec.minimum}`;
          else if (f.spec.maximum !== undefined && n > f.spec.maximum) fail = `must be ≤ ${f.spec.maximum}`;
          else args[f.name] = n;
        } else {
          args[f.name] = raw;
        }
      }
      if (fail) {
        f.error.textContent = `${f.name} ${fail}`;
        f.error.hidden = false;
        f.control.setAttribute("aria-invalid", "true");
        f.control.setAttribute("aria-errormessage", f.error.id);
        errors.push({ field: f.name, message: fail, control: f.control });
      }
    }
    if (errors.length) errors[0].control.focus();
    return { args, errors };
  }

  setValues(values);
  return {
    el: grid,
    getArgs,
    setValues,
    fields,
    focusFirst: () => { if (fields[0]) fields[0].control.focus(); },
  };
}
