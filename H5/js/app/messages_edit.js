"use strict";
/**
 * 用户消息编辑/重发与思考·工具块构建器（原 messages.js 1251-2433 行拆出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 */
(function (App) {
    "use strict";
    const { el, state, chatInner } = App;
    // ---------- 用户消息编辑（GPT 网页同款：hover 编辑 → textarea 重发） ----------
    // 与 GPT 的差异：重发时提供两种删除范围——「重新生成该轮」（原地替换该轮，
    // 之后的轮次保留）与「删除该轮及之后」（GPT 语义，后续轮次一并删除）。
    // 后端按 data-round 精确删除 JSONL 轮次并清理不再引用的用户上传附件。
    // ---------- 用户消息操作行（独立一行，SVG 图标按钮）：复制 / 编辑 ----------
    // 复制：把该消息原始文本回填到剪贴板；编辑：进入编辑态。
    // 与 GPT 网页的差异：重发时提供两种删除范围（重新生成该轮 / 删除该轮及之后）。
    const EDIT_COPY_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
    const EDIT_PENCIL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>';
    const EDIT_CHECK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M20 6 9 17l-5-5"/></svg>';
    const EDIT_TRASH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-.9 14a2 2 0 0 1-2 1.9H7.9a2 2 0 0 1-2-1.9L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';
    function attachUserEditAction(msg) {
        const actions = el("div", "msg-user-actions");
        const copyBtn = el("button", "msg-user-action-btn msg-user-copy-btn");
        copyBtn.type = "button";
        copyBtn.title = "复制";
        copyBtn.innerHTML = EDIT_COPY_ICON + '<span class="msg-user-action-label">复制</span>';
        copyBtn.addEventListener("click", function () {
            const data = msg._editData || {};
            // content 可能是字符串（纯文本消息）或多部件列表（多模态消息）；
            // 带引用时输出人可读文本（「引用 1：…\n\n问题：…」），不输出协议标签
            const plain = contentToPlainText(data.content);
            const text = (data.quotes && data.quotes.length && window.QuoteUtils)
                ? QuoteUtils.composeCopyText(data.quotes, plain)
                : plain;
            if (!text) {
                App.toast("该消息没有可复制的文本");
                return;
            }
            const done = function () {
                copyBtn.classList.add("copied");
                const label = copyBtn.querySelector(".msg-user-action-label");
                if (label)
                    label.textContent = "已复制";
                setTimeout(function () {
                    copyBtn.classList.remove("copied");
                    if (label)
                        label.textContent = "复制";
                }, 1400);
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done, function () {
                    App.toast("复制失败（浏览器未授权剪贴板）");
                });
            }
            else {
                // 兼容降级：临时 textarea + execCommand
                const ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.select();
                try {
                    document.execCommand("copy");
                    done();
                }
                catch (_) {
                    App.toast("复制失败");
                }
                ta.remove();
            }
        });
        const editBtn = el("button", "msg-user-action-btn msg-user-edit-btn");
        editBtn.type = "button";
        editBtn.title = "编辑这条消息并重发";
        editBtn.innerHTML = EDIT_PENCIL_ICON + '<span class="msg-user-action-label">编辑</span>';
        editBtn.addEventListener("click", function () { beginUserMessageEdit(msg); });
        const insertBtn = el("button", "msg-user-action-btn msg-user-insert-btn", "插入对话");
        insertBtn.type = "button";
        insertBtn.title = "在这条消息之前插入新对话，生成时只使用此前的上下文";
        insertBtn.addEventListener("click", function () { beginUserMessageEdit(msg, true); });
        // 删除该轮（用户消息与回复一并删除，后续轮次保留、轮次号自动前移）：
        // hover 显示，点击弹确认框，确认后走 single 删除并重载会话
        const deleteBtn = el("button", "msg-user-action-btn msg-user-delete-btn");
        deleteBtn.type = "button";
        deleteBtn.title = "删除该轮对话（含该轮回复，后续轮次保留）";
        deleteBtn.innerHTML = EDIT_TRASH_ICON + '<span class="msg-user-action-label">删除</span>';
        deleteBtn.addEventListener("click", function () { requestDeleteRound(msg); });
        // 显示顺序：删除在左（危险操作与内容区隔离），复制/编辑靠右——
        // 常用的复制/编辑位于视觉末端，减少删除按钮的误触概率
        actions.appendChild(deleteBtn);
        actions.appendChild(insertBtn);
        actions.appendChild(copyBtn);
        actions.appendChild(editBtn);
        msg.appendChild(actions);
    }
    // 从历史 content 部件提取附件项（media:// 引用，直接复用无需重新上传）
    function extractUserMediaItems(content) {
        const items = [];
        (Array.isArray(content) ? content : []).forEach(function (part) {
            if (!part || typeof part !== "object")
                return;
            const type = part.type || "";
            let ref = "";
            if (type === "image_url" && part.image_url)
                ref = part.image_url.url || "";
            else if (type === "video_url" && part.video_url)
                ref = part.video_url.url || "";
            else if (type === "input_audio" && part.input_audio)
                ref = part.input_audio.data || "";
            if (typeof ref !== "string" || ref.indexOf("media://") !== 0)
                return;
            const stored = ref.slice("media://".length);
            const dot = stored.lastIndexOf(".");
            items.push({
                kind: type === "image_url" ? "image" : type === "video_url" ? "video" : "audio",
                media_ref: ref,
                stored_name: stored,
                format: dot > 0 ? stored.slice(dot + 1).toLowerCase() : "",
            });
        });
        return items;
    }
    // content 正文提取（单一入口）：字符串原样返回；多部件列表取 text 部件
    // 拼接。编辑回填、复制按钮共用——纯文本消息 content 是字符串而非数组，
    // 此前两处分别各自实现导致字符串分支漏掉（编辑框不回填/复制按钮失效）
    function contentToPlainText(content) {
        if (typeof content === "string")
            return content;
        if (!Array.isArray(content))
            return content == null ? "" : String(content);
        return content
            .map(function (part) {
            return part && part.type === "text" && typeof part.text === "string" ? part.text : "";
        })
            .join("\n");
    }
    /**
     * 单轮删除编排（hover 删除按钮入口）：确认框 → single 删除该轮整轮
     * （用户消息与回复一并删除，后续轮次保留、轮次号自动前移）→ 重载会话。
     * 运行中由后端 delete_rounds 拒绝（409），前端仅提示；此处不主动停止
     * 生成——删除是破坏性操作且无"顺带重发"的诉求，等待完成或手动停止。
     */
    function requestDeleteRound(msg) {
        if (!msg || !msg._editData || msg.classList.contains("is-editing"))
            return;
        const data = msg._editData;
        const round = Number(data.round);
        if (!round || round < 1)
            return;
        if (App.state.streaming && App.state.streamingSession === App.state.sessionId) {
            App.toast("会话正在生成回复，请等待完成或停止后再删除");
            return;
        }
        if (typeof App.openConfirmDialog !== "function") {
            if (!window.confirm("确认删除第 " + round + " 轮对话？该轮回复将一并删除，后续轮次保留。"))
                return;
            performDeleteRound(round);
            return;
        }
        App.openConfirmDialog({
            title: "删除该轮对话",
            message: "将删除第 " + round + " 轮（该轮用户消息与回复一并删除，" +
                "不再引用的附件会被清理），后续轮次保留且轮次号自动前移。确认删除？",
            confirmText: "确认删除",
            onConfirm: function () { performDeleteRound(round); },
        });
    }
    async function performDeleteRound(round) {
        const sessionId = App.state.sessionId;
        if (!sessionId)
            return;
        try {
            await API.deleteRounds(sessionId, round, { mode: "single", deleteFiles: true });
        }
        catch (err) {
            App.toast("删除轮次失败：" + err.message);
            return;
        }
        // 删除成功：整段重载会话（data-round 与编辑数据随新解析自动刷新）
        await App.openSession(sessionId);
        App.toast("已删除第 " + round + " 轮对话");
    }
    /**
     * 在流生成的提问气泡上就地补挂轮次标记与操作入口（编辑/复制/删除）。
     * round_started 事件到达时调用：普通发送的 userNode 此前 round=null 没有
     * 挂入口（收尾不重载就永远没有），后端在任务启动后立即推送本轮最终
     * 轮次号，前端补上 data-round/_editData/操作行；answer 节点同步打标。
     * 附接回放（刷新重连）同样调用：replay marker 携带轮次号与提问原始部件，
     * 复用历史节点或新建气泡后即可挂完整入口（旧后端缺部件时 userContent
     * 为空，跳过打标等收尾重载随历史回放补挂）。
     * targetRound/insertRound 路径已在 send() 内打标，此函数幂等跳过。
     * @param {object} activeStream 活动流对象（含 userNode/messageNode/userContent）
     * @param {number} round 本轮最终轮次号（1-based）
     */
    function attachLiveRoundEntry(activeStream, round) {
        if (!activeStream || typeof round !== "number" || round < 1)
            return;
        const userNode = activeStream.userNode;
        if (!userNode)
            return;
        // 刷新重连可复用已打标的历史提问，但回复容器是新建的；也必须补上
        // 轮次归属，否则编辑重发会漏删该容器内的子任务/思考/工具轨迹。
        if (activeStream.messageNode) {
            activeStream.messageNode.dataset.round = userNode.dataset.round || String(round);
        }
        // 已有轮次标记（历史渲染节点 / send() 内编辑重发路径已打标）：视为幂等
        // 命中直接返回，不改写——节点归属与入口数据以先打的标为准，避免错轮
        if (userNode.dataset.round) {
            return;
        }
        // 提问完整 content 未知（旧后端 replay 无部件、纯媒体提问无文本）时
        // 不挂入口：编辑重发会丢失多模态附件，等收尾重载随历史回放补挂
        if (activeStream.userContent == null)
            return;
        userNode.dataset.round = String(round);
        userNode._editData = {
            content: activeStream.userContent,
            round: round,
            quotes: activeStream.quotes || [],
        };
        attachUserEditAction(userNode);
    }
    function beginUserMessageEdit(msg, insertBefore) {
        if (!msg || !msg._editData || msg.classList.contains("is-editing"))
            return;
        // 运行中也允许编辑：发送编排（confirmEditResend）在用户二次确认后会
        // 停止进行中的生成任务再删除/重发；此处仅提示用户存在未完成生成
        if (App.state.streaming && App.state.streamingSession === App.state.sessionId) {
            App.toast("会话正在生成回复：确认发送后会先停止生成再重发");
        }
        const data = msg._editData;
        const originalText = insertBefore ? "" : contentToPlainText(data.content);
        const mediaItems = insertBefore ? [] : extractUserMediaItems(data.content);
        // 引用快照（选中文本引用到提问）：编辑态可移除、保留，随重发一起上送
        const originalQuotes = insertBefore ? [] : (data.quotes || []).slice();
        const keptQuotes = originalQuotes.slice();
        const keptDocs = [];
        msg.classList.add("is-editing");
        msg._editRestore = msg.querySelector(".msg-bubble");
        if (msg._editRestore && !insertBefore)
            msg._editRestore.style.display = "none";
        const panel = el("div", "msg-bubble msg-edit-bubble");
        const textarea = el("textarea", "msg-edit-textarea");
        textarea.value = originalText;
        textarea.rows = Math.min(10, Math.max(2, originalText.split("\n").length));
        textarea.placeholder = insertBefore ? "在此轮之前插入新消息…" : "编辑消息内容…";
        // 引用芯片（编辑态）：保留/移除旧引用，也可从当前选区新增引用。
        const quoteRow = el("div", "msg-edit-quote-row");
        const addQuoteButton = el("button", "msg-edit-upload", "引用所选文字");
        addQuoteButton.type = "button";
        addQuoteButton.title = "先在对话正文中选择一段文字，再点击添加引用";
        let quoteSnapshot = null;
        addQuoteButton.addEventListener("mousedown", function (event) {
            // 按钮获取焦点会清掉浏览器选区；先读取并保留来源快照。
            if (event.button !== 0)
                return;
            if (App.captureQuoteSelection)
                quoteSnapshot = App.captureQuoteSelection();
            event.preventDefault();
        });
        addQuoteButton.addEventListener("click", function () {
            const snapshot = quoteSnapshot || (App.captureQuoteSelection && App.captureQuoteSelection());
            quoteSnapshot = null;
            if (!snapshot) {
                App.toast("请先在对话正文中选择一段可引用文字");
                return;
            }
            const check = QuoteUtils.checkQuoteAppend(keptQuotes, snapshot.text);
            if (!check.ok) {
                App.toast(check.reason);
                return;
            }
            keptQuotes.push({
                id: "q_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
                text: snapshot.text,
                source: QuoteUtils.normalizeQuoteSource(snapshot.source),
            });
            renderQuoteRow();
        });
        function renderQuoteRow() {
            quoteRow.innerHTML = "";
            quoteRow.appendChild(addQuoteButton);
            keptQuotes.forEach(function (quote, index) {
                const chip = el("span", "msg-edit-quote-chip");
                chip.title = quote.text;
                chip.appendChild(el("span", "msg-edit-quote-badge", "引用 " + (index + 1)));
                chip.appendChild(el("span", "msg-edit-quote-text", window.QuoteUtils ? QuoteUtils.quotePreview(quote.text, 60) : quote.text));
                const remove = el("button", "msg-edit-chip-remove", "×");
                remove.type = "button";
                remove.title = "移除该引用";
                remove.addEventListener("click", function (event) {
                    event.stopPropagation();
                    keptQuotes.splice(index, 1);
                    renderQuoteRow();
                });
                chip.appendChild(remove);
                quoteRow.appendChild(chip);
            });
            quoteRow.style.display = "";
        }
        renderQuoteRow();
        // 附件芯片：旧 media:// 引用可直接复用，新选文件仅保存在浏览器内存，
        // 确认发送后才上传；图片/视频显示缩略图，音频显示徽标
        const keptMedia = mediaItems.slice();
        const editSessionId = App.state.sessionId;
        let editDisposed = false, attachmentsPending = 0;
        let attachmentQueue = Promise.resolve();
        const mediaRow = el("div", "msg-edit-media-row");
        const docRow = el("div", "msg-edit-media-row msg-edit-doc-row");
        const uploadControls = el("div", "msg-edit-upload-row");
        const uploadButton = el("button", "msg-edit-upload", "添加图片、音视频或文档");
        uploadButton.type = "button";
        const uploadHintText = "可直接粘贴剪贴板图片，点击发送后上传";
        const uploadHint = el("span", "msg-edit-upload-hint", uploadHintText);
        const fileInput = el("input", "msg-edit-file-input");
        fileInput.type = "file";
        fileInput.multiple = true;
        fileInput.setAttribute("aria-label", "选择要添加到历史消息的附件");
        uploadButton.addEventListener("click", function () { fileInput.click(); });
        uploadControls.appendChild(uploadButton);
        uploadControls.appendChild(uploadHint);
        uploadControls.appendChild(fileInput);
        function revokeObjectUrl(item) {
            if (!item || !item.objUrl)
                return;
            try {
                URL.revokeObjectURL(item.objUrl);
            }
            catch (_) { /* ignore */ }
            item.objUrl = "";
        }
        function renderMediaRow() {
            mediaRow.innerHTML = "";
            keptMedia.forEach(function (item, index) {
                const chip = el("span", "msg-edit-chip");
                const remove = el("button", "msg-edit-chip-remove", "×");
                remove.type = "button";
                remove.title = "移除该附件";
                remove.addEventListener("click", function (event) {
                    event.stopPropagation();
                    revokeObjectUrl(item);
                    keptMedia.splice(index, 1);
                    renderMediaRow();
                });
                const src = item.objUrl || (item.media_ref
                    ? App.resolveMediaSrc(item.media_ref, App.state.sessionId) : "");
                if (item.kind === "image" && src) {
                    const thumb = el("img", "msg-edit-chip-thumb");
                    thumb.src = src;
                    thumb.alt = "附件图片";
                    chip.appendChild(thumb);
                    chip.appendChild(el("span", "msg-edit-chip-name", chipShortName(item.stored_name || item.name)));
                    // 点击预览（编辑态同样支持，与消息气泡缩略图一致）
                    chip.classList.add("clickable");
                    chip.title = "点击预览";
                    chip.addEventListener("click", function () {
                        App.openMediaPreview({ type: "image", src: src, title: "图片" });
                    });
                }
                else if (item.kind === "video" && src) {
                    const thumb = el("video", "msg-edit-chip-thumb");
                    thumb.src = src + "#t=0.1";
                    thumb.muted = true;
                    thumb.preload = "metadata";
                    thumb.playsInline = true;
                    chip.appendChild(thumb);
                    chip.appendChild(el("span", "msg-edit-chip-name", "🎬 " + chipShortName(item.stored_name || item.name)));
                    chip.classList.add("clickable");
                    chip.title = "点击播放";
                    chip.addEventListener("click", function () {
                        App.openMediaPreview({ type: "video", src: src, title: "视频" });
                    });
                }
                else if (item.kind === "audio" && src) {
                    chip.appendChild(el("span", "msg-edit-chip-name", item.name || mediaChipLabel(item.media_ref, "🎵")));
                    chip.classList.add("clickable");
                    chip.title = "点击收听";
                    chip.addEventListener("click", function () {
                        App.openMediaPreview({ type: "audio", src: src, title: "音频" });
                    });
                }
                else {
                    chip.appendChild(el("span", "msg-edit-chip-name", item.name || mediaChipLabel(item.media_ref, "🎵")));
                }
                chip.appendChild(remove);
                mediaRow.appendChild(chip);
            });
            mediaRow.style.display = keptMedia.length ? "" : "none";
        }
        function renderDocRow() {
            docRow.innerHTML = "";
            keptDocs.forEach(function (item, index) {
                const chip = el("span", "msg-edit-chip");
                chip.appendChild(el("span", "msg-edit-chip-name", "📄 " + item.name));
                const remove = el("button", "msg-edit-chip-remove", "×");
                remove.type = "button";
                remove.title = "移除待上传文档";
                remove.addEventListener("click", function (event) {
                    event.stopPropagation();
                    keptDocs.splice(index, 1);
                    renderDocRow();
                });
                chip.appendChild(remove);
                docRow.appendChild(chip);
            });
            docRow.style.display = keptDocs.length ? "" : "none";
        }
        async function stageEditFiles(files) {
            if (editDisposed || !panel.isConnected || App.state.sessionId !== editSessionId) {
                panel._cleanupEdit();
                return;
            }
            const mediaFiles = files.filter(function (file) { return App.mediaKindOf(file.name); });
            const docFiles = files.filter(function (file) { return !App.mediaKindOf(file.name); });
            const stagedMediaCount = keptMedia.filter(function (item) { return !!item.file; }).length;
            let mediaAdded = 0;
            for (const file of mediaFiles) {
                if (stagedMediaCount + mediaAdded >= App.MAX_PENDING_MEDIA) {
                    App.toast("最多新增 " + App.MAX_PENDING_MEDIA + " 个待上传媒体文件");
                    break;
                }
                const item = await App.prepareMediaFile(file);
                if (editDisposed || !panel.isConnected || App.state.sessionId !== editSessionId) {
                    panel._cleanupEdit();
                    return;
                }
                if (!item)
                    continue;
                if (typeof URL.createObjectURL === "function") {
                    item.objUrl = URL.createObjectURL(item.file);
                }
                keptMedia.push(item);
                mediaAdded += 1;
            }
            if (mediaAdded && !App.getChatModelVision()) {
                App.toast("当前模型不支持视觉：新增媒体会存档，但模型可能无法读取图片内容");
            }
            for (const file of docFiles) {
                if (keptDocs.length >= 10) {
                    App.toast("最多新增 10 个文档");
                    break;
                }
                if (file.size > App.MAX_DOC_FILE_SIZE) {
                    App.toast("文件超过 10MB 限制：" + file.name);
                    continue;
                }
                keptDocs.push({
                    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
                    file: file,
                    name: file.name,
                });
            }
            renderMediaRow();
            renderDocRow();
        }
        function enqueueEditFiles(files) {
            if (!files.length || editDisposed)
                return;
            attachmentsPending++;
            sendBtn.disabled = true;
            uploadHint.textContent = "正在处理附件…";
            // 新附件使旧的发送/删除确认预览失效，处理完成后再确认当前内容。
            if (panel._confirmShown && panel._planArea) {
                panel._confirmShown = false;
                panel._planArea.replaceChildren();
            }
            attachmentQueue = attachmentQueue.then(function () { return stageEditFiles(files); })
                .catch(function () {
                if (!editDisposed && panel.isConnected) {
                    renderMediaRow();
                    renderDocRow();
                    App.toast("附件处理失败，请重新粘贴或选择文件");
                }
            })
                .finally(function () {
                attachmentsPending--;
                if (!editDisposed && panel.isConnected) {
                    sendBtn.disabled = attachmentsPending > 0;
                    uploadHint.textContent = attachmentsPending ? "正在处理附件…" : uploadHintText;
                }
            });
        }
        fileInput.addEventListener("change", function () {
            const files = Array.from(fileInput.files || []);
            fileInput.value = "";
            enqueueEditFiles(files);
        });
        textarea.addEventListener("paste", function (event) {
            const files = ClipboardMediaUtils.mediaFiles(event.clipboardData, App.mediaKindOf);
            if (!files.length)
                return; // 纯文字沿用浏览器原生粘贴。
            event.preventDefault();
            enqueueEditFiles(files);
        });
        renderMediaRow();
        renderDocRow();
        panel._cleanupEdit = function () {
            editDisposed = true;
            keptMedia.forEach(revokeObjectUrl);
        };
        panel.appendChild(uploadControls);
        panel.appendChild(mediaRow);
        panel.appendChild(docRow);
        // 引用芯片行在附件行下方（两者都不占 textarea 文本值）
        panel.appendChild(quoteRow);
        panel.appendChild(textarea);
        const foot = el("div", "msg-edit-foot");
        const modes = el("div", "msg-edit-modes");
        let mode = insertBefore ? "insert" : "regen";
        const regenBtn = el("button", "msg-edit-mode selected", "重新生成该轮");
        const truncateBtn = el("button", "msg-edit-mode", "删除该轮及之后");
        regenBtn.type = "button";
        truncateBtn.type = "button";
        regenBtn.title = "删除该轮回复后原位重新生成，之后的轮次保留（其上下文不含旧回复）";
        truncateBtn.title = "删除该轮及其后所有轮次（新回复追加在末尾，GPT 同款语义）";
        regenBtn.addEventListener("click", function () {
            mode = "regen";
            regenBtn.classList.add("selected");
            truncateBtn.classList.remove("selected");
        });
        truncateBtn.addEventListener("click", function () {
            mode = "truncate";
            truncateBtn.classList.add("selected");
            regenBtn.classList.remove("selected");
        });
        if (insertBefore) {
            modes.appendChild(el("span", "msg-edit-insert-hint", "在第 " + data.round + " 轮之前插入 · 仅使用此前上下文"));
        }
        else {
            modes.appendChild(regenBtn);
            modes.appendChild(truncateBtn);
        }
        foot.appendChild(modes);
        const buttons = el("div", "msg-edit-buttons");
        const cancelBtn = el("button", "msg-edit-cancel", "取消");
        const sendBtn = el("button", "msg-edit-send", "发送");
        cancelBtn.type = "button";
        sendBtn.type = "button";
        // 面板内确认条（复用 plans 区域）：发送/取消防误触的统一入口
        function showPlanConfirm(message, confirmLabel, onConfirm) {
            const area = panel._planArea;
            if (!area) {
                onConfirm();
                return;
            }
            panel._confirmShown = true;
            area.innerHTML = "";
            const box = el("div", "msg-edit-confirm");
            box.appendChild(el("div", "msg-edit-confirm-desc", message));
            const actions = el("div", "msg-edit-confirm-actions");
            const cancel = el("button", "msg-edit-cancel", "取消");
            const ok = el("button", "msg-edit-send", confirmLabel);
            cancel.type = "button";
            ok.type = "button";
            cancel.addEventListener("click", function () {
                panel._confirmShown = false;
                area.innerHTML = "";
            });
            ok.addEventListener("click", function () {
                if (editDisposed)
                    return;
                ok.disabled = true;
                onConfirm();
            });
            actions.appendChild(cancel);
            actions.appendChild(ok);
            box.appendChild(actions);
            area.appendChild(box);
        }
        // 编辑是否产生过改动（文本/附件集合/引用集合任一变化即视为已编辑）
        function isEdited() {
            if (attachmentsPending)
                return true;
            if (textarea.value !== originalText)
                return true;
            if (keptDocs.length)
                return true;
            if (keptMedia.length !== mediaItems.length)
                return true;
            if (keptQuotes.length !== originalQuotes.length)
                return true;
            if (keptQuotes.some(function (quote, index) {
                return !originalQuotes[index]
                    || originalQuotes[index].text !== quote.text
                    || JSON.stringify(originalQuotes[index].source || null)
                        !== JSON.stringify(quote.source || null);
            }))
                return true;
            return keptMedia.some(function (item, index) {
                return !mediaItems[index] || mediaItems[index].stored_name !== item.stored_name;
            });
        }
        cancelBtn.addEventListener("click", function () {
            if (!isEdited()) {
                // 内容未变：直接退出，不打扰
                exitUserMessageEdit(msg);
                return;
            }
            // 内容已改：防误触确认（放弃将丢失本次编辑）
            showPlanConfirm("修改尚未发送，确认放弃本次修改？", "放弃修改", function () {
                exitUserMessageEdit(msg);
            });
        });
        // Esc 等全局取消入口共用同一防误触规则（未改直接退出，改了先确认）；
        // 确认条已弹出时再按 Esc 视为「确认放弃」直接退出，避免二次弹窗死循环
        panel._requestCancel = function () {
            if (panel._confirmShown) {
                exitUserMessageEdit(msg);
                return;
            }
            if (!isEdited()) {
                exitUserMessageEdit(msg);
                return;
            }
            showPlanConfirm("修改尚未发送，确认放弃本次修改？", "放弃修改", function () {
                exitUserMessageEdit(msg);
            });
        };
        sendBtn.addEventListener("click", function () {
            if (attachmentsPending || editDisposed)
                return;
            const text = textarea.value.trim();
            if (!text && !keptMedia.length) {
                App.toast("消息内容不能为空");
                return;
            }
            if (mode === "insert") {
                App.confirmEditResend({
                    msg: msg, round: data.round, text: text,
                    media: keptMedia, docs: keptDocs.slice(), quotes: keptQuotes.slice(), mode: mode,
                });
                return;
            }
            if (mode === "truncate") {
                // 删除该轮及之后：confirmEditResend 内部先 dry_run 展示明细再确认
                App.confirmEditResend({
                    msg: msg,
                    round: data.round,
                    text: text,
                    media: keptMedia,
                    docs: keptDocs.slice(),
                    quotes: keptQuotes.slice(),
                    mode: mode,
                });
                return;
            }
            // 重新生成该轮：发送前轻量确认（旧回复将被替换，防手抖误触）
            showPlanConfirm("将以当前内容重新生成该轮，旧回复将被替换。", "确认发送", function () {
                if (attachmentsPending)
                    return;
                App.confirmEditResend({
                    msg: msg,
                    round: data.round,
                    text: text,
                    media: keptMedia,
                    docs: keptDocs.slice(),
                    quotes: keptQuotes.slice(),
                    mode: mode,
                });
            });
        });
        buttons.appendChild(cancelBtn);
        buttons.appendChild(sendBtn);
        foot.appendChild(buttons);
        panel.appendChild(foot);
        const planArea = el("div", "msg-edit-plans");
        panel.appendChild(planArea);
        // 预演结果展示区（confirmEditResend 填充）：确认后执行真实删除
        panel._planArea = planArea;
        panel._getText = function () { return textarea.value.trim(); };
        panel._getMedia = function () { return keptMedia.slice(); };
        if (insertBefore)
            msg.insertBefore(panel, msg.firstChild);
        else
            msg.appendChild(panel);
        textarea.focus();
        textarea.selectionStart = textarea.value.length;
        App.autosize && App.autosize();
    }
    function exitUserMessageEdit(msg) {
        if (!msg || !msg.classList.contains("is-editing"))
            return;
        const panel = msg.querySelector(".msg-edit-bubble");
        if (panel) {
            panel.querySelectorAll(".msg-edit-chip-thumb").forEach(function (thumb) {
                if (thumb.tagName === "VIDEO") {
                    try {
                        thumb.pause();
                    }
                    catch (_) { /* ignore */ }
                }
                thumb.removeAttribute("src");
            });
            if (panel._cleanupEdit)
                panel._cleanupEdit();
            panel.remove();
        }
        if (msg._editRestore)
            msg._editRestore.style.display = "";
        msg._editRestore = null;
        msg.classList.remove("is-editing");
    }
    // 媒体徽标文案：固定格式（图标+类型+名称），名称取 media:// 引用的存储文件名
    function mediaChipLabel(mediaRef, icon) {
        if (mediaRef && mediaRef.indexOf("media://") === 0) {
            const stored = mediaRef.slice("media://".length);
            const dot = stored.lastIndexOf(".");
            return icon + " · " + (dot > 0 ? stored.slice(0, dot) : stored);
        }
        return icon;
    }
    // 附件芯片短名：stored_name 去扩展名，超长截断（缩略图芯片用）
    function chipShortName(stored) {
        if (!stored)
            return "附件";
        const dot = stored.lastIndexOf(".");
        const base = dot > 0 ? stored.slice(0, dot) : stored;
        return base.length > 24 ? base.slice(0, 24) + "…" : base;
    }
    function appendStaticAssistant(content) {
        const body = el("div", "msg-assistant-body");
        body.innerHTML = Markdown.render(content);
        App.highlightCodeBlocks(body);
        chatInner.appendChild(body);
        App.updateCodeblockCopyButtons();
    }
    function buildThinkBlock(identity) {
        const wrap = el("div", "think-block");
        const toggle = el("button", "think-toggle");
        toggle.innerHTML = '<span class="think-dots"><i></i><i></i><i></i></span><svg class="icon" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg><span>思考过程</span>';
        // 展开态持久化：历史窗口滚动重建会销毁 DOM；用户显式点按记录到 App
        // 随页面存活的 Map，重建后按“会话+轮次+事件”稳定键恢复展开，滚动
        // 回看长任务时不再一滚全收起。流式/子任务等无稳定身份的不记录。
        function stageKey() {
            if (!identity)
                return null;
            const session = identity.sessionId != null ? identity.sessionId : state.sessionId;
            if (identity.round != null && identity.sourceEvent != null) {
                return "think:" + session + ":" + identity.round + ":e" + identity.sourceEvent;
            }
            if (identity.historyIndex != null)
                return "think:" + session + ":h" + identity.historyIndex;
            return null;
        }
        function rememberStage(open) {
            const key = stageKey();
            if (!key)
                return;
            const map = App.__stageOpenChoices || (App.__stageOpenChoices = new Map());
            if (open)
                map.set(key, true);
            else
                map.delete(key);
            while (map.size > 800)
                map.delete(map.keys().next().value);
        }
        // 实时预览行：流式期间在「思考过程」右侧显示最新一行思考（0.2s 节流，
        // 新行替换旧行）；思考结束/权威回放时清除，收尾保持折叠头原样
        const preview = el("span", "think-preview");
        toggle.appendChild(preview);
        const content = el("div", "think-content");
        toggle.addEventListener("click", function () {
            wrap.classList.toggle("open");
            rememberStage(wrap.classList.contains("open"));
        });
        // 展开态下双击显示区域即可折叠（展开仍走头部按钮）
        content.title = "双击折叠";
        content.addEventListener("dblclick", function () {
            wrap.classList.remove("open");
            rememberStage(false);
        });
        const restoredKey = stageKey();
        if (restoredKey && App.__stageOpenChoices && App.__stageOpenChoices.get(restoredKey)) {
            wrap.classList.add("open");
        }
        wrap.appendChild(toggle);
        wrap.appendChild(content);
        let lastPreview = "";
        let lastPreviewAt = 0;
        let previewTimer = null;
        const PREVIEW_THROTTLE_MS = 200;
        // 思考文本双缓冲：textContent += / getter 在长思考时是 O(全长) 的
        // 序列化+重排热路径（高速模型逐帧追加时页面卡顿主源之一）。
        // 权威文本留在内存缓冲，80ms 批量落 DOM 一次；读取走缓冲（零 DOM 开销）
        let fullText = ""; // 权威累积文本（含未落 DOM 部分）
        let dirtyText = false; // 缓冲有未落 DOM 增量
        let bufTimer = null;
        function flushText() {
            if (bufTimer) {
                clearTimeout(bufTimer);
                bufTimer = null;
            }
            if (!dirtyText)
                return;
            dirtyText = false;
            content.textContent = fullText;
            content.scrollTop = content.scrollHeight;
        }
        function scheduleTextFlush() {
            dirtyText = true;
            if (bufTimer)
                return;
            bufTimer = setTimeout(flushText, 80);
        }
        // 取累积文本的最后一个非空行作为预览（新行替代旧行）；全部空白则清空。
        // 截断到 160 字符兜底：即使环境样式异常（按钮内容宽度计算不受控），
        // DOM 文本也不会超长
        function latestLine() {
            // 走内存缓冲：避免每次渲染预览都对整个思考块做 textContent 序列化
            const text = fullText;
            let tail = "";
            for (let i = text.length - 1; i >= 0; i--) {
                const ch = text[i];
                if (ch === "\n" || ch === "\r") {
                    if (tail)
                        break;
                    continue;
                }
                tail = ch + tail;
            }
            return tail.trim().slice(0, 160);
        }
        function renderPreview() {
            const line = latestLine();
            if (!line) {
                preview.textContent = "";
                preview.title = "";
                return;
            }
            preview.textContent = line;
            preview.title = line;
            lastPreview = line;
            lastPreviewAt = Date.now();
        }
        function schedulePreview() {
            const elapsed = Date.now() - lastPreviewAt;
            if (elapsed >= PREVIEW_THROTTLE_MS) {
                renderPreview();
                return;
            }
            if (previewTimer)
                return;
            previewTimer = setTimeout(function () {
                previewTimer = null;
                renderPreview();
            }, PREVIEW_THROTTLE_MS - elapsed);
        }
        function clearPreview() {
            if (previewTimer) {
                clearTimeout(previewTimer);
                previewTimer = null;
            }
            preview.textContent = "";
            preview.title = "";
            lastPreview = "";
            lastPreviewAt = 0;
        }
        return {
            wrap: wrap,
            setText: function (t) {
                fullText = String(t || "");
                if (bufTimer) {
                    clearTimeout(bufTimer);
                    bufTimer = null;
                }
                dirtyText = false;
                content.textContent = fullText;
                content.scrollTop = content.scrollHeight;
                schedulePreview();
            },
            add: function (delta) {
                fullText += delta;
                // 直播追加即刷新时间戳（大折叠计时取 max，冻结在首 chunk 会系统性偏小；
                // 历史回放走 setText 不经此函数，不污染落盘时间）。
                if (wrap.dataset)
                    wrap.dataset.stageEndTimestamp = new Date().toISOString();
                scheduleTextFlush();
                schedulePreview();
            },
            textContent: function () { return fullText; },
            streaming: function () { wrap.classList.add("is-streaming"); },
            done: function () {
                clearPreview();
                flushText(); // 收尾强制刷出缓冲增量（不能丢最后 80ms 的思考内容）
                wrap.classList.remove("is-streaming");
            },
        };
    }
    // 工具调用块：头部（名称+状态）+ 可展开主体（输入 JSON / 输出文本）
    // 默认折叠，不自动展开（避免流式期间反复撑高页面）；用户点开即可看到
    // 逐帧流入的参数数据。上游把 arguments 整段一次性下发时（部分供应商
    // 不分片），由打字机揭示器按节奏逐步显示，模拟逐帧生成的观感。
    function buildToolBlock(name, identity, options) {
        identity = Object.assign({ sessionId: state.sessionId, agentId: "main" }, identity);
        const wrap = el("div", "tool-block");
        // 调用 ID 落盘到 DOM：流管线收到重放的 tool_calls/tool_start/tool_return
        // 时按 callId 收养历史已渲染的卡片（复用其 ui 控制器），而不是新建重复卡。
        // ui 控制器挂在节点上供收养时取回（msg._editData 同模式）。
        if (identity.toolCallId)
            wrap.dataset.toolCallId = String(identity.toolCallId);
        const head = el("button", "tool-block-head");
        head.type = "button";
        const header = el("div", "tool-block-header");
        const approvalHost = el("div", "permission-inline-actions");
        approvalHost.hidden = true;
        const toolIconClass = "icon tool-block-tool-icon" +
            (App.isBuiltinToolName(name) ? " is-builtin-tool-icon" : "");
        const toolIcon = App.toolIconSvg(name, toolIconClass);
        const statusIcon = '<svg class="icon tool-status-icon tool-spin" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>';
        head.innerHTML =
            toolIcon + statusIcon +
                '<span class="tool-block-generic-action">调用工具</span>' +
                '<span class="tool-block-action" hidden></span>' +
                '<span class="tool-block-name" hidden></span>' +
                '<span class="tool-block-file" hidden></span>' +
                '<span class="tool-block-diff" hidden></span>' +
                '<svg class="icon tool-block-chevron" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
        const body = el("div", "tool-block-body");
        const inLabel = el("span", "tool-io-label", "输入");
        const inPre = el("pre", "tool-io", "");
        const outLabel = el("span", "tool-io-label", "输出");
        const outPre = el("pre", "tool-io tool-io-out", "");
        let currentToolName = "";
        function updateToolIdentity(nextName) {
            currentToolName = String(nextName || "tool");
            const isBuiltin = App.isBuiltinToolName(currentToolName);
            const actionName = App.toolDisplayName(currentToolName);
            const icon = head.querySelector(".tool-block-tool-icon");
            if (icon) {
                icon.outerHTML = App.toolIconSvg(currentToolName, "icon tool-block-tool-icon" + (isBuiltin ? " is-builtin-tool-icon" : ""));
            }
            const genericAction = head.querySelector(".tool-block-generic-action");
            const action = head.querySelector(".tool-block-action");
            const nameNode = head.querySelector(".tool-block-name");
            genericAction.hidden = Boolean(actionName);
            action.hidden = !actionName;
            action.textContent = actionName;
            nameNode.hidden = Boolean(actionName);
            nameNode.textContent = currentToolName;
        }
        updateToolIdentity(name);
        body.appendChild(inLabel);
        body.appendChild(inPre);
        body.appendChild(outLabel);
        body.appendChild(outPre);
        let streaming = false;
        // 打字机揭示器：小增量（真分片流式）直接追加；大增量（整段下发）进入
        // 揭示队列按固定节奏逐步显示。折叠态下照常消费，用户展开时看到的是
        // 进行中的生成过程。
        let revealQueue = ""; // 待揭示的剩余文本
        let revealTimer = null;
        const REVEAL_CHARS_PER_TICK = 6; // 每 tick 揭示的字符数
        const REVEAL_INTERVAL_MS = 16; // tick 间隔（约每秒 375 字符）
        // 输出区上方的展示用 diff 视图（由 setDiff 注入；clearDiff 在文本重设时移除）
        let diffView = null;
        let pendingDiff = null;
        function clearDiff() {
            if (diffView && diffView.parentNode) {
                diffView.parentNode.removeChild(diffView);
            }
            diffView = null;
            pendingDiff = null;
        }
        function ensureDiffView() {
            if (!pendingDiff || diffView)
                return;
            diffView = App.messageDiff.buildDiffView(pendingDiff);
            body.insertBefore(diffView, outLabel);
        }
        function stopReveal() {
            if (revealTimer) {
                clearInterval(revealTimer);
                revealTimer = null;
            }
        }
        function startReveal() {
            if (revealTimer)
                return;
            revealTimer = setInterval(function () {
                if (!revealQueue.length) {
                    stopReveal();
                    return;
                }
                const piece = revealQueue.slice(0, REVEAL_CHARS_PER_TICK);
                revealQueue = revealQueue.slice(REVEAL_CHARS_PER_TICK);
                inPre.textContent += piece;
                inPre.scrollTop = inPre.scrollHeight;
            }, REVEAL_INTERVAL_MS);
        }
        function flushReveal() {
            stopReveal();
            if (revealQueue) {
                inPre.textContent += revealQueue;
                revealQueue = "";
                inPre.scrollTop = inPre.scrollHeight;
            }
        }
        // 展开态持久化（与 buildThinkBlock 同机制）：tool_call_id 全局唯一，
        // 无调用 ID 的旧记录回退“会话+轮次+历史下标”键。identity 会被
        // bindPermission 原地更新，点按时读取最新值，流式绑定后点按同样记录。
        function toolStageKey() {
            if (identity.toolCallId)
                return "tool:" + (identity.agentId || "main") + ":" + identity.toolCallId;
            if (identity.sessionId != null && identity.round != null && identity.historyIndex != null) {
                return "tool:" + identity.sessionId + ":" + identity.round + ":h" + identity.historyIndex;
            }
            return null;
        }
        function rememberToolStage(open) {
            const key = toolStageKey();
            if (!key)
                return;
            const map = App.__stageOpenChoices || (App.__stageOpenChoices = new Map());
            if (open)
                map.set(key, true);
            else
                map.delete(key);
            while (map.size > 800)
                map.delete(map.keys().next().value);
        }
        head.addEventListener("click", function () {
            wrap.classList.toggle("open");
            rememberToolStage(wrap.classList.contains("open"));
            if (wrap.classList.contains("open"))
                ensureDiffView();
        });
        // 展开态下双击主体区域即可折叠（展开仍走头部按钮）
        body.title = "双击折叠";
        body.addEventListener("dblclick", function () {
            wrap.classList.remove("open");
            rememberToolStage(false);
        });
        const restoredToolKey = toolStageKey();
        if (restoredToolKey && App.__stageOpenChoices && App.__stageOpenChoices.get(restoredToolKey)) {
            wrap.classList.add("open");
        }
        header.appendChild(head);
        header.appendChild(approvalHost);
        wrap.appendChild(header);
        wrap.appendChild(body);
        // 头部文件名：完整路径（相对路径按会话工作目录解析），一行放不下
        // 自动换行（CSS 钳制最多两行，超出省略；完整路径始终在 hover title
        // 与点击复制内容里）
        function setFileTagValue(path) {
            const fileTag = head.querySelector(".tool-block-file");
            if (!fileTag)
                return;
            const group = path && typeof path === "object" && Array.isArray(path.paths) ? path : null;
            const full = group ? String(group.label || "") : App.messageDiff.resolveToolPath(path);
            if (!full)
                return;
            const copy = group ? group.paths.map(App.messageDiff.resolveToolPath).join("\n") : full;
            fileTag.textContent = full;
            fileTag.title = copy + "（点击复制并展开/折叠）";
            fileTag.onclick = function () {
                // 点击文件名：复制路径，同时冒泡到 head 触发展开/折叠
                //（不再 stopPropagation，否则该区域单击无法展开）。
                try {
                    navigator.clipboard.writeText(copy).then(function () { }, function () { });
                }
                catch (_) { /* 忽略 */ }
            };
            fileTag.hidden = false;
        }
        let waitingId = null;
        let approvalOutput = "";
        function approvalText(text) {
            // A later tool result always wins over permission status/error text.
            if (!outPre.textContent || ["执行中…", "(无输出)"].includes(outPre.textContent) || outPre.textContent === approvalOutput) {
                outPre.textContent = text;
                approvalOutput = text;
            }
        }
        const ui = {
            wrap: wrap,
            approvalHost: approvalHost,
            get toolCallId() { return identity.toolCallId; },
            get sessionId() { return identity.sessionId; },
            bindPermission: function (next) {
                Object.assign(identity, next);
                if (identity.toolCallId)
                    wrap.dataset.toolCallId = String(identity.toolCallId);
                if (App.permissions && identity.toolCallId)
                    App.permissions.bindTool(ui, identity);
            },
            setApprovalWaiting: function (approval, outcome) {
                if (approval && approval.state === "denied" && approval.task_state !== "suspended") {
                    ui.finish();
                    approvalText("已拒绝，本次工具不会执行。");
                    return;
                }
                const id = approval && approval.id;
                const changed = id !== waitingId;
                waitingId = id;
                wrap.classList.toggle("is-waiting", Boolean(approval));
                if (approval) {
                    streaming = false;
                    flushReveal();
                    wrap.classList.remove("is-streaming", "is-executing");
                    if (approval.args)
                        ui.setInput(FormatUtils.prettyJson(approval.args));
                    if (changed || !approvalOutput)
                        approvalText(approval.state === "pending" ? "等待你的允许或拒绝，本次工具尚未执行。" :
                            approval.state === "denied" ? "已拒绝，本次工具不会执行。" : "已允许，等待执行结果…");
                }
                else if (outcome) {
                    if (["cancelled", "completed", "denied"].includes(outcome.state))
                        ui.finish();
                    const ended = outcome.task_state === "failed" ? "任务异常结束" : "任务已取消";
                    const text = outcome.state === "cancelled" ? (outcome.execution_state === "executing" ? ended + "，工具执行结果未知，请核对实际结果。" :
                        outcome.execution_state === "completed" ? ended + "，此工具已有执行记录，请重新加载会话查看结果。" : ended + "，本次工具未执行。") :
                        outcome.state === "invalidated" ? "审批已更新，请重新确认当前操作。" :
                            outcome.state === "approved" ? "已允许，等待执行结果…" :
                                outcome.state === "denied" ? "已拒绝，本次工具不会执行。" :
                                    outcome.state === "completed" ? "本次工具调用已结束，请重新加载会话查看完整结果。" : "审批已失效，请重新加载会话查看任务状态。";
                    approvalText(text);
                }
                else if (approvalOutput)
                    approvalText("当前没有待确认的审批，请重新加载会话查看任务状态。");
            },
            setApprovalError: function (message) { approvalText("确认失败：" + message); },
            setFileTag: setFileTagValue,
            setName: updateToolIdentity,
            setInput: function (t) {
                // 整体重设（结果返回/历史回放路径）：清空揭示队列直接显示最终文本
                stopReveal();
                revealQueue = "";
                inPre.textContent = t || "{}";
            },
            setOutput: function (t) { outPre.textContent = t || "(无输出)"; },
            // 附带展示用 diff 视图（write_file/edit_file 结果）：渲染在输出区上方，
            // 头部显示完整文件路径（可换行、垂直居中）+ "+N绿 / -M红" 双徽标
            setDiff: function (diff) {
                clearDiff();
                if (!diff)
                    return;
                pendingDiff = diff;
                if (!options || !options.deferDetails || wrap.classList.contains('open'))
                    ensureDiffView();
                const badge = head.querySelector(".tool-block-diff");
                if (badge) {
                    badge.innerHTML = "";
                    const added = Number(diff.lines_added) || 0;
                    const removed = Number(diff.lines_removed) || 0;
                    const skipped = diff.diff_skipped || "";
                    const noStats = diff.stats_complete === false && Array.isArray(diff.files) &&
                        diff.files.every((file) => file.lines_added == null || file.lines_removed == null);
                    if (noStats) {
                        badge.appendChild(el("span", "diff-num", "统计不可用"));
                    }
                    else if (skipped === "unchanged") {
                        badge.appendChild(el("span", "diff-num is-add", "无变化"));
                    }
                    else if (skipped === "file_too_large") {
                        badge.appendChild(el("span", "diff-num is-del", "文件过大"));
                    }
                    else {
                        // +N 恒绿、-M 恒红（不再按 add-only/del-only 整体变色）
                        badge.appendChild(el("span", "diff-num is-add", "+" + added));
                        badge.appendChild(el("span", "diff-num is-del", "-" + removed));
                        if (diff.stats_complete === false)
                            badge.appendChild(el("span", "diff-num", "部分统计"));
                    }
                    badge.hidden = false;
                    wrap.classList.add("has-diff");
                }
                // 头部文件名：完整路径（与参数提取路径共用同一渲染）
                if (Array.isArray(diff.files)) {
                    const paths = diff.files.map((item) => item.path || item.display_path).filter(Boolean);
                    setFileTagValue(paths.length === 1 ? paths[0] : { label: paths.length + " 个文件", paths: paths });
                }
                else
                    setFileTagValue(diff.path);
            },
            clearDiff: function () {
                clearDiff();
                const badge = head.querySelector(".tool-block-diff");
                if (badge) {
                    badge.innerHTML = "";
                    badge.hidden = true;
                }
                const fileTag = head.querySelector(".tool-block-file");
                if (fileTag) {
                    fileTag.textContent = "";
                    fileTag.hidden = true;
                }
                wrap.classList.remove("has-diff");
            },
            // 流式参数生成开始：标记状态并清空输出占位（不改变折叠态）
            beginStream: function () {
                streaming = true;
                wrap.classList.add("is-streaming");
                outPre.textContent = "";
                inPre.textContent = "";
                revealQueue = "";
                clearDiff();
            },
            // 追加一帧参数增量（模型逐 token 生成的 arguments 文本）
            addInputDelta: function (delta) {
                if (!delta)
                    return;
                if (delta.length <= REVEAL_CHARS_PER_TICK * 2) {
                    inPre.textContent += delta;
                    inPre.scrollTop = inPre.scrollHeight;
                }
                else {
                    revealQueue += delta;
                    startReveal();
                }
            },
            // 整体重设输入文本（对象参数路径），同样走揭示队列保持观感一致
            setInputText: function (t) {
                const text = t || "";
                if (!streaming) {
                    inPre.textContent = text;
                    return;
                }
                stopReveal();
                revealQueue = text;
                inPre.textContent = "";
                startReveal();
            },
            // 参数已完整、工具开始执行：等待结果阶段。若揭示动画未播完，
            // 立即放行剩余文本（参数实际已完整）
            executing: function () {
                streaming = false;
                flushReveal();
                wrap.classList.remove("is-streaming");
                wrap.classList.remove("is-waiting");
                wrap.classList.add("is-executing");
                outPre.textContent = "执行中…";
            },
            finish: function () {
                waitingId = null;
                streaming = false;
                flushReveal();
                wrap.classList.remove("is-streaming");
                wrap.classList.remove("is-executing");
                wrap.classList.remove("is-waiting");
                const statusIcon = head.querySelector(".tool-status-icon");
                if (statusIcon)
                    statusIcon.remove();
                if (!outPre.textContent || outPre.textContent === "执行中…") {
                    outPre.textContent = "(无输出)";
                }
            },
        };
        ui.bindPermission(identity);
        wrap._toolBlockUi = ui;
        return ui;
    }
    // ---------- 导出（供其它模块经 App.* 调用） ----------
    App.attachUserEditAction = attachUserEditAction;
    App.exitUserMessageEdit = exitUserMessageEdit;
    App.attachLiveRoundEntry = attachLiveRoundEntry;
    // confirmEditResend 定义在 chat.js（发送编排）并由其导出，此处不重复赋值：
    // 若引用本模块不存在的函数名会在加载期抛 ReferenceError 中断整个 IIFE，
    // 导致后续所有 App.* 导出丢失（历史加载报 xxx is not a function）
    App.cancelAnyUserEdit = function () {
        // Esc 等全局入口：取消当前会话所有处于编辑态的用户消息。
        // 有未发送修改时走面板内确认（panel._requestCancel，与取消按钮同规则），
        // 无修改直接退出不打扰
        document.querySelectorAll(".msg.msg-user.is-editing").forEach(function (msg) {
            const panel = msg.querySelector(".msg-edit-panel");
            if (panel && typeof panel._requestCancel === "function") {
                panel._requestCancel();
                return;
            }
            exitUserMessageEdit(msg);
        });
    };
    App.buildThinkBlock = buildThinkBlock;
    App.buildToolBlock = buildToolBlock;
    // 以下为 messages.js 拆分新增导出（原模块内直接调用，现跨文件经 App.* 调用）
    App.contentToPlainText = contentToPlainText;
})(window.App);
