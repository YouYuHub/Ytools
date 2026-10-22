/** Small JSON Schema form for manual MCP tests; the server remains the schema validator. */
(function (App) {
  "use strict";
  if (!App) return;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const omitted = Symbol("omitted");
  let nextId = 0;
  function node(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  function resolve(schema, root, seen = new Set()) {
    if (!object(schema)) return {};
    if (!schema.$ref) return schema;
    if (typeof schema.$ref !== "string" || !schema.$ref.startsWith("#/") || seen.has(schema.$ref)) return { ...schema, $unresolved: true };
    seen.add(schema.$ref);
    let target = root;
    try {
      for (const key of decodeURIComponent(schema.$ref.slice(2)).split("/").map(k => k.replace(/~1/g, "/").replace(/~0/g, "~"))) {
        if (!object(target) || !own(target, key)) return { ...schema, $unresolved: true };
        target = target[key];
      }
    } catch (_) { return { ...schema, $unresolved: true }; }
    return { ...resolve(target, root, seen), ...Object.fromEntries(Object.entries(schema).filter(([key]) => key !== "$ref")) };
  }
  function shape(raw, root) {
    let schema = resolve(raw, root), nullable = false;
    const alternatives = schema.anyOf || schema.oneOf;
    if (Array.isArray(alternatives)) {
      const nonnull = alternatives.filter(s => object(s) && s.type !== "null");
      if (alternatives.length === 2 && nonnull.length === 1 && alternatives.some(s => s.type === "null")) {
        schema = { ...schema, ...resolve(nonnull[0], root) };
        delete schema.anyOf; delete schema.oneOf; nullable = true;
      }
    }
    let type = schema.type;
    if (Array.isArray(type)) { nullable = nullable || type.includes("null"); const types = type.filter(t => t !== "null"); type = types.length === 1 ? types[0] : undefined; }
    if (!type && schema.properties) type = "object";
    return { schema, type, nullable, complex: !!(schema.$unresolved || schema.anyOf || schema.oneOf || schema.allOf || schema.if || schema.not || schema.patternProperties) };
  }
  function supported(schema) {
    const info = shape(schema, schema);
    return object(schema) && info.type === "object" && !info.complex && Object.keys(info.schema.properties || {}).length <= 100;
  }
  function create(root, values) {
    if (!supported(root) || !object(values)) throw new Error("此 Schema 请使用 JSON 模式填写。");
    let count = 0;
    const host = node("div", "mcp-param-form");
    function fail(control, text) {
      control.setAttribute("aria-invalid", "true");
      throw Object.assign(new Error(text), { control });
    }
    function jsonControl(schema, type, value, present, label) {
      const input = node("textarea"); input.rows = 3; input.spellcheck = false;
      input.value = present ? JSON.stringify(value, null, 2) : "";
      input.placeholder = type === "array" ? "[]" : type === "object" ? "{}" : "填写 JSON 值，例如字符串需加双引号";
      return { element: input, control: input, hint: "JSON 格式；完整约束将在执行前由后端验证。", read() {
        let parsed;
        try { parsed = JSON.parse(input.value); } catch (_) { fail(input, label + "：请填写有效 JSON。"); }
        return parsed;
      } };
    }
    function objectControl(schema, value, path, depth) {
      const area = node("div", "mcp-param-object"), properties = object(schema.properties) ? schema.properties : {};
      const required = new Set(Array.isArray(schema.required) ? schema.required : []);
      const entries = Object.entries(properties).map(([key, property]) => [key, parameter(key, property, required.has(key), value[key], own(value, key), [...path, key], depth + 1)]);
      entries.forEach(([, entry]) => area.append(entry.element));
      const extra = Object.create(null);
      for (const key of Object.keys(value)) if (!own(properties, key)) extra[key] = value[key];
      let extraInput = null;
      if (schema.additionalProperties !== false || Object.keys(extra).length) {
        const details = node("details", "mcp-param-extra");
        details.open = Object.keys(extra).length > 0;
        details.append(node("summary", "", "其他参数（JSON 对象，可选）"));
        extraInput = node("textarea"); extraInput.rows = 3; extraInput.value = JSON.stringify(extra, null, 2); extraInput.spellcheck = false;
        extraInput.setAttribute("aria-label", (path.join(".") || "工具") + "其他参数");
        details.append(extraInput, node("p", "mcp-test-hint", "JSON 中已有的其他字段会保留；未列出的字段也可在此填写。"));
        area.append(details);
      }
      if (!entries.length) area.prepend(node("p", "mcp-test-hint", "此工具未声明固定参数字段。"));
      return { element: area, read(partial) {
        let out = Object.create(null);
        if (extraInput) {
          try { out = JSON.parse(extraInput.value); } catch (_) { fail(extraInput, "其他参数：请填写有效 JSON 对象。"); }
          if (!object(out)) fail(extraInput, "其他参数必须是 JSON 对象。");
          if (Object.keys(out).some(key => own(properties, key))) fail(extraInput, "其他参数中存在已列出的字段，请在对应输入框中填写。");
          out = Object.assign(Object.create(null), out);
        }
        for (const [key, entry] of entries) { const val = entry.read(partial); if (val !== omitted) out[key] = val; }
        return out;
      } };
    }
    function parameter(key, raw, required, value, present, path, depth) {
      const info = shape(raw, root), schema = info.schema, label = path.join(".");
      const wrap = node("div", "mcp-param-field"), head = node("div", "mcp-param-head");
      if (info.type === "object" && !info.complex) wrap.classList.add("mcp-param-wide");
      wrap.dataset.paramPath = JSON.stringify(path);
      const caption = node("label", "mcp-param-label", key), badges = node("span", "mcp-param-type", (info.type || "JSON") + (required ? " · 必填" : " · 可选"));
      head.append(caption, badges); wrap.append(head);
      let include = null;
      if (!required) {
        const option = node("label", "mcp-param-option"); include = node("input"); include.type = "checkbox"; include.checked = present;
        option.append(include, node("span", "", "发送此参数")); head.append(option);
      }
      if (schema.description || schema.title) wrap.append(node("p", "mcp-test-hint", schema.description || schema.title));
      let nullable = null;
      if (info.nullable) {
        const option = node("label", "mcp-param-option"); nullable = node("input"); nullable.type = "checkbox"; nullable.checked = present && value === null;
        option.append(nullable, node("span", "", "设为 null")); wrap.append(option);
      }
      const content = node("fieldset", "mcp-param-value"); content.setAttribute("aria-label", label); wrap.append(content);
      const typesMatch = !present || !info.type || (value === null && info.nullable) ||
        (info.type === "object" ? object(value) : info.type === "array" ? Array.isArray(value) :
          info.type === "integer" ? Number.isInteger(value) : info.type === "number" ? typeof value === "number" : typeof value === info.type);
      const enumValues = Array.isArray(schema.enum) ? schema.enum : own(schema, "const") ? [schema.const] : null;
      const enumMatch = !present || (value === null && info.nullable) || !enumValues || enumValues.some(v => JSON.stringify(v) === JSON.stringify(value));
      let editor;
      if (++count > 180 || depth > 4 || info.complex || !typesMatch || !enumMatch) editor = jsonControl(schema, info.type, value, present, label);
      else if (enumValues || info.type === "boolean") {
        const options = enumValues || [true, false], input = node("select");
        const empty = node("option", "", "请选择"); empty.value = ""; input.append(empty);
        options.forEach((val, index) => { const option = node("option", "", typeof val === "string" ? val : JSON.stringify(val)); option.value = String(index); input.append(option); });
        const selected = present ? options.findIndex(v => JSON.stringify(v) === JSON.stringify(value)) : -1;
        input.value = selected >= 0 ? String(selected) : "";
        editor = { element: input, control: input, read() { if (input.value === "") fail(input, label + "：请选择一个值。"); return options[Number(input.value)]; } };
      } else if (info.type === "object") editor = objectControl(schema, object(value) ? value : {}, path, depth);
      else if (info.type === "array" || !["string", "number", "integer"].includes(info.type)) editor = jsonControl(schema, info.type, value, present, label);
      else {
        const numeric = info.type !== "string", input = node(numeric ? "input" : "textarea");
        if (numeric) { input.type = "number"; input.step = info.type === "integer" ? "1" : "any"; if (schema.minimum != null) input.min = schema.minimum; if (schema.maximum != null) input.max = schema.maximum; }
        else { input.rows = 1; input.spellcheck = false; }
        input.value = present && value !== null ? String(value) : "";
        editor = { element: input, control: input, read() {
          if (!numeric) return input.value;
          const number = Number(input.value);
          if (!input.value.trim() || !Number.isFinite(number) || (info.type === "integer" && !Number.isInteger(number))) fail(input, label + "：请填写有效" + (info.type === "integer" ? "整数。" : "数字。"));
          return number;
        } };
      }
      content.append(editor.element);
      const control = editor.control || content;
      control.id = "mcpParam" + (++nextId); caption.htmlFor = control.id;
      if (required) control.setAttribute("aria-required", "true");
      if (editor.hint) wrap.append(node("p", "mcp-test-hint", editor.hint));
      function update() {
        wrap.classList.toggle("is-omitted", !!include && !include.checked);
        content.disabled = !!nullable && nullable.checked;
      }
      // Typing into an optional (possibly nested) field opts it into the request.
      content.addEventListener("input", () => { if (include) include.checked = true; update(); });
      content.addEventListener("change", () => { if (include) include.checked = true; update(); });
      if (include) include.addEventListener("change", update);
      if (nullable) nullable.addEventListener("change", () => { if (include) include.checked = true; update(); });
      update();
      return { element: wrap, read(partial) {
        if (include && !include.checked) return omitted;
        if (nullable && nullable.checked) return null;
        // An unfinished required field may be omitted when opening the JSON editor,
        // but must be completed before an actual form submission.
        if (partial && editor.control && editor.control.value === "" && info.type !== "string") return omitted;
        return editor.read(partial);
      } };
    }
    const editor = objectControl(shape(root, root).schema, values, [], 0); host.append(editor.element);
    return { element: host, read({ partial = false } = {}) {
      host.querySelectorAll('[aria-invalid="true"]').forEach(el => el.removeAttribute("aria-invalid"));
      return editor.read(partial);
    } };
  }
  function sample(root) {
    function visit(raw, depth) {
      if (depth > 4) return null;
      const { schema, type } = shape(raw, root);
      if (own(schema, "const")) return schema.const;
      if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
      if (type === "object") {
        const out = Object.create(null);
        (Array.isArray(schema.required) ? schema.required : []).forEach(key => { out[key] = visit((schema.properties || {})[key], depth + 1); });
        return out;
      }
      if (type === "array") return [];
      if (type === "boolean") return false;
      if (type === "number" || type === "integer") return schema.minimum == null ? 0 : type === "integer" ? Math.ceil(schema.minimum) : schema.minimum;
      return "";
    }
    return visit(root, 0) || {};
  }
  App.mcpSchemaForm = { supported, create, sample };
})(window.App);
