/**
 * V2 文件历史版本链前端（docs/file_diff.md §9）：
 * - 主页面文件变更统计面板：会话内被改文件列表（只显示文件名 / hover 见完整路径 /
 *   已保留或已全部撤回的文件自动隐藏 / 全部保留 · 全部撤回 · 清理留档入口）
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

  function renderPanel() {
    const listNode = document.getElementById("fileChangesList");
    const footNode = document.getElementById("fileChangesFoot");
    if (!listNode) return;
    listNode.innerHTML = "";
    if (!panelFiles.length) {
      listNode.appendChild(el("div", "fh-empty", "本会话还没有文件变更记录"));
      if (footNode) footNode.classList.add("hidden");
      return;
    }
    if (footNode) footNode.classList.remove("hidden");
    for (const file of panelFiles) {
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
  };

  document.addEventListener("DOMContentLoaded", function () {
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
        if (confirmModal && !confirmModal.classList.contains("hidden")) closeConfirm();
      }
    });
    // 编辑器页（独立窗口）内保留/撤回后回到主页面：focus / 可见性恢复时同步徽标。
    // 不带 force——受同会话节流窗口约束：焦点事件可能被输入法/扩展/系统通知
    // 打成风暴，节流后同一风暴窗口内最多 1 次 /file_diff/list（静默期零请求）
    window.addEventListener("focus", function () {
      refreshBadge();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refreshBadge();
    });
    // 初始同步一次（此后全部事件驱动：openSession / startNewChat /
    // ensureSessionId / 历史导入钩子调用 noteSessionChanged；
    // 不再保留任何定时轮询——静默期对 /file_diff/list 零请求）
    noteSessionChanged();
  });
})(window.App);
