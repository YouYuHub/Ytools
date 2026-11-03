"use strict";
/**
 * 消息基础渲染：代码高亮/保活重渲染/用户气泡（原 messages.js 978-1250 行拆出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 */
(function (App) {
    "use strict";
    const { el, chatInner, stickToBottom } = App;
    // ---------- 消息渲染 ----------
    // Prism 语法高亮：仅处理容器内 language-* 的代码元素（diff 组合语言由插件处理）
    function highlightCodeBlocks(container) {
        if (window.Prism && container)
            Prism.highlightAllUnder(container);
    }
    // ---------- 保活渲染：重渲染时保留代码块滚动位置与 md-svg 控件 ----------
    // 流式期间正文按 Markdown.render 全量重建，普通代码块的 pre 和
    // ```svg/mermaid/canvas 控件会随节点一起被销毁重建；保存/恢复代码块
    // pre 的滚动位置，避免生成增量到达时用户在块内滚动被拉回顶部。
    // md-svg 控件还需保留原节点，否则 canvas iframe 会被丢弃重建（表现为
    // "生成中点运行无反应"、切到代码视图拖动滚动条立刻回滚）。
    // md 表格同理保留原节点：否则流式期间横向滚动条每帧归零、展开中的
    // 「更多」菜单被销毁、点击横跨重建会丢失（mousedown/mouseup 落在新旧
    // 两个节点上）。表格以原始 md 源码精确配对（data-table-raw 属性，
    // 与滚动/菜单等 DOM 状态无关）；正在生长的表格配对不上，按新建处理
    // 并走滚动/菜单兜底恢复。
    // 修复采用「锚点挖洞」整块移植：
    //   1. 配对决策走 WidgetReuse.plan（kind+源码精确 → svg/canvas 流式前缀 →
    //      运行中 canvas iframe 兜底）；
    //   2. 保留的旧块**绝不脱离文档**——HTML5 规范 iframe 摘下再插回会重建
    //      browsing context 导致重载重跑；因此新树里对应位置换成 marker 占位，
    //      以保留块为锚把新内容分段 insertBefore，最后清理旧残留节点；
    //   3. 配不上的控件按新建处理（正常升级/重建）。
    function renderPreservingWidgets(host, html) {
        if (!host || typeof html !== "string")
            return;
        const codeScroll = Array.prototype.map.call(host.querySelectorAll(".codeblock pre"), function (pre) { return { top: pre.scrollTop, left: pre.scrollLeft }; });
        // 表格横向滚动 + 「更多」菜单展开态：与代码块同理按块序快照。
        // 已完成表格走下方节点复用（原节点保留，状态天然延续）；快照只兜底
        // 正在生长的表格（源码变化配对不上、必须重建）。
        const tableScroll = Array.prototype.map.call(host.querySelectorAll(".md-table-block .md-table-scroll"), function (box) { return { top: box.scrollTop, left: box.scrollLeft }; });
        const tableMenuOpen = Array.prototype.map.call(host.querySelectorAll(".md-table-block"), function (block) {
            const menu = block.querySelector(".md-table-menu");
            return Boolean(menu && !menu.classList.contains("hidden"));
        });
        function replaceHostHtml() {
            host.innerHTML = html;
            restoreCodeblockScroll(host, codeScroll);
            restoreTableState(host, tableScroll, tableMenuOpen);
        }
        // Markdown.render() 的返回值会统一包在 <div class="md"> 中。控件是
        // .md 的直接子节点，因此后续插入/清理必须以旧、新 .md 为根；若以
        // host 为根，insertBefore 会拿到不属于 host 的后代控件作为锚点并抛错，
        // 导致第一个 Mermaid 图出现后所有后续流式重绘都失败。
        function directMarkdownRoot(container) {
            const children = container && container.children;
            if (!children)
                return container;
            for (let i = 0; i < children.length; i++) {
                if (children[i].classList && children[i].classList.contains("md"))
                    return children[i];
            }
            return container;
        }
        const oldRoot = directMarkdownRoot(host);
        const oldBlocks = Array.prototype.slice.call(oldRoot.querySelectorAll(".md-svg-block, .md-table-block"));
        if (!oldBlocks.length) {
            replaceHostHtml();
            return;
        }
        const fresh = document.createElement("div");
        fresh.innerHTML = html;
        const freshRoot = directMarkdownRoot(fresh);
        const newBlocks = Array.prototype.slice.call(freshRoot.querySelectorAll(".md-svg-block, .md-table-block"));
        if (!newBlocks.length) {
            // 新渲染里已无控件（源码被改掉/删除）：正常替换，控件随之消失
            replaceHostHtml();
            return;
        }
        // 锚点分段算法只处理根容器的直接子控件。遇到嵌套控件时安全退回
        // 普通重绘，避免把后代节点误传给根容器 insertBefore 而中断流式更新。
        if (oldBlocks.some(function (block) { return block.parentNode !== oldRoot; }) ||
            newBlocks.some(function (block) { return block.parentNode !== freshRoot; })) {
            replaceHostHtml();
            return;
        }
        const describe = function (block) {
            if (block.classList && block.classList.contains("md-table-block")) {
                const rawHolder = block.querySelector("[data-table-raw]");
                return {
                    kind: "table",
                    code: rawHolder ? String(rawHolder.getAttribute("data-table-raw") || "") : "",
                };
            }
            return {
                kind: block.dataset.svgKind || "",
                code: Markdown.unescapeHtml(block.dataset.svgCode || ""),
                hasFrame: Boolean(block.querySelector(".md-canvas-frame")),
            };
        };
        const plan = WidgetReuse.plan(oldBlocks.map(describe), newBlocks.map(describe));
        // 新树中配对成功的位置换成 marker 占位（保留块原地不动）；
        // 同时把最新源码同步到保留节点（复制代码/重跑按钮始终用当前版本）
        const markerFor = new Map(); // marker element -> reuseIndex
        const keepSet = new Set();
        for (let k = 0; k < newBlocks.length; k++) {
            const reuseIndex = plan[k].reuseIndex;
            if (reuseIndex < 0)
                continue;
            const keep = oldBlocks[reuseIndex];
            if (newBlocks[k].dataset.svgCode != null) {
                keep.dataset.svgCode = newBlocks[k].dataset.svgCode;
            }
            const marker = document.createElement("span");
            marker.className = "md-rpw-slot";
            marker.hidden = true;
            markerFor.set(marker, reuseIndex);
            keepSet.add(keep);
            freshRoot.replaceChild(marker, newBlocks[k]);
        }
        if (!markerFor.size) {
            replaceHostHtml();
            return;
        }
        // 分段搬移：以最靠前的保留块为锚插入其前内容；遇 marker 则推进锚点到
        // 该保留块的下一个兄弟（保留块本身永不脱离文档，iframe 不重载）；
        // 尾部剩余内容追加到最后。
        const nodes = Array.prototype.slice.call(freshRoot.childNodes);
        // 只记录本次插入的节点。把标记写到 DOM 节点上会跨越下一次流式重绘，
        // 令上次的正文被误判为“新节点”而永远无法清理，逐帧累积重复内容。
        const freshNodes = new Set();
        let refNode = null;
        for (let i = 0; i < oldBlocks.length; i++) {
            if (keepSet.has(oldBlocks[i])) {
                refNode = oldBlocks[i];
                break;
            }
        }
        for (let i = 0; i < nodes.length; i++) {
            const node = nodes[i];
            const reuseIndex = markerFor.get(node);
            if (reuseIndex !== undefined) {
                refNode = oldBlocks[reuseIndex].nextSibling;
                continue;
            }
            freshNodes.add(node);
            oldRoot.insertBefore(node, refNode);
        }
        // 清理旧残留：顶层节点既非本轮新插入、也非保留块的一律移除
        // （快照后遍历，避免 live childNodes 边删边移位）
        WidgetReuse.removeStaleNodes(oldRoot, freshNodes, keepSet);
        restoreCodeblockScroll(host, codeScroll);
        restoreTableState(host, tableScroll, tableMenuOpen);
    }
    // 普通代码块会随流式 Markdown 全量重建，按块序恢复滚动位置。
    // 流式回答只在尾部追加，已有代码块的顺序稳定；超出新内容范围的位置
    // 由浏览器自然限制到当前可滚动区域。
    function restoreCodeblockScroll(host, savedPositions) {
        if (!host || !savedPositions || !savedPositions.length)
            return;
        const codeBlocks = host.querySelectorAll(".codeblock pre");
        for (let i = 0; i < savedPositions.length && i < codeBlocks.length; i += 1) {
            const pre = codeBlocks[i];
            const saved = savedPositions[i];
            if (!saved)
                continue;
            pre.scrollTop = saved.top;
            pre.scrollLeft = saved.left;
        }
    }
    // 表格状态恢复（节点复用配对不上的兜底，即正在生长的表格）：
    // 滚动位置按块序恢复（流式只在尾部追加，旧表顺序稳定）；
    // 「更多」菜单仅在表格数量不变时按块序恢复展开（数量变了说明增删，
    // 按序恢复可能开错表，宁可不开）。复用命中的表不受影响（原节点保留，
    // 写入的是同一份值）。
    function restoreTableState(host, savedScroll, savedMenuOpen) {
        if (!host)
            return;
        const blocks = host.querySelectorAll(".md-table-block");
        if (savedScroll && savedScroll.length) {
            for (let i = 0; i < savedScroll.length && i < blocks.length; i += 1) {
                const box = blocks[i].querySelector(".md-table-scroll");
                const saved = savedScroll[i];
                if (!box || !saved)
                    continue;
                box.scrollTop = saved.top;
                box.scrollLeft = saved.left;
            }
        }
        if (savedMenuOpen && savedMenuOpen.length && savedMenuOpen.length === blocks.length) {
            for (let i = 0; i < blocks.length; i += 1) {
                if (!savedMenuOpen[i])
                    continue;
                const menu = blocks[i].querySelector(".md-table-menu");
                const btn = blocks[i].querySelector(".md-table-more-btn");
                if (menu)
                    menu.classList.remove("hidden");
                if (btn)
                    btn.setAttribute("aria-expanded", "true");
            }
        }
    }
    // 用户气泡：content 可为字符串或多部件列表（多模态消息与其历史回放）。
    // 列表取 text 部件作为正文；image_url 渲染图片缩略图；video_url 渲染视频
    // 首帧（浏览器原生 <video>，#t=0.1 确保首帧绘制）；input_audio 渲染固定
    // 格式音频徽标；docs 为发送时随消息的会话文档快照（file_memory，仅前端
    // 展示，不进入消息内容部件）；quotes 为选中文本引用快照（结构化数据，
    // 卡片置顶、正文在下；渲染走 textContent，绝不把引用原文交给 innerHTML）
    function buildUserBubble(content, sessionId, docs, quotes) {
        const bubble = el("div", "msg-bubble");
        const attachmentBlocks = el("div", "msg-user-attachment-blocks");
        // 引用卡片（引用 1/引用 2…）：置于问题正文之前（GPT 同款信息层级）
        if (App.buildQuoteList) {
            const quoteList = App.buildQuoteList(quotes);
            if (quoteList)
                attachmentBlocks.appendChild(quoteList);
        }
        const parts = Array.isArray(content) ? content : null;
        let text = String(content == null ? "" : content);
        if (parts) {
            text = parts
                .map(function (part) {
                return part && part.type === "text" && typeof part.text === "string" ? part.text : "";
            })
                .filter(function (t) { return t.trim(); })
                .join("\n");
        }
        // 布局：媒体（图片/视频/音频/文档）在上、文字在下——媒体是视觉主体先入眼，
        // 配文紧随其后（GPT Web / Claude 同款顺序）
        if (parts) {
            const mediaRow = el("div", "msg-user-media");
            parts.forEach(function (part) {
                if (!part || typeof part !== "object")
                    return;
                const type = part.type || "";
                if (type === "image_url") {
                    const url = part.image_url && typeof part.image_url.url === "string" ? part.image_url.url : "";
                    if (!url)
                        return;
                    const src = App.resolveMediaSrc(url, sessionId);
                    if (!src)
                        return;
                    const thumb = el("img", "msg-user-media-thumb clickable", "");
                    thumb.src = src;
                    thumb.alt = "附件图片";
                    thumb.title = "点击预览";
                    thumb.addEventListener("click", function () {
                        App.openMediaPreview({ type: "image", src: src, title: "图片" });
                    });
                    mediaRow.appendChild(thumb);
                    return;
                }
                if (type === "video_url") {
                    // 与图片一致：渲染视频首帧缩略图，点击后在预览模态框中播放
                    const url = part.video_url && typeof part.video_url.url === "string" ? part.video_url.url : "";
                    if (!url)
                        return;
                    const src = App.resolveMediaSrc(url, sessionId);
                    if (!src)
                        return;
                    const video = el("video", "msg-user-media-thumb clickable", "");
                    video.src = src + "#t=0.1";
                    video.muted = true;
                    video.preload = "metadata";
                    video.playsInline = true;
                    video.title = "点击播放";
                    video.addEventListener("click", function () {
                        App.openMediaPreview({ type: "video", src: src, title: "视频" });
                    });
                    mediaRow.appendChild(video);
                    return;
                }
                // 音频：固定格式徽标（不内联播放），点击在预览模态框中收听
                if (type === "input_audio") {
                    const mediaRef = part.input_audio && typeof part.input_audio.data === "string" ? part.input_audio.data : "";
                    const audioSrc = App.resolveMediaSrc(mediaRef, sessionId);
                    const chip = el("div", "msg-user-media-audio");
                    chip.appendChild(el("span", "msg-user-media-icon", "🎵"));
                    chip.appendChild(el("span", "msg-user-media-label", "音频"));
                    chip.title = "点击收听";
                    if (audioSrc) {
                        chip.classList.add("clickable");
                        chip.addEventListener("click", function () {
                            App.openMediaPreview({ type: "audio", src: audioSrc, title: "音频" });
                        });
                    }
                    mediaRow.appendChild(chip);
                    return;
                }
            });
            // 随消息发送的会话文档（前端展示块；内容经系统提示词注入，不进 content）
            (docs || []).forEach(function (doc) {
                if (!doc || !doc.filename)
                    return;
                const filename = String(doc.filename);
                const extension = (filename.split(".").pop() || "FILE").toUpperCase();
                const chip = el("div", "msg-user-media-doc clickable");
                chip.appendChild(el("span", "msg-user-media-icon", "📄"));
                chip.appendChild(el("span", "msg-user-media-label", extension.slice(0, 5)));
                chip.title = filename + "（点击预览）";
                chip.setAttribute("aria-label", "文档：" + filename + "，点击预览");
                chip.classList.add("clickable");
                chip.addEventListener("click", function () {
                    App.openMediaPreviewForDocument(doc);
                });
                mediaRow.appendChild(chip);
            });
            if (mediaRow.childNodes.length)
                attachmentBlocks.appendChild(mediaRow);
        }
        if (attachmentBlocks.querySelector(".msg-quote-tile, .msg-user-media-thumb, .msg-user-media-audio, .msg-user-media-doc"))
            bubble.appendChild(attachmentBlocks);
        if (text)
            bubble.appendChild(el("div", "msg-user-text", text));
        return bubble;
    }
    function appendUserMessage(content, ts, sessionId, docs, container, round, quotes, sourceEventIndex) {
        const msg = el("div", "msg msg-user");
        if (ts)
            msg.appendChild(el("div", "msg-time", FormatUtils.fmtTime(ts)));
        App.setStageTimestamp(msg, ts);
        msg.appendChild(buildUserBubble(content, sessionId, docs, quotes));
        // 历史轮次的用户消息：记录原始内容供编辑态回填，并挂 hover「编辑」入口。
        // round 为 null（实时发送）时编辑入口在收尾重载后随历史回放出现
        if (round != null) {
            msg.dataset.round = String(round);
            if (Number.isInteger(sourceEventIndex) && sourceEventIndex >= 0) {
                msg.dataset.sourceEvent = String(sourceEventIndex);
            }
            msg._editData = { content: content, round: round, quotes: quotes || [] };
            App.attachUserEditAction(msg);
        }
        // container 缺省追加到会话流末尾；传入正在流式的助手消息节点时，
        // 气泡嵌入其当前内容之后（工具结果位置），保持注入消息的时间顺序
        (container || chatInner).appendChild(msg);
        const attachmentBlocks = msg.querySelector(".msg-user-attachment-blocks");
        if (attachmentBlocks && App.fitAttachmentBlockGrid) {
            App.fitAttachmentBlockGrid(attachmentBlocks);
        }
        // 流式注入（attachment 指向流节点）时尊重自动贴底暂停：用户上滚阅读
        // 时不被拉回；整段历史回放后由 history.js 的 scrollToBottom 置底复位
        if (!container || (container !== chatInner && container.nodeType !== 11))
            stickToBottom();
        return msg;
    }
    // ---------- 导出（供其它模块经 App.* 调用） ----------
    App.highlightCodeBlocks = highlightCodeBlocks;
    App.appendUserMessage = appendUserMessage;
    App.renderPreservingWidgets = renderPreservingWidgets;
})(window.App);
