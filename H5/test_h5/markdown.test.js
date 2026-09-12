const assert = require("node:assert");
const test = require("node:test");

const path = require("path");
// Node 测试也加载真实 KaTeX（UMD 走 CommonJS 分支）：
// 有 katex 时公式用例验证真实渲染路径（与浏览器一致），
// 加载失败（文件缺失等）则公式用例自动覆盖降级路径
try {
  global.katex = require(path.join(__dirname, "..", "js", "vendor", "katex", "katex.min.js"));
} catch (_) { /* 无 katex：降级路径 */ }
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

// ---------- SVG 生成代码块（```svg 双视图控件） ----------

test("render: ```svg 代码块渲染为双视图控件（默认图片视图）", function () {
  Markdown.setMediaResolver(null);
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100"><circle cx="50" cy="50" r="40" fill="red"/></svg>';
  const src = "前文\n\n```svg\n" + svg + "\n```\n\n后文";
  const html = Markdown.render(src);
  assert.ok(html.includes('class="md-svg-block"'), html);
  // 图片视图内联 SVG 原样渲染
  assert.ok(html.includes('<circle cx="50" cy="50" r="40" fill="red"/>'), html);
  // 代码视图：源码经转义进 pre/code
  assert.ok(html.includes('<code class="language-markup">&lt;svg'), html);
  // 三个功能按钮 + 双视图
  assert.ok(html.includes('data-svg-action="view-image"'), html);
  assert.ok(html.includes('data-svg-action="view-code"'), html);
  assert.ok(html.includes('data-svg-action="copy-code"'), html);
  assert.ok(html.includes('data-svg-action="copy-image"'), html);
  // data-svg-code 属性：转义后的源码（还原后可复制）
  assert.ok(html.includes('data-svg-code="&lt;svg'), html);
  // 独立成块（不混入段落 p 标签）
  assert.ok(html.includes("</svg></div></div>"), html);
  // 栅栏遮蔽后不再出现普通 ```svg 代码块复制按钮（不重复渲染）
  assert.ok(!html.includes("```svg"), html);
});

test("render: 一行一张图（多个 svg 块各自独立成行）", function () {
  const block = (fill) => '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10" height="10"><rect width="10" height="10" fill="' + fill + '"/></svg>\n```';
  const html = Markdown.render(block("red") + "\n\n" + block("blue"));
  assert.ok(html.includes('fill="red"'), html);
  assert.ok(html.includes('fill="blue"'), html);
  assert.ok(html.split('class="md-svg-block"').length === 3, html);
});

test("render: 未闭合的 ```svg 栅栏按普通代码块显示（流式中间态）", function () {
  const html = Markdown.render("```svg\n<svg xmlns=\"x\"><circle/></svg>");
  assert.ok(!html.includes("md-svg-block"), html);
  assert.ok(html.includes("codeblock"), html);
});

test("render: ```svg 栅栏内的媒体标签不被误提取", function () {
  Markdown.setMediaResolver(function () { return "/resolved"; });
  const src = '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10" height="10"><text>&lt;image src="a.png"&gt;</text></svg>\n```';
  const html = Markdown.render(src);
  // 栅栏内的伪标签样文本不会被当成媒体控件提取
  assert.ok(!html.includes("md-media"), html);
  assert.ok(html.includes("md-svg-block"), html);
});

test("render: 普通语言代码块含 svg 行内文本不受影响", function () {
  const html = Markdown.render("```python\n# <svg> 示例\nprint(1)\n```");
  assert.ok(html.includes("codeblock"), html);
  assert.ok(!html.includes("md-svg-block"), html);
});

// ---------- KaTeX 数学公式（$…$ / $$…$$ / \(…\) / \[…\]） ----------

test("render: 行内公式 $…$ 渲染为 katex 结构（有 katex 环境时）", function () {
  if (typeof window === "undefined" || !window.katex) return; // Node 环境降级路径另有断言
  const html = Markdown.render("质能方程 $E=mc^2$ 很有名。");
  assert.ok(html.includes("md-math-inline"), html);
  assert.ok(html.includes("katex"), html);
  assert.ok(html.includes('data-tex="E=mc^2"'), html);
});

test("render: 无 katex 环境时公式降级为原文展示（不丢内容）", function () {
  // 显式临时摘除 katex（无论环境是否加载了它），确定性覆盖降级路径
  const saved = global.katex;
  try {
    delete global.katex;
    const html = Markdown.render("质能方程 $E=mc^2$ 很有名。");
    assert.ok(html.includes("md-math-inline"), html);
    assert.ok(html.includes("is-raw"), html);
    assert.ok(html.includes("E=mc^2"), html);
  } finally {
    if (saved) global.katex = saved;
  }
});

test("render: 块级公式 $$…$$ 独立成段", function () {
  const html = Markdown.render("前文\n\n$$\n\\int_0^\\infty e^{-x^2}dx\n$$\n\n后文");
  assert.ok(html.includes("md-math-display"), html);
  assert.ok(html.includes("\\int_0^\\infty"), html);
});

test("render: \\(…\\) 与 \\[…\\] 定界符", function () {
  const html = Markdown.render("行内 \\(a_1+b_2\\) 与块级 \\[x=\\frac{1}{2}\\]");
  assert.ok(html.includes("md-math-inline"), html);
  assert.ok(html.includes("md-math-display"), html);
  assert.ok(html.includes("a_1+b_2"), html);
  assert.ok(html.includes("x=\\frac{1}{2}"), html);
});

test("render: 价格文本（纯数字美元）不误判为公式", function () {
  const html = Markdown.render("这台电脑 $5 到 $8 一个，共 $99。");
  assert.ok(!html.includes("md-math"), html);
  assert.ok(html.includes("$5"), html);
});

test("render: 代码块内的公式样例不被提取", function () {
  const html = Markdown.render("```text\n公式示例 $$x^2$$ 与 $y_1$\n```");
  assert.ok(html.includes("codeblock"), html);
  assert.ok(!html.includes("md-math"), html);
  // 栅栏内容完整保留（遮蔽后已还原）
  assert.ok(html.includes("$$x^2$$"), html);
});

test("render: 公式内的 Markdown 语法字符不被误解析", function () {
  const html = Markdown.render("公式 $a*b*c_1$ 含特殊字符");
  // 公式整体提取为占位符，* 与 _ 不再触发粗体/下标语法
  assert.ok(!html.includes("<strong>"), html);
  if (typeof window !== "undefined" && window.katex) {
    assert.ok(html.includes("katex"), html);
  } else {
    assert.ok(html.includes("a*b*c_1"), html);
  }
});

test("render: 未闭合的公式定界符保持原文（流式中间态）", function () {
  const html = Markdown.render("计算 $x^2 + ");
  assert.ok(!html.includes("md-math"), html);
  assert.ok(html.includes("$x^2 + "), html);
});

// ---------- Mermaid 图表（```mermaid 双视图 + 异步渲染占位） ----------

test("render: ```mermaid 代码块渲染为双视图控件（渲染中占位）", function () {
  const src = "前文\n\n```mermaid\ngraph TD\n    A[开始] --> B[结束]\n```\n\n后文";
  const html = Markdown.render(src);
  // 控件结构与 SVG 同构：切换/复制按钮齐全
  assert.ok(html.includes('data-svg-kind="mermaid"'), html);
  assert.ok(html.includes('data-svg-action="view-code"'), html);
  assert.ok(html.includes('data-svg-action="copy-code"'), html);
  assert.ok(html.includes('data-svg-action="copy-image"'), html);
  // 图片视图为异步渲染占位（带渲染中提示与定位 id），代码视图含源码
  assert.ok(html.includes("md-mermaid-loading"), html);
  assert.ok(html.includes("data-mermaid-holder="), html);
  assert.ok(html.includes("data-mermaid-state=\"pending\""), html);
  assert.ok(html.includes("graph TD"), html);
  // data-svg-code 保存原文（转义一层）
  assert.ok(html.includes('data-svg-code="graph TD'), html);
});

test("render: 一行一张 mermaid 图（多块独立）", function () {
  const block = (dir) => "```mermaid\ngraph " + dir + "\n  A --> B\n```";
  const html = Markdown.render(block("TD") + "\n\n" + block("LR"));
  assert.ok(html.split('data-svg-kind="mermaid"').length === 3, html);
});

test("render: 未闭合的 ```mermaid 栅栏按普通代码块显示（流式中间态）", function () {
  const html = Markdown.render("```mermaid\ngraph TD\n  A --> B");
  assert.ok(!html.includes("md-mermaid-block"), html);
  assert.ok(html.includes("codeblock"), html);
});

test("render: ```mermaid 栅栏内的媒体标签与公式不被误提取", function () {
  Markdown.setMediaResolver(function () { return "/resolved"; });
  const src = "```mermaid\ngraph TD\n  A[<image src=x.png>] --> B\n  C --> D\n```\n\n公式 $a^2$ 在栅栏外";
  const html = Markdown.render(src);
  // 栅栏内伪标签样文本不会被提取为媒体控件；栅栏外公式正常提取
  assert.ok(html.includes("md-svg-block"), html);
  assert.ok(!html.includes('data-media-src="x.png"'), html);
  assert.ok(html.includes("md-math"), html);
});

test("render: mermaid 语法错误内容仍构建控件（异步渲染失败时降级提示）", function () {
  const html = Markdown.render("```mermaid\n完全不是图表语法\n```");
  // 提取按"全部非空行"记录，控件照常构建；错误处理在异步渲染链路
  assert.ok(html.includes('data-svg-kind="mermaid"'), html);
  assert.ok(html.includes("完全不是图表语法"), html);
});

// ---------- Canvas 程序块（```canvas 运行确认 + 沙箱） ----------

test("render: ```canvas 渲染为待运行控件（默认不自动执行）", function () {
  const html = Markdown.render("```canvas\nconsole.log('hi');\n```");
  assert.ok(html.includes('data-svg-kind="canvas"'), html);
  assert.ok(html.includes("md-canvas-block"), html);
  assert.ok(html.includes("data-canvas-state=\"idle\""), html);
  // 无 iframe 注入（沙箱 iframe 只在用户点运行后由 messages.js 创建）
  assert.ok(!html.includes("<iframe"), html);
  // 运行按钮存在且为运行态文案
  assert.ok(html.includes('data-canvas-action="run"'), html);
  assert.ok(html.includes("▶ 运行"), html);
});

test("render: canvas 控件保留完整源码（代码视图与复制）", function () {
  const code = "const ctx = stage.getContext('2d');\nctx.fillRect(0, 0, 100, 100);";
  const html = Markdown.render("```canvas\n" + code + "\n```");
  // 代码视图可见源码（未转义还原前是转义形态，检查关键片段）
  assert.ok(html.includes("stage.getContext"), html);
  assert.ok(html.includes("ctx.fillRect"), html);
  // 运行时脚本与用户代码分离：源码原文存 data-svg-code
  assert.ok(html.includes("data-svg-code="), html);
});

test("render: canvas 与 svg/mermaid 混排时栅栏一一对应", function () {
  const src = [
    "```svg",
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10" height="10"><rect width="10" height="10" fill="red"/></svg>',
    "```",
    "",
    "```canvas",
    "console.log('x');",
    "```",
    "",
    "```mermaid",
    "graph TD\nA-->B",
    "```",
  ].join("\n");
  const html = Markdown.render(src);
  assert.ok(html.includes('data-svg-kind="svg"'), html);
  assert.ok(html.includes('data-svg-kind="canvas"'), html);
  assert.ok(html.includes('data-svg-kind="mermaid"'), html);
  assert.ok(html.split('class="md-svg-block').length === 4, html); // 3 个控件 + split 余数
});

test("render: 未闭合的 ```canvas 栅栏按普通代码块显示（流式中间态）", function () {
  const html = Markdown.render("前文\n```canvas\nconsole.log(1);");
  assert.ok(html.includes("codeblock"), html);
  assert.ok(!html.includes("md-canvas-block"), html);
});

test("render: ```canvas 栅栏内的媒体标签与公式不被误提取", function () {
  Markdown.setMediaResolver(function () { return "/resolved"; });
  const src = '```canvas\nconsole.log("<image src=\\"a.png\\">");\nconst t = "$x^2$";\n```';
  const html = Markdown.render(src);
  assert.ok(!html.includes("md-media"), html);
  assert.ok(!html.includes("md-math"), html);
  assert.ok(html.includes("md-canvas-block"), html);
});

test("api: renderMermaidInto 在无 DOM 环境安全导出", function () {
  assert.strictEqual(typeof Markdown.ensureMermaidLoaded, "function");
  assert.strictEqual(typeof Markdown.renderMermaidInto, "function");
  // Node 无 document：懒加载应走到 script 创建前抛错而非崩溃（防御性）
  if (typeof document === "undefined") {
    return Markdown.ensureMermaidLoaded().then(
      function () { throw new Error("无 DOM 环境不应成功"); },
      function () { /* 预期：加载失败被拒绝 */ }
    );
  }
});

// ---------- Mermaid 缓存 / 重渲染链路（模拟 mermaid 全局） ----------

function makeFakeHolder() {
  const svgEl = {
    getAttribute: function (name) { return name === "viewBox" ? "0 0 10 5" : null; },
    setAttribute: function () {},
    style: {},
  };
  return {
    classList: {
      _set: new Set(),
      add: function (c) { this._set.add(c); },
      remove: function (c) { this._set.delete(c); },
      contains: function (c) { return this._set.has(c); },
    },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    textContent: "",
    querySelector: function (sel) { return sel === "svg" ? svgEl : null; },
  };
}

test("mermaid: 相同源码重复渲染命中缓存（不重复调 mermaid.render）", async function () {
  let renderCalls = 0;
  global.mermaid = {
    initialize: function () {},
    render: function () {
      renderCalls++;
      return Promise.resolve({ svg: '<svg viewBox="0 0 10 5" width="100" height="50"></svg>' });
    },
  };
  try {
    const code = "graph TD\nA-->B";
    const h1 = makeFakeHolder();
    await Markdown.renderMermaidInto("md-mermaid-t1", code, h1, { renderId: "md-mermaid-t1", cachedCode: code });
    assert.strictEqual(renderCalls, 1);
    const h2 = makeFakeHolder();
    await Markdown.renderMermaidInto("md-mermaid-t2", code, h2, { renderId: "md-mermaid-t2", cachedCode: code });
    // 命中缓存：render 不再被调用，svg 直接复用
    assert.strictEqual(renderCalls, 1);
    assert.ok(h2.querySelector("svg"), "holder2 应写入缓存的 svg");
    assert.ok(!h2.classList.contains("md-mermaid-error"), "缓存命中不应带错误态");
  } finally {
    delete global.mermaid;
  }
});

test("mermaid: 重渲染换用当次控件 id 调 mermaid.render", async function () {
  const ids = [];
  global.mermaid = {
    render: function (id) {
      ids.push(id);
      return Promise.resolve({ svg: '<svg viewBox="0 0 4 2"></svg>' });
    },
  };
  try {
    const h = makeFakeHolder();
    // 用例间共享模块级缓存：改用本用例专属源码，避免命中上一个用例的缓存
    await Markdown.renderMermaidInto("md-mermaid-x1", "graph TD\nX1-->Y1", h, { renderId: "md-mermaid-x1", cachedCode: "graph TD\nX1-->Y1" });
    await Markdown.renderMermaidInto("md-mermaid-x2", "graph LR\nX2-->Y2", h, { renderId: "md-mermaid-x2", cachedCode: "graph LR\nX2-->Y2" });
    assert.deepStrictEqual(ids, ["md-mermaid-x1", "md-mermaid-x2"]);
  } finally {
    delete global.mermaid;
  }
});

test("mermaid: 渲染失败不写缓存（重试会真正再渲染）", async function () {
  let calls = 0;
  global.mermaid = {
    render: function () {
      calls++;
      return Promise.reject(new Error("语法错误"));
    },
  };
  try {
    const code = "完全不是图表语法";
    const h1 = makeFakeHolder();
    await Markdown.renderMermaidInto("md-mermaid-e1", code, h1, { renderId: "md-mermaid-e1", cachedCode: code })
      .then(function () { throw new Error("应失败"); }, function () { /* 预期失败 */ });
    assert.strictEqual(calls, 1);
    assert.ok(h1.classList.contains("md-mermaid-error"), "失败应落错误态");
    const h2 = makeFakeHolder();
    await Markdown.renderMermaidInto("md-mermaid-e2", code, h2, { renderId: "md-mermaid-e2", cachedCode: code })
      .then(function () { throw new Error("应失败"); }, function () { /* 预期失败 */ });
    // 失败未缓存：第二次仍真正调用 render（可重试）
    assert.strictEqual(calls, 2);
  } finally {
    delete global.mermaid;
  }
});

test("mermaid: 缓存按 LRU 淘汰（超上限后最早条目重新渲染）", async function () {
  let renderCalls = 0;
  global.mermaid = {
    render: function () {
      renderCalls++;
      return Promise.resolve({ svg: '<svg viewBox="0 0 2 1"></svg>' });
    },
  };
  try {
    for (let i = 0; i < 45; i++) {
      const code = "graph TD\nN" + i + "-->M" + i;
      await Markdown.renderMermaidInto("id-" + i, code, makeFakeHolder(), { renderId: "id-" + i, cachedCode: code });
    }
    const callsAfterFill = renderCalls; // 45 次全新渲染
    // 最早写入的条目已被淘汰：再次渲染应真正调用 render
    const oldCode = "graph TD\nN0-->M0";
    await Markdown.renderMermaidInto("id-again", oldCode, makeFakeHolder(), { renderId: "id-again", cachedCode: oldCode });
    assert.strictEqual(renderCalls, callsAfterFill + 1);
  } finally {
    delete global.mermaid;
  }
});

test("render: mermaid 控件带 ↻ 重渲染按钮（失败自恢复入口）", function () {
  const html = Markdown.render("```mermaid\ngraph TD\nA-->B\n```");
  assert.ok(html.includes('data-svg-action="rerender"'), html);
  assert.ok(html.includes("↻ 重渲染"), html);
});

test("render: canvas 控件带 ↺ 重置按钮", function () {
  const html = Markdown.render("```canvas\nconsole.log(1);\n```");
  assert.ok(html.includes('data-canvas-action="reset"'), html);
  assert.ok(html.includes("↺ 重置"), html);
});
