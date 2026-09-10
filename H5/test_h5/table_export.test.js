const assert = require("node:assert");
const test = require("node:test");
const path = require("path");

const Markdown = require(path.join(__dirname, "..", "js", "markdown.js"));
const TableCanvas = require(path.join(__dirname, "..", "js", "table_canvas.js"));

function unescapeAttr(value) {
  return value
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

// ---------- 「更多」菜单结构 ----------

test("table menu: 含更多按钮与复制图片 / 复制 Markdown / 下载 Excel 菜单项", function () {
  const src = "| 文件 | 说明 | 链接 |\n|---|---|---|\n| a | `x | y` | [l](https://a.b) |";
  const html = Markdown.render(src);
  assert.ok(html.includes('data-table-action="more"'), html);
  // 「复制图片」收进 menu：顶部常驻区只有「复制」+「更多」，menu 区含三个菜单项
  const moreMarker = html.indexOf('<div class="md-table-more">');
  const menuMarker = html.indexOf('<div class="md-table-menu hidden" role="menu">');
  assert.ok(moreMarker > -1 && menuMarker > moreMarker, "menu 结构必须存在");
  const topPart = html.slice(0, moreMarker);
  assert.ok(topPart.includes('data-table-action="copy"'), topPart);
  assert.ok(!topPart.includes('data-table-action="copy-image"'), topPart);
  const menuPart = html.slice(menuMarker);
  assert.ok(menuPart.includes('data-table-action="copy-md"'), menuPart);
  assert.ok(menuPart.includes('data-table-action="copy-image"'), menuPart);
  assert.ok(menuPart.includes('data-table-action="download-xlsx"'), menuPart);
});

test("table menu: data-table-raw 还原完整原始 md（含竖线转义与行内代码）", function () {
  const lines = [
    "| 文件 | 说明 | 链接 |",
    "|---|---|---|",
    "| a.py | **主入口** `main.py` | [docs](https://example.com) |",
    "| b.csv | 含 \\| 转义 | `x | y` |",
  ];
  const html = Markdown.render(lines.join("\n"));
  const match = html.match(/data-table-raw="([^"]*)"/);
  assert.ok(match, "data-table-raw 必须存在");
  const restored = unescapeAttr(match[1]);
  assert.strictEqual(restored, lines.join("\n"));
});

test("table menu: 属性内特殊字符按 HTML 属性转义（引号二次转义防破坏属性结构）", function () {
  const src = '| a | b |\n|---|---|\n| "引用" | `c<d` |';
  const html = Markdown.render(src);
  // 渲染期整篇先 escapeHtml 一次（" → &quot;），属性里再 esc 一层 → &amp;quot;
  assert.ok(html.includes("&amp;quot;"), html);
  // 按 HTML 解析器语义解码一层后应回到渲染期转义形态
  const raw = html.match(/data-table-raw="([^"]*)"/)[1];
  const onceThrough = unescapeAttr(raw);
  assert.ok(onceThrough.includes("&quot;"), onceThrough);
});

test("table menu: 菜单默认隐藏且 aria 已声明", function () {
  const html = Markdown.render("| a | b |\n|---|---|\n| 1 | 2 |");
  assert.ok(html.includes('md-table-menu hidden"'), html);
  assert.ok(html.includes('aria-haspopup="menu"'), html);
});

// ---------- table_canvas：换行与布局 ----------

// 简化测量：单位宽度 1/字符（"字符个数"语义，与列宽上限同单位）
const measure = function (text) {
  let w = 0;
  for (const ch of String(text)) w += ch.charCodeAt(0) > 0xff ? 2 : 1;
  return w;
};

function layout(matrix, maxColWidth) {
  return TableCanvas.computeLayout(matrix, {
    paddingX: 10,
    paddingY: 6,
    lineHeight: 17,
    maxColWidth: maxColWidth,
    minColWidth: 48,
    measureWidth: measure,
  });
}

test("wrapCellLines: 逐字换行且不丢字符", function () {
  assert.deepStrictEqual(TableCanvas.wrapCellLines("abcdefghij", 4, measure), ["abcd", "efgh", "ij"]);
  assert.deepStrictEqual(TableCanvas.wrapCellLines("一二三四五", 4, measure), ["一二", "三四", "五"]);
  assert.deepStrictEqual(TableCanvas.wrapCellLines("", 4, measure), [""]);
});

test("computeLayout: 超出列宽的长文本换行为多行且拼接无损", function () {
  const long = "src/client/terminal_client.cpp 是客户端主循环实现文件，包含 read_loop 与断线重连逻辑说明";
  const matrix = [["文件", "说明"], ["a.py", "主入口"], ["long_col", long]];
  const cap = 60; // 列宽上限 60（内宽 40），长文本身宽约 90 → 必然换行
  const l = layout(matrix, cap);
  const lines = l.cellLines[2][1];
  assert.ok(lines.length > 1, "长文本应换行为多行: " + JSON.stringify(lines));
  // 换行拼接等于原文（无字符丢失 → 替代旧的截断加省略号行为）
  assert.strictEqual(lines.join(""), long);
  // 行高自适应：行数 × 17 + 上下 padding 12
  assert.strictEqual(l.rowHeights[2], lines.length * 17 + 12);
  // 短行保持单行基准
  assert.strictEqual(l.rowHeights[1], 17 + 12);
  // 长列换行后被收紧到真实内容宽度，且不超过上限
  assert.ok(l.colWidths[1] <= cap, "列宽不应超过上限: " + l.colWidths[1]);
});

test("computeLayout: 列宽触顶 maxColWidth", function () {
  const l = layout([["h"], ["y".repeat(90)]], 100);
  assert.strictEqual(l.colWidths[0], 100);
  // 长连续 ASCII 串被硬切断成多行且总长不变
  const joined = l.cellLines[1][0].join("");
  assert.strictEqual(joined, "y".repeat(90));
});

test("drawTableCanvas 导出存在（浏览器入口）", function () {
  assert.strictEqual(typeof TableCanvas.drawTableCanvas, "function");
});
