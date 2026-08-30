const assert = require("node:assert");
const test = require("node:test");

const path = require("path");
const Markdown = require(path.join(__dirname, "..", "js", "markdown.js"));

test("render: 表格单元格内的行内代码竖线不再切断分列", function () {
  const src = [
    "| # | 命令 | 结果 |",
    "|---|------|------|",
    "| 1 | `echo A && dir /b setting` | ✅ 完整返回 |",
    "| 2 | `type x.json \\| findstr /c:\"Q\" && echo DONE` | ✅ 完整返回 |",
    "| 3 | `a | b` 裸竖线 | ✅ |",
  ].join("\n");
  const html = Markdown.render(src);
  // 行内代码完整保留（含竖线），未被切断
  assert.ok(html.includes("<code>echo A &amp;&amp; dir /b setting</code>"), html);
  assert.ok(html.includes("<code>type x.json | findstr /c:&quot;Q&quot; &amp;&amp; echo DONE</code>"), html);
  assert.ok(html.includes("<code>a | b</code>"), html);
  // 每行恰好 3 个单元格
  const rowCount = (html.match(/<tr>/g) || []).length;
  assert.strictEqual(rowCount, 4, "header + 3 rows");
  assert.ok(!html.includes("<td></td><td>findstr"), "不应出现被切断的列");
});

test("render: 行内代码保护代码内的强调/链接语法", function () {
  const html = Markdown.render("前 `code **bold** [x](https://a.b)` 后");
  assert.ok(html.includes("<code>code **bold** [x](https://a.b)</code>"), html);
  assert.ok(!html.includes("<strong>"), html);
  assert.ok(!html.includes("<a "), html);
});

test("render: 转义竖线在普通单元格中还原为 |", function () {
  const src = "| a \\| b | c |\n|---|---|\n| x \\| y | z |";
  const html = Markdown.render(src);
  assert.ok(html.includes("<th>a | b</th>"), html);
  assert.ok(html.includes("<td>x | y</td>"), html);
});

test("render: 普通表格结构不受影响", function () {
  const src = "| # | 命令 | 结果 |\n|---|------|------|\n| 1 | `dir` | ✅ |";
  const html = Markdown.render(src);
  assert.ok(html.includes("<table><thead><tr>"), html);
  assert.ok(html.includes("<th>#</th>"), html);
  assert.ok(html.includes("<td>✅</td>"), html);
});
