/**
 * md 表格 → PNG canvas 绘制工具（无外部依赖；消息区复制图片使用）
 * - tableToMatrix: DOM table → 二维文本矩阵
 * - wrapCellLines: 单元格文本逐字换行（纯函数，Node 可单测）
 * - computeLayout: 纯函数布局（两轮列宽收敛 + 行高自适应），可在 Node 单测
 * - drawTableCanvas: 浏览器绘制入口（白底黑字网格，超尺寸返回 null）
 *
 * 旧实现固定 24px 行高、超宽文本截断加"…"，长内容被"溢出隐藏"；
 * 现按列宽逐字换行、行高随行数自适应，长文本完整可见。
 */
(function (global) {
  // DOM table → 二维文本矩阵（按 cell.textContent 取数）
  function tableToMatrix(table) {
    return Array.from(table.rows).map(function (row) {
      return Array.from(row.cells).map(function (cell) { return cell.textContent.trim(); });
    });
  }

  // 逐字换行：把文本按可用宽度切成多行（CJK/英文通用；超长英文单词硬切断）
  function wrapCellLines(text, innerWidth, measureWidth) {
    if (!text) return [""];
    const lines = [];
    let current = "";
    for (const ch of String(text)) {
      const cand = current + ch;
      if (current === "" || measureWidth(cand) <= innerWidth) {
        current = cand;
      } else {
        lines.push(current);
        current = ch;
      }
    }
    if (current) lines.push(current);
    return lines.length ? lines : [""];
  }

  /**
   * 纯函数布局。
   * opts:
   *   paddingX/paddingY    单元格左右/上下内边距
   *   lineHeight           单元格内每行文本行高
   *   maxColWidth/minColWidth 列宽上下限
   *   measureWidth(text)   文本宽度测量函数（浏览器传 probe.measureText，测试可注入）
   * 返回 {colWidths, cellLines, rowHeights, width, height}
   * 列宽两轮收敛：先按单行最大文本宽定初始列宽，再按换行后真实最大行宽收紧。
   */
  function computeLayout(matrix, opts) {
    const paddingX = opts.paddingX;
    const paddingY = opts.paddingY;
    const lineHeight = opts.lineHeight;
    const maxColWidth = opts.maxColWidth;
    const minColWidth = opts.minColWidth;
    const measure = opts.measureWidth;
    const colCount = Math.max.apply(null, matrix.map(function (row) { return row.length; }));

    // 第一轮：按单行整段文本宽度设初始列宽
    const colWidths = [];
    for (let c = 0; c < colCount; c++) {
      let width = 0;
      matrix.forEach(function (row) {
        width = Math.max(width, measure(row[c] || ""));
      });
      colWidths.push(Math.min(Math.max(Math.ceil(width) + paddingX * 2, minColWidth), maxColWidth));
    }

    // 第二轮：按初列换行后的真实最大行宽收紧列宽
    for (let c = 0; c < colCount; c++) {
      let widest = 0;
      matrix.forEach(function (row) {
        wrapCellLines(row[c] || "", colWidths[c] - paddingX * 2, measure).forEach(function (line) {
          widest = Math.max(widest, measure(line));
        });
      });
      colWidths[c] = Math.min(Math.max(Math.ceil(widest) + paddingX * 2, minColWidth), maxColWidth);
    }

    // 最终换行（按收敛后的列宽）+ 行高自适应
    const cellLines = [];
    const rowHeights = [];
    matrix.forEach(function (row) {
      const wrapped = [];
      let maxLines = 1;
      for (let c = 0; c < colCount; c++) {
        const lines = wrapCellLines(row[c] || "", colWidths[c] - paddingX * 2, measure);
        wrapped.push(lines);
        maxLines = Math.max(maxLines, lines.length);
      }
      cellLines.push(wrapped);
      rowHeights.push(maxLines * lineHeight + paddingY * 2);
    });

    const width = colWidths.reduce(function (sum, w) { return sum + w; }, 0) + 1;
    const height = rowHeights.reduce(function (sum, h) { return sum + h; }, 0) + 1;
    return { colWidths: colWidths, cellLines: cellLines, rowHeights: rowHeights, width: width, height: height };
  }

  // 浏览器绘制：白底黑字网格 PNG canvas；宽/高超 8000px 返回 null
  function drawTableCanvas(table) {
    const matrix = tableToMatrix(table);
    if (!matrix.length) return null;
    const probe = document.createElement("canvas").getContext("2d");
    const font = "12px " + (getComputedStyle(document.body).fontFamily || "system-ui");
    probe.font = font;
    const layout = computeLayout(matrix, {
      paddingX: 10,
      paddingY: 6,
      lineHeight: 17,
      maxColWidth: 320,
      minColWidth: 48,
      measureWidth: function (t) { return probe.measureText(t).width; },
    });
    if (layout.width > 8000 || layout.height > 8000) return null;
    const colWidths = layout.colWidths;
    const rowHeights = layout.rowHeights;
    const cellLines = layout.cellLines;
    const width = layout.width;
    const height = layout.height;
    const colCount = colWidths.length;

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    // 白底黑字：粘贴到浅色文档/聊天中最通用，不随主题变化
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);
    ctx.font = font;
    ctx.textBaseline = "middle";
    ctx.strokeStyle = "#d0d0d0";
    let y = 0;
    matrix.forEach(function (row, r) {
      const rowHeight = rowHeights[r];
      if (r === 0) {
        ctx.fillStyle = "#f0f0f0";
        ctx.fillRect(0, y, width, rowHeight);
      }
      ctx.fillStyle = r === 0 ? "#111111" : "#222222";
      ctx.font = (r === 0 ? "600 " : "") + font;
      let x = 0;
      for (let c = 0; c < colCount; c++) {
        const lines = cellLines[r][c];
        const blockHeight = lines.length * 17;
        // 多行块在行内垂直居中
        let lineY = y + (rowHeight - blockHeight) / 2 + 17 / 2;
        for (let li = 0; li < lines.length; li++) {
          ctx.fillText(lines[li], x + 10, lineY);
          lineY += 17;
        }
        x += colWidths[c];
      }
      ctx.beginPath();
      ctx.moveTo(0.5, y + rowHeight + 0.5);
      ctx.lineTo(width - 0.5, y + rowHeight + 0.5);
      ctx.stroke();
      y += rowHeight;
    });
    let x = 0;
    for (let c = 0; c <= colCount; c++) {
      ctx.beginPath();
      ctx.moveTo(x + 0.5, 0);
      ctx.lineTo(x + 0.5, height - 0.5);
      ctx.stroke();
      x += colWidths[c] || 0;
    }
    ctx.strokeRect(0.5, 0.5, width - 1, height - 1);
    return canvas;
  }

  const api = { tableToMatrix, wrapCellLines, computeLayout, drawTableCanvas };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.TableCanvas = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
