/**
 * 独立文件 diff 编辑器页（editor.html），V2.3 全文可编辑：
 * - URL 参数：?session_id=xxx&key=xxxxxxxx
 * - 全文视图：基线全文 + 差异标记（full_view rows）；ctx 行 / add 行均可编辑
 *   （编辑非差异行不改变 diff 语义，保存整体入链 user_edit 版本）；
 *   del 行红块只读无行号
 * - 行内语法高亮：Prism 高亮层（pre.eh-hl）+ 透明 textarea 叠加（overlay）
 * - 差异块导航：顶栏固定「↑上一块 / ↓下一块」（jumpHunk 按滚动位置锚定相邻块头）
 * - 操作：保存（乐观锁）/ 回退基线 / 回退某轮 / 从磁盘刷新 / 保留封版；
 *   主题下拉与主应用共用 ThemeManager 偏好（跟随系统/浅色/深色）
 * 依赖：api.js / theme.js / Prism；由 filehistory.js 面板点击新窗口打开。
 */
(function (global) {
  "use strict";

  const params = new URLSearchParams(location.search);
  const sessionId = params.get("session_id") || "default";
  const fileKey = params.get("key") || "";
  const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

  // diff 文件后缀 → Prism 语言名（与 messages.js DIFF_FILE_EXTS 同口径）
  const LANG_EXTS = {
    py: "python", js: "javascript", mjs: "javascript", jsx: "javascript",
    ts: "typescript", tsx: "typescript",
    cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", h: "cpp", c: "cpp",
    java: "java", cs: "csharp",
    css: "css", less: "less", scss: "scss", sass: "sass", qss: "css", qml: "qml",
    html: "markup", htm: "markup", xml: "markup", svg: "markup",
    go: "go", rs: "rust", sh: "bash", bash: "bash",
    json: "json", jsonc: "json", jsonl: "json",
    yaml: "yaml", yml: "yaml", ini: "ini", env: "ini",
    cmake: "cmake", makefile: "makefile", dockerfile: "docker",
  };

  const dom = {
    titleWrap: document.getElementById("ehTitleWrap"),
    bar: null,
    body: document.getElementById("ehBody"),
    loading: document.getElementById("ehLoading"),
    save: document.getElementById("ehSave"),
    revert: document.getElementById("ehRevert"),
    roundSelect: document.getElementById("ehRoundSelect"),
    sync: document.getElementById("ehSync"),
    keep: document.getElementById("ehKeep"),
    themeSelect: document.getElementById("ehThemeSelect"),
    prevHunk: document.getElementById("ehPrevHunk"),
    nextHunk: document.getElementById("ehNextHunk"),
    close: document.getElementById("ehClose"),
    confirm: document.getElementById("ehConfirm"),
    confirmMessage: document.getElementById("ehConfirmMessage"),
    confirmOk: document.getElementById("ehConfirmOk"),
    confirmCancel: document.getElementById("ehConfirmCancel"),
    confirmBackdrop: document.getElementById("ehConfirmBackdrop"),
  };

  let st = null;         // 编辑器状态 {key,path,displayPath,hash,endsWithNl,rows,currentLines,langs,truncated,rounds,kept}
  let confirmAction = null;
  let toastNode = null;
  let toastTimer = null;
  let undoStack = [];    // 结构编辑快照栈（Ctrl+Z 逐级恢复）

  // ---------- 基础工具 ----------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function toast(message) {
    if (!toastNode) {
      toastNode = el("div", "eh-toast");
      document.body.appendChild(toastNode);
    }
    toastNode.textContent = message;
    toastNode.classList.add("is-show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastNode.classList.remove("is-show");
    }, 2400);
  }

  function askConfirm(message, action) {
    if (!dom.confirm) { action(); return; }
    confirmAction = action;
    dom.confirmMessage.textContent = message;
    dom.confirm.classList.remove("hidden");
  }

  function closeConfirm() {
    if (dom.confirm) dom.confirm.classList.add("hidden");
    confirmAction = null;
  }

  /** 推断文件语言的 Prism 语言名（无匹配返回空串）。 */
  function langOf(path) {
    const base = String(path || "").replace(/\\/g, "/").split("/").pop().toLowerCase();
    const ext = base.indexOf(".") >= 0 ? base.split(".").pop() : "";
    if (ext && LANG_EXTS[ext]) return LANG_EXTS[ext];
    if (base.startsWith("dockerfile")) return "docker";
    if (base.startsWith("makefile") || base.startsWith("gnumakefile")) return "makefile";
    if (base.startsWith("cmakelists")) return "cmake";
    if (base.startsWith(".env")) return "ini";
    return "";
  }

  function prismReady(lang) {
    return !!(global.Prism && Prism.languages && Prism.languages[lang]);
  }

  /** 行文本 → Prism 高亮 HTML（未就绪/异常返回 null，调用方回退纯文本）。 */
  function highlight(text, lang) {
    if (!prismReady(lang)) return null;
    try {
      const tokens = Prism.tokenize(text, Prism.languages[lang]);
      return Prism.Token.stringify(Prism.util.encode(tokens), lang); // 已 HTML 转义
    } catch (_) {
      return null;
    }
  }

  function fileNameOf(displayPath) {
    const parts = String(displayPath || "").split(/[\\/]/);
    return parts[parts.length - 1] || displayPath;
  }

  function buildRoundChoices(versions) {
    const rounds = [];
    for (const v of versions) {
      const r = v.round || 0;
      if (r > 0 && rounds.indexOf(r) === -1) rounds.push(r);
    }
    return rounds.sort(function (a, b) { return b - a; });
  }

  // ---------- 数据加载 ----------
  function load() {
    dom.loading.textContent = "加载中...";
    Promise.all([
      API.fileFullView(sessionId, fileKey),
      API.fileContent(sessionId, fileKey),
      API.fileVersions(sessionId, fileKey),
    ]).then(function (results) {
      const view = results[0];
      const content = results[1];
      const versions = results[2].versions || [];
      const useFull = !view.truncated && !view.diff_skipped;
      const currentText = (content.content || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      st = {
        key: fileKey,
        path: content.path,
        displayPath: content.display_path,
        hash: content.hash,
        kept: content.kept,
        baselineV: view.baseline_v,
        currentV: view.current_v,
        endsWithNl: !!view.current_ends_with_nl,
        full: useFull,
        // 统一渲染行序列：full 用 rows；compact 用 diff 解析（仅此模式保留 hunk 头按钮）
        rows: useFull ? normalizeRows(view.rows) : parseCompact(view.diff),
        currentLines: currentText.split("\n"),
        skipped: view.diff_skipped || "",
        truncated: !!view.truncated,
        lang: langOf(content.display_path || content.path),
        versions: versions,
        rounds: buildRoundChoices(versions),
        // 操作前的全文（结构编辑的脏状态恢复基点：Ctrl+Z / 撤销结构操作）
        dirtySnapshot: null,
        scrollTop: dom.body.scrollTop,
      };
      render();
      dom.body.scrollTop = st.scrollTop;   // 重载后尽量保持阅读位置
    }).catch(function (err) {
      dom.loading.textContent = "加载失败：" + err.message + "（请确认后端服务与 key 参数）";
    });
  }

  /** full_view rows → 渲染行序列。 */
  function normalizeRows(rows) {
    return (rows || []).map(function (r) {
      if (r.t === "ctx") return { kind: "ctx", oldNo: r.o, newNo: r.n, text: r.s };
      if (r.t === "del") return { kind: "del", oldNo: r.o, text: r.s, hunk: r.h };
      return { kind: "add", newNo: r.n, text: r.s, hunk: r.h };
    });
  }

  /** 紧凑 unified diff → 行序列（回退模式）。 */
  function parseCompact(diffText) {
    const rows = [];
    if (!diffText) return rows;
    let oldNo = 0;
    let newNo = 0;
    let hunkIndex = -1;
    for (const line of diffText.split("\n")) {
      const m = HUNK_RE.exec(line);
      if (m) {
        hunkIndex = rows.filter(r => r.kind === "hunk").length;
        rows.push({ kind: "hunk", index: hunkIndex });
        oldNo = parseInt(m[1], 10);
        newNo = parseInt(m[3], 10);
        if (oldNo === 0) oldNo = 1;
        continue;
      }
      if (line.startsWith("--- a/") || line.startsWith("+++ b/")) continue;
      if (line.startsWith("+")) {
        rows.push({ kind: "add", newNo: newNo, text: line.slice(1), hunk: hunkIndex });
        newNo += 1;
      } else if (line.startsWith("-")) {
        rows.push({ kind: "del", oldNo: oldNo, text: line.slice(1), hunk: hunkIndex });
        oldNo += 1;
      } else {
        rows.push({ kind: "ctx", oldNo: oldNo, newNo: newNo, text: line, hunk: hunkIndex });
        oldNo += 1;
        newNo += 1;
      }
    }
    return rows;
  }

  /**
   * 重组全文。
   * - full 视图：按行序直接拼接所有 ctx/add 行的"当前值"（rows 顺序即文件行序，
   *   del 行不属于当前文件）——回车拆行 / 退格并行的结构编辑无需维护索引映射，
   *   行号只是展示，保存永远正确；
   * - compact 回退：以当前版本全文为底，仅替换被编辑过的 add 行。
   */
  function composeContent() {
    if (st.full) {
      const parts = [];
      for (const row of st.rows) {
        if (row.kind === "ctx" || row.kind === "add") {
          parts.push(row.node ? row.node.value : row.text);
        }
      }
      return parts.join("\n") + (st.endsWithNl ? "\n" : "");
    }
    const result = st.currentLines.slice();
    for (const row of st.rows) {
      if (row.kind === "add" && row.node) {
        const idx = row.newNo - 1;
        const value = row.node.value;
        if (value !== row.text && idx >= 0 && idx < result.length) result[idx] = value;
      }
    }
    return result.join("\n") + (st.endsWithNl ? "\n" : "");
  }

  // ---------- 渲染 ----------
  function render() {
    dom.body.innerHTML = "";
    sel = null;                 // 重渲染丢弃旧选区（DOM 已重建）
    paintedRows = [];
    renderTitle();
    renderBar();
    if (st.skipped === "file_too_large") {
      dom.body.appendChild(el("div", "eh-skip fh-skip", "文件过大，未生成行级 diff"));
      return;
    }
    if (st.truncated) {
      dom.body.appendChild(el("div", "fh-skip", "文件较大，已切换为紧凑 diff 视图（仅显示差异块附近内容）"));
    }
    if (!st.rows.length) {
      // 无差异：渲染全文只读视图（仍可编辑——全文皆可编辑且保存）
      st.rows = st.currentLines.map(function (text, i) {
        return { kind: "ctx", oldNo: i + 1, newNo: i + 1, text: text };
      });
    }
    const table = el("div", "eh-table");
    let pendingHunk = -1;
    for (const row of st.rows) {
      if (row.kind === "hunk") {
        pendingHunk = -1;
        table.appendChild(hunkHead(row.index, false));
        continue;
      }
      if (row.kind !== "ctx" && typeof row.hunk === "number" && row.hunk !== pendingHunk) {
        pendingHunk = row.hunk;
        table.appendChild(hunkHead(row.hunk, true));
      } else if (row.kind === "ctx" && pendingHunk >= 0) {
        pendingHunk = -1;
      }
      table.appendChild(renderRow(row));
    }
    dom.body.appendChild(table);
  }

  function renderTitle() {
    dom.titleWrap.innerHTML = "";
    const strong = document.createElement("strong");
    strong.textContent = st.displayPath;
    strong.title = st.path + "（点击复制）";
    strong.addEventListener("click", function () {
      copyText(st.path, "完整路径已复制");
    });
    const sub = el("span", "eh-sub",
      `基线 v${st.baselineV == null ? "-" : st.baselineV} → 当前 v${st.currentV}` +
      (st.kept ? " · 已保留（新代跟踪中）" : "") + (st.lang ? " · " + st.lang : ""));
    dom.titleWrap.appendChild(strong);
    dom.titleWrap.appendChild(sub);
  }

  function renderBar() {
    dom.save.disabled = false;
    dom.save.textContent = "保存";
    // 回退轮次下拉
    if (st.rounds.length) {
      dom.roundSelect.hidden = false;
      dom.roundSelect.innerHTML = "";
      const def = el("option", "", "回退到某轮发起时...");
      def.value = "";
      dom.roundSelect.appendChild(def);
      for (const r of st.rounds) {
        const opt = el("option", "", "第 " + r + " 轮发起时");
        opt.value = String(r);
        dom.roundSelect.appendChild(opt);
      }
    } else {
      dom.roundSelect.hidden = true;
    }
    dom.keep.textContent = st.kept ? "再次保留" : "保留";
  }

  /** hunk 头部：全文模式下只作视觉分隔（区间按钮接口语义仍在，但全文坐标由 full_view 提供） */
  function hunkHead(index, withButtons) {
    const node = el("div", "fh-hunk-head");
    const labelWrap = el("span", "fh-hunk-label-wrap");
    labelWrap.appendChild(el("span", "fh-hunk-label",
      index >= 0 ? "差异块 #" + (index + 1) : "差异块"));
    if (!withButtons || !(index >= 0)) {
      node.appendChild(labelWrap);
      return node;
    }
    const mk = function (cls, text, title, fn) {
      const b = el("button", cls, text);
      b.title = title;
      b.addEventListener("click", fn);
      return b;
    };
    labelWrap.appendChild(mk("fh-hunk-keep", "保留此处及之后",
      "接受此差异块及其后所有差异块，之前的变更还原为基线并写回磁盘",
      function () {
        askConfirm("保留此处及之后的所有差异块？之前的修改将被还原为基线（版本历史仍可回退）。",
          function () { keepHunk(index, true); });
      }));
    labelWrap.appendChild(mk("fh-hunk-undo", "撤回此处及之后",
      "把该差异块及其后所有差异块还原为基线内容（不可恢复）",
      function () {
        askConfirm("撤回此处及之后的所有差异块？之后的所有修改将被还原为基线（版本历史仍可查看）。确定撤回？",
          function () { undoHunk(index, true); });
      }));
    labelWrap.appendChild(mk("fh-hunk-keep", "保留此处",
      "仅接受此差异块：其余变更还原为基线，结果固化为新基线并写回磁盘",
      function () {
        askConfirm("保留此处将只接受该差异块、其余差异块全部还原为基线，并把结果写回磁盘。确定？",
          function () { keepHunk(index, false); });
      }));
    labelWrap.appendChild(mk("fh-hunk-undo", "撤回此块",
      "把该差异块还原为基线内容（不可恢复）",
      function () {
        askConfirm("撤回该差异块会把此处的修改还原为基线内容，撤回后无法恢复。确定撤回？",
          function () { undoHunk(index, false); });
      }));
    node.appendChild(labelWrap);
    return node;
  }

  /**
   * 顶栏「上一块/下一块」：按当前滚动位置（视口顶部 24px 锚定带内命中的
   * 块头为"当前块"，未命中取视口上方最近块头）确定基准，再滚到相邻块。
   * 不以块头元素为锚——sticky 钉住的块头 rect 不反映真实文档位置，
   * 也不能用 scrollIntoView（会把横向滚动拉回最左破坏长行阅读位置）。
   * @param {-1|1} dir -1 上一块 / 1 下一块
   */
  function jumpHunk(dir) {
    const heads = hunkHeads();
    if (!heads.length) { toast("没有可跳转的差异块"); return; }
    const anchor = currentHunkIndex(heads);
    let target;
    if (dir < 0) {
      target = anchor < 0 ? heads[heads.length - 1]   // 未命中：直接跳最前一块
        : heads[Math.max(0, anchor - 1)];
    } else {
      target = anchor < 0 ? heads[0]                  // 未命中：直接跳最后一块
        : heads[Math.min(heads.length - 1, anchor + 1)];
    }
    if (target === heads[anchor]) { toast(anchor === 0 ? "已经是第一个差异块" : "已经是最后一个差异块"); }
    scrollToHunkHead(target);
  }

  /** 按渲染顺序收集所有差异块头（含每次重渲染后的新节点） */
  function hunkHeads() {
    return Array.prototype.slice.call(dom.body.querySelectorAll(".fh-hunk-head"));
  }

  /**
   * 当前块索引：以滚动容器顶为基准的 32px 锚定带内命中的块头即"当前块"
   * （scrollToHunkHead 定位到 +8px，正好落带内，连续点击可逐块推进）；
   * 带内没有（页顶/页中空档）取容器上方最近块头，上方没有（还没滚到
   * 第一块）返回 -1（由调用方按方向决定落到头/尾块）。
   */
  function currentHunkIndex(heads) {
    const bodyTop = dom.body.getBoundingClientRect().top;
    const band = 32;
    let lastAbove = -1;
    for (let i = 0; i < heads.length; i++) {
      const top = heads[i].getBoundingClientRect().top - bodyTop;
      if (top >= 0 && top <= band) return i;
      if (top < 0) lastAbove = i;
    }
    return lastAbove;
  }

  /**
   * 纵向滚动到目标块头（容器顶下 8px），短促高亮辅助视线定位。
   * 只调 scrollTop，不碰 scrollLeft（横向阅读位置保持不变）。
   */
  function scrollToHunkHead(head) {
    const headRect = head.getBoundingClientRect();
    const bodyRect = dom.body.getBoundingClientRect();
    dom.body.scrollTop += headRect.top - bodyRect.top - 8;
    head.classList.add("is-flash");
    setTimeout(function () { head.classList.remove("is-flash"); }, 900);
  }

  function renderRow(row) {
    const line = el("div", "eh-line eh-" + row.kind);
    const no = el("span", "eh-no");
    if (row.kind === "ctx") {
      no.textContent = (row.oldNo == null ? "" : row.oldNo) + " | " + row.newNo;
    } else if (row.kind === "add") {
      no.textContent = String(row.newNo);
    }
    line.appendChild(no);
    row.noNode = no;
    row.lineEl = line;

    if (row.kind === "del") {
      line.appendChild(el("span", "eh-text", row.text === "" ? " " : row.text));
      return line;
    }
    // ctx / add：可编辑 overlay（高亮层 + 透明 textarea）
    const cell = el("div", "eh-edit");
    const hl = document.createElement("pre");
    hl.className = "eh-hl" + (st.lang ? " language-" + st.lang : "");
    const input = document.createElement("textarea");
    input.className = "eh-input";
    input.spellcheck = false;
    input.value = row.text;
    const paint = function () {
      const html = highlight(input.value, st.lang);
      if (html != null) hl.innerHTML = html;
      else hl.textContent = input.value;
      syncHeight(input, hl, cell);
    };
    input.addEventListener("input", function () {
      paint();
      markDirty();
    });
    if (st.full) {
      // 结构化行编辑：回车拆行 / 退格并行 / Delete 并下 / 方向键跨行 / 多行粘贴
      input.addEventListener("keydown", function (e) { rowKeydown(e, row); });
      input.addEventListener("paste", function (e) { rowPaste(e, row); });
      // 多行选择：普通点击定锚点（清旧选区）→ 拖动跨行转入结构化选区；
      // Shift+点击扩展；单行内拖动/点击全部为浏览器原生行为
      input.addEventListener("mousedown", function (e) { rowMousedown(e, row); });
      input.addEventListener("click", function (e) {
        if (suppressClickOnce) {          // 跨行拖动结束后的 click 不重置选区
          suppressClickOnce = false;
          return;
        }
        if (e.shiftKey && sel) {
          // Shift+点击扩展：selectionStart 已被浏览器改为区间端点而非光标位，
          // 用几何近似（等宽列宽）取真实拖放点
          sel.fRow = row;
          sel.fOff = caretApprox(row, e);
          paintSel();
        } else {
          clearSel();
        }
      });
    }
    row.node = input;
    row.hlEl = hl;              // 高亮层引用（多行选区包裹片段用）
    row.paint = paint;
    paint();
    cell.appendChild(hl);
    cell.appendChild(input);
    line.appendChild(cell);
    // 此刻行尚未插入文档（脱离布局），首测行高不可信——入队待插入后复测
    queueHeightSync(row);
    return line;
  }

    /** 让高亮层跟随输入行数撑高（textarea 自动行高）。 */
  function syncHeight(input, hl, cell) {
    input.style.height = "auto";
    const h = Math.max(input.scrollHeight, 21);
    input.style.height = h + "px";
    hl.style.minHeight = h + "px";
    // 高亮层与输入层都是 absolute（inset:0），不参与行布局：
    // 行高必须显式写到 .eh-edit 单元格，换行成多行的行才能把整行撑高，
    // 否则内容溢出行盒盖住下一行（行号与下一行正文重叠）
    if (cell) cell.style.height = h + "px";
  }

  // 首次渲染（renderRow）发生在行节点插入 DOM 之前，textarea.scrollHeight
  // 无布局可用（恒 0），行高全部塌成 21px——长行换行后溢出与下一行重叠。
  // 把这类"脱离文档的测量"收集起来，等插入后再批量复测一次行高。
  const pendingHeightRows = [];
  let heightSyncScheduled = false;

  function queueHeightSync(row) {
    if (pendingHeightRows.indexOf(row) === -1) pendingHeightRows.push(row);
    if (heightSyncScheduled) return;
    heightSyncScheduled = true;
    requestAnimationFrame(function () {
      heightSyncScheduled = false;
      const rows = pendingHeightRows.splice(0);
      for (const pending of rows) {
        if (pending.lineEl && pending.lineEl.isConnected && pending.paint) {
          pending.paint();
        }
      }
    });
  }

  // 视口宽度变化会改变每行的换行位置（已量好的行高全部失真），
  // 防抖后对全部可编辑行整表复测
  let resizeTimer = null;
  window.addEventListener("resize", function () {
    if (!st || !st.rows) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      for (const row of st.rows) {
        if (row.paint) queueHeightSync(row);
      }
    }, 150);
  });

  // ---------- 结构化行编辑（full 视图） ----------
  // 每行是独立 textarea，行与行之间的"换行"不是任何 textarea 内的字符，
  // 必须显式处理跨界按键：拆行=插入新行+后续行号顺延；并行=合并+删行。
  function isEditableRow(row) {
    return row && (row.kind === "ctx" || row.kind === "add");
  }

  function editableNeighbors(row) {
    const idx = st.rows.indexOf(row);
    let prev = null;
    let next = null;
    for (let i = idx - 1; i >= 0; i--) {
      if (isEditableRow(st.rows[i])) { prev = st.rows[i]; break; }
    }
    for (let i = idx + 1; i < st.rows.length; i++) {
      if (isEditableRow(st.rows[i])) { next = st.rows[i]; break; }
    }
    return { idx: idx, prev: prev, next: next };
  }

  // ---------- 多行选择（full 视图） ----------
  // 两种方式：① 点击定锚点 → Shift+点击扩展；② 按下左键直接拖动跨行。
  // 选区支持：Ctrl+C 复制 / Ctrl+X 剪切 / Ctrl+Y·Ctrl+Shift+Z 重做 /
  // Backspace·Delete 删除 / 直接输入替换 / Ctrl+A 全选 / Esc 取消。
  let sel = null;              // {aRow,aOff,fRow,fOff}
  let dragSel = null;          // 鼠标拖动选区进行中 {aRow,aOff,moved}
  let suppressClickOnce = false;
  let redoStack = [];          // 结构编辑重做栈（Ctrl+Y / Ctrl+Shift+Z）
  let paintedRows = [];        // 本轮程序化铺开原生选区的行（clearSel 时收拢）

  function clearSel() {
    if (!sel) return;
    clearSelPaint();
    sel = null;
  }

  function selBounds() {
    let startRow = sel.aRow, startOff = sel.aOff, endRow = sel.fRow, endOff = sel.fOff;
    let si = st.rows.indexOf(startRow);
    let ei = st.rows.indexOf(endRow);
    if (si > ei || (si === ei && startOff > endOff)) {
      [startRow, endRow] = [endRow, startRow];
      [startOff, endOff] = [endOff, startOff];
      [si, ei] = [ei, si];
    }
    return { startRow, startOff, endRow, endOff, si, ei };
  }

  function paintSel() {
    clearSelPaint();
    const b = selBounds();
    // 原生观感驱动：范围首尾行只选鼠标扫过的字符区间、中间行整行。
    // 渲染分两类——锚点/聚焦行用浏览器原生 ::selection（已配主题深蓝）；
    // 其余行因 Chrome 不渲染非聚焦输入框的选区，在高亮层内用 .eh-sel span
    // 精确包裹选中片段（同色深蓝+白字，视觉与原生一致）；del 红块行无输入框，
    // 整行类高亮兜底。
    for (let i = b.si; i <= b.ei; i++) {
      const row = st.rows[i];
      if (isEditableRow(row) && row.node) {
        let s = 0;
        let t = row.node.value.length;
        if (i === b.si) s = b.startOff;
        if (i === b.ei) t = b.endOff;
        if (t < s) t = s;
        // 向下延伸到整行边界、且抓点贴近起始行行首 → 吸附行首，
        // 避免起始行锚点左侧文本被一并选入（观感如"多选了一截"）
        if (dragSel && dragSel.aSnap && b.ei !== b.si &&
            i === b.si && t === row.node.value.length) {
          s = 0;
        }
        row.node.setSelectionRange(s, t);   // 聚焦行走原生渲染；顺带支持复制
        if (t > s && document.activeElement !== row.node) {
          paintRowSelection(row, s, t);     // 非聚焦行在高亮层铺选区
          row.selWrapped = true;
        }
      } else if (row.lineEl) {
        row.lineEl.classList.add("is-selected");
      }
      paintedRows.push(row);
    }
  }

  /** 在高亮层内精确包裹选中片段（textNode 切三段，选中段套 .eh-sel）。 */
  function paintRowSelection(row, s, t) {
    const hl = row.hlEl;
    if (!hl) return;
    const val = row.node.value;
    hl.textContent = "";
    if (s > 0) hl.appendChild(document.createTextNode(val.slice(0, s)));
    if (t > s) hl.appendChild(el("span", "eh-sel", val.slice(s, t)));
    if (t < val.length) hl.appendChild(document.createTextNode(val.slice(t)));
  }

  /** 收拢上一轮铺开的选区/包裹片段/行级标记（避免幽灵高亮残留）。 */
  function clearSelPaint() {
    for (const row of paintedRows) {
      if (row.selWrapped) {
        row.selWrapped = false;
        if (row.paint) row.paint();          // 恢复整行 Prism 高亮
      }
      if (row.node) {
        // 收拢到当前选区起点（不跳动、不留残色）
        const ss = Math.min(row.node.selectionStart, row.node.value.length);
        row.node.setSelectionRange(ss, ss);
      }
      if (row.lineEl) row.lineEl.classList.remove("is-selected");
    }
    paintedRows = [];
  }

  /** 选区覆盖的精确文本（首尾按偏移截取，中间整行）。 */
  function selText() {
    const b = selBounds();
    if (b.si === b.ei) {
      return b.startRow.node.value.slice(b.startOff, b.endOff);
    }
    const parts = [b.startRow.node.value.slice(b.startOff)];
    for (let i = b.si + 1; i < b.ei; i++) {
      const r = st.rows[i];
      if (r.kind === "ctx" || r.kind === "add") parts.push(r.node ? r.node.value : r.text);
      else if (r.kind === "del") parts.push(r.text);
    }
    parts.push(b.endRow.node.value.slice(0, b.endOff));
    return parts.join("\n");
  }

  function copySelection(cut) {
    const text = selText();
    try {
      navigator.clipboard.writeText(text).then(function () {}, function () {});
    } catch (_) { /* 忽略 */ }
    if (cut) deleteSelection();
  }

  /** 删除选区：同行内截断；跨行 = 首行保留前段 + 尾行并入后段，
   * 中间行（含红块行）全部移除（红块被删即"接受该删除"，保存内容随之缺失）。 */
  function deleteSelection() {
    if (!sel) return;
    const b = selBounds();
    if (b.si === b.ei) {
      if (b.startOff === b.endOff) { clearSel(); return; }
      pushUndoSnapshot(b.startRow);
      const v = b.startRow.node.value;
      b.startRow.node.value = v.slice(0, b.startOff) + v.slice(b.endOff);
      b.startRow.paint();
      b.startRow.node.focus();
      b.startRow.node.setSelectionRange(b.startOff, b.startOff);
      clearSel();
      markDirty();
      return;
    }
    pushUndoSnapshot(b.startRow);
    const tail = b.endRow.node.value.slice(b.endOff);
    // 移除 (si, ei] 区间所有行（含尾行；del 行一并移除 = 接受删除）
    for (let i = b.ei; i > b.si; i--) {
      const r = st.rows[i];
      st.rows.splice(i, 1);
      if (r.lineEl && r.lineEl.parentNode) r.lineEl.parentNode.removeChild(r.lineEl);
    }
    b.startRow.node.value = b.startRow.node.value.slice(0, b.startOff) + tail;
    b.startRow.paint();
    renumberRows();
    clearSel();
    markDirty();
    b.startRow.node.focus();
    b.startRow.node.setSelectionRange(b.startOff, b.startOff);
  }

  /** 全选：首个可编辑行行首 → 末个可编辑行行尾。 */
  function selectAllEditable() {
    let first = null;
    let last = null;
    for (const row of st.rows) {
      if (isEditableRow(row)) {
        if (!first) first = row;
        last = row;
      }
    }
    if (!first) return;
    clearSel();
    sel = {
      aRow: first, aOff: 0,
      fRow: last, fOff: last.node.value.length,
    };
    paintSel();
  }

  // ---------- 鼠标拖动跨行选择 ----------
  // 按下记锚点行与坐标（偏移按需由坐标定格，不依赖可被原生拖动改写的
  // selectionStart）→ 跨行转入结构化选区（首尾精确到字符）→ 拖回锚点行
  // 收敛为单行原生选区。
  function rowFromLineEl(lineEl) {
    if (!lineEl) return null;
    for (const row of st.rows) {
      if (row.lineEl === lineEl) return row;
    }
    return null;
  }

  function caretApprox(row, e) {
    // 精确光标定位：在镜像容器（与 textarea 同盒模型：同宽/同字体/
    // pre-wrap + break-all）中于第 k 字符后放置零宽标记，二分查找标记
    // 落入鼠标坐标处的偏移——浏览器实测矩形，精确处理 CJK 宽字符与换行
    // （旧的"等宽列宽折算"遇中文会系统性偏移数个字符，已废弃）
    const input = row.node;
    const val = input.value;
    if (!val.length) return 0;
    let mirror = row.mirrorEl;
    if (!mirror || !mirror.parentNode) {
      mirror = document.createElement("pre");
      const cs = getComputedStyle(input);
      mirror.style.cssText =
        "position:absolute;visibility:hidden;left:0;top:0;margin:0;border:0;" +
        "pointer-events:none;z-index:-1;white-space:" + cs.whiteSpace +
        ";word-break:" + cs.wordBreak +
        // Chrome 下 computed style 的 font 简写恒为空串，必须逐项复制，
        // 否则镜像回落默认字体导致二分偏移系统性偏差
        ";font-family:" + cs.fontFamily +
        ";font-size:" + cs.fontSize +
        ";font-weight:" + cs.fontWeight +
        ";font-style:" + cs.fontStyle +
        ";line-height:" + cs.lineHeight +
        ";letter-spacing:" + cs.letterSpacing +
        ";tab-size:" + cs.tabSize;
      // 挂到 .eh-edit（position:relative，与 textarea 同一包含块且同原点）——
      // 绝不能挂到 lineEl.firstChild（行号列）：那是 static 定位，镜像会飘到
      // 错误参照系，矩形与鼠标坐标对不上，二分退化为 0/全长 → 整行选中
      row.node.parentNode.appendChild(mirror);
      row.mirrorEl = mirror;
    }
    mirror.style.width = input.clientWidth + "px";
    const mark = document.createElement("span");
    mark.textContent = "\u200b";
    let lo = 0;
    let hi = val.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      mirror.textContent = val.slice(0, mid);
      mirror.appendChild(mark);
      const r = mark.getBoundingClientRect();
      const sameBand = e.clientY >= r.top - 2 && e.clientY <= r.bottom + 2;
      // 标记在鼠标位置之前（上一行 / 同行左侧）→ 偏移应更大
      const after = sameBand ? (e.clientX >= r.left) : (e.clientY > r.bottom);
      if (after) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  function rowMousedown(e, row) {
    if (e.button !== 0 || !st.full) return;
    // 全面接管拖动手势：阻止原生文本选择（其被锚定在单个 textarea 内，
    // 跨行必然失败，且会改写锚点 selectionStart、与结构化选区互相污染）。
    // 手动聚焦 + 按下坐标定格锚点偏移，拖动全程由 bindDragSelect 驱动
    if (!e.shiftKey) clearSel();
    e.preventDefault();
    row.node.focus();
    const off = caretApprox(row, e);
    row.node.setSelectionRange(off, off);
    dragSel = {
      aRow: row,
      aOff: off,
      moved: false,
      // 抓点贴近起始行行首 → 多行延伸时起始行吸附行首（防左侧残留）
      aSnap: e.clientX - row.node.getBoundingClientRect().left < 10,
    };
  }

  function bindDragSelect() {
    // 点击任何非可编辑输入区（行号列 / 红块行 / hunk 头 / 按钮 / 空白）→
    // 立即取消多行选区（可编辑行内由 rowMousedown 自行处理）
    document.addEventListener("mousedown", function (e) {
      if (!st || !st.full || !sel || e.button !== 0 || e.shiftKey) return;
      const t = e.target;
      const line = t && t.closest ? t.closest(".eh-line") : null;
      const row = rowFromLineEl(line);
      if (row && isEditableRow(row) && row.node.contains(t)) return;
      clearSel();
    }, true);
    document.addEventListener("mousemove", function (e) {
      if (!dragSel || !st || !st.full) return;
      const hit = document.elementFromPoint(e.clientX, e.clientY);
      const row = hit ? rowFromLineEl(hit.closest(".eh-line")) : null;
      if (!row || !isEditableRow(row)) return;
      const off = caretApprox(row, e);
      if (!dragSel.moved && row === dragSel.aRow && off === dragSel.aOff) return;
      dragSel.moved = true;
      suppressClickOnce = true;
      // 单行/跨行统一走同一条结构化选区路径：锚点行（聚焦行）的选区由
      // 浏览器原生 ::selection 渲染，其余行在高亮层包裹渲染，拖回锚点行
      // 自动收敛为单行选区（无状态残留）
      sel = { aRow: dragSel.aRow, aOff: dragSel.aOff, fRow: row, fOff: off };
      paintSel();
    });
    document.addEventListener("mouseup", function () {
      dragSel = null;
      setTimeout(function () { suppressClickOnce = false; }, 0);
    });
  }

  /** 重做：弹出重做栈恢复；当前状态压回撤销栈（与 undo 对偶）。 */
  function redoStructureEdit() {
    if (!redoStack.length) {
      toast("没有可重做的编辑");
      return;
    }
    const snapshot = redoStack.pop();
    undoStack.push(serializeRows());
    st.dirtySnapshot = snapshot;
    restoreFromSnapshot(snapshot);
  }

  /** 光标处插入一个字符（选区删除后接续输入用）。 */
  function insertCharAt(row, ch) {
    const input = row.node;
    const start = input.selectionStart;
    input.value = input.value.slice(0, start) + ch + input.value.slice(input.selectionEnd);
    input.setSelectionRange(start + 1, start + 1);
    row.paint();
    markDirty();
  }

  function rowKeydown(e, row) {
    const input = row.node;
    // —— 跨行选区激活时优先处理 ——
    if (sel && sel.aRow !== sel.fRow) {
      if (e.key === "Backspace" || e.key === "Delete") {
        e.preventDefault();
        deleteSelection();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c") {
        e.preventDefault();
        copySelection(false);
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "x") {
        e.preventDefault();
        copySelection(true);
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
        e.preventDefault();
        redoStructureEdit();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "z") {
        e.preventDefault();
        redoStructureEdit();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z") {
        e.preventDefault();
        undoStructureEdit();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
        e.preventDefault();
        selectAllEditable();
        return;
      }
      if (e.key === "Escape") {
        clearSel();
        return;
      }
      if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        deleteSelection();
        insertCharAt(row, e.key);
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        deleteSelection();
        splitRowAtCaret(row);
        return;
      }
      if (e.key.startsWith("Arrow")) clearSel();   // 方向键取消选区走默认
      return;
    }
    // —— 单行状态 ——
    // Ctrl+Z：有结构编辑历史时撤销结构操作；否则放行浏览器默认（撤本行输入）
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z") {
      if (undoStack.length) {
        e.preventDefault();
        undoStructureEdit();
      }
      return;
    }
    // Ctrl+Y / Ctrl+Shift+Z：重做最近一次被撤销的结构编辑
    if ((e.ctrlKey || e.metaKey)
      && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) {
      if (redoStack.length) {
        e.preventDefault();
        redoStructureEdit();
      }
      return;
    }
    // Ctrl+Y / Ctrl+Shift+Z：重做最近一次被撤销的结构编辑
    if ((e.ctrlKey || e.metaKey)
      && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) {
      if (redoStack.length) {
        e.preventDefault();
        redoStructureEdit();
      }
      return;
    }
    // Ctrl+A 二次按下：单行全选 → 扩展为全文件多行选区
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
      if (input.selectionStart === 0 && input.selectionEnd === input.value.length) {
        e.preventDefault();
        selectAllEditable();
      }
      return;   // 首次按下走浏览器默认（全选本行）
    }
    if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      splitRowAtCaret(row);
      return;
    }
    // 行首退格：并入上一可编辑行（上一行为红块时静默忽略，避免跨块误删）
    if (e.key === "Backspace" && input.selectionStart === 0 && input.selectionEnd === 0) {
      e.preventDefault();
      mergeWithPrevious(row);
      return;
    }
    // 行尾 Delete：下一可编辑行并入当前行
    if (
      e.key === "Delete"
      && input.selectionStart === input.value.length
      && input.selectionEnd === input.value.length
    ) {
      e.preventDefault();
      mergeWithNext(row);
      return;
    }
    // 上/下方向键在行边界跳转相邻可编辑行
    if (e.key === "ArrowUp" && input.selectionStart === 0 && input.selectionEnd === 0) {
      const prev = editableNeighbors(row).prev;
      if (prev) {
        e.preventDefault();
        prev.node.focus();
        prev.node.setSelectionRange(prev.node.value.length, prev.node.value.length);
      }
      return;
    }
    if (
      e.key === "ArrowDown"
      && input.selectionStart === input.value.length
      && input.selectionEnd === input.value.length
    ) {
      const next = editableNeighbors(row).next;
      if (next) {
        e.preventDefault();
        next.node.focus();
        next.node.setSelectionRange(0, 0);
      }
    }
  }

  /** 在光标处拆行：左半留在当前行，右半成为新行；后续行号顺延，光标落新行首。 */
  function splitRowAtCaret(row) {
    const input = row.node;
    const caret = input.selectionStart;
    const after = input.value.slice(input.selectionEnd);
    pushUndoSnapshot(row);
    input.value = input.value.slice(0, caret);
    const newRow = {
      kind: row.kind,
      oldNo: undefined,        // 新插入行没有基线行号（老行号列显示为空）
      newNo: row.newNo + 1,
      text: after,
    };
    const idx = st.rows.indexOf(row);
    st.rows.splice(idx + 1, 0, newRow);
    const lineEl = renderRow(newRow);
    row.lineEl.parentNode.insertBefore(lineEl, row.lineEl.nextSibling);
    row.paint();               // 当前行内容缩短：重绘高亮与行高
    renumberRows();
    markDirty();
    newRow.node.focus();
    newRow.node.setSelectionRange(0, 0);
    return newRow;
  }

  /** 退格在行首：本行并入上一可编辑行（本行为空即被删除），光标落在接缝处。 */
  function mergeWithPrevious(row) {
    const prev = editableNeighbors(row).prev;
    if (!prev) return;
    pushUndoSnapshot(row);
    const joinAt = prev.node.value.length;
    prev.node.value = prev.node.value + row.node.value;
    removeRow(row);
    prev.paint();
    renumberRows();
    markDirty();
    prev.node.focus();
    prev.node.setSelectionRange(joinAt, joinAt);
  }

  /** Delete 在行尾：下一可编辑行并入当前行。 */
  function mergeWithNext(row) {
    const next = editableNeighbors(row).next;
    if (!next) return;
    pushUndoSnapshot(row);
    row.node.value = row.node.value + next.node.value;
    removeRow(next);
    row.paint();
    renumberRows();
    markDirty();
    row.node.setSelectionRange(row.node.value.length, row.node.value.length);
  }

  function removeRow(row) {
    const idx = st.rows.indexOf(row);
    if (idx >= 0) st.rows.splice(idx, 1);
    if (row.lineEl && row.lineEl.parentNode) {
      row.lineEl.parentNode.removeChild(row.lineEl);
    }
  }

  /** 结构变化后重排可编辑行的新行号（oldNo 不动：基线行号不随编辑漂移）。 */
  function renumberRows() {
    let next = 1;
    for (const row of st.rows) {
      if (!isEditableRow(row)) continue;
      row.newNo = next++;
      if (!row.noNode) continue;
      if (row.kind === "ctx") {
        row.noNode.textContent = (row.oldNo == null ? "" : row.oldNo) + " | " + row.newNo;
      } else {
        row.noNode.textContent = String(row.newNo);
      }
    }
  }

  /** 粘贴含换行的多行文本：按行拆分插入（复用回车拆行机制）。 */
  function rowPaste(e, row) {
    const clip = e.clipboardData ? e.clipboardData.getData("text") : "";
    if (!clip || clip.indexOf("\n") < 0) return;   // 单行粘贴走默认行为
    e.preventDefault();
    const segments = clip.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    const input = row.node;
    const start = input.selectionStart;
    input.value = input.value.slice(0, start) + segments[0] + input.value.slice(input.selectionEnd);
    let cur = row;
    for (let i = 1; i < segments.length; i++) {
      cur.node.value = segments[i] + cur.node.value;   // 剩余段前插当前片段
      cur.node.setSelectionRange(segments[i].length, segments[i].length);
      cur = splitRowAtCaret(cur);                      // 剩余内容滑入新行
    }
    markDirty();
  }

  function markDirty() {
    dom.save.textContent = "保存 *";
    redoStack.length = 0;   // 新编辑使重做历史失效（与常规编辑器一致）
  }

  // ---------- 结构编辑的撤销/重做（结构操作前手动快照） ----------
  function pushUndoSnapshot(row) {
    st.dirtySnapshot = serializeRows();
    redoStack.length = 0;                 // 新编辑分支使重做历史失效
    if (!undoStack.length || undoStack[undoStack.length - 1] !== st.dirtySnapshot) {
      undoStack.push(st.dirtySnapshot);
      if (undoStack.length > 50) undoStack.shift();
    }
  }

  /** 把行序列压成可恢复的纯数据（不含 DOM 引用；hunk 标记保留 index）。 */
  function serializeRows() {
    return st.rows.map(function (row) {
      if (row.kind === "hunk") return { kind: "hunk", index: row.index };
      if (row.kind === "del") return { kind: "del", text: row.text, newNo: row.newNo, hunk: row.hunk };
      return { kind: row.kind, text: row.node ? row.node.value : row.text, newNo: row.newNo };
    });
  }

  /** 撤销：整体重建为快照时刻的行序列（光标回到首个可编辑行首）。 */
  function restoreFromSnapshot(snapshot) {
    dom.body.innerHTML = "";
    sel = null;
    // 按快照重建 rows（保留 lineEl 引用挂点）
    st.rows = snapshot.map(function (item) {
      return Object.assign({}, item);
    });
    const table = el("div", "eh-table");
    let pendingHunk = -1;
    for (const row of st.rows) {
      if (row.kind === "hunk") {
        pendingHunk = -1;
        table.appendChild(hunkHead(row.index, false));
        continue;
      }
      if (row.kind !== "ctx" && typeof row.hunk === "number" && row.hunk !== pendingHunk) {
        pendingHunk = row.hunk;
        table.appendChild(hunkHead(row.hunk, true));
      } else if (row.kind === "ctx" && pendingHunk >= 0) {
        pendingHunk = -1;
      }
      table.appendChild(renderRow(row));
    }
    dom.body.appendChild(table);
    markDirty();
    for (const row of st.rows) {
      if (isEditableRow(row) && row.node) {
        row.node.focus();
        row.node.setSelectionRange(0, 0);
        break;
      }
    }
  }

  function undoStructureEdit() {
    if (!undoStack.length) {
      toast("没有可撤销的结构编辑");
      return;
    }
    redoStack.push(serializeRows());     // 当前状态入重做栈（Ctrl+Y 可恢复）
    const snapshot = undoStack.pop();
    st.dirtySnapshot = undoStack.length ? undoStack[undoStack.length - 1] : null;
    restoreFromSnapshot(snapshot);
  }

  function copyText(text, okMsg) {
    try {
      navigator.clipboard.writeText(text).then(function () {
        toast(okMsg || "已复制");
      }, function () { /* 忽略 */ });
    } catch (_) { /* 忽略 */ }
  }

  // ---------- 操作 ----------
  function saveEditor() {
    const content = composeContent();
    dom.save.disabled = true;
    dom.save.textContent = "保存中...";
    API.fileSave(sessionId, st.key, content, st.hash)
      .then(function (result) {
        toast("已保存（新版本 v" + result.version.v + "）");
        load();
      })
      .catch(function (err) {
        toast("保存失败：" + err.message);
        dom.save.disabled = false;
        dom.save.textContent = "保存 *";
      });
  }

  function undoHunk(hunkIndex, untilHunk) {
    API.fileHunkUndo(sessionId, st.key, hunkIndex, untilHunk)
      .then(function (result) {
        toast("已撤回 " + (result.undone_count || 1) + " 个差异块（新版本 v"
          + (result.version ? result.version.v : "?") + "）");
        load();
      })
      .catch(function (err) { toast("撤回失败：" + err.message); });
  }

  function keepHunk(hunkIndex, untilHunk) {
    API.fileHunkKeep(sessionId, st.key, hunkIndex, untilHunk)
      .then(function () {
        toast("已保留该差异块（其余还原，新基线已写盘）");
        load();
      })
      .catch(function (err) { toast("保留失败：" + err.message); });
  }

  function bindActions() {
    dom.save.addEventListener("click", function () {
      if (st) saveEditor();
    });
    dom.revert.addEventListener("click", function () {
      if (!st) return;
      askConfirm(
        "把整个文件回退到基线（本轮任务首次修改前的状态）？磁盘文件将被覆盖，此操作经版本链仍可再回退。",
        function () {
          API.fileRollback(sessionId, st.key, { target: "baseline" })
            .then(function () { toast("已回退到基线"); load(); })
            .catch(function (err) { toast("回退失败：" + err.message); });
        }
      );
    });
    dom.roundSelect.addEventListener("change", function () {
      const val = dom.roundSelect.value;
      dom.roundSelect.value = "";
      if (!val || !st) return;
      const round = parseInt(val, 10);
      askConfirm(
        "把该文件回退到第 " + round + " 轮会话发起时的状态？（之后的修改将被还原，磁盘文件同步改写）",
        function () {
          API.fileRollback(sessionId, st.key, { to_round: round })
            .then(function () { toast("已回退到第 " + round + " 轮发起时的状态"); load(); })
            .catch(function (err) { toast("回退失败：" + err.message); });
        }
      );
    });
    dom.sync.addEventListener("click", function () {
      if (!st) return;
      API.fileSyncFromDisk(sessionId, st.key)
        .then(function (result) {
          toast(result.synced
            ? "已并入磁盘最新内容（v" + result.version.v + "），diff 已重算"
            : (result.message || "磁盘内容与版本链一致"));
          load();
        })
        .catch(function (err) { toast("刷新失败：" + err.message); });
    });
    dom.keep.addEventListener("click", function () {
      if (!st) return;
      askConfirm(
        "保留后该文件的当前历史代将被锁定（不可再撤回/回退到其中的版本），并以当前内容开新代跟踪。确定保留？",
        function () {
          API.fileKeep(sessionId, st.key)
            .then(function () { toast("已保留：历史已锁定，此后变更在新代跟踪"); load(); })
            .catch(function (err) { toast("保留失败：" + err.message); });
        }
      );
    });
    dom.close.addEventListener("click", function () { global.close(); });
    // 差异块导航：顶栏固定按钮，按当前滚动位置锚定相邻块头跳转
    dom.prevHunk.addEventListener("click", function () { if (st) jumpHunk(-1); });
    dom.nextHunk.addEventListener("click", function () { if (st) jumpHunk(1); });
    // 主题切换：与主应用共用同一份偏好（ThemeManager / ytools-theme-preference），
    // system 选项由 ThemeManager 按 prefers-color-scheme 解析并在系统切换时跟随
    if (dom.themeSelect) {
      dom.themeSelect.value = window.ThemeManager.getPreference();
      dom.themeSelect.addEventListener("change", function () {
        window.ThemeManager.setPreference(dom.themeSelect.value);
      });
    }
    dom.confirmOk.addEventListener("click", function () {
      const action = confirmAction;
      closeConfirm();
      if (action) action();
    });
    dom.confirmCancel.addEventListener("click", closeConfirm);
    dom.confirmBackdrop.addEventListener("click", closeConfirm);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (dom.confirm && !dom.confirm.classList.contains("hidden")) closeConfirm();
        else global.close();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && st) {
        e.preventDefault();
        saveEditor();
      }
    });
  }

  bindActions();
  bindDragSelect();
  load();
})(window);
