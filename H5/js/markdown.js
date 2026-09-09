/**
 * 轻量 Markdown 渲染器（先转义 HTML，再解析块级与行内语法）
 * 支持：标题 / 代码块 / 行内代码 / 粗斜体 / 链接 / 列表 / 引用 / 表格 / 分割线
 */
(function (global) {
  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // 行内占位符前缀/后缀（控制字符，正常文本不会出现）
  const PH_OPEN = "\u0001";
  const PH_CLOSE = "\u0001";
  // 媒体伪标签占位符（控制字符，与行内代码占位不同前缀）
  const MEDIA_PH_OPEN = "\u0002";

  // 媒体伪标签解析器：由 App 在启动时注入（media.js），把标签 src 解析为
  // 可访问 URL（media:// → 会话媒体端点；http(s) 原样；其它按本地路径 →
  // /file/get_local_file）。未注入时控件仍渲染但无法加载。
  let mediaResolver = null;

  function setMediaResolver(fn) { mediaResolver = typeof fn === "function" ? fn : null; }

  // 模型输出的伪标签（转义后形态）：&lt;image ...&gt;...&lt;/image&gt; 或自闭合。
  // 属性值以 &quot; 包裹（escapeHtml 把 " 转义为 &quot;）。
  const MEDIA_TAG_RE = /&lt;(image|audio|video|pdf)((?:[^&]|&(?!gt;))*?)(?:\/&gt;|&gt;([\s\S]*?)&lt;\/\1&gt;)/g;
  const MEDIA_ATTR_RE = /([a-zA-Z_][\w:-]*)\s*=\s*&quot;((?:(?!&quot;).)*)&quot;/g;

  function unescapeTagText(text) {
    return String(text)
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&amp;/g, "&");
  }

  function parseMediaAttrs(attrText) {
    const attrs = {};
    String(attrText || "").replace(MEDIA_ATTR_RE, function (_, name, value) {
      attrs[name.toLowerCase()] = unescapeTagText(value);
      return _;
    });
    return attrs;
  }

  function mediaKindLabel(kind) {
    if (kind === "image") return "🖼️ 图片";
    if (kind === "audio") return "🎵 音频";
    if (kind === "video") return "🎬 视频";
    return "📄 PDF";
  }

  // 单个伪标签 → 媒体控件 HTML。只注入白名单属性（src/alt/title），
  // 模型给出的 class/style/事件等一律丢弃；URL 经 escapeHtml 后入属性。
  function buildMediaWidget(kind, attrs, rawTag) {
    const src = attrs.src || "";
    const name = (attrs.alt || attrs.title || "").trim() ||
      (src ? src.replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "") || kind;
    let resolved = "";
    try { resolved = mediaResolver ? String(mediaResolver(kind, src) || "") : ""; } catch (_) { resolved = ""; }
    const esc = escapeHtml;
    const broken = resolved ? "" : " is-broken";
    return (
      '<div class="md-media md-media-' + kind + broken + '"' +
      ' data-media-kind="' + kind + '"' +
      ' data-media-src="' + esc(src) + '"' +
      (resolved ? ' data-media-url="' + esc(resolved) + '"' : "") +
      ' data-media-raw="' + esc(rawTag) + '">' +
      (kind === "image"
        ? '<img class="md-media-el" loading="lazy" alt="' + esc(name) + '"' +
        (resolved ? ' src="' + esc(resolved) + '"' : "") + '>'
        : kind === "video"
          ? '<video class="md-media-el" controls preload="metadata"' +
          (resolved ? ' src="' + esc(resolved) + '"' : "") + '></video>'
          : kind === "audio"
            ? '<div class="md-media-caption">' + mediaKindLabel(kind) + " · " + esc(name) + "</div>" +
            '<audio class="md-media-el" controls preload="metadata"' +
            (resolved ? ' src="' + esc(resolved) + '"' : "") + '></audio>'
            : '<div class="md-media-caption">' + mediaKindLabel(kind) + " · " + esc(name) + "</div>") +
      '<div class="md-media-actions">' +
      '<button type="button" class="md-media-btn" data-media-action="preview">预览</button>' +
      '<button type="button" class="md-media-btn" data-media-action="delete" title="不会删除实际文件">删除</button>' +
      "</div>" +
      '<div class="md-media-status">用户已删除/文件不存在</div>' +
      "</div>"
    );
  }

  // 提取全部伪标签为占位符：流式增量重渲染是纯函数，闭合后才成控件，
  // 未闭合的半截标签按普通文本显示
  function extractMediaTags(escapedText) {
    const widgets = [];
    const out = escapedText.replace(MEDIA_TAG_RE, function (match, kind, attrText, _body) {
      widgets.push(buildMediaWidget(kind, parseMediaAttrs(attrText), unescapeTagText(match)));
      return MEDIA_PH_OPEN + (widgets.length - 1) + MEDIA_PH_OPEN;
    });
    return { text: out, widgets: widgets };
  }

  function restoreMediaPlaceholders(html, widgets) {
    if (!widgets.length) return html;
    const PH_RE = new RegExp(MEDIA_PH_OPEN + "(\\d+)" + MEDIA_PH_OPEN, "g");
    // 独立成段的占位（常见形态）：控件替换整个 <p>，避免块级 div 嵌进段落
    let out = html.replace(new RegExp("<p>" + MEDIA_PH_OPEN + "(\\d+)" + MEDIA_PH_OPEN + "</p>", "g"),
      function (_, idx) {
        const widget = widgets[Number(idx)];
        return widget != null ? widget : "";
      });
    return out.replace(PH_RE, function (_, idx) {
      const widget = widgets[Number(idx)];
      return widget != null ? widget : "";
    });
  }

  function renderInline(text) {
    // 先提取行内代码段做占位保护：避免代码内容中的 *、_、[、| 等
    // 被后续粗斜体/链接/表格分列规则误处理
    const codes = [];
    let out = text.replace(/`([^`]+)`/g, function (match, code) {
      codes.push(code);
      return PH_OPEN + (codes.length - 1) + PH_CLOSE;
    });
    out = out
      .replace(/\\\|/g, "|")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return out.replace(new RegExp(PH_OPEN + "(\\d+)" + PH_CLOSE, "g"), function (_, idx) {
      // 表格内行内代码的竖线以 \| 转义书写，渲染时还原为 |
      return "<code>" + codes[Number(idx)].replace(/\\\|/g, "|") + "</code>";
    });
  }

  // 表格行分列：先保护行内代码段与转义竖线（\|），再按剩余的 | 切分
  function splitTableRow(line) {
    const placeholders = [];
    const masked = line.replace(/`[^`]*`|\\\|/g, function (match) {
      placeholders.push(match);
      return PH_OPEN + (placeholders.length - 1) + PH_CLOSE;
    });
    const cells = masked.split("|").map(function (cell) {
      return cell.trim();
    });
    // 去掉行首/行尾分隔竖线产生的空单元格（中间的空单元格是合法空列，保留）
    if (cells.length && cells[0] === "") cells.shift();
    if (cells.length && cells[cells.length - 1] === "") cells.pop();
    return cells.map(function (cell) {
      return cell.replace(new RegExp(PH_OPEN + "(\\d+)" + PH_CLOSE, "g"), function (_, idx) {
        return placeholders[Number(idx)];
      });
    });
  }

  // 整行媒体占位行（提取伪标签后的常见形态）：一行恰好一个占位符
  const MEDIA_LINE_RE = new RegExp("^" + MEDIA_PH_OPEN + "\\d+" + MEDIA_PH_OPEN + "$");

  // 段落冲刷：连续的「整行媒体占位」各自独立成段（行间不插 <br>），渲染后
  // 媒体控件成为相邻 inline-block 兄弟节点，容器 >680px 时可两列并排；
  // 其余行仍合并为一个段落（行间 <br> 保持换行语义）
  function flushParagraph(buf) {
    const out = [];
    let textBuf = [];
    buf.forEach(function (line) {
      if (MEDIA_LINE_RE.test(line.trim())) {
        if (textBuf.length) {
          out.push("<p>" + textBuf.map(renderInline).join("<br>") + "</p>");
          textBuf = [];
        }
        out.push("<p>" + line.trim() + "</p>");
      } else {
        textBuf.push(line);
      }
    });
    if (textBuf.length) {
      out.push("<p>" + textBuf.map(renderInline).join("<br>") + "</p>");
    }
    return out.join("");
  }

  function renderTable(lines) {
    const rows = lines.map(splitTableRow);
    const isDivider = rows.length > 1 && rows[1].every(function (c) { return /^:?-{2,}:?$/.test(c); });
    if (!isDivider) return null;

    let html = "<table><thead><tr>";
    rows[0].forEach(function (c) { html += "<th>" + renderInline(c) + "</th>"; });
    html += "</tr></thead><tbody>";
    rows.slice(2).forEach(function (row) {
      html += "<tr>";
      row.forEach(function (c) { html += "<td>" + renderInline(c) + "</td>"; });
      html += "</tr>";
    });
    html += "</tbody></table>";
    // 独立滚动块：宽表格在自身容器内水平滚动，不撑破消息宽度；
    // hover 显示「复制 / 复制图片」按钮（点击行为在 app.js 事件委托中处理）
    return (
      '<div class="md-table-block">' +
      '<div class="md-table-scroll">' + html + "</div>" +
      '<div class="md-table-actions">' +
      '<button class="md-table-btn" type="button" data-table-action="copy">复制</button>' +
      '<button class="md-table-btn" type="button" data-table-action="copy-image">复制图片</button>' +
      "</div>" +
      "</div>"
    );
  }

  // 代码块语言标签 → Prism 语言名（未收录的返回空串，按纯文本展示）
  const LANG_MAP = {
    python: "python", py: "python",
    javascript: "javascript", js: "javascript", jsx: "javascript", mjs: "javascript",
    typescript: "typescript", ts: "typescript", tsx: "typescript",
    cpp: "cpp", "c++": "cpp", cc: "cpp", cxx: "cpp", c: "cpp", hpp: "cpp", h: "cpp",
    java: "java",
    csharp: "csharp", cs: "csharp",
    css: "css",
    less: "less",
    scss: "scss", sass: "sass",
    qss: "css",
    qml: "qml",
    markup: "markup", html: "markup", htm: "markup", xml: "markup", svg: "markup",
    go: "go", golang: "go",
    rust: "rust", rs: "rust",
    bash: "bash", sh: "bash", shell: "bash", zsh: "bash",
    json: "json", jsonc: "json", jsonl: "json",
    cmake: "cmake", cmakelists: "cmake",
    makefile: "makefile", make: "makefile",
    docker: "docker", dockerfile: "docker",
    yaml: "yaml", yml: "yaml",
    ini: "ini", env: "ini", dotenv: "ini",
    gitignore: "ignore", ignore: "ignore",
  };
  // diff 头部文件名扩展名 → Prism 语言名（推断 diff 内层语法）
  const DIFF_EXTS = {
    py: "python",
    js: "javascript", mjs: "javascript", jsx: "javascript",
    ts: "typescript", tsx: "typescript",
    cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", h: "cpp", c: "cpp",
    java: "java",
    cs: "csharp",
    css: "css",
    less: "less",
    scss: "scss", sass: "sass",
    qss: "css",
    qml: "qml",
    html: "markup", htm: "markup",
    go: "go",
    rs: "rust",
    sh: "bash", bash: "bash",
    json: "json", jsonc: "json", jsonl: "json",
    yaml: "yaml", yml: "yaml",
    ini: "ini", env: "ini",
    cmake: "cmake",
    makefile: "makefile",
    dockerfile: "docker",
  };

  function inferDiffLang(lines) {
    for (let k = 0; k < Math.min(lines.length, 30); k++) {
      const m = lines[k].match(/diff --git a\/\S+ b\/(\S+)$|^\+\+\+ b\/(\S+)$|^--- a\/(\S+)$/);
      if (!m) continue;
      const file = m[1] || m[2] || m[3];
      const ext = (file.split(".").pop() || "").toLowerCase();
      if (DIFF_EXTS[ext]) return DIFF_EXTS[ext];
      // 无扩展名的特殊文件名（Dockerfile / Makefile / CMakeLists.txt / .env）
      const base = file.toLowerCase();
      if (base.startsWith("dockerfile")) return "docker";
      if (base.startsWith("makefile")) return "makefile";
      if (base.startsWith("gnumakefile")) return "makefile";
      if (base.startsWith("cmakelists")) return "cmake";
      if (base.startsWith(".env")) return "ini";
    }
    return "";
  }

  function render(src) {
    const escaped = escapeHtml(String(src || ""));
    const media = extractMediaTags(escaped);
    const text = media.text;
    const lines = text.split("\n");
    const html = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // 代码块（带语言标题栏 + 复制按钮）
      if (/^```/.test(line)) {
        const lang = line.slice(3).trim() || "code";
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        i++; // 跳过结尾 ```
        // 决定 Prism 语言类：diff/patch 推断内层语言（diff-<lang>），其余按标签映射
        let prismLang = "";
        if (lang === "diff" || lang === "patch") {
          const inner = inferDiffLang(buf);
          prismLang = inner ? "diff-" + inner : "diff";
        } else {
          prismLang = LANG_MAP[lang.toLowerCase()] || "";
        }
        const cls = prismLang ? ' class="language-' + prismLang + '"' : "";
        html.push(
          '<div class="codeblock"><div class="codeblock-head">' +
          '<span class="codeblock-lang">' + lang + "</span></div>" +
          '<button class="copy-btn" type="button" aria-label="复制代码">' +
          '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>' +
          '<span class="copy-label">复制</span>' +
          "</button>" +
          "<pre><code" + cls + ">" + buf.join("\n") + "</code></pre></div>"
        );
        continue;
      }

      // 标题
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        const level = heading[1].length;
        html.push("<h" + level + ">" + renderInline(heading[2]) + "</h" + level + ">");
        i++;
        continue;
      }

      // 分割线
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        html.push("<hr>");
        i++;
        continue;
      }

      // 引用
      if (/^&gt;\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^&gt;\s?/, ""));
          i++;
        }
        html.push("<blockquote>" + buf.map(renderInline).join("<br>") + "</blockquote>");
        continue;
      }

      // 表格
      if (line.includes("|") && i + 1 < lines.length && /^\|?[\s:|-]+\|?$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
        const buf = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
          buf.push(lines[i]);
          i++;
        }
        const table = renderTable(buf);
        if (table) {
          html.push(table);
          continue;
        }
        html.push("<p>" + buf.map(renderInline).join("<br>") + "</p>");
        continue;
      }

      // 无序列表
      if (/^\s*[-*+]\s+/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          buf.push("<li>" + renderInline(lines[i].replace(/^\s*[-*+]\s+/, "")) + "</li>");
          i++;
        }
        html.push("<ul>" + buf.join("") + "</ul>");
        continue;
      }

      // 有序列表
      if (/^\s*\d+\.\s+/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          buf.push("<li>" + renderInline(lines[i].replace(/^\s*\d+\.\s+/, "")) + "</li>");
          i++;
        }
        html.push("<ol>" + buf.join("") + "</ol>");
        continue;
      }

      // 空行
      if (line.trim() === "") {
        i++;
        continue;
      }

      // 普通段落（合并连续行；整行媒体占位各自独立成段，便于两列并排）
      const buf = [line];
      i++;
      while (
        i < lines.length &&
        lines[i].trim() !== "" &&
        !/^(#{1,4}\s|```|&gt;|\s*[-*+]\s|\s*\d+\.\s)/.test(lines[i])
      ) {
        buf.push(lines[i]);
        i++;
      }
      html.push(flushParagraph(buf));
    }

    return '<div class="md">' +
      restoreMediaPlaceholders(html.join(""), media.widgets) + "</div>";
  }

  const api = { render, setMediaResolver };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.Markdown = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
