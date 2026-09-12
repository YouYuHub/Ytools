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
  // 数学公式占位符（控制字符，与前两者不同前缀）
  const MATH_PH_OPEN = "\u0003";
  const MATH_PH_CLOSE = "\u0003";

  // KaTeX 数学公式渲染（window.katex 由 index.html 提前引入；Node 单测等
  // 无 katex 环境下降级为原文展示，不影响其余渲染）。
  // 支持：$…$ 行内 / $$…$$ 块级 / \(…\) 行内 / \[…\] 块级（模型常见四种写法）。
  // 提取在媒体标签/块级解析之前：公式内的 * _ | 等不能被其它语法误吃。
  // 定界符歧义防护：美元符行内公式要求内部含 LaTeX 命令（\）或上下标/花括号，
  // 纯数字价格（如 $5 到 $8）不会被当成公式；未闭合的定界符保持原文。
  function mathExtract(escapedText) {
    const items = [];   // {display: bool, tex: string}，tex 为未转义原文
    function push(display, tex) {
      items.push({ display: display, tex: tex });
      return MATH_PH_OPEN + (items.length - 1) + MATH_PH_CLOSE;
    }
    let text = String(escapedText || "");

    // 块级 $$…$$（可跨行；贪婪配对，同段两个 $$ 之间为一段公式）
    text = text.replace(/\$\$([\s\S]+?)\$\$/g, function (_m, tex) {
      return push(true, unescapeTagText(tex));
    });
    // 块级 \[…\]
    text = text.replace(/\\\[([\s\S]+?)\\\]/g, function (_m, tex) {
      return push(true, unescapeTagText(tex));
    });
    // 行内 \(…\)
    text = text.replace(/\\\(([\s\S]+?)\\\)/g, function (_m, tex) {
      return push(false, unescapeTagText(tex));
    });
    // 行内 $…$：内部需含 LaTeX 特征（\命令、^、_、花括号包内容）才认作公式，
    // 避免「两个价格 $5 … $8」被误判；tex 内不允许换行（行内公式单行）
    text = text.replace(/\$([^$\n]+)\$/g, function (match, tex) {
      const hasCommand = /\\[a-zA-Z]+/.test(tex);
      const hasScript = /[\^_]\{?[^{}\s]/.test(tex);
      if (!hasCommand && !hasScript) return match;
      return push(false, unescapeTagText(tex));
    });
    return { text: text, items: items };
  }

  // 占位符 → KaTeX HTML；katex 缺失/渲染失败时降级为原文（code 样式）。
  // 原文随 data-tex 保存（转义一层），便于后续扩展「复制源码」等交互。
  function buildMathHtml(display, tex) {
    const esc = escapeHtml;
    let html = "";
    let failed = false;
    if (global.katex && typeof global.katex.renderToString === "function") {
      try {
        html = global.katex.renderToString(tex, {
          displayMode: display,
          throwOnError: false,
          strict: false,
          output: "html",
        });
      } catch (_) {
        failed = true;
      }
    } else {
      failed = true;
    }
    const cls = display ? "md-math md-math-display" : "md-math md-math-inline";
    if (failed || !html) {
      // 降级：等宽原文 + 占位说明，保持版式可读
      const body = '<code class="md-math-raw">' + esc(tex) + "</code>";
      return '<span class="' + cls + ' is-raw" data-tex="' + esc(tex) + '">' + body + "</span>";
    }
    return '<span class="' + cls + '" data-tex="' + esc(tex) + '">' + html + "</span>";
  }

  function restoreMathPlaceholders(html, items) {
    if (!items.length) return html;
    const RE = new RegExp(MATH_PH_OPEN + "(\\d+)" + MATH_PH_CLOSE, "g");
    // 块级公式独占一行时替换整个 <p>，避免 display 块嵌进段落
    let out = html.replace(new RegExp("<p>" + MATH_PH_OPEN + "(\\d+)" + MATH_PH_CLOSE + "</p>", "g"),
      function (_, idx) {
        const item = items[Number(idx)];
        return item ? buildMathHtml(item.display, item.tex) : "";
      });
    return out.replace(RE, function (_, idx) {
      const item = items[Number(idx)];
      return item ? buildMathHtml(item.display, item.tex) : "";
    });
  }

  // 媒体伪标签解析器：由 App 在启动时注入（media.js），把标签 src 解析为
  // 可访问 URL（media:// → 会话媒体端点；http(s) 原样；其它按本地路径 →
  // /file/get_local_file）。未注入时控件仍渲染但无法加载。
  let mediaResolver = null;

  function setMediaResolver(fn) { mediaResolver = typeof fn === "function" ? fn : null; }

  // 模型输出的伪标签（转义后形态）：&lt;image ...&gt;...&lt;/image&gt; 或自闭合。
  // 属性值以 &quot; 包裹（escapeHtml 把 " 转义为 &quot;）。
  const MEDIA_TAG_RE = /&lt;(image|audio|video|pdf)((?:[^&]|&(?!gt;))*?)(?:\/&gt;|&gt;([\s\S]*?)&lt;\/\1&gt;)/g;
  const MEDIA_ATTR_RE = /([a-zA-Z_][\w:-]*)\s*=\s*&quot;((?:(?!&quot;).)*)&quot;/g;

  // 解析 <svg> 开标签里的 viewBox，返回宽高比（w/h；非法/缺失返回 null）
  function viewBoxAspectRatio(open) {
    const m = /\bviewBox\s*=\s*["']\s*([\d.eE+-]+)[\s,]+([\d.eE+-]+)[\s,]+([\d.eE+-]+)[\s,]+([\d.eE+-]+)/i.exec(open);
    if (!m) return null;
    const w = parseFloat(m[3]);
    const h = parseFloat(m[4]);
    return w > 0 && h > 0 && Number.isFinite(w) && Number.isFinite(h)
      ? Number((w / h).toFixed(6)) : null;
  }

  // 把「填满宽度 + 超长钳制」声明合并进 <svg> 开标签的 style（不覆盖其它内联样式）：
  // aspect-ratio + width:min(100%, 170vh*比例) —— 默认填满控件宽度（左右仅剩
  // 视图自身的 4px 窄边距），高度按比例联动；仅当填满宽度后高度会超过约
  // 1.7 屏（极竖长内容）才按高度收窄，避免页面被无限拉高。
  // 注意不能把这里钳制值压回 70vh：一旦触顶，等比内容只能居中缩小，
  // 两侧会重新出现大片透明留白（"图片只占中间一部分宽度"）
  function applyAdaptiveStyle(open, ar) {
    const styleVal = "aspect-ratio:" + ar +
      ";width:min(100%,calc(170vh*" + ar + "));height:auto";
    const styleMatch = /\sstyle\s*=\s*["']([^"']*)["']/i.exec(open);
    if (styleMatch) {
      // 追加到原值末尾：同名声明后写的生效，净化源码自带的 width/height 干扰
      open = open.replace(styleMatch[0],
        ' style="' + styleMatch[1].replace(/;\s*$/, "") + ";" + styleVal + '"');
    } else {
      open = open.replace(/<svg/i, '<svg style="' + styleVal + '"');
    }
    return open;
  }

  // 内联视图专用 SVG 准备（不影响代码视图与「复制代码」的原文）：
  // 1) 流式铺满：去掉根节点固定 width/height（缺 viewBox 时由其转出），
  //    否则 CSS height:auto 会取属性内在高度导致视口压扁、内容居中留白；
  // 2) 触顶收字框：按 viewBox 比例注入 aspect-ratio 自适应样式（见 applyAdaptiveStyle）；
  // 3) 渲染净化：移除 <script>、on* 事件属性与 javascript: 链接——
  //    innerHTML 插入的 SVG <script> 会执行，这里是渲染前唯一防线；
  //    代码视图/复制始终保留完整原文（净化只作用于图片视图）。
  function makeDisplaySvg(rawSvg) {
    let svg = String(rawSvg);
    svg = svg.replace(/<script\b[\s\S]*?<\/script\s*>/gi, "")
      .replace(/<script\b[^>]*\/\s*>/gi, "");
    const openMatch = svg.match(/<svg\b[^>]*>/i);
    if (openMatch) {
      let open = openMatch[0];
      const w = /\bwidth\s*=\s*["']([\d.]+)/i.exec(open);
      const h = /\bheight\s*=\s*["']([\d.]+)/i.exec(open);
      if (!/\bviewBox\s*=/i.test(open) && w && h) {
        open = open.replace(/<svg/i, '<svg viewBox="' + w[1] + " " + h[1] + '"');
      }
      const ar = viewBoxAspectRatio(open);
      if (ar) open = applyAdaptiveStyle(open, ar);
      open = open.replace(/\s(width|height)\s*=\s*["'][^"']*["']/gi, "");
      svg = svg.replace(openMatch[0], open);
    }
    svg = svg.replace(/\son[a-z]+\s*=\s*["'][^"']*["']/gi, "");
    svg = svg.replace(/((?:xlink:)?href)\s*=\s*["']\s*javascript:[^"']*["']/gi, '$1="#"');
    return svg;
  }

  // SVG 代码 → 双视图控件：头部（名称 + 代码/图片切换 + 复制代码 + 复制图片），
  // 主体「代码视图」（语法高亮 pre/code，与普通代码块同款复制语义）与
  // 「图片视图」（inline SVG，整行一张独占显示）二选一显示，默认看图片。
  // 原始 SVG 源码经 escapeHtml 后存入 data-svg-code，复制/切图时还原使用；
  // alt 属性作为控件名称，缺省用「SVG 图片」。
  function buildSvgWidget(svgCode, attrs) {
    const esc = escapeHtml;
    const name = (attrs.alt || attrs.title || "").trim() || "SVG 图片";
    // 图片视图：净化后的 SVG 内联进文档（去除固定尺寸实现流式铺满、
    // 移除脚本与事件属性防执行）；源码原文完整保留在代码视图与 data-svg-code
    const svgInline = '<div class="md-svg-view" data-svg-view="image">' + makeDisplaySvg(svgCode) + "</div>";
    return (
      '<div class="md-svg-block" data-svg-kind="svg" data-svg-code="' + esc(svgCode) + '">' +
      '<div class="md-svg-head">' +
      '<span class="md-svg-name">' + esc(name) + "</span>" +
      '<div class="md-svg-viewswitch">' +
      '<button type="button" class="md-svg-tab is-active" data-svg-action="view-image">图片</button>' +
      '<button type="button" class="md-svg-tab" data-svg-action="view-code">代码</button>' +
      "</div>" +
      '<div class="md-svg-actions">' +
      '<button type="button" class="md-svg-btn" data-svg-action="copy-code">复制代码</button>' +
      '<button type="button" class="md-svg-btn" data-svg-action="copy-image">复制图片</button>' +
      "</div>" +
      "</div>" +
      '<div class="md-svg-code" data-svg-view="code" hidden="">' +
      '<pre class="md-svg-pre"><code class="language-markup">' + esc(svgCode) + "</code></pre>" +
      "</div>" +
      svgInline +
      "</div>"
    );
  }

  // Mermaid 图表 → 双视图控件：与 SVG 控件同构（代码/图片切换 + 复制代码 +
  // 复制图片），差异在于「图片视图」由 Mermaid.js **异步**渲染——初始放
  // 「渲染中」占位，messages.js 的懒加载链路完成 mermaid.render 后回填 SVG；
  // 语法错误时标记 failed 并显示错误说明（代码视图始终可看）。
  // data-mermaid-id/state 供异步渲染链路定位与去重；源码原文存 data-svg-code
  // （复制代码复用 SVG 控件的复制逻辑，无需新增交互）。
  let mermaidWidgetSeq = 0;
  function buildMermaidWidget(code) {
    const esc = escapeHtml;
    const id = "md-mermaid-" + (++mermaidWidgetSeq);
    return (
      '<div class="md-svg-block md-mermaid-block" data-svg-kind="mermaid"' +
      ' data-svg-code="' + esc(code) + '"' +
      ' data-mermaid-id="' + id + '" data-mermaid-state="pending">' +
      '<div class="md-svg-head">' +
      '<span class="md-svg-name">Mermaid 图</span>' +
      '<div class="md-svg-viewswitch">' +
      '<button type="button" class="md-svg-tab is-active" data-svg-action="view-image">图片</button>' +
      '<button type="button" class="md-svg-tab" data-svg-action="view-code">代码</button>' +
      "</div>" +
      '<div class="md-svg-actions">' +
      '<button type="button" class="md-svg-btn" data-svg-action="copy-code">复制代码</button>' +
      '<button type="button" class="md-svg-btn" data-svg-action="copy-image">复制图片</button>' +
      "</div>" +
      "</div>" +
      '<div class="md-svg-code" data-svg-view="code" hidden="">' +
      '<pre class="md-svg-pre"><code>' + esc(code) + "</code></pre>" +
      "</div>" +
      '<div class="md-svg-view" data-svg-view="image">' +
      '<div class="md-mermaid-loading" data-mermaid-holder="' + id + '">Mermaid 渲染中…</div>' +
      "</div>" +
      "</div>"
    );
  }

  // Canvas 程序块 → 「待运行」控件：```canvas 栅栏内是一段 JS 绘图脚本，
  // **默认不执行**——先展示预览占位与代码视图，用户点「运行」确认后才在
  // 沙箱 iframe 中执行（运行时与消息交互在 messages.js 的沙箱链路完成）。
  // data-canvas-code 存源码原文；图片视图初始为「未运行」占位，代码视图
  // 与 SVG 控件同款（等宽 + 滚动 + 复制代码）；「截图」按钮在运行后可用，
  // 复用 SVG 控件的导出链路（canvas 像素直接 toBlob）。
  let canvasWidgetSeq = 0;
  function buildCanvasWidget(code) {
    const esc = escapeHtml;
    const id = "md-canvas-" + (++canvasWidgetSeq);
    return (
      '<div class="md-svg-block md-canvas-block" data-svg-kind="canvas"' +
      ' data-svg-code="' + esc(code) + '"' +
      ' data-canvas-id="' + id + '" data-canvas-state="idle">' +
      '<div class="md-svg-head">' +
      '<span class="md-svg-name">Canvas 程序</span>' +
      '<div class="md-svg-viewswitch">' +
      '<button type="button" class="md-svg-tab is-active" data-svg-action="view-image">画布</button>' +
      '<button type="button" class="md-svg-tab" data-svg-action="view-code">代码</button>' +
      "</div>" +
      '<div class="md-svg-actions">' +
      '<button type="button" class="md-svg-btn md-canvas-run" data-canvas-action="run">▶ 运行</button>' +
      '<button type="button" class="md-svg-btn" data-canvas-action="shot" title="导出当前画布为 PNG">截图</button>' +
      '<button type="button" class="md-svg-btn" data-svg-action="copy-code">复制代码</button>' +
      "</div>" +
      "</div>" +
      '<div class="md-svg-code" data-svg-view="code" hidden="">' +
      '<pre class="md-svg-pre"><code class="language-javascript">' + esc(code) + "</code></pre>" +
      "</div>" +
      '<div class="md-svg-view md-canvas-view" data-svg-view="image">' +
      '<div class="md-canvas-placeholder">未运行 · 点击「▶ 运行」在沙箱中执行脚本</div>' +
      "</div>" +
      "</div>"
    );
  }

  // 按栅栏语言分发构建双视图控件
  function buildFenceWidget(kind, code) {
    if (kind === "mermaid") return buildMermaidWidget(code);
    if (kind === "canvas") return buildCanvasWidget(code);
    return buildSvgWidget(code, {});
  }

  // ---------- Mermaid 懒加载 + 异步渲染（与 messages.js 回填链路配合） ----------
  // mermaid.min.js 5.5MB：不做首屏静态引入，首次出现 ```mermaid 图时才注入
  // <script> 动态加载；加载完成后 initialize 一次（startOnLoad=false、
  // securityLevel=strict 禁用 HTML 标签与点击回调）。
  const MERMAID_BASE_CONFIG = {
    startOnLoad: false,
    securityLevel: "strict",
    theme: "default",
  };
  let mermaidLoadPromise = null;
  let mermaidInitPromise = null;

  function ensureMermaidLoaded() {
    if (global.mermaid && typeof global.mermaid.render === "function") {
      if (!mermaidInitPromise) {
        mermaidInitPromise = Promise.resolve().then(function () {
          if (global.mermaid.initialize) global.mermaid.initialize(MERMAID_BASE_CONFIG);
        });
      }
      return mermaidInitPromise;
    }
    if (mermaidLoadPromise) return mermaidLoadPromise;
    mermaidLoadPromise = new Promise(function (resolve, reject) {
      const script = document.createElement("script");
      script.src = "js/vendor/mermaid/mermaid.min.js";
      script.onload = function () {
        try {
          if (global.mermaid && global.mermaid.initialize) {
            global.mermaid.initialize(MERMAID_BASE_CONFIG);
          }
          resolve();
        } catch (err) {
          reject(err);
        }
      };
      script.onerror = function () {
        mermaidLoadPromise = null;
        reject(new Error("mermaid.min.js 加载失败"));
      };
      (document.head || document.documentElement).appendChild(script);
    });
    return mermaidLoadPromise;
  }

  // mermaid.render 包装：结果 SVG 写入 holder 并做自适应宽度处理；
  // 渲染失败时 holder 显示错误说明并继续抛出（调用方可做状态标记）。
  function renderMermaidInto(id, code, holder) {
    return ensureMermaidLoaded().then(function () {
      return Promise.resolve(global.mermaid.render(id, code)).then(function (result) {
        const svg = (result && result.svg) || "";
        if (!svg) throw new Error("渲染结果为空");
        holder.innerHTML = svg;
        const svgEl = holder.querySelector("svg");
        if (svgEl) {
          // 与 SVG 控件同款自适应（触顶收字框）：按 viewBox 比例把宽度钳到
          // min(100%, 70vh*比例)，高度随比例联动——宽图触 70vh 上限时
          // 容器跟着收窄，消除 100% 宽 + 居中缩放的两侧透明留白
          const vb = String(svgEl.getAttribute("viewBox") || "")
            .trim().split(/[\s,]+/).map(Number);
          if (vb.length === 4 && vb[2] > 0 && vb[3] > 0 &&
            Number.isFinite(vb[2]) && Number.isFinite(vb[3])) {
            // 与 SVG 控件同款：默认填满宽度，仅极竖长（>1.7 屏）才按高度收窄
            const ar = Number((vb[2] / vb[3]).toFixed(6));
            svgEl.style.aspectRatio = String(ar);
            svgEl.style.width = "min(100%, calc(170vh * " + ar + "))";
            svgEl.style.maxWidth = "100%";
            svgEl.style.height = "auto";
          } else {
            svgEl.style.maxWidth = "100%";
            svgEl.style.width = "100%";
            svgEl.style.height = "auto";
          }
          svgEl.style.display = "block";
          svgEl.style.margin = "0 auto";
        }
        return true;
      });
    }).catch(function (err) {
      holder.classList.add("md-mermaid-error");
      holder.textContent = "Mermaid 渲染失败：" + ((err && err.message) || "语法错误");
      throw err;
    });
  }

  // SVG/Mermaid/Canvas 生成代码块（转义后形态）：```svg / ```mermaid / ```canvas，
  // 块内是一份完整源码。栅栏遮蔽发生在媒体标签提取/块级解析之前；未闭合的半截
  // 栅栏不处理（流式中间态按普通代码块展示，闭合后自动变控件）。
  const MEDIA_FENCE_RE = /^```(svg|mermaid|canvas)\s*$/;

  function unescapeTagText(text) {
    return String(text)
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&amp;/g, "&");
  }

  // 还原被 escapeHtml 转义的文本到原始内容（用于 data-table-raw 等属性回读）
  function unescapeHtml(text) {
    return String(text)
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
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
  // 未闭合的半截标签按普通文本显示。
  // 随后做 ```svg / ```mermaid 代码块栅栏遮蔽：块内文本置空，媒体标签提取/
  // 块级解析都不会命中栅栏内内容，代码块解析按完整栅栏正常渲染（提取函数
  // 只遮蔽并记录源码，双视图控件在块级代码块分支构建）。
  function extractMediaTags(escapedText) {
    const widgets = [];
    const fenceBlocks = [];  // 各媒体栅栏 {kind:"svg"|"mermaid", code}，文档顺序
    const text = escapedText.replace(MEDIA_TAG_RE, function (match, kind, attrText, _body) {
      widgets.push(buildMediaWidget(kind, parseMediaAttrs(attrText), unescapeTagText(match)));
      return MEDIA_PH_OPEN + (widgets.length - 1) + MEDIA_PH_OPEN;
    });
    const lines = text.split("\n");
    let fenceStart = -1;
    let fenceKind = "";
    let buf = [];
    for (let li = 0; li <= lines.length; li++) {
      const isEnd = li === lines.length;
      const line = isEnd ? null : lines[li];
      if (fenceStart < 0) {
        if (!isEnd) {
          const m = line.match(MEDIA_FENCE_RE);
          if (m) {
            fenceStart = li;
            fenceKind = m[1].toLowerCase();
            buf = [line];
          }
        }
        continue;
      }
      if (isEnd) {
        // 未闭合栅栏（流式中间态）：不遮蔽、不提取，整体按普通代码块展示；
        // 栅栏闭合后（流结束或下一帧）才会升级为双视图控件
        fenceStart = -1;
        fenceKind = "";
        buf = [];
        continue;
      }
      if (/^```/.test(line)) {
        // 栅栏收尾：闭合
        buf.push(line);
        const body = buf.slice(1, buf.length - 1);
        let code = "";
        if (fenceKind === "svg") {
          // SVG：取第一个 <svg 行 → 最后一个 </svg> 行（栅栏内说明文字忽略）
          let firstSvg = -1;
          let lastSvgEnd = -1;
          for (let k = 0; k < body.length; k++) {
            if (firstSvg < 0 && body[k].indexOf("&lt;svg") !== -1) firstSvg = k;
            if (body[k].indexOf("&lt;/svg&gt;") !== -1) lastSvgEnd = k;
          }
          if (firstSvg >= 0 && lastSvgEnd >= firstSvg) {
            code = body.slice(firstSvg, lastSvgEnd + 1).join("\n");
          }
        } else {
          // Mermaid / Canvas：取全部非空行（整体源码，去掉首尾空行）
          let s = 0;
          let e = body.length - 1;
          while (s <= e && body[s].trim() === "") s++;
          while (e >= s && body[e].trim() === "") e--;
          if (s <= e) code = body.slice(s, e + 1).join("\n");
        }
        fenceBlocks.push({ kind: fenceKind, code: code });
        for (let k = 0; k < buf.length; k++) {
          // 首尾栅栏行保留，其余块内行遮蔽为空（不删行，保持行号对齐）
          lines[fenceStart + k] = k === 0 || k === buf.length - 1 ? buf[k] : "";
        }
        fenceStart = -1;
        fenceKind = "";
        buf = [];
        continue;
      }
      buf.push(line);
    }
    return { text: lines.join("\n"), widgets: widgets, fenceBlocks: fenceBlocks };
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

  // 表格操作按钮组：常驻「复制」+「更多」下拉（复制 Markdown / 复制图片 / 下载 Excel）。
  // more 菜单展开/收起与点击行为在 app/messages.js 的事件委托中处理；
  // 原始 md 表格文本经 escapeHtml 后存入 data-table-raw，复制/下载时还原使用
  //（复制图片用不到 raw，保持菜单结构统一）。
  function buildTableActions(rawTable) {
    const rawAttr = escapeHtml(rawTable);
    return (
      '<div class="md-table-actions">' +
      '<button class="md-table-btn" type="button" data-table-action="copy">复制</button>' +
      '<div class="md-table-more">' +
      '<button class="md-table-btn md-table-more-btn" type="button" data-table-action="more"' +
      ' aria-haspopup="menu" aria-expanded="false">更多 ▾</button>' +
      '<div class="md-table-menu hidden" role="menu">' +
      '<button type="button" role="menuitem" data-table-action="copy-md" data-table-raw="' + rawAttr + '">复制 Markdown</button>' +
      '<button type="button" role="menuitem" data-table-action="copy-image">复制图片</button>' +
      '<button type="button" role="menuitem" data-table-action="download-xlsx" data-table-raw="' + rawAttr + '">下载 Excel</button>' +
      "</div>" +
      "</div>" +
      "</div>"
    );
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
    // 独立滚动块：宽表格在自身容器内水平滚动，不撑破消息宽度
    return (
      '<div class="md-table-block">' +
      '<div class="md-table-scroll">' + html + "</div>" +
      buildTableActions(lines.join("\n")) +
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

  // 代码栅栏内容遮蔽（供数学公式提取防误伤）：``` 围栏内的行临时替换为
  // \u0004 重复串（控制字符，公式/媒体/块级解析都不会命中），提取完成后
  // 按记录原样还原。返回待还原记录 [{idx, orig}]；流式未闭合栅栏遮到末尾。
  function maskFenceLines(lines) {
    const records = [];
    let open = -1;
    for (let i = 0; i < lines.length; i++) {
      if (open < 0) {
        if (/^```/.test(lines[i])) open = i;
        continue;
      }
      if (/^```/.test(lines[i])) {
        for (let k = open + 1; k < i; k++) {
          records.push({ idx: k, orig: lines[k] });
          lines[k] = MATH_PH_OPEN === "\u0003" ? "\u0004".repeat(lines[k].length) : lines[k];
        }
        open = -1;
      }
    }
    if (open >= 0) {
      for (let k = open + 1; k < lines.length; k++) {
        records.push({ idx: k, orig: lines[k] });
        lines[k] = "\u0004".repeat(lines[k].length);
      }
    }
    return records;
  }

  function render(src) {
    const escaped = escapeHtml(String(src || ""));
    const media = extractMediaTags(escaped);
    let workText = media.text;
    let math = { text: workText, items: [] };
    // 数学公式提取：普通代码栅栏内容先遮蔽（防 $$…$$ 示例被误提取），
    // 提取后按记录还原栅栏原文，块级解析拿到的是完整栅栏内容
    const fenceLines = workText.split("\n");
    const fenceRecords = maskFenceLines(fenceLines);
    if (fenceRecords.length) {
      math = mathExtract(fenceLines.join("\n"));
      const restored = math.text.split("\n");
      fenceRecords.forEach(function (r) { restored[r.idx] = r.orig; });
      workText = restored.join("\n");
    } else {
      math = mathExtract(workText);
      workText = math.text;
    }
    const text = workText;
    const lines = text.split("\n");
    const html = [];
    let i = 0;
    let svgBlockIdx = 0;   // 已消费的 ```svg 栅栏序号（与 svgBlocks 文档顺序对应）

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
        // SVG/Mermaid/Canvas 生成代码块：栅栏内文本已被置空遮蔽；源码取自
        // fenceBlocks（与文档中媒体栅栏按序一一对应，空内容栅栏同样消费
        // 序号），无完整内容时退回普通代码块展示
        const langKey = lang.toLowerCase();
        if (langKey === "svg" || langKey === "mermaid" || langKey === "canvas") {
          if (svgBlockIdx < media.fenceBlocks.length) {
            const block = media.fenceBlocks[svgBlockIdx++];
            if (block.code) {
              html.push(buildFenceWidget(block.kind, unescapeTagText(block.code)));
              continue;
            }
          } else {
            svgBlockIdx++;
          }
        }
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
      restoreMathPlaceholders(
        restoreMediaPlaceholders(html.join(""), media.widgets),
        math.items
      ) + "</div>";
  }

  const api = {
    render,
    setMediaResolver,
    unescapeHtml,
    buildFenceWidget,
    ensureMermaidLoaded,
    renderMermaidInto,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.Markdown = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
