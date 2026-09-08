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

// ---------- 媒体伪标签（<image>/<audio>/<video>/<pdf>） ----------

test("render: 伪标签渲染为媒体控件并携带解析后的 URL 与标签原文", function () {
  Markdown.setMediaResolver(function (kind, src) {
    if (src.indexOf("media://") === 0) return "/media/" + src.slice(8);
    if (/^https?:/i.test(src)) return src;
    return "/local?p=" + encodeURIComponent(src);
  });
  const html = Markdown.render('前文\n\n<image src="./out/a.png" alt="图表"></image>\n\n后文');
  assert.ok(html.includes('class="md-media md-media-image"'), html);
  assert.ok(html.includes('data-media-src="./out/a.png"'), html);
  assert.ok(html.includes('data-media-url="/local?p=' + encodeURIComponent("./out/a.png") + '"'), html);
  // 标签原文随控件保存（属性值内再转义一层），供删除时替换历史 JSONL
  assert.ok(html.includes('data-media-raw="&lt;image src=&quot;./out/a.png&quot; alt=&quot;图表&quot;&gt;&lt;/image&gt;"'), html);
  assert.ok(html.includes('<img class="md-media-el"'), html);
  assert.ok(html.includes('alt="图表"'), html);
  // 白名单外属性被丢弃
  assert.ok(!html.includes("onclick"), html);
  // 预览/删除操作按钮
  assert.ok(html.includes('data-media-action="preview"'), html);
  assert.ok(html.includes('data-media-action="delete"'), html);
  assert.ok(html.includes("用户已删除/文件不存在"), html);
});

test("render: audio/video/pdf 控件与 media:// 解析", function () {
  const html = Markdown.render(
    '<audio src="media://a.mp3" title="录音"></audio>\n\n' +
    '<video src="https://x.com/v.mp4"></video>\n\n' +
    '<pdf src="./report.pdf" title="月报"></pdf>'
  );
  assert.ok(html.includes("md-media-audio"), html);
  assert.ok(html.includes('<audio class="md-media-el"'), html);
  assert.ok(html.includes('data-media-url="/media/a.mp3"'), html);
  assert.ok(html.includes("md-media-video"), html);
  assert.ok(html.includes('src="https://x.com/v.mp4"'), html);
  assert.ok(html.includes("md-media-pdf"), html);
  assert.ok(html.includes("📄 PDF · 月报"), html);
});

test("render: 自闭合伪标签与解析器缺省（is-broken）", function () {
  Markdown.setMediaResolver(null);
  const html = Markdown.render('<image src="media://abc.png"/>');
  assert.ok(html.includes("md-media-image is-broken"), html);
  assert.ok(html.includes('data-media-raw="&lt;image src=&quot;media://abc.png&quot;/&gt;"'), html);
});

test("render: 未闭合的半截伪标签按普通文本显示（流式中间态）", function () {
  Markdown.setMediaResolver(function () { return "/resolved"; });
  const html = Markdown.render('加载中 <image src="x.png"> 还有文字');
  assert.ok(!html.includes("md-media"), html);
  assert.ok(html.includes("&lt;image"), html);
});
