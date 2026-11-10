/**
 * md 表格复制/导出（原 messages.js 4217-4383 行拆出；无对外导出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 *
 * 事件委托挂在 document（而非 chatInner）：md 表格不只在主聊天区渲染，
 * Skills 提示词预览等模态框里的 Markdown.render 也生成同一套复制/更多按钮；
 * 挂在 chatInner 上时这些区域的按钮 hover 能看见、点击无反应。
 */
(function (App: any) {
  "use strict";

  // ---------- md 表格：复制（原始 Markdown）与复制为图片 / 下载 Excel ----------
  // 矩阵转换与 canvas 网格绘制抽到 js/table_canvas.js（TableCanvas 全局，纯函数可单测）
  function tableToMatrix(table: any) {
    return TableCanvas.tableToMatrix(table);
  }

  // 复制原始 markdown 表格文本（渲染时保存在 data-table-raw，含 \\| 转义与行内代码原貌）
  async function copyTableMarkdown(rawMarkdown: any) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(rawMarkdown);
      return;
    }
    // 兜底：隐藏 textarea + execCommand（非安全上下文等场景）
    const helper = document.createElement("textarea");
    helper.value = rawMarkdown;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    (document.body as HTMLElement).appendChild(helper);
    helper.select();
    try { document.execCommand("copy"); } finally { helper.remove(); }
  }

  // 下载表格为 Excel（后端解析原始 md 表格并生成 xlsx，见 /export/table/xlsx）
  async function downloadTableXlsx(rawMarkdown: any) {
    const res = await fetch(API.BASE + "/export/table/xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: rawMarkdown }),
    });
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || body.message || detail;
      } catch (_) { /* 非 JSON 响应 */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "table.xlsx";
    (document.body as HTMLElement).appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // 把表格绘制为 PNG canvas：实现抽到 js/table_canvas.js（TableCanvas 全局）。
  // 列宽两轮收敛 + 行高自适应，长文本逐字换行完整可见，不再截断加省略号；
  // 换行/布局纯函数在 Node 单测覆盖（test_h5/table_export.test.js）。
  function drawTableCanvas(table: any) {
    return TableCanvas.drawTableCanvas(table);
  }

  async function copyTableImage(table: any) {
    const canvas = drawTableCanvas(table);
    if (!canvas) throw new Error("表格过大，无法生成图片");
    const blob: any = await new Promise(function(resolve: any) { canvas.toBlob(resolve, "image/png"); });
    if (navigator.clipboard && (window as any).ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      return "已复制表格图片";
    }
    // 剪贴板写图片不可用（非安全上下文等）：降级为保存图片
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "table.png";
    link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    return "已保存 table.png";
  }

  // 关闭所有表格「更多」菜单
  function closeAllTableMenus(exceptWrap?: any) {
    document.querySelectorAll(".md-table-menu").forEach(function(menu: any) {
      if (menu !== exceptWrap) menu.classList.add("hidden");
    });
    document.querySelectorAll(".md-table-more-btn").forEach(function(btn: any) {
      if (btn.closest(".md-table-actions") === exceptWrap?.closest(".md-table-actions")) return;
      btn.setAttribute("aria-expanded", "false");
    });
  }

  // 表格操作按钮（事件委托：markdown 渲染会重建节点）
  // 监听挂在 document 而非 chatInner：md 表格不只在主聊天区渲染，Skills 提示词
  // 预览、任务模板预览等模态框里的 Markdown.render 也会生成同一套按钮。挂在
  // chatInner 上时这些区域的按钮“hover 能看见、点了没反应”（事件不会冒泡到
  // chatInner）。挂 document 对所有渲染位置统一生效，主聊天区行为不变。
  document.addEventListener("click", async function(e: any) {
    // e.target 可能是 Text/document 等非 Element（closest 不存在），先归一
    const origin = e.target && e.target.closest ? e.target : (e.target && e.target.parentElement);
    if (!origin || !origin.closest) return;
    const btn = origin.closest(".md-table-btn");
    if (btn && btn.dataset.tableAction === "more") {
      // 「更多」菜单展开/收起；同时互斥关闭其它表格的菜单
      const menu = btn.parentElement.querySelector(".md-table-menu");
      const willOpen = menu.classList.contains("hidden");
      closeAllTableMenus();
      if (willOpen) {
        menu.classList.remove("hidden");
        btn.setAttribute("aria-expanded", "true");
      } else {
        btn.setAttribute("aria-expanded", "false");
      }
      return;
    }
    // 点击菜单外的其它区域时收起全部菜单
    closeAllTableMenus();
    let target = btn;
    if (!target) {
      const menuBtn = origin.closest('[role="menuitem"]');
      if (!menuBtn) return;
      target = menuBtn;
      // 点击菜单项后收起本表格菜单
      target.closest(".md-table-more")?.querySelector(".md-table-menu")?.classList.add("hidden");
      target.closest(".md-table-more")?.querySelector(".md-table-more-btn")?.setAttribute("aria-expanded", "false");
    }
    if (target.disabled) return;
    const block = target.closest(".md-table-block");
    if (!block) return;
    const table = block.querySelector(".md-table-scroll table");
    const rawAttr = target.getAttribute("data-table-raw") || "";
    const rawMarkdown = Markdown.unescapeHtml(rawAttr);
    if (!table && !rawMarkdown) return;
    const action = target.dataset.tableAction;
    const original = target.textContent;
    target.disabled = true;
    target.textContent = "…";
    try {
      let message = "";
      if (action === "copy-md") {
        await copyTableMarkdown(rawMarkdown);
        message = "已复制 Markdown";
      } else if (action === "download-xlsx") {
        await downloadTableXlsx(rawMarkdown);
        message = "已下载 table.xlsx";
      } else if (action === "copy-image") {
        message = await copyTableImage(table);
      } else {
        const tsv = tableToMatrix(table).map(function(row: any) { return row.join("\t"); }).join("\n");
        // 兜底「复制」：优先纯文本 TSV + HTML 双格式（文档/Excel 粘贴仍保留表格）
        if (navigator.clipboard && (window as any).ClipboardItem) {
          await navigator.clipboard.write([
            new ClipboardItem({
              "text/plain": new Blob([tsv], { type: "text/plain" }),
              "text/html": new Blob(["<table>" + table.innerHTML + "</table>"], { type: "text/html" }),
            }),
          ]);
        } else {
          await navigator.clipboard.writeText(tsv);
        }
        message = "已复制表格";
      }
      target.textContent = "✓ " + message;
    } catch (err) {
      target.textContent = action === "download-xlsx" ? "下载失败" : "复制失败";
    }
    setTimeout(function () {
      target.textContent = original;
      target.disabled = false;
    }, 1500);
  });

  // 点击表格区外时收起「更多」菜单（捕获阶段，避免先命中其它交互）
  document.addEventListener("click", function(e: any) {
    const origin = e.target && e.target.closest ? e.target : (e.target && e.target.parentElement);
    if (origin && origin.closest && origin.closest(".md-table-more")) return;
    closeAllTableMenus();
  }, true);
  document.addEventListener("keydown", function(e: any) {
    if (e.key === "Escape") closeAllTableMenus();
  });


})((window as unknown as { App: any }).App);
