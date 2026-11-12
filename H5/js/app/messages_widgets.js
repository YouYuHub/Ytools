"use strict";
/**
 * 媒体伪标签/四控件缩放/SVG 双视图块（原 messages.js 3191-3582 行拆出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 */
(function (App) {
    "use strict";
    const { el, chatInner } = App;
    // ---------- 媒体伪标签控件（<image>/<audio>/<video>/<pdf>）交互 ----------
    // 删除：把会话历史 JSONL 中该标签原文替换为"用户已删除/文件不存在"，
    // 之后历史回放与回传给模型的内容都不再引用该文件
    async function deleteMediaWidget(widget) {
        const rawTag = widget.dataset.mediaRaw || "";
        if (!rawTag) {
            App.toast("缺少标签原文，无法从历史中移除");
            return;
        }
        if (!window.confirm("确定删除该媒体引用吗？\n历史记录中将替换为「用户已删除/文件不存在」。"))
            return;
        try {
            const result = await API.removeMediaTag(App.state.sessionId, rawTag);
            if (result && result.state === "succeed") {
                const placeholder = el("div", "md-media-removed", "用户已删除/文件不存在");
                widget.replaceWith(placeholder);
                App.toast("已从历史记录中移除该媒体引用");
            }
            else {
                App.toast((result && result.describe) || "移除失败");
            }
        }
        catch (err) {
            App.toast("移除失败：" + err.message);
        }
    }
    // 事件委托：markdown 渲染会重建节点，点击行为必须挂在容器上
    chatInner.addEventListener("click", function (e) {
        const mediaBtn = e.target.closest("[data-media-action]");
        if (!mediaBtn)
            return;
        const widget = mediaBtn.closest(".md-media");
        if (!widget)
            return;
        const action = mediaBtn.dataset.mediaAction;
        if (action === "delete") {
            deleteMediaWidget(widget);
            return;
        }
        if (action === "preview") {
            const kind = widget.dataset.mediaKind || "image";
            const url = widget.dataset.mediaUrl || "";
            const src = widget.dataset.mediaSrc || "";
            if (!url) {
                App.toast("文件不存在或路径无法解析：" + src);
                return;
            }
            App.openMediaPreview({ type: kind, src: url, title: src });
        }
    });
    // ---------- 四控件（svg/mermaid/canvas/chart）显示缩放 ----------
    // 缩放状态 = 块的 data-zoom（倍率）+ CSS 变量 --md-zoom：
    // - svg/mermaid 图片视图：内联宽度表达式乘 var(--md-zoom)，放大后由视图
    //   容器（overflow:auto）滚动查看、缩小则居中（margin:0 auto）；
    // - canvas 沙箱：iframe 尺寸不变，倍率经 postMessage 下发给沙箱，stage
    //   元素尺寸乘倍率（object-fit:contain 等比放大 + body 滚动）。
    // 导出 PNG 不受查看缩放影响：svgBlockToPngBlob 除以当前倍率还原基准尺寸。
    const ZoomUtils = window.ZoomUtils || {
        normalizeScale: function (s) {
            const n = Number(s);
            return Number.isFinite(n) && n > 0 ? n : 1;
        },
        stepScale: function (s, d) {
            const n = Number(s) > 0 ? Number(s) : 1;
            return d > 0 ? n * 1.1 : n / 1.1;
        },
        canStep: function () { return true; },
    };
    function zoomScaleOf(block) {
        const s = block && block.dataset ? parseFloat(block.dataset.zoom) : NaN;
        return Number.isFinite(s) && s > 0 ? s : 1;
    }
    function updateZoomControls(block) {
        const scale = zoomScaleOf(block);
        const resetBtn = block.querySelector(".md-zoom-reset");
        if (resetBtn)
            resetBtn.textContent = Math.round(scale * 100) + "%";
        const outBtn = block.querySelector('[data-zoom-action="out"]');
        const inBtn = block.querySelector('[data-zoom-action="in"]');
        if (outBtn)
            outBtn.disabled = !ZoomUtils.canStep(scale, -1);
        if (inBtn)
            inBtn.disabled = !ZoomUtils.canStep(scale, +1);
    }
    // 应用倍率：写 dataset 与 CSS 变量；canvas 运行中同步下发沙箱；
    // 全屏中的 svg/mermaid 由 syncSvgFullscreenLayout 按新倍率重算 px 宽度
    function applyZoomScale(block, scale) {
        if (!block)
            return;
        const next = ZoomUtils.normalizeScale(scale);
        block.dataset.zoom = String(next);
        block.style.setProperty("--md-zoom", String(next));
        block.classList.toggle("is-zoomed", next !== 1);
        updateZoomControls(block);
        if (document.fullscreenElement === block)
            App.syncSvgFullscreenLayout();
        if (block.classList.contains("md-canvas-block")) {
            const frame = block.querySelector(".md-canvas-frame");
            if (frame && frame.contentWindow) {
                frame.contentWindow.postMessage({
                    type: "canvas-zoom",
                    id: block.dataset.canvasId || "",
                    zoom: next,
                }, "*");
            }
        }
    }
    function stepZoom(block, dir) {
        applyZoomScale(block, ZoomUtils.stepScale(zoomScaleOf(block), dir));
    }
    // 缩放按钮（三控件共用）：独立委托，早于 SVG/Canvas 两套 action 委托判断
    // （缩放按钮无 data-svg-action/data-canvas-action，天然不冲突）
    chatInner.addEventListener("click", function (e) {
        const btn = e.target.closest("[data-zoom-action]");
        if (!btn)
            return;
        const block = btn.closest(".md-svg-block");
        if (!block)
            return;
        const action = btn.dataset.zoomAction;
        if (action === "in")
            stepZoom(block, +1);
        else if (action === "out")
            stepZoom(block, -1);
        else if (action === "reset")
            applyZoomScale(block, 1);
    });
    // 滚轮缩放（svg/mermaid/chart 图片视图）：非全屏只响应 Ctrl+滚轮，
    // 普通滚轮留给页面滚动；全屏内普通滚轮直接按档位步进（Ctrl 同样有效），
    // 到边界（25%/800%）或纯水平滚动时不拦截，放行给容器滚动，
    // 避免滚轮被“吸住”。canvas 在跨源 iframe 内，滚轮事件到不了父文档，
    // 其缩放走按钮/沙箱消息（此处防御性跳过）。
    chatInner.addEventListener("wheel", function (e) {
        if (!e.deltaY)
            return;
        const view = e.target.closest ? e.target.closest(".md-svg-view") : null;
        if (!view)
            return;
        const block = view.closest(".md-svg-block");
        if (!block || block.classList.contains("md-canvas-block"))
            return;
        const dir = e.deltaY < 0 ? +1 : -1;
        if (document.fullscreenElement !== block) {
            if (!e.ctrlKey)
                return;
            e.preventDefault();
            stepZoom(block, dir);
            return;
        }
        var cur = zoomScaleOf(block);
        if (!ZoomUtils.canStep(cur, dir))
            return;
        var next = ZoomUtils.stepScale(cur, dir);
        if (!(next > 0) || next === cur)
            return;
        e.preventDefault();
        applyZoomScale(block, next);
    }, { passive: false });
    // 放大后按住拖动平移（与图片预览一致的手感）：滚动容器 scroll 方案，
    // 无需 transform 数学；仅放大档（>100%）启用，缩小/100% 不拦截。
    // document 级 move/up：移出视图也能继续拖；视图重建后自动失效。
    let svgPanDrag = null;
    chatInner.addEventListener("mousedown", function (e) {
        if (e.button !== 0 || svgPanDrag)
            return;
        const view = e.target.closest ? e.target.closest(".md-svg-view") : null;
        if (!view)
            return;
        const block = view.closest(".md-svg-block");
        if (!block || zoomScaleOf(block) <= 1)
            return;
        if (view.scrollWidth <= view.clientWidth + 1 && view.scrollHeight <= view.clientHeight + 1)
            return;
        svgPanDrag = { view: view, x: e.clientX, y: e.clientY,
            sl: view.scrollLeft, st: view.scrollTop };
        view.classList.add("is-panning");
        e.preventDefault();
    });
    document.addEventListener("mousemove", function (e) {
        if (!svgPanDrag)
            return;
        const view = svgPanDrag.view;
        if (!view.isConnected) {
            svgPanDrag = null;
            return;
        }
        view.scrollLeft = svgPanDrag.sl - (e.clientX - svgPanDrag.x);
        view.scrollTop = svgPanDrag.st - (e.clientY - svgPanDrag.y);
    });
    document.addEventListener("mouseup", function () {
        if (!svgPanDrag)
            return;
        svgPanDrag.view.classList.remove("is-panning");
        svgPanDrag = null;
    });
    // ---------- SVG 生成控件（```svg 双视图块）交互 ----------
    // 视图切换/复制代码/复制图片全部事件委托：markdown 流式重渲染会重建节点
    chatInner.addEventListener("click", async function (e) {
        const btn = e.target.closest("[data-svg-action]");
        if (!btn)
            return;
        const block = btn.closest(".md-svg-block");
        if (!block)
            return;
        const action = btn.dataset.svgAction;
        // Mermaid 失败/显示异常时的手动重渲染（仅 mermaid 控件有此按钮）：
        // 重新走调度链路（命中缓存瞬时复用，未命中则重新 mermaid.render）
        if (action === "rerender") {
            App.rerenderMermaid(block);
            return;
        }
        if (action === "view-image" || action === "view-code") {
            const want = action === "view-image" ? "image" : "code";
            block.querySelectorAll("[data-svg-view]").forEach(function (view) {
                view.hidden = view.dataset.svgView !== want;
            });
            block.querySelectorAll(".md-svg-tab").forEach(function (tab) {
                tab.classList.toggle("is-active", tab === btn);
            });
            return;
        }
        // 全屏（SVG/Mermaid 控件共用）：作用于整个控件块，当前视图（图片/代码）
        // 铺满全屏视口；Esc 或再点一次退出。fullscreenchange 统一刷新按钮文案
        if (action === "full") {
            if (document.fullscreenElement === block) {
                document.exitFullscreen();
                return;
            }
            if (!document.fullscreenEnabled) {
                App.toast("当前环境不支持全屏显示");
                return;
            }
            block.requestFullscreen().catch(function (err) {
                App.toast("进入全屏失败：" + (err && err.message || err));
            });
            return;
        }
        const original = btn.textContent;
        btn.disabled = true;
        if (action === "copy-code") {
            try {
                const code = Markdown.unescapeHtml(block.dataset.svgCode || "");
                await navigator.clipboard.writeText(code);
                btn.textContent = "✓ 已复制";
            }
            catch (_) {
                btn.textContent = "复制失败";
            }
        }
        else if (action === "copy-image") {
            let blob = null;
            let firstErr = "";
            try {
                blob = await svgBlockToPngBlob(block);
            }
            catch (err) {
                firstErr = (err && err.message) || String(err);
            }
            if (blob) {
                try {
                    if (navigator.clipboard && window.ClipboardItem) {
                        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
                        btn.textContent = "✓ 已复制图片";
                    }
                    else {
                        throw new Error("剪贴板图片接口不可用");
                    }
                }
                catch (err) {
                    // 写剪贴板失败（焦点丢失/权限拒绝等）：导出本身成功，降级为下载 PNG
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = "svg-image.png";
                    link.click();
                    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
                    btn.textContent = "✓ 已下载 PNG";
                    App.toast("写入剪贴板失败（" + ((err && err.message) || err) + "），已降级为下载");
                }
            }
            else {
                // 导出环节失败（图未渲染/解码失败等）：按钮 + toast 透出具体原因
                btn.textContent = "复制失败";
                App.toast("复制图片失败：" + (firstErr || "未知错误"));
            }
        }
        else {
            btn.disabled = false;
            return;
        }
        setTimeout(function () {
            btn.textContent = original;
            btn.disabled = false;
        }, 1500);
    });
    // 导出专用净化：foreignObject → <text>/<tspan>。mermaid 12 用 foreignObject
    // 承载节点/边标签（HTML 内容），SVG 一经 Image 解码 + canvas 绘制就会被浏览器
    // 判定为「污染画布」，toBlob/toDataURL 抛 SecurityError（headless Chrome 实测：
    // 去 foreignObject 即干净，仅去 <style> 无效，htmlLabels:false 配置在 12 上也不
    // 生效）。把 HTML 标签替换为等价纯 SVG 文本后即可安全导出——只处理导出克隆，
    // 页面显示用的 SVG 不动；产物样式表里的 .label text 规则会为生成文本补齐
    // 颜色/字号，视觉与显示基本一致。多行标签（多个 <p>）转多个 <tspan> 分行。
    const SVG_NS = "http://www.w3.org/2000/svg";
    function sanitizeSvgForExport(svgEl) {
        svgEl.querySelectorAll("foreignObject").forEach(function (fo) {
            const w = parseFloat(fo.getAttribute("width")) || 0;
            const h = parseFloat(fo.getAttribute("height")) || 0;
            const ps = fo.querySelectorAll("p");
            let lines;
            if (ps.length) {
                lines = [];
                ps.forEach(function (p) { lines.push((p.textContent || "").trim()); });
            }
            else {
                lines = (fo.textContent || "").trim().split(/\n+/).map(function (s) { return s.trim(); });
            }
            const text = document.createElementNS(SVG_NS, "text");
            text.setAttribute("x", w / 2);
            text.setAttribute("y", h / 2);
            text.setAttribute("text-anchor", "middle");
            text.setAttribute("dominant-baseline", "middle");
            const lineH = 18; // 16px 默认字号的近似行高
            const startY = -(lineH * (lines.length - 1)) / 2;
            lines.forEach(function (line, i) {
                const tspan = document.createElementNS(SVG_NS, "tspan");
                tspan.setAttribute("x", w / 2);
                tspan.setAttribute("y", h / 2 + startY + i * lineH);
                tspan.textContent = line;
                text.appendChild(tspan);
            });
            fo.parentNode.replaceChild(text, fo);
        });
        // 其它潜在脏源（iframe/object/embed/script）一并移除，防二次污染
        svgEl.querySelectorAll("iframe,object,embed,script").forEach(function (n) { n.remove(); });
        return svgEl;
    }
    // 把 SVG 双视图块的图片视图光栅化为 PNG：
    // 内联 SVG → Blob(data:image/svg+xml) → Image 解码 → canvas 绘制 → PNG Blob。
    // 尺寸优先取实际渲染的显示尺寸（getBoundingClientRect）——mermaid 产物的
    // svg 根是 width="100%" 且 height 已被移除，直接读属性会得到 100×viewBox
    // 的崩坏比例；不可见（代码视图激活）时回退 viewBox 基准尺寸。
    // 按 2x~3x（跟随 devicePixelRatio）超采样导出，粘贴出去不发虚。
    async function svgBlockToPngBlob(block) {
        const view = block.querySelector('[data-svg-view="image"] svg, [data-svg-view="image"] img.md-animated-svg');
        if (!view) {
            // mermaid 渲染失败/被中断时图片视图没有 svg——给出可操作的提示
            if (block.dataset.svgKind === "mermaid" && block.dataset.mermaidState !== "done") {
                throw new Error("图片尚未渲染成功，点「↻ 重渲染」恢复后再试");
            }
            throw new Error("找不到可导出的 SVG");
        }
        // 基准尺寸（CSS 像素）：元素盒在 max-height 触顶时会水平 letterbox
        // （内容按 preserveAspectRatio 居中缩小、两侧留透明），直接用元素盒
        // 导出会把留白框进 PNG（表现为白色外框）——按 viewBox 比例从元素盒
        // 中「contain 裁剪」出实际绘制内容的尺寸
        let baseW = 0, baseH = 0;
        const visible = !!(view.getClientRects && view.getClientRects().length);
        const rect = visible ? view.getBoundingClientRect() : null;
        let sourceSvg = view;
        if (view.localName === "img") {
            // 动画预览在 <img> 的 image 环境中运行；导出时从同一图片源码重建 SVG。
            // PNG 是静态图片，因此只导出动画的单帧快照。
            const imageUrl = view.getAttribute("src") || "";
            const separator = imageUrl.indexOf(",");
            if (!imageUrl.startsWith("data:image/svg+xml") || separator < 0)
                throw new Error("SVG 图片来源无效");
            const parsed = new DOMParser().parseFromString(decodeURIComponent(imageUrl.slice(separator + 1)), "image/svg+xml");
            sourceSvg = parsed.documentElement;
            if (!sourceSvg || sourceSvg.localName !== "svg")
                throw new Error("SVG 源码无法解析");
        }
        const vb = (sourceSvg.getAttribute("viewBox") || "").split(/[\s,]+/).map(parseFloat);
        const vbOk = vb.length === 4 && isFinite(vb[2]) && isFinite(vb[3]) && vb[2] > 0 && vb[3] > 0;
        if (rect && rect.width > 2 && rect.height > 2) {
            if (vbOk) {
                const ar = vb[2] / vb[3];
                baseW = Math.min(rect.width, rect.height * ar);
                baseH = Math.min(rect.height, rect.width / ar);
            }
            else {
                baseW = rect.width;
                baseH = rect.height;
            }
            // 除当前显示缩放：导出始终按 100% 基准尺寸，不随查看倍率放大/缩小
            const zoom = zoomScaleOf(block);
            baseW = baseW / zoom;
            baseH = baseH / zoom;
        }
        if (!(baseW > 2 && baseH > 2)) {
            if (vbOk) {
                baseW = vb[2];
                baseH = vb[3];
            }
            else {
                baseW = 640;
                baseH = 480;
            }
        }
        baseW = Math.max(2, Math.round(baseW));
        baseH = Math.max(2, Math.round(baseH));
        // 高清倍率：2x 起、跟随 devicePixelRatio、上限 3x；长边 6000px 兜底防爆内存
        let scale = Math.min(Math.max(2, window.devicePixelRatio || 1), 3);
        let w = Math.round(baseW * scale);
        let h = Math.round(baseH * scale);
        const MAX_EDGE = 6000;
        if (Math.max(w, h) > MAX_EDGE) {
            const k = MAX_EDGE / Math.max(w, h);
            w = Math.round(w * k);
            h = Math.round(h * k);
            scale = w / baseW;
        }
        // 总像素上限：超宽图长边钳制后面积仍可能爆表，canvas 尺寸过大会让
        // toBlob 静默返回 null（表现为复制失败无报错）——按面积再钳一次
        const MAX_PIXELS = 40000000; // 40MP，远超常规图，仅拦截极端长条图
        if (w * h > MAX_PIXELS) {
            const k = Math.sqrt(MAX_PIXELS / (w * h));
            w = Math.round(w * k);
            h = Math.round(h * k);
            scale = w / baseW;
        }
        const clone = sourceSvg.cloneNode(true);
        // 去内联样式（mermaid 的 max-width / 自适应 width:100% 在独立解码语境
        // 无意义且会干扰固有尺寸），改用显式像素尺寸 + viewBox 保证比例
        clone.removeAttribute("style");
        clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
        clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
        clone.setAttribute("width", baseW);
        clone.setAttribute("height", baseH);
        if (!vbOk)
            clone.setAttribute("viewBox", "0 0 " + baseW + " " + baseH);
        // 两段式导出：先按原始克隆保真导出（保留 foreignObject 的 HTML 标签）；
        // toBlob 抛 SecurityError（画布被污染）时做净化（foreignObject→<text>）
        // 重导一次——净化牺牲一点排版保真，换取完整文字与干净画布。
        let clean = false;
        for (;;) {
            const source = clean ? sanitizeSvgForExport(clone) : clone;
            const blobSrc = new Blob([new XMLSerializer().serializeToString(source)], { type: "image/svg+xml;charset=utf-8" });
            const url = URL.createObjectURL(blobSrc);
            try {
                const img = await new Promise(function (resolve, reject) {
                    const image = new Image();
                    image.onload = function () { resolve(image); };
                    image.onerror = function () { reject(new Error("SVG 解码失败")); };
                    image.src = url;
                });
                const canvas = document.createElement("canvas");
                canvas.width = w;
                canvas.height = h;
                canvas.getContext("2d").drawImage(img, 0, 0, w, h);
                const blob = await new Promise(function (resolve, reject) {
                    canvas.toBlob(function (b) {
                        b ? resolve(b) : reject(new Error("PNG 导出失败"));
                    }, "image/png");
                });
                return blob;
            }
            catch (err) {
                const msg = String((err && err.message) || err);
                if (!clean && /taint|secur/i.test(msg)) {
                    clean = true; // 画布污染：净化克隆后重导一次
                    continue;
                }
                throw err;
            }
            finally {
                setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
            }
        }
    }
    // ---------- 导出（供其它模块经 App.* 调用） ----------
    // zoomScaleOf 为 messages.js 拆分新增导出（原模块内直接调用，现跨文件经 App.* 调用）
    App.zoomScaleOf = zoomScaleOf;
})(window.App);
