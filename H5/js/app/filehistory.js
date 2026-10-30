/**
 * V2 文件历史版本链前端（docs/file_diff.md §9）：
 * - 主页面文件变更统计面板：会话内被改文件列表（只显示文件名 / hover 见完整路径 /
 *   后缀筛选按钮 + 多选下拉菜单（纯前端过滤）/ 已保留或已全部撤回的文件自动隐藏 /
 *   全部保留 · 全部撤回 · 清理留档入口）
 * - diff 编辑器已迁移至独立页 editor.html（js/app/editor.js）：
 *   面板点击文件 → 新窗口打开 editor.html?session_id=...&key=...
 * 依赖：API（js/api.js）、App 核心（core.js）；须在 chat.js 之后加载。
 */

window.App = window.App || {};
(function (App) {
  "use strict";

  const el = App.el;
  const toast = App.toast;
  const state = App.state;

  // ---------- 面板状态 ----------
  let panelFiles = [];          // /file_diff/list 结果缓存
  let extFilter = [];           // 后缀筛选：已选小写后缀数组（[] = 全部；"" 表示“无后缀”；按点选顺序排列，用于按钮文案）
  let editorState = null;       // 打开的编辑器状态 {key, displayPath, baselineV, currentV, hash, rows, hunks}

  // ---------- 解析 unified diff → 行序列（与后端 difflib 输出强约定） ----------
  const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

  /**
   * 解析 diff 文本为渲染行序列：
   * [{kind: hunk, index}, {kind: ctx, oldNo, newNo, text},
   *  {kind: del, oldNo, text}, {kind: add, newNo, text}]
   * 每个连续变更组（hunk 内 add/del 段）挂在最近的 hunk 上，供单块撤回。
   */
  function parseDiffRows(diffText) {
    const rows = [];
    if (!diffText) return rows;
    const lines = diffText.split("\n");
    let oldNo = 0;
    let newNo = 0;
    let hunkIndex = -1;
    for (const line of lines) {
      const m = HUNK_RE.exec(line);
      if (m) {
        hunkIndex = rows.filter(r => r.kind === "hunk").length;
        rows.push({ kind: "hunk", index: hunkIndex });
        oldNo = parseInt(m[1], 10);
        newNo = parseInt(m[3], 10);
        if (oldNo === 0) oldNo = 1; // 新建文件场景 @@ -0,0：旧行号从 0 起步
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

  /** 编辑器重组全文：ctx 行原文 + add 行当前值；del 行（旧内容）不进入。 */
  function composeCurrentContent(rows) {
    const parts = [];
    for (const row of rows) {
      if (row.kind === "ctx") parts.push(row.text);
      else if (row.kind === "add") parts.push(row.node ? row.node.value : row.text);
      // del 行 = 被替换的旧内容：保存即接受新内容，天然丢弃
    }
    return parts.join("\n");
  }

  // ---------- 主页面统计面板 ----------
  /** 路径 → 文件名（末段）：面板列表只展示文件名，完整路径进 hover title。 */
  function baseNameOf(displayPath) {
    const parts = String(displayPath || "").split(/[\\/]/);
    return parts[parts.length - 1] || String(displayPath || "");
  }

  /** 路径 → 小写后缀（无后缀 / 纯点文件 / 末尾点返回 ""）。 */
  function extOf(filePath) {
    const base = baseNameOf(filePath);
    const dot = base.lastIndexOf(".");
    if (dot <= 0 || dot === base.length - 1) return "";
    return base.slice(dot + 1).toLowerCase();
  }

  /**
   * 文件列表 → 后缀选项 [{ext, count}]（字母序，“无后缀”沉底）。
   * 下拉菜单选项唯一来源：永远从当前列表现算，不会出现“选了没结果”的幽灵项。
   */
  function collectFileExts(files) {
    const counts = new Map();
    for (const file of files || []) {
      const ext = extOf(file.display_path || file.path);
      counts.set(ext, (counts.get(ext) || 0) + 1);
    }
    return Array.from(counts.entries())
      .map(function (entry) { return { ext: entry[0], count: entry[1] }; })
      .sort(function (a, b) {
        if (!a.ext) return 1;
        if (!b.ext) return -1;
        return a.ext < b.ext ? -1 : (a.ext > b.ext ? 1 : 0);
      });
  }

  /** 文件是否命中后缀筛选（空数组 = 全部；含 "" 则无后缀文件也命中）。 */
  function matchExtFilter(file, exts) {
    if (!exts || !exts.length) return true;
    return exts.indexOf(extOf(file.display_path || file.path)) !== -1;
  }

  /**
   * 筛选按钮文案与高亮（每次渲染同步，数据变化后自动回落也经此刷新）。
   * 0 项=“全部后缀”；1~2 项直接列出（如 py, md）；3 项及以上收拢为“已选 N 项”。
   */
  function updateFilterButton() {
    const btn = document.getElementById("fileChangesFilterBtn");
    if (!btn) return;
    const label = !extFilter.length ? "全部后缀"
      : (extFilter.length <= 2 ? extFilter.map(function (e) { return e || "(无后缀)"; }).join(", ")
        : ("已选 " + extFilter.length + " 项"));
    btn.textContent = label + " ▾";
    btn.classList.toggle("is-active", extFilter.length > 0);
    btn.title = !extFilter.length ? "按文件后缀筛选列表（可多选）"
      : ("当前仅显示 " + extFilter.map(function (e) { return e ? ("." + e) : "无后缀文件"; }).join("、") + "（点击切换）");
  }

  /** 重建下拉菜单（选项永远从当前 panelFiles 现算，已选项打勾）。 */
  function renderFilterMenu() {
    const menu = document.getElementById("fileChangesFilterMenu");
    if (!menu) return;
    menu.innerHTML = "";
    const mkItem = function (label, ext, count, isAll) {
      const selected = isAll ? !extFilter.length : extFilter.indexOf(ext) !== -1;
      const item = el("button", "fh-filter-item" + (selected ? " is-selected" : ""));
      item.type = "button";
      item.setAttribute("role", "menuitemcheckbox");
      item.setAttribute("aria-checked", selected ? "true" : "false");
      item.appendChild(el("span", "fh-filter-check", selected ? "✓" : ""));
      item.appendChild(el("span", "fh-filter-label", label));
      item.appendChild(el("span", "fh-filter-count", String(count)));
      item.addEventListener("click", function (e) {
        e.stopPropagation();
        if (isAll) {
          extFilter = [];
          closeFilterMenu();
        } else {
          // 多选切换：菜单保持打开，方便连续点选多项
          const at = extFilter.indexOf(ext);
          if (at === -1) extFilter.push(ext);
          else extFilter.splice(at, 1);
        }
        renderPanel();
      });
      return item;
    };
    menu.appendChild(mkItem("全部后缀", null, panelFiles.length, true));
    const exts = collectFileExts(panelFiles);
    for (const entry of exts) {
      menu.appendChild(mkItem(entry.ext || "(无后缀)", entry.ext, entry.count, false));
    }
    clampFilterMenu(); // 数据变化重建后重算方向与限高（关闭时内部直接返回）
    alignFilterSelection(); // 重建节点滚动归零，重新对齐选中项
  }

  /**
   * 关闭下拉菜单（返回关闭前是否处于打开态，供 Esc 链判断是否消费按键）。
   */
  function closeFilterMenu() {
    const menu = document.getElementById("fileChangesFilterMenu");
    if (!menu || menu.classList.contains("hidden")) return false;
    menu.classList.add("hidden");
    const btn = document.getElementById("fileChangesFilterBtn");
    if (btn) btn.setAttribute("aria-expanded", "false");
    return true;
  }

  /**
   * 下拉菜单视口钳制（与 quotes.js clampQuotePopover 同思路）：
   * 选项多时菜单自然高度可能超过按钮上方空间，顶部选项顶出视口且无法触达
   * （面板 fixed 定位，滚动页面也救不回来）。展开时实测自然高度：上方放不下
   * 且下方空间更大则翻转向下开；展开侧空间仍不足 240px 时限高走内部滚动。
   * 高度基准优先 visualViewport（移动端键盘/捏合缩放只动视觉视口，不动
   * layout viewport），桌面端回退 innerHeight。调用时机见 scheduleFilterMenuClamp。
   */
  // 上次钳制落定的几何指纹（自然高|按钮上下|视口）：rAF 复验无漂移时跳过，
  // 避免重复设置同样的方向/限高（菜单开关前后几何一致时也直接复用旧值）
  let lastFilterClampKey = "";
  function clampFilterMenu() {
    const menu = document.getElementById("fileChangesFilterMenu");
    const btn = document.getElementById("fileChangesFilterBtn");
    if (!menu || !btn || menu.classList.contains("hidden")) return;
    const wrap = menu.parentElement;
    // 先解除限高测自然高度；注意测量后无论走哪条分支，文末必恢复/重设限高，
    // 绝不能把 "none" 留到函数外（否则菜单按自然高度撑开顶出视口，且后续
    // 同几何复验命中指纹跳过时也不会修复——2026-10-27 压力测试抓到的真 bug）。
    menu.style.maxHeight = "none";
    const naturalH = menu.scrollHeight || 0;
    const margin = 8;
    const btnRect = btn.getBoundingClientRect ? btn.getBoundingClientRect() : { top: 0, bottom: 0 };
    const viewport = window.visualViewport;
    const viewTop = viewport ? viewport.offsetTop : 0;
    const viewH = (viewport ? viewport.offsetTop + viewport.height : window.innerHeight) || 0;
    const spaceAbove = btnRect.top - viewTop - margin;
    const spaceBelow = viewH - btnRect.bottom - margin;
    const openBelow = naturalH > spaceAbove && spaceBelow > spaceAbove + 96;
    // 翻转偏置说明：向下需要决定性优势（>96px）才翻转，否则一律向上。
    // 原因有二：其一任务栏/系统遮挡只可能吃掉底部空间（且无事件可监听，
    // 如窗口底边伸到任务栏后面时 innerHeight 会虚高），顶部几乎永远安全；
    // 其二数学上依然恒贴合——被纠正回上方的情况满足 below ≤ above+96，
    // 上开高度 min(240, above) ≤ above 恒成立，不可能溢出。
    const avail = openBelow ? spaceBelow : spaceAbove;
    const maxH = Math.min(240, Math.max(avail, 0)) + "px";
    const key = naturalH + "|" + btnRect.top + "|" + btnRect.bottom + "|" + viewH + "|" + viewTop;
    if (key !== lastFilterClampKey) {
      lastFilterClampKey = key;
      if (wrap) wrap.classList.toggle("is-below", openBelow);
    }
    menu.style.maxHeight = maxH; // 必恢复：同值写入浏览器即 no-op，无回流代价
  }

  // 选中项滚入可见（与几何钳制分离：菜单每次重建节点滚动归零，都要重新对齐；
  // 而 rAF 复验无漂移时只跳过几何部分，不重复滚动）
  function alignFilterSelection() {
    const menu = document.getElementById("fileChangesFilterMenu");
    if (!menu || menu.classList.contains("hidden")) return;
    // 有选中项时滚入可见（长列表点选重建后，选中项可能滚出视野）
    const sel = menu.querySelector(".fh-filter-item.is-selected");
    if (sel && typeof sel.scrollIntoView === "function") sel.scrollIntoView({ block: "nearest" });
  }

  // 视口跟踪：菜单打开期间，任何改变可见几何的事件都重算方向与限高
  // （rAF 节流，与 quotes.js scheduleQuotePopoverClamp 同模式；关闭时钳制内部直接返回）
  let filterClampFrame = 0;
  function scheduleFilterMenuClamp() {
    if (filterClampFrame) return;
    // 无 rAF 的环境（jsdom 单测/极老浏览器）退化为同步执行
    if (typeof window.requestAnimationFrame !== "function") {
      clampFilterMenu();
      return;
    }
    filterClampFrame = window.requestAnimationFrame(function () {
      filterClampFrame = 0;
      clampFilterMenu();
    });
  }

  /**
   * 刷新顶栏「文件变更」徽标（红点数字）。
   * @param {object} [prefetched] 可选：刚拉取过的 /file_diff/list 数据，直接复用免二次请求
   */
  let badgeInflight = false; // 在途去重：同会话统计请求进行中不重复发起
  // 节流兜底：非强制路径（focus/visibilitychange/外部调用）同会话短窗口内
  // 只发一次。focus 事件可能被输入法/浏览器扩展/系统通知等触发成风暴，
  // 每次直调都会变成对 /file_diff/list 的轮询——统一节流兜底。数据变更
  // 源（会话切换 noteSessionChanged、文件工具结果防抖刷新）带 force 不受限。
  const BADGE_REFRESH_MIN_INTERVAL_MS = 1500;
  let lastBadgeRequestedAt = 0;
  function refreshBadge(prefetched, force) {
    const btn = document.getElementById("fileChangesBtn");
    if (!btn) return;
    const badge = btn.querySelector(".fh-badge");
    if (!state.sessionId) {
      btn.classList.add("hidden");
      return;
    }
    const apply = function (data) {
      // 数据已刷新为最新：消费跨窗口变更标记（编辑器页写操作留下的）
      try {
        if (window.FileHistorySync) window.FileHistorySync.consumeDirty();
      } catch (_) { /* 标记失败不影响徽标渲染 */ }
      panelFiles = data.files || [];
      const stats = data.stats || { total: 0, added: 0, removed: 0 };
      btn.classList.remove("hidden");
      if (stats.total > 0) {
        badge.textContent = String(stats.total);
        badge.classList.remove("hidden");
        btn.title = `文件变更 ${stats.total} 个文件（+${stats.added} -${stats.removed} 行）`;
      } else {
        badge.classList.add("hidden");
        btn.title = "文件变更";
      }
    };
    if (prefetched && prefetched.stats) {
      apply(prefetched);
      return;
    }
    // 静默守卫三重：在途去重 + 同会话节流窗口 + 响应归属校验——杜绝任何
    // 路径（含未知调用方/焦点风暴）把徽标刷新变成轮询源
    const requestedSession = state.sessionId;
    if (badgeInflight) return;
    const now = Date.now();
    if (!force && now - lastBadgeRequestedAt < BADGE_REFRESH_MIN_INTERVAL_MS) return;
    badgeInflight = true;
    lastBadgeRequestedAt = now;
    API.listFileChanges(requestedSession).then(function (data) {
      badgeInflight = false;
      if (state.sessionId === requestedSession) apply(data);
    }).catch(function () {
      badgeInflight = false;
      if (state.sessionId === requestedSession) btn.classList.add("hidden");
    });
  }

  // 会话变化通知（事件驱动，替代旧 2s 轮询看门狗）：sessionId 与上次已同步
  // 值不同才刷新一次；相同则零请求。由 openSession/startNewChat/
  // ensureSessionId/本地导入预览四个赋值点显式调用
  let lastNotedSession;
  function noteSessionChanged() {
    if (state.sessionId === lastNotedSession) return;
    lastNotedSession = state.sessionId;
    refreshBadge(undefined, true); // 会话切换是数据变更源：强制刷新不受节流限制
  }

  /**
   * 标记驱动的条件刷新（focus / 可见性恢复兜底路径）：仅当存在未消费的
   * 跨窗口变更标记时才拉取——无任何文件链写操作时焦点事件零请求，静默期
   * 不再产生轮询式请求。拉取成功后由 apply 消费标记；失败则标记保留，
   * 下次焦点自动重试。
   */
  function refreshBadgeIfDirty() {
    try {
      if (!window.FileHistorySync || !window.FileHistorySync.peekDirty()) return;
    } catch (_) {
      return;
    }
    refreshBadge(undefined, true);
  }

  function renderPanel() {
    const listNode = document.getElementById("fileChangesList");
    const footNode = document.getElementById("fileChangesFoot");
    if (!listNode) return;
    listNode.innerHTML = "";
    if (!panelFiles.length) {
      listNode.appendChild(el("div", "fh-empty", "本会话还没有文件变更记录"));
      if (footNode) footNode.classList.add("hidden");
      closeFilterMenu(); // 列表清空时菜单无意义，一并收起
      return;
    }
    if (footNode) footNode.classList.remove("hidden");
    // 保留/撤回/清理后数据变化：已选项若在新列表中消失则剔除（部分消失保留剩余项，
    // 全部消失回到全部）；选项本就从数据现算，选中态恒有匹配，不会出现“有筛选无结果”
    if (extFilter.length) {
      const available = {};
      const exts = collectFileExts(panelFiles);
      for (const entry of exts) available[entry.ext] = true;
      extFilter = extFilter.filter(function (e) { return available[e]; });
    }
    const visible = panelFiles.filter(function (file) { return matchExtFilter(file, extFilter); });
    // 顺序关键：必须先把列表行全部挂上（按钮位置随行数变化），再重建菜单并钳制。
    // 反过来（先钳制后挂行）会量到清空列表时的按钮高位，行挂上后按钮下移，
    // 菜单按旧几何向下展开就会顶出视口底部——多次点选（行数反复横跳）必现。
    for (const file of visible) {
      const row = el("button", "fh-file-row");
      row.type = "button";
      row.title = file.path;  // hover 可见完整路径
      const name = el("span", "fh-file-name", baseNameOf(file.display_path || file.path));
      const meta = el("span", "fh-file-meta");
      if (file.added) meta.appendChild(el("span", "fh-add", "+" + file.added));
      if (file.removed) meta.appendChild(el("span", "fh-del", "-" + file.removed));
      if (file.kept) meta.appendChild(el("span", "fh-kept", "已保留"));
      const arrow = el("span", "fh-arrow", "›");
      row.appendChild(name);
      row.appendChild(meta);
      row.appendChild(arrow);
      row.addEventListener("click", function () {
        closePanel();
        openEditor(file.key);
      });
      listNode.appendChild(row);
    }
    updateFilterButton();
    renderFilterMenu(); // 列表落定后再重建菜单并钳制（关闭时钳制内部直接返回）
    scheduleFilterMenuClamp(); // 首帧后再验一次：兜底字体/图片等异步布局（幂等，无变化即无操作）
  }

  function togglePanel() {
    const panel = document.getElementById("fileChangesPanel");
    if (!panel) return;
    if (!state.sessionId) {
      toast("开始对话后才会记录文件变更");
      return;
    }
    const hidden = panel.classList.contains("hidden");
    App.closeMenus && App.closeMenus();
    if (hidden) {
      panel.classList.remove("hidden");
      const stats = document.getElementById("fileChangesStats");
      stats.textContent = "加载中...";
      refreshPanelData();
    } else {
      panel.classList.add("hidden");
    }
  }

  function closePanel() {
    const panel = document.getElementById("fileChangesPanel");
    if (panel) panel.classList.add("hidden");
    closeFilterMenu(); // 面板收起时下拉菜单不保持打开态
  }

  /** 清理留档：删除当前会话所有"已全部保留/撤回"文件的版本链目录。 */
  function cleanupHistories() {
    if (!state.sessionId) return;
    askConfirm(
      "清理所有已全部保留/撤回文件的版本链留档？该操作不可恢复（仍有未决变更的文件不受影响）。",
      function () {
        API.fileCleanup(state.sessionId, true).then(function (result) {
          toast(result.removed_count ? "已清理 " + result.removed_count + " 个留档" : "没有可清理的留档");
          refreshPanelData();
        }).catch(function (err) {
          toast("清理失败：" + err.message);
        });
      }
    );
  }

  function refreshPanelData() {
    API.listFileChanges(state.sessionId).then(function (data) {
      panelFiles = data.files || [];
      const s = data.stats || {};
      const stats = document.getElementById("fileChangesStats");
      if (stats) {
        stats.textContent = panelFiles.length
          ? `${s.total} 个未决文件 · +${s.added} -${s.removed} 行`
          : "暂无未决变更";
      }
      renderPanel();
      // 面板数据与顶栏徽标同步刷新（保留/撤回/清档后红点数字立即反映）
      refreshBadge(data);
    }).catch(function (err) {
      const stats = document.getElementById("fileChangesStats");
      if (stats) stats.textContent = "加载失败：" + err.message;
      renderPanel();
    });
  }

  /** 全部保留：所有未决变更封版为新代基线（防误触二次确认）。 */
  function keepAll() {
    if (!state.sessionId) return;
    askConfirm(
      "全部保留：接受当前会话所有文件的未决变更，并封版为新基线（之后不可再撤回）；封版后的版本链留档将一并清理。确认继续？",
      function () {
        API.fileKeepAll(state.sessionId).then(function (result) {
          const n = result.kept_count || 0;
          const cleaned = result.cleaned_count || 0;
          const skip = (result.skipped || []).length;
          toast(n ? `已保留 ${n} 个文件` + (cleaned ? `，清理 ${cleaned} 个留档` : "")
            + (skip ? `（跳过 ${skip} 个）` : "") : "没有可保留的变更");
          refreshPanelData();
        }).catch(function (err) {
          toast("全部保留失败：" + err.message);
        });
      }
    );
  }

  /** 全部撤回：所有未决变更回退到本轮基线（历史版本可在编辑器找回）。 */
  function revertAll() {
    if (!state.sessionId) return;
    askConfirm(
      "全部撤回：当前会话所有文件的未决变更将回退到本轮基线；新建文件将恢复为\"未创建\"状态（磁盘空文件会被删除），历史版本仍可在编辑器找回。确认继续？",
      function () {
        API.fileRevertAll(state.sessionId).then(function (result) {
          const n = result.reverted_count || 0;
          const removed = (result.reverted || []).filter(function (item) { return item.disk_removed; }).length;
          const skip = (result.skipped || []).length;
          toast(n ? `已撤回 ${n} 个文件` + (removed ? `（含 ${removed} 个新建文件已删除）` : "")
            + (skip ? `（跳过 ${skip} 个）` : "") : "没有可撤回的变更");
          refreshPanelData();
        }).catch(function (err) {
          toast("全部撤回失败：" + err.message);
        });
      }
    );
  }

  // ---------- 二次确认 ----------
  let confirmState = null; // {onConfirm}

  function askConfirm(message, onConfirm) {
    const modal = document.getElementById("fhConfirmModal");
    if (!modal) {
      // 兜底（理论上模态框常驻 DOM）：直接执行
      onConfirm();
      return;
    }
    confirmState = { onConfirm: onConfirm };
    document.getElementById("fhConfirmMessage").textContent = message;
    modal.classList.remove("hidden");
  }

  function closeConfirm() {
    const modal = document.getElementById("fhConfirmModal");
    if (modal) modal.classList.add("hidden");
    confirmState = null;
  }

  // ---------- 独立编辑器页（editor.html） ----------
  function openEditor(key) {
    const url = "editor.html?session_id=" + encodeURIComponent(state.sessionId || "default")
      + "&key=" + encodeURIComponent(key);
    // 不能用 noopener 打开（该模式下 window.open 同步返回 null，无法区分拦截与否）；
    // 打开成功后立即与 opener 断开，兼顾安全与检测
    let win = null;
    try {
      win = window.open(url, "_blank");
    } catch (_) { /* 拦截异常走兜底 */ }
    if (win) {
      try { win.opener = null; } catch (_) { /* 忽略 */ }
      return;
    }
    // 返回 null：真被弹窗拦截 → 兜底为当前页跳转（编辑器页 Esc/关闭按钮可返回）
    location.href = url;
  }

  // ---------- 导出与初始化 ----------
  App.fileHistory = {
    refreshBadge: refreshBadge,
    noteSessionChanged: noteSessionChanged,
    togglePanel: togglePanel,
    closePanel: closePanel,
    openEditor: openEditor,
    cleanupHistories: cleanupHistories,
    keepAll: keepAll,
    revertAll: revertAll,
    collectFileExts: collectFileExts, // 测试钩子：后缀选项统计
    matchExtFilter: matchExtFilter,   // 测试钩子：后缀命中判断
  };

  // DOM 初始化只执行一次：防止脚本被重复引入/重复执行时事件翻倍
  // （翻倍会导致筛选按钮点一次开关两次、Esc 处理两次等难以察觉的故障）
  let domInitDone = false;
  document.addEventListener("DOMContentLoaded", function () {
    if (domInitDone) return;
    domInitDone = true;
    const btn = document.getElementById("fileChangesBtn");
    if (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        togglePanel();
      });
    }
    // 点击面板/按钮外部时收起统计面板
    document.addEventListener("click", function (e) {
      const panel = document.getElementById("fileChangesPanel");
      if (!panel || panel.classList.contains("hidden")) return;
      if (!panel.contains(e.target) && !(btn && btn.contains(e.target))) {
        closePanel();
      }
    });
    // 清理留档 / 全部保留 / 全部撤回 入口
    const cleanupBtn = document.getElementById("fileChangesCleanup");
    if (cleanupBtn) {
      cleanupBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        cleanupHistories();
      });
    }
    const keepAllBtn = document.getElementById("fileChangesKeepAll");
    if (keepAllBtn) {
      keepAllBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        keepAll();
      });
    }
    const revertAllBtn = document.getElementById("fileChangesRevertAll");
    if (revertAllBtn) {
      revertAllBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        revertAll();
      });
    }
    // 后缀筛选：按钮开关下拉菜单，选项从当前列表现算；纯前端过滤，不发请求
    const filterBtn = document.getElementById("fileChangesFilterBtn");
    if (filterBtn) {
      filterBtn.addEventListener("click", function (e) {
        e.stopPropagation(); // 吞掉，否则冒泡到文档点击会立刻关掉刚打开的菜单
        const menu = document.getElementById("fileChangesFilterMenu");
        if (!menu) return;
        menu.classList.toggle("hidden");
        filterBtn.setAttribute("aria-expanded", menu.classList.contains("hidden") ? "false" : "true");
        clampFilterMenu(); // 打开后按视口钳制方向与高度（关闭时内部直接返回）
        alignFilterSelection(); // 打开即对齐当前选中项
      });
    }
    // 窗口/视口/侧栏几何变化时重算菜单方向与限高（rAF 节流；关闭时内部直接返回）
    window.addEventListener("resize", scheduleFilterMenuClamp);
    window.addEventListener("sidebarresizeend", scheduleFilterMenuClamp);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", scheduleFilterMenuClamp);
      window.visualViewport.addEventListener("scroll", scheduleFilterMenuClamp);
    }
    // 点击菜单/按钮外部时收起下拉菜单（与面板关闭各自独立判断，互不干扰）
    document.addEventListener("click", function (e) {
      const wrap = e.target && e.target.closest ? e.target.closest(".fh-filter-wrap") : null;
      if (!wrap) closeFilterMenu();
    });
    // 二次确认框
    const confirmOk = document.getElementById("fhConfirmOk");
    const confirmCancel = document.getElementById("fhConfirmCancel");
    const confirmBackdrop = document.getElementById("fhConfirmBackdrop");
    if (confirmOk) {
      confirmOk.addEventListener("click", function () {
        const action = confirmState && confirmState.onConfirm;
        closeConfirm();
        if (action) action();
      });
    }
    if (confirmCancel) confirmCancel.addEventListener("click", closeConfirm);
    if (confirmBackdrop) confirmBackdrop.addEventListener("click", closeConfirm);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        const confirmModal = document.getElementById("fhConfirmModal");
        if (confirmModal && !confirmModal.classList.contains("hidden")) {
          closeConfirm();
        } else if (closeFilterMenu()) {
          e.stopPropagation(); // 菜单开着：仅关菜单
        } else if (extFilter.length) {
          e.stopPropagation(); // 有筛选：仅回到全部
          extFilter = [];
          renderPanel();
        }
        // 干净态：放行，Esc 照常关闭面板（若有全局处理）
      }
    });
    // 跨窗口即时同步：编辑器页写操作成功即写标记并广播（file_history_sync.js），
    // 主页面收到广播立即刷新徽标——无需等待用户切回窗口
    if (window.FileHistorySync) {
      window.FileHistorySync.subscribe(function () {
        refreshBadge(undefined, true);
      });
    }
    // 编辑器页（独立窗口）内保留/撤回后回到主页面：focus / 可见性恢复兜底同步。
    // 标记驱动：仅当存在未消费的变更标记时才刷新（成功后消费标记）——
    // 无文件链写操作时焦点事件零请求；广播丢失时靠标记兜底，刷新失败下次重试
    window.addEventListener("focus", function () {
      refreshBadgeIfDirty();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refreshBadgeIfDirty();
    });
    // 初始同步一次（此后全部事件驱动：openSession / startNewChat /
    // ensureSessionId / 历史导入钩子调用 noteSessionChanged；
    // 不再保留任何定时轮询——静默期对 /file_diff/list 零请求）
    noteSessionChanged();
  });
})(window.App);
