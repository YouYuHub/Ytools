/* File diff rendering and tool-path labels; loaded before messages.js. */
(function (App) {
  "use strict";
  const { el } = App;

  // ---------- 展示用 diff 视图（write_file / edit_file 结果） ----------
  // 解析 unified diff 文本：@@ hunk 头（淡化分隔）、+ 添加（绿）、- 删除（红）、
  // 空格行上下文；首字符规则解析，与后端 difflib 输出格式强约定。
  // 行内语法高亮：从 ---/+++ 头推断文件语言 → 行文本经 Prism 高亮后注入
  // .diff-text（Prism 输出已 HTML 转义，textContent 先行、高亮成功才替换）；
  // 无 Prism / 语言不支持 / 高亮异常自动回退纯文本。

  // diff 文件名后缀 → Prism 语言名（与 markdown.js DIFF_EXTS 同口径；未收录返回空串）
  const DIFF_FILE_EXTS = {
    py: "python",
    js: "javascript", mjs: "javascript", jsx: "javascript",
    ts: "typescript", tsx: "typescript",
    cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", h: "cpp", c: "cpp",
    java: "java", cs: "csharp",
    css: "css", less: "less", scss: "scss", sass: "sass", qss: "css",
    qml: "qml", html: "markup", htm: "markup", xml: "markup", svg: "markup",
    go: "go", rs: "rust",
    sh: "bash", bash: "bash",
    json: "json", jsonc: "json", jsonl: "json",
    yaml: "yaml", yml: "yaml",
    ini: "ini", env: "ini",
    cmake: "cmake", makefile: "makefile", dockerfile: "docker",
  };

  // 从 diff 头部（--- a/xxx / +++ b/xxx）推断内层语言；缓存按 diffObj 引用
  const diffLangCache = new WeakMap();
  function inferDiffFileLang(diffObj, diffText) {
    if (diffObj && diffLangCache.has(diffObj)) return diffLangCache.get(diffObj);
    let lang = "";
    const head = (diffText || "").split("\n", 4);
    for (let i = 0; i < head.length && !lang; i++) {
      // difflib 头：`--- a/相对路径` / `+++ b/相对路径`；兼容裸路径写法
      const m = head[i].match(/^--- (?:a\/)?(\S+)|^\+\+\+ (?:b\/)?(\S+)/);
      if (!m) continue;
      const file = m[1] || m[2] || "";
      const base = file.replace(/\\/g, "/").split("/").pop().toLowerCase();
      const ext = base.indexOf(".") >= 0 ? base.split(".").pop() : "";
      if (ext && DIFF_FILE_EXTS[ext]) lang = DIFF_FILE_EXTS[ext];
      else if (base.startsWith("dockerfile")) lang = "docker";
      else if (base.startsWith("makefile") || base.startsWith("gnumakefile")) lang = "makefile";
      else if (base.startsWith("cmakelists")) lang = "cmake";
      else if (base.startsWith(".env")) lang = "ini";
    }
    if (diffObj) diffLangCache.set(diffObj, lang);
    return lang;
  }

  // 行文本 → Prism 高亮 HTML（未高亮时返回 null，调用方回退 textContent）。
  // 与官方 highlight() 同链路：tokenize 后必须先 util.encode 转义再 stringify
  //（Token.stringify 自身不转义字符串内容，漏 encode 会把 <script> 裸注入）。
  function highlightDiffLine(text, lang) {
    if (!window.Prism || !Prism.languages || !Prism.languages[lang]) return null;
    try {
      const encoded = Prism.util.encode(Prism.tokenize(text, Prism.languages[lang]));
      return Prism.Token.stringify(encoded, lang);   // 输出已 HTML 转义
    } catch (_) {
      return null;
    }
  }

  function buildDiffView(diffObj) {
    if (diffObj && Array.isArray(diffObj.files)) {
      const batch = el("div", "diff-batch");
      diffObj.files.forEach(function (file) {
        if (!file || typeof file !== "object") return;
        const section = el("details", "diff-file");
        const head = el("summary", "diff-file-head");
        const action = {add: "新建", update: "修改", delete: "删除"}[file.action] || "变更";
        head.appendChild(el("span", "diff-file-path", file.display_path || file.path || "文件"));
        head.appendChild(el("span", "diff-title", action));
        if (file.lines_added != null && file.lines_removed != null) {
          head.appendChild(el("span", "diff-num is-add", "+" + file.lines_added));
          head.appendChild(el("span", "diff-num is-del", "-" + file.lines_removed));
        } else head.appendChild(el("span", "diff-title", "统计不可用"));
        section.appendChild(head);
        let rendered = false;
        function show() {
          if (!section.open || rendered) return;
          rendered = true;
          section.appendChild(buildDiffView(file));
        }
        section.addEventListener("toggle", show);
        section.open = diffObj.files.length === 1;
        show();
        batch.appendChild(section);
      });
      return batch;
    }
    const box = el("div", "diff-view");
    const statsBits = [];
    const added = Number(diffObj && diffObj.lines_added) || 0;
    const removed = Number(diffObj && diffObj.lines_removed) || 0;
    if (added) statsBits.push('+' + added);
    if (removed) statsBits.push('-' + removed);
    const truncated = !!(diffObj && diffObj.diff_truncated);
    const skipped = (diffObj && diffObj.diff_skipped) || "";

    const bar = el("div", "diff-bar");
    bar.appendChild(el("span", "diff-title", "文件变更"));
    const stats = el("span", "diff-stats");
    if (statsBits.length) {
      statsBits.forEach(function (bit) {
        stats.appendChild(el("span", bit.charAt(0) === "+" ? "diff-num is-add" : "diff-num is-del", bit));
      });
    } else if (skipped === "unchanged") {
      stats.appendChild(el("span", "diff-num", "内容无变化"));
    } else if (skipped === "file_too_large") {
      stats.appendChild(el("span", "diff-num", "文件过大，跳过 diff"));
    }
    if (truncated) {
      stats.appendChild(el("span", "diff-num", "diff 已截断"));
    }
    bar.appendChild(stats);
    box.appendChild(bar);

    const text = String((diffObj && diffObj.diff) || "");
    if (text) {
      // 语言推断一次并缓存（WeakMap 随 diffObj 生命周期，历史回放同样命中）
      const lineLang = inferDiffFileLang(diffObj, text);
      const pre = el("pre", "diff-code" + (lineLang ? " language-" + lineLang : ""));
      const lines = text.split("\n");
      let oldNo = 0;
      let newNo = 0;
      lines.forEach(function (line, index) {
        if (line.indexOf("@@") === 0) {
          const hunk = el("div", "diff-line is-hunk", line);
          const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?/.exec(line);
          if (match) {
            oldNo = parseInt(match[1], 10);
            newNo = parseInt(match[2], 10);
          }
          pre.appendChild(hunk);
          return;
        }
        if (index < 2 && (line.indexOf("--- ") === 0 || line.indexOf("+++ ") === 0)) {
          pre.appendChild(el("div", "diff-line is-file", line));
          return;
        }
        const kind = line.charAt(0);
        const cls = kind === "+" ? "is-add" : (kind === "-" ? "is-del" : "is-ctx");
        const row = el("div", "diff-line " + cls);
        // 双行号列：左列=旧行号（+ 行留空）、右列=新行号（- 行留空），
        // 两个固定宽度右对齐子列，删除/新增的号码不再挤在同一位置
        const gutter = el("span", "diff-no");
        gutter.appendChild(el("span", "diff-no-old", kind === "+" ? "" : String(oldNo)));
        gutter.appendChild(el("span", "diff-no-new", kind === "-" ? "" : String(newNo)));
        row.appendChild(gutter);
        row.appendChild(el("span", "diff-sign", kind === " " ? "" : kind));
        const cell = el("span", "diff-text");
        const content = line.slice(1);
        if (lineLang) {
          const html = highlightDiffLine(content, lineLang);
          if (html != null) {
            cell.innerHTML = html;   // Prism Token.stringify 输出已转义，安全
          } else {
            cell.textContent = content;
          }
        } else {
          cell.textContent = content;
        }
        row.appendChild(cell);
        if (kind === "+") {
          newNo += 1;
        } else if (kind === "-") {
          oldNo += 1;
        } else {
          oldNo += 1;
          newNo += 1;
        }
        pre.appendChild(row);
      });
      box.appendChild(pre);
    }
    return box;
  }

  // 从工具参数（对象或 JSON 文本均可）提取标题栏路径信息：
  // read_file/write_file/edit_file 用 path（旧数据 full_file_name 兜底）；search_files 用搜索目录 dir_path；
  // 兜底 _file_diff 无 path 字段的旧数据。参数可能是流式未闭合 JSON，宽松正则兜底。
  function extractPathFromArgsValue(args, name) {
    let obj = args;
    if (typeof args === "string") {
      args = args.trim();
      if (!args) return "";
      try {
        obj = JSON.parse(args);
      } catch (_) {
        const m = /"path"\s*:\s*"([^"]+)"/.exec(args)
          || /"full_file_name"\s*:\s*"([^"]+)"/.exec(args)
          || /"dir_path"\s*:\s*"([^"]+)"/.exec(args);
        return m ? m[1].replace(/\\\\/g, "\\") : "";
      }
    }
    if (!obj || typeof obj !== "object") return "";
    if (name === "search_files" || name === "list_files") {
      // 目录 + 搜索模式组合展示；未指定目录时回退显示模式（保证有识别信息）
      const dir = String(obj.dir_path || "").trim();
      const pattern = String(obj.pattern || "").trim();
      if (dir && pattern) return dir + "（" + pattern + "）";
      return dir || pattern;
    }
    return String(obj.path || obj.full_file_name || "").trim();
  }

  // 相对路径按会话工作目录解析为完整路径（模型常传相对路径，标题栏展示
  // 完整定位）；已是绝对路径（盘符/根/~）或无法得知工作目录时原样返回
  function resolveToolPath(path) {
    const raw = String(path || "").trim();
    if (!raw) return "";
    if (/^([A-Za-z]:[\\/]|[\\/]|~)/.test(raw)) return raw;
    const workDir = String((App.state && App.state.workDir) || "").trim();
    if (!workDir) return raw;
    const rel = raw.replace(/^\.[\\/]/, "");
    const sep = workDir.indexOf("\\") !== -1 ? "\\" : "/";
    return workDir.replace(/[\\/]+$/, "") + sep + rel;
  }

  // 从 unified diff 文件头（--- a/xxx / +++ b/xxx）提取展示路径
  function extractPathFromDiffText(diffText) {
    if (!diffText) return "";
    const m = /^--- (?:a\/)?(.+)$/m.exec(diffText);
    return m ? m[1].trim() : "";
  }

  App.messageDiff = Object.freeze({buildDiffView, extractPathFromArgsValue, resolveToolPath, extractPathFromDiffText});
})(window.App);
