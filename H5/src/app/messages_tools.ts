/**
 * 工具结果渲染与子任务块（原 messages.js 2434-3190 行拆出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 */
(function (App: any) {
  "use strict";
  const {
    el, state, toast, chatInner
  } = App;

  // ---------- 展示用 diff 视图（write_file / edit_file 结果） ----------
  // 保留消息编排入口；差异展示与路径标签由独立模块实现。
  function buildDiffView(diffObj: any) { return App.messageDiff.buildDiffView(diffObj); }
  function extractPathFromArgsValue(args: any, name: any) { return App.messageDiff.extractPathFromArgsValue(args, name); }
  function resolveToolPath(path: any) { return App.messageDiff.resolveToolPath(path); }
  function extractPathFromDiffText(diffText: any) { return App.messageDiff.extractPathFromDiffText(diffText); }

  /** read_media 结果的图片条：可展开的缩略图网格，点击缩略图进预览弹窗。
   * 幂等：重复应用先移除旧条（保留展开态）；解析失败或无条目时不画条。 */
  function renderReadMediaStrip(block: any, resultText: any) {
    if (!block || !block.wrap || typeof ReadMediaStrip === "undefined") return;
    const parsed = ReadMediaStrip.parseReadMediaResult(resultText);
    const old = block.wrap.querySelector(".read-media-strip");
    const wasOpen = Boolean(old && old.classList.contains("open"));
    if (old) old.remove();
    if (!parsed) return;
    const sessionId = (block.sessionId != null && block.sessionId !== "")
      ? block.sessionId : state.sessionId;
    const model = ReadMediaStrip.buildStripItems(API, parsed, sessionId);
    if (!model.items.length) return;
    const strip = ReadMediaStrip.buildMediaStrip({
      el: el,
      onPreview: function(item: any) {
        if (App.openMediaPreview && item.url) {
          App.openMediaPreview({ type: item.previewType, src: item.url, title: item.name });
        }
      },
    }, model.headerText, model.items);
    const body = block.wrap.querySelector(".tool-block-body");
    (body || block.wrap).appendChild(strip);
    if (wasOpen) {
      const head = strip.querySelector(".read-media-head");
      if (head) head.click();
    }
  }

  /** 工具输出统一入口：带 file_diff 的结构化结果渲染 diff 视图 + 统计摘要文本，
   * 其余一律按原文纯文本输出（MCP 版同名工具 / 旧历史无 diff 自动回退）。
   * args（可选，对象或 JSON 文本）：工具原始参数——用于在 _file_diff 无 path
   * 字段的旧数据里兜底提取完整文件路径（path，兼容旧名 full_file_name），以及为 read_file /
   * write_file / search_files 等无 diff 结果在标题栏显示路径/目录信息。 */
  function applyToolResult(block: any, name: any, resultText: any, fileDiff: any, args: any) {
    if (name === "edit_file" || name === "write_file" || name === "apply_patch" || name === "run_command" || name === "poll_command" || name === "write_stdin" || name === "stop_command") {
      const toolIcon = block && block.wrap && block.wrap.querySelector(".tool-block-tool-icon");
      if (toolIcon) {
        const failed = name === "run_command" || name === "poll_command" || name === "write_stdin" || name === "stop_command"
          ? isCommandFailureResult(resultText)
          : isToolFailureResult(resultText);
        toolIcon.classList.toggle("is-failed", failed);
      }
    }
    let parsed = null;
    let patchResult = null;
    if (name === "apply_patch") {
      try { patchResult = JSON.parse(resultText); } catch (_) { /* 旧结果保留原文 */ }
      if (!patchResult || !Array.isArray(patchResult.files) ||
          patchResult.files.some((file: any) => !file || typeof file !== "object")) patchResult = null;
    }
    if (fileDiff && typeof fileDiff === "object") {
      try {
        const obj = JSON.parse(resultText);
        if (obj && typeof obj === "object" && obj.path != null) parsed = obj;
      } catch (_) { /* 回退纯文本 */ }
      // 旧数据 _file_diff 无 path 字段：从参数或 diff 文件头兜底提取
      if (parsed && !fileDiff.path) {
        fileDiff.path = extractPathFromArgsValue(args, name)
          || extractPathFromDiffText(fileDiff.diff) || "";
      }
    }
    if (patchResult) {
      const lines = [String(patchResult.error || patchResult.message || "补丁结果")];
      const statuses: any = {applied: "已应用", rolled_back: "已回滚", rollback_failed: "回滚失败", commit_failed: "提交失败", not_applied: "未执行"};
      patchResult.files.forEach(function(file: any) {
        let text = String(file.path || file.display_path || "文件") + " · " + (statuses[file.status] || file.status || "变更");
        if (file.status === "applied" && file.lines_added != null && file.lines_removed != null) text += " +" + file.lines_added + " -" + file.lines_removed;
        if (file.diff_skipped === "file_too_large") text += " · 文件过大，diff/统计不可用";
        if (file.history_recorded === true) text += " · 已记录回退版本";
        else if (file.history_recorded === false && file.history_reason !== "unchanged" && file.status === "applied") text += " · 未记录回退版本";
        if (file.needs_attention || file.status === "rollback_failed") text = "⚠ 需核对磁盘状态：" + text;
        lines.push(text);
      });
      if (patchResult.needs_attention) lines.push("请先重新读取标记的文件，核对磁盘内容后再继续修改。");
      block.setOutput(lines.join("\n"));
      if (fileDiff && patchResult.success !== false) block.setDiff(fileDiff);
      else if (block.clearDiff) block.clearDiff();
      const paths = patchResult.files.map((file: any) => file.path || file.display_path).filter(Boolean);
      block.setFileTag(paths.length === 1 ? paths[0] : {label: paths.length + " 个文件", paths: paths});
    } else if (name === "run_command" || name === "poll_command" || name === "write_stdin" || name === "stop_command") {
      let commandResult = null;
      try { commandResult = JSON.parse(resultText); } catch (_) { /* 旧版纯文本 */ }
      if (commandResult && typeof commandResult.message === "string") {
        const parts = [commandResult.message];
          if (commandResult.input_status) {
            const labels: any = {accepted: "已写入", queued: "排队中", writing: "写入中", failed: "失败", not_sent: "未发送"};
            parts.push("输入：" + (labels[commandResult.input_status] || commandResult.input_status));
          }
          if (commandResult.input_error) parts.push("输入错误：" + commandResult.input_error);
          if (commandResult.output_reset) parts.push("日志已重置，以下从头读取。");
          if (commandResult.cursor_warning) parts.push(commandResult.cursor_warning);
        if (commandResult.stdout) parts.push("--- stdout ---\n" + commandResult.stdout);
        if (commandResult.stderr) parts.push("--- stderr ---\n" + commandResult.stderr);
        if (commandResult.output_tail) parts.push("--- 输出末尾 ---\n" + commandResult.output_tail);
        if (commandResult.output) parts.push("--- 新增输出 ---\n" + commandResult.output);
        block.setOutput(parts.join("\n"));
      } else {
        block.setOutput(resultText);
      }
    } else if (parsed) {
      const statsBits = [];
      if (parsed.lines_added || parsed.lines_removed) {
        statsBits.push("diff +" + parsed.lines_added + " -" + parsed.lines_removed + " 行");
      } else if (parsed.diff_skipped) {
        statsBits.push(parsed.diff_skipped === "unchanged" ? "内容无变化" : "文件过大，跳过 diff");
      } else if (parsed.created != null) {
        statsBits.push(parsed.created ? "新建文件" : parsed.action === "append" ? "追加文件" : "全文覆盖");
      }
      if (parsed.history_recorded === true) statsBits.push("已记录回退版本");
      else if (parsed.history_recorded === false && parsed.history_reason !== "unchanged") {
        const reasons: any = {
          file_too_large: "文件超出版本记录上限，未记录回退版本",
          history_quota_exceeded: "会话快照配额已满，未记录本次回退版本",
          history_version_limit: "文件版本数已达上限，未记录本次回退版本",
        };
        statsBits.push(reasons[parsed.history_reason] || "版本未记录，无法从本次变更回退");
      }
      const head = String(parsed.message || (name === "edit_file" ? "[edit_file] 已修改" : "[write_file] 已写入"));
      const note = statsBits.length ? head + "（" + statsBits.join("，") + "）" : head;
      block.setOutput(note);
      block.setDiff(fileDiff);
    } else {
      block.setOutput(resultText);
    }
    // 标题栏路径：diff 未带路径（含无 diff 的 read_file / search_files /
    // 旧数据 write_file）时从参数补齐；diff 已带路径时 setDiff 已设置
    if (!patchResult && (!fileDiff || !fileDiff.path)) {
      block.setFileTag(extractPathFromArgsValue(args, name));
    }
    // 读取媒体：输出文本之外，追加可展开的图片缩略图条
    if (name === "read_media") {
      renderReadMediaStrip(block, resultText);
    }
  }

  function isToolFailureResult(resultText: any) {
    let value = resultText;
    if (typeof value === "string") {
      try { value = JSON.parse(value); } catch (_) { /* 按普通文本错误识别 */ }
    }
    if (value && typeof value === "object") {
      if (value.error != null && value.error !== "") return true;
      if (value.ok === false || value.success === false || value.isError === true || value.is_error === true) return true;
      if (typeof value.status === "string" && /^(error|failed|failure|aborted)$/i.test(value.status.trim())) return true;
      return false;
    }
    return /^(?:error|failed|failure|错误|失败)(?:\b|[:：\s])/i.test(String(value || "").trim());
  }

  function isCommandFailureResult(resultText: any) {
    if (typeof resultText === "string") {
      try {
        const result = JSON.parse(resultText);
        if (result && typeof result === "object") {
            if (result.error || result.input_error || result.input_status === "failed") return true;
          if (result.timed_out === true) return true;
          if (result.status === "stopped" || result.status === "failed") return true;
          if (typeof result.exit_code === "number") return result.exit_code !== 0;
        }
      } catch (_) { /* 旧版纯文本结果 */ }
    }
    const text = typeof resultText === "string"
      ? resultText
      : JSON.stringify(resultText == null ? "" : resultText);
    // run_command 的首行是工具生成的元数据，包含退出码和超时状态；只检查
    // 这一行，避免 stdout/stderr 正文（例如读取文件中的“stderr”）造成误判。
    const header = text.split(/\r?\n/, 1)[0];
    const exitCode = /\bexit=(-?\d+)\b/i.exec(header);
    if (/命令超时|已超过 timeout=/i.test(header)) return true;
    if (exitCode) return Number(exitCode[1]) !== 0;
    return /^(?:\[run_command\]\s*)?(?:error|failed|failure|错误|失败)(?:\b|[:：\s])/i.test(header.trim());
  }

  // ---------- 子任务块（sub_agent） ----------
  // 父智能体经 sub_agent 工具派发的子任务独立渲染块：完整保存思考/正文/
  // 工具轨迹/todo，按 agent_id 聚合。SSE（start/delta/model_call/tool_start/
  // tool_result/todo/done）与历史回放（history_parser 聚合的 agentBlock
  // 记录）共用同一状态机；首次运行自动展开，用户手动折叠后保留状态，
  // done 后折叠为摘要态，点击头部可再展开。

  const SUB_AGENT_STATUS_TEXT: any = {
    done: "已完成",
    error: "出错",
    stopped: "已停止",
    interrupted: "已中断",
    timeout: "超时",
    max_rounds: "轮次上限",
  };
  const SUB_AGENT_ERROR_STATUSES = ["error", "stopped", "interrupted", "timeout", "max_rounds"];

  function subAgentTaskSummary(task: any) {
    const text = String(task || "");
    const firstLine = text.split("\n").find(function(line: any) { return line.trim(); }) || "";
    return firstLine.trim().slice(0, 80) || "子任务";
  }

  function buildSubAgentBlock(sessionId: any) {
    const agentSessionId = sessionId || state.sessionId;
    const wrap = el("div", "agent-block is-running");
    const header = el("div", "agent-header");
    const head = el("button", "agent-head");
    head.type = "button";
    const iconWrap = el("span", "agent-icon");
    iconWrap.innerHTML = App.toolIconSvg("sub_agent", "icon is-builtin-tool-icon");
    const title = el("span", "agent-title", "子任务");
    const summary = el("span", "agent-summary");
    const badge = el("span", "agent-badge", "运行中");
    const chevron = el("span", "agent-chevron");
    chevron.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
    head.appendChild(iconWrap);
    head.appendChild(title);
    head.appendChild(summary);
    head.appendChild(badge);
    head.appendChild(chevron);
    const body = el("div", "agent-body");
    const stopAgentBtn = el("button", "agent-stop", "停止");
    stopAgentBtn.type = "button";
    stopAgentBtn.title = "仅停止本子任务，父任务和其他子任务继续";
    stopAgentBtn.hidden = true;
    header.appendChild(head);
    header.appendChild(stopAgentBtn);
    const approvalHost = el("div", "permission-inline-actions");
    approvalHost.hidden = true;
    header.appendChild(approvalHost);
    wrap.appendChild(header);
    wrap.appendChild(body);

    const dispatchDetails = el("details", "agent-dispatch");
    dispatchDetails.appendChild(el("summary", "agent-label", "调用参数"));
    const dispatchPre = el("pre", "agent-pre");
    dispatchDetails.appendChild(dispatchPre);
    let dispatchText = "";
    let hasStarted = false;

    let taskSummary = "";
    let taskText = "";
    const taskSection = el("div", "agent-task-section");
    let todoItems: any = null;
    let rounds: any = [];          // [{seq, el, thinkBlock, contentLabel, contentEl, toolEntries: Map}]
    let status = "running";
    let finalReplyText = "";
    let usageTotal: any = null;
    let errorText = "";
    let roundsLimit: any = null;
    let isDone = false;
    let agentId = "";
    let parentRound: any = null;
    let approvalWaiting = false;
    let activityText = "运行中";
    function applyProgress(evt: any) {
      if (isDone) return;
      const labels: any = { queued: "排队中", model: "请求模型", tools: "执行工具", retrying: "重试中",
        compacting: "压缩上下文", waiting_approval: "等待确认", waiting_budget: "等待调整预算" };
      activityText = labels[evt.activity] || "运行中";
      const elapsed = Number(evt.elapsed_seconds);
      if (Number.isFinite(elapsed) && elapsed > 0) activityText += " · " + Math.floor(elapsed) + "秒";
      if (!approvalWaiting) badge.textContent = activityText;
      const detail = typeof evt.detail === "string" ? evt.detail : "";
      badge.title = [detail, evt.rounds != null ? "已调用模型 " + evt.rounds + " 轮" : "",
        Number(evt.idle_seconds) >= 5 ? "距上次输出 " + Math.floor(evt.idle_seconds) + " 秒" : ""].filter(Boolean).join("；");
    }
    function bindPermissions() {
      if (App.permissions && agentId) App.permissions.bindAgent(ui, { sessionId: agentSessionId, agentId: agentId, round: parentRound });
    }
    let hasUserControlledExpansion = false;
    stopAgentBtn.addEventListener("click", async function () {
      if (!agentId || isDone || stopAgentBtn.disabled) return;
      stopAgentBtn.disabled = true;
      stopAgentBtn.textContent = "停止中…";
      try {
        const result = await API.stopSubAgent(agentSessionId, agentId);
        if (!result.ok) {
          stopAgentBtn.hidden = true;
          toast("子任务已结束或不在运行中");
        }
      } catch (err) {
        if (!isDone) {
          stopAgentBtn.disabled = false;
          stopAgentBtn.textContent = "停止";
        }
        toast("停止子任务失败：" + (err as any).message);
      }
    });

    head.addEventListener("click", function () {
      if (!body.children.length) return;
      wrap.classList.toggle("open");
      hasUserControlledExpansion = true;
      head.setAttribute("aria-expanded", wrap.classList.contains("open") ? "true" : "false");
    });
    body.title = "双击折叠";
    body.addEventListener("dblclick", function () {
      if (!wrap.classList.contains("open")) return;
      wrap.classList.remove("open");
      hasUserControlledExpansion = true;
      head.setAttribute("aria-expanded", "false");
    });

    function appendLabeledPre(labelText: any, cls: any) {
      body.appendChild(el("div", "agent-label", labelText));
      const pre = el("pre", "agent-pre" + (cls ? " " + cls : ""));
      body.appendChild(pre);
      return pre;
    }

    function autoOpen() {
      if (!hasUserControlledExpansion && !wrap.classList.contains("open")) {
        wrap.classList.add("open");
        head.setAttribute("aria-expanded", "true");
      }
    }

    function scrollToPreBottom(pre: any) {
      if (pre) pre.scrollTop = pre.scrollHeight;
    }

    /** 状态徽标与块级状态类。 */
    function applyStatus(nextStatus: any) {
      status = nextStatus;
      isDone = nextStatus !== "running";
      stopAgentBtn.hidden = isDone;
      wrap.classList.remove("is-running", "is-done", "is-error");
      if (nextStatus === "running") wrap.classList.add("is-running");
      else if (SUB_AGENT_ERROR_STATUSES.indexOf(nextStatus) >= 0) wrap.classList.add("is-error");
      else wrap.classList.add("is-done");
      badge.textContent = nextStatus === "running" ? "运行中" : (SUB_AGENT_STATUS_TEXT[nextStatus] || nextStatus);
      badge.title = "";
      if (isDone) {
        approvalWaiting = false;
        wrap.classList.remove("is-waiting");
      }
    }

    function findRound(seq: any) {
      for (let i = rounds.length - 1; i >= 0; i -= 1) {
        if (rounds[i].seq === seq) return rounds[i];
      }
      return null;
    }

    /** model_call：新开一轮分区。思考复用主区 think-block（默认折叠+流式
     * 三点动画），正文区始终显示（空时隐藏标题），工具行复用
     * 主区 tool-block（默认折叠）。authoritative=true 表示来自 model_call
     * 权威整轮数据（或历史回放）：思考/正文按整轮数据替换（delta 流式期间
     * 已实时显示同内容），避免重复叠加。 */
    function beginRound(evt: any, authoritative?: any) {
      let round = findRound(evt.seq);
      if (!round && evt.seq != null && rounds.length) {
        const last = rounds[rounds.length - 1];
        // 兼容旧流：无 seq 的 delta 先建隐式轮，后续 model_call 认领归位
        if (last.seq == null && !last.hydrated) {
          last.seq = evt.seq;
          const labelEl = last.el.querySelector(".agent-round-label");
          if (labelEl) labelEl.textContent = "第 " + evt.seq + " 轮";
          round = last;
        }
      }
      if (!round) {
        const section = el("div", "agent-round");
        if (rounds.length) section.appendChild(el("div", "agent-round-divider"));
        section.appendChild(el("div", "agent-round-label", "第 " + (evt.seq != null ? evt.seq : rounds.length + 1) + " 轮"));
        const thinkBlock = App.buildThinkBlock();          // 思考：默认折叠 + 流式三点
        const contentLabel = el("div", "agent-label is-empty", "正文"); // 正文始终显示，无内容隐藏标题
        const contentEl = el("div", "agent-content");
        const toolsWrap = el("div", "agent-tools");
        section.appendChild(thinkBlock.wrap);
        section.appendChild(contentLabel);
        section.appendChild(contentEl);
        section.appendChild(toolsWrap);
        round = {
          seq: evt.seq,
          el: section,
          thinkBlock: thinkBlock,
          contentLabel: contentLabel,
          contentEl: contentEl,
          contentText: "",
          toolsWrap: toolsWrap,
          toolEntries: new Map(),
          hydrated: false,
        };
        rounds.push(round);
        body.appendChild(section);
      }
      if (typeof evt.reasoning_content === "string" && evt.reasoning_content) {
        // 替换式写入：与 delta 流式期间累积的内容一致，防止重复叠加
        round.thinkBlock.setText(evt.reasoning_content);
        round.thinkBlock.wrap.classList.remove("is-empty");
      } else if (authoritative && !round.thinkBlock.textContent()) {
        round.thinkBlock.wrap.classList.add("is-empty");
      }
      if (typeof evt.content === "string" && evt.content) {
        round.contentText = evt.content;
        App.renderPreservingWidgets(round.contentEl, Markdown.render(evt.content));
        round.contentLabel.classList.remove("is-empty");
        App.highlightCodeBlocks(round.contentEl);
        App.updateCodeblockCopyButtons();
      } else if (authoritative && !round.contentText) {
        round.contentLabel.classList.add("is-empty");
      }
      (evt.tool_calls || []).forEach(function(call: any) {
        const fn = call.function || {};
        const entry = findToolEntry(round, call.id) || addToolLine(round, fn.name || call.name || "tool", fn.arguments || "", null, call.id);
        // tool_calls 声明行登记：tool_start/tool_result 按 id 回填，避免重复建行
        if (call.id && !round.toolEntries.has(call.id)) round.toolEntries.set(call.id, entry);
      });
      if (authoritative) {
        round.hydrated = true;
        round.thinkBlock.done(); // 权威数据到达：清除流式三点指示
      }
      return round;
    }

    /** 参数文本统一序列化：tool_start.arguments / model_call tool_calls 可能是对象。 */
    function normalizeArgsText(argsText: any) {
      if (argsText == null) return "";
      if (typeof argsText === "string") return argsText;
      try { return JSON.stringify(argsText, null, 2); } catch (e) { return String(argsText); }
    }

    /** 轮内工具行：复用主区 tool-block（头部折叠，主体=输入/输出），返回
     * 与主区一致的 ui 控制器（tool_start 标执行中 / tool_result 填结果）。 */
    function addToolLine(round: any, name: any, argsText: any, resultText: any, toolCallId: any) {
      const block = App.buildToolBlock(name, { sessionId: agentSessionId, agentId: agentId, toolCallId: toolCallId, round: parentRound });
      if (argsText != null && argsText !== "") block.setInput(normalizeArgsText(argsText));
      if (resultText != null) block.setOutput(resultText);
      round.toolsWrap.appendChild(block.wrap);
      return block;
    }

    function findToolEntry(round: any, toolCallId: any) {
      if (toolCallId && round.toolEntries.has(toolCallId)) return round.toolEntries.get(toolCallId);
      return null;
    }

    function currentRound() {
      return rounds.length ? rounds[rounds.length - 1] : null;
    }

    /** 流式期间正文 md 节流重渲染：与主消息区一致用 Markdown.render 全量重建，
     * 但按帧直接渲染会高频重排（每块含代码高亮/复制按钮重建），40ms 合并一次。 */
    function scheduleContentRender(round: any) {
      if (round.contentRenderTimer) return;
      round.contentRenderTimer = setTimeout(function () {
        round.contentRenderTimer = null;
        if (round.contentText) {
          App.renderPreservingWidgets(round.contentEl, Markdown.render(round.contentText));
          App.highlightCodeBlocks(round.contentEl);
          App.updateCodeblockCopyButtons();
        }
      }, 40);
    }

    /** SSE phase=delta：实时追加到所属轮；seq 已知但轮未建时先建隐式轮占位
     * （新一轮 delta 先于 model_call 到达，不能落进上一轮）。 */
    function appendLiveDelta(evt: any) {
      const round = currentRoundBySeq(evt) || beginRound({ seq: evt.seq != null ? evt.seq : null });
      const rc = typeof evt.reasoning_delta === "string" ? evt.reasoning_delta : "";
      const ct = typeof evt.content_delta === "string" ? evt.content_delta : "";
      if (rc) {
        round.thinkBlock.wrap.classList.remove("is-empty");
        round.thinkBlock.add(rc);
        round.thinkBlock.streaming();
      }
      if (ct) {
        round.contentText = (round.contentText || "") + ct;
        round.contentLabel.classList.remove("is-empty");
        round.contentEl.classList.remove("is-empty");
        scheduleContentRender(round);
      }
      autoOpen();
    }

    function currentRoundBySeq(evt: any) {
      // seq 已知：严格匹配所属轮（找不到交给调用方建隐式轮），避免串轮
      if (evt.seq != null) return findRound(evt.seq);
      // 旧流兼容：无 seq 的 delta 落到最新轮（无轮由调用方建隐式轮）
      return rounds.length ? rounds[rounds.length - 1] : null;
    }

    /** tool_start：执行中占位行（tool_result 到达时回填）。
     * 后端字段为 function_name（旧数据兼容 tool_name）；若 model_call 已按
     * tool_calls 预建声明行，则复用该块并标执行中，避免重复。 */
    function addToolEntry(evt: any) {
      const round = currentRoundBySeq(evt) || beginRound({ seq: evt.seq });
      const toolCallId = evt.tool_call_id || "";
      let entry = findToolEntry(round, toolCallId);
      if (entry) {
        entry.executing();
        return;
      }
      entry = addToolLine(round, evt.function_name || evt.tool_name || "tool", evt.arguments, null, toolCallId);
      entry.executing();
      if (toolCallId) round.toolEntries.set(toolCallId, entry);
    }

    /** tool_result：按 tool_call_id 回填结果（缺 start 时补建整行）。 */
    function fillToolEntry(evt: any) {
      const round = currentRoundBySeq(evt) || beginRound({ seq: evt.seq });
      let entry = findToolEntry(round, evt.tool_call_id);
      if (!entry) {
        entry = addToolLine(round, evt.tool_name || "tool", evt.arguments, null, evt.tool_call_id);
        if (evt.tool_call_id) round.toolEntries.set(evt.tool_call_id, entry);
      }
      entry.finish();
      const resultText = typeof evt.result === "string" ? evt.result : JSON.stringify(evt.result, null, 2);
      applyToolResult(entry, evt.tool_name, resultText, evt.file_diff, evt.arguments);
    }

    function renderTodo(items: any) {
      if (!items || !items.length) return null;
      const box = el("div", "agent-todo");
      items.forEach(function(item: any) {
        const row = el("div", "agent-todo-item is-" + (item.status || "pending"));
        const mark = item.status === "done" ? "✓" : (item.status === "in_progress" ? "○" : "·");
        row.appendChild(el("span", "agent-todo-mark", mark));
        row.appendChild(el("span", "agent-todo-text", String(item.content || item.text || "")));
        box.appendChild(row);
      });
      return box;
    }

    /** start / todo / done 统一重渲染头部与任务/计划/收尾区。 */
    function renderBody() {
      // 只替换任务/计划区域，保留调用参数的折叠状态和已生成的轮次内容。
      if (taskSection.parentNode) taskSection.parentNode.removeChild(taskSection);
      while (taskSection.firstChild) taskSection.removeChild(taskSection.firstChild);
      if (taskText) {
        taskSection.appendChild(el("div", "agent-label", "任务"));
        taskSection.appendChild(el("pre", "agent-pre agent-task", taskText));
      }
      if (todoItems && todoItems.length) {
        taskSection.appendChild(el("div", "agent-label", "计划"));
        const todoBox = renderTodo(todoItems);
        if (todoBox) taskSection.appendChild(todoBox);
      }
      const keep = [];
      while (body.firstChild) keep.push(body.removeChild(body.firstChild));
      if (taskSection.children.length) body.appendChild(taskSection);
      keep.forEach(function(node: any) { body.appendChild(node); });
    }

    function applyDone(evt: any) {
      if (isDone) return;
      if (evt && evt.status) applyStatus(evt.status);
      finalReplyText = evt && typeof evt.final_reply === "string" ? evt.final_reply : finalReplyText;
      usageTotal = evt && evt.usage_total && typeof evt.usage_total === "object" ? evt.usage_total : usageTotal;
      errorText = evt && typeof evt.error === "string" ? evt.error : errorText;
      // 收尾清理：未到达 model_call 的轮次清掉思考流式三点；
      // 待渲染的正文 md 定时器立即刷出（避免 done 先于 40ms 节流到达丢字）
      rounds.forEach(function(r: any) {
        r.thinkBlock.done();
        if (r.contentRenderTimer) {
          clearTimeout(r.contentRenderTimer);
          r.contentRenderTimer = null;
          if (r.contentText) {
            App.renderPreservingWidgets(r.contentEl, Markdown.render(r.contentText));
            App.highlightCodeBlocks(r.contentEl);
            App.updateCodeblockCopyButtons();
          }
        }
      });
      // 收尾区：最终回复（按 md 渲染；模型产出多为结构化文本，可读性更好）
      if (finalReplyText) {
        const finalPre = appendLabeledPre("最终回复 → 父智能体", "agent-final-reply");
        finalPre.innerHTML = Markdown.render(finalReplyText);
        App.highlightCodeBlocks(finalPre);
        App.updateCodeblockCopyButtons();
      }
      if (usageTotal && Number(usageTotal.total_tokens) > 0) {
        body.appendChild(el("div", "agent-usage", FormatUtils.usageText(usageTotal)));
      }
      if (errorText) {
        body.appendChild(el("div", "agent-error-detail", errorText));
      }
      // 头部徽标补充轮次（已完成 · 3 轮）
      if (evt && evt.rounds != null) {
        badge.textContent = badge.textContent + " · " + evt.rounds + " 轮";
      }
      wrap.classList.remove("open"); // done 后折叠为摘要态（可点头部展开）
      head.setAttribute("aria-expanded", "false");
    }

    /** tool_return.sub_agent 到达：块尾追加"已返回父智能体"引用条。 */
    function markReturned(subInfo: any) {
      if (wrap.querySelector(".agent-returned")) return;
      const bar = el("div", "agent-returned", "✔ 最终回复已返回父智能体");
      wrap.appendChild(bar);
      const info = subInfo && subInfo.usage_total && Number(subInfo.usage_total.total_tokens) > 0
        ? " · " + FormatUtils.usageText(subInfo.usage_total)
        : "";
      if (info) bar.title = "子任务消耗 " + info;
    }

    const ui = {
      wrap: wrap,
      approvalHost: approvalHost,
      setApprovalWaiting: function(approval: any) {
        if (isDone) return;
        const waiting = approval && (approval.state === "pending" || !approval.state || approval.task_state === "suspended");
        if (!waiting && !approvalWaiting) return;
        approvalWaiting = Boolean(waiting);
        wrap.classList.toggle("is-waiting", approvalWaiting);
        const next = waiting ? (approval.task_state === "suspended" ? "待恢复" : "等待确认") : activityText;
        if (badge.textContent !== next) badge.textContent = next;
      },
      applyApproval: function(approval: any) {
        agentId = approval.agent_id; parentRound = approval.round || parentRound;
        bindPermissions();
        const round = currentRoundBySeq({ seq: approval.seq }) || beginRound({ seq: approval.seq });
        let entry = findToolEntry(round, approval.tool_call_id);
        if (!entry) {
          entry = addToolLine(round, approval.tool, approval.args, null, approval.tool_call_id);
          round.toolEntries.set(approval.tool_call_id, entry);
        }
        if (entry.bindPermission) entry.bindPermission({ sessionId: agentSessionId, agentId: agentId, toolCallId: approval.tool_call_id, round: parentRound });
      },
      setDispatchInput: function(value: any) {
        dispatchText = typeof value === "string" ? value : JSON.stringify(value, null, 2);
        let parsed = null;
        try { parsed = JSON.parse(dispatchText); } catch (_) { /* 参数尚未生成完整 */ }
        dispatchPre.textContent = parsed ? JSON.stringify(parsed, null, 2) : dispatchText;
        if (!dispatchDetails.parentNode) body.appendChild(dispatchDetails);
        if (!hasStarted && !isDone) {
          badge.textContent = "准备中";
          if (parsed && parsed.task) summary.textContent = subAgentTaskSummary(parsed.task);
        }
      },
      applyEvent: function(evt: any) {
        if (!evt || typeof evt !== "object") return;
        if (evt.agent_id) agentId = evt.agent_id;
        if (evt.round) parentRound = evt.round;
        bindPermissions();
        if (agentId && !isDone) stopAgentBtn.hidden = false;
        const phase = evt.phase;
        if (phase === "start") {
          hasStarted = true;
          taskText = String(evt.task || "");
          taskSummary = subAgentTaskSummary(taskText);
          summary.textContent = taskSummary;
          if (Array.isArray(evt.todo) && evt.todo.length) todoItems = evt.todo;
          if (evt.rounds_limit != null) roundsLimit = Number(evt.rounds_limit);
          activityText = evt.queued ? "排队中" : "运行中";
          if (!approvalWaiting && !isDone) badge.textContent = activityText;
          head.title = "模型调用轮次上限：" + (roundsLimit > 0 ? roundsLimit : "不限制") +
            "；整体超时：" + (evt.timeout_seconds == null ? "未记录" :
              Number(evt.timeout_seconds) > 0 ? evt.timeout_seconds + " 秒" : "不限制");
          renderBody();
          autoOpen();
          return;
        }
        if (phase === "progress" || phase === "heartbeat") { applyProgress(evt); return; }
        if (phase === "delta") { appendLiveDelta(evt); return; }
        if (phase === "model_call") { beginRound(evt, true); return; }
        if (phase === "tool_start") { addToolEntry(evt); return; }
        if (phase === "tool_result") { fillToolEntry(evt); return; }
        if (phase === "todo") {
          todoItems = Array.isArray(evt.todos) ? evt.todos : todoItems;
          renderBody();
          return;
        }
        if (phase === "done") { applyDone(evt); return; }
        if (phase === "notice") {
          if (evt.queued === false && !isDone) {
            activityText = "运行中";
            if (!approvalWaiting) badge.textContent = activityText;
          }
          // 交付保障重试提示（空收尾重试/断流续跑/todo 提醒）：
          // 以浅色条目追加到块内；历史回放侧 history_parser 对该 phase
          // 与历史回放使用同一提示文案。
          const message = typeof evt.message === "string" ? evt.message : "";
          if (message) {
            body.appendChild(el("div", "agent-notice", "↻ " + message));
            autoOpen();
          }
          return;
        }
      },
      /** 历史回放：entries 逐条重放 + done 聚合字段。 */
      hydrate: function(record: any) {
        if (!record) return;
        agentId = record.agent_id || agentId; parentRound = record.round || parentRound;
        (record.entries || []).forEach(function(entry: any) {
          const evt = Object.assign({}, entry, { phase: entry.phase });
          if (entry.phase === "model_call") {
            beginRound(evt, true);
          } else if (entry.phase === "tool_start") {
            addToolEntry(evt);
          } else if (entry.phase === "tool_result") {
            fillToolEntry(evt);
          } else if (entry.phase === "todo") {
            applyEventTodo(evt);
          } else if (entry.phase === "notice") {
            const message = typeof entry.message === "string" ? entry.message : "";
            if (message) body.appendChild(el("div", "agent-notice", "↻ " + message));
          }
          // Search uses the parent JSONL event index, even after entries are
          // aggregated into one agent block and tool calls/results share a UI.
          if (Number.isInteger(entry.source_event_index)) {
            const round = findRound(entry.seq);
            if (round && entry.phase === "model_call") {
              round.thinkBlock.wrap.dataset.agentEvent = String(entry.source_event_index);
              round.contentEl.dataset.agentEvent = String(entry.source_event_index);
            } else if (round && (entry.phase === "tool_start" || entry.phase === "tool_result")) {
              const tool = findToolEntry(round, entry.tool_call_id);
              if (tool) tool.wrap.dataset[entry.phase === "tool_result" ? "agentResultEvent" : "agentEvent"] = String(entry.source_event_index);
            }
          }
        });
        applyDone({
          status: record.status || "interrupted",
          final_reply: record.final_reply || "",
          rounds: record.rounds,
          usage_total: record.usage_total,
          error: record.error || "",
          ended_at: record.ended_at,
        });
        if (Number.isInteger(record.done_event_index)) {
          body.querySelectorAll(".agent-final-reply, .agent-error-detail").forEach(function(node: any) {
            node.dataset.agentEvent = String(record.done_event_index);
          });
        }
        bindPermissions();
      },
      markReturned: markReturned,
      isDone: function () { return isDone; },
      /** 流中断兜底：未收到 done 的块标记中断态。 */
      finalizeInterrupted: function () {
        if (!isDone) applyDone({ status: "interrupted" });
      },
    };
    return ui;

    function applyEventTodo(evt: any) {
      todoItems = Array.isArray(evt.todos) ? evt.todos : todoItems;
      renderBody();
    }
  }

  // 代码块复制（事件委托）
  chatInner.addEventListener("click", function(e: any) {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    const code = btn.closest(".codeblock").querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(function () {
      btn.querySelector(".copy-label").textContent = "已复制";
      setTimeout(function () { btn.querySelector(".copy-label").textContent = "复制"; }, 1500);
    });
  });

  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.applyToolResult = applyToolResult;
  App.buildSubAgentBlock = buildSubAgentBlock;

})((window as unknown as { App: any }).App);
