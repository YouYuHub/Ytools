/**
 * 后端 API 封装（见 docs/api_docs.md）
 * 默认指向文档约定端口 48621；如需改动可在 localStorage 设置 ytools-api-base 覆盖，
 * 例如 localStorage.setItem('ytools-api-base', 'http://192.168.1.10:48621')
 */
const api_url = localStorage.getItem("ytools-api-base")
  || "http://127.0.0.1:48621";

(function (global) {
  const BASE_KEY = "ytools-api-base";
  const LEGACY_KEY = "api-base";
  let storedBase = localStorage.getItem(BASE_KEY);
  if (storedBase == null) {
    const legacy = localStorage.getItem(LEGACY_KEY);
    if (legacy != null) {
      storedBase = legacy;
      localStorage.setItem(BASE_KEY, legacy);
      localStorage.removeItem(LEGACY_KEY);
    }
  }
  const BASE = (storedBase || api_url).replace(/\/$/, "");

  async function request(path, options) {
    const res = await fetch(BASE + path, options);
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || body.message || JSON.stringify(body);
      } catch (_) { /* 忽略非 JSON 响应 */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // ---------- 会话历史 ----------
  function listSessions() {
    return request("/chat_history/sessions");
  }

  function streamStatus(sessionId) {
    return request("/chat_stream/status?session_id=" + encodeURIComponent(sessionId));
  }

  function getSessionMeta(sessionId) {
    return request("/chat_history/meta?session_id=" + encodeURIComponent(sessionId));
  }

  /**
   * 获取当前会话的模型上下文 token 统计。
   * @param {string} sessionId 会话 ID
  * @param {number} [maxRounds] 参与统计的最近轮数；<=0 表示全部；省略时由服务端跟随聊天设置
   * @param {string[]} [toolNames] 当前选中的工具名称
   */
  function getContextTokenStats(sessionId, maxRounds, toolNames) {
    const params = new URLSearchParams();
    params.set("session_id", sessionId || "default");
    if (maxRounds != null) params.set("max_rounds", String(maxRounds));
    // 只有当前确实选了工具时才统计工具 schema，避免无工具状态产生额外开销。
    if (Array.isArray(toolNames) && toolNames.length) {
      params.set("include_tools", "true");
      toolNames.forEach(function (name) { params.append("tool_names", name); });
    }
    return request("/chat_context/token_stats?" + params.toString());
  }

  function updateSessionTitle(sessionId, title) {
    return request(
      "/chat_history/title?title=" + encodeURIComponent(title) + "&session_id=" + encodeURIComponent(sessionId),
      { method: "PUT" }
    );
  }

  function fetchSessionFile(sessionId) {
    return fetch(BASE + "/chat_history/file?session_id=" + encodeURIComponent(sessionId)).then(function (res) {
      if (!res.ok) throw new Error("加载会话失败");
      return res.text();
    });
  }

  function deleteSession(sessionId) {
    return request("/chat_history/delete_file?session_id=" + encodeURIComponent(sessionId), { method: "DELETE" });
  }

  /**
   * 上传 jsonl 聊天历史文件到后端（保存为后端会话）
   * @param {string} sessionId 目标会话ID（可传文件名，后端会自动去掉后缀并规整）
   * @param {File} file jsonl 文件
   * @param {boolean} [overwrite=false] 是否强制覆盖同名历史文件；False 时同名自动追加时间戳另存
   * @returns {Promise<{state, session_id, filename, imported_rounds, collision, meta, ...}>}
   */
  function uploadChatHistory(sessionId, file, overwrite) {
    const fd = new FormData();
    fd.append("file", file);
    const params = new URLSearchParams();
    params.set("session_id", sessionId || "default");
    if (overwrite) params.set("overwrite", "true");
    return fetch(BASE + "/chat_history/upload_chat_file?" + params.toString(), {
      method: "POST",
      body: fd,
    }).then(function (res) {
      if (!res.ok) {
        return res.json().then(function (body) {
          throw new Error(body.detail || ("上传失败 " + res.status));
        });
      }
      return res.json();
    });
  }

  // ---------- 工具 ----------
  /**
   * 列出工具；后端默认复用 TTL 内的探测缓存（首屏毫秒级返回），
   * refresh 为 true 时要求绕过缓存强制重探（"配置工具"弹窗的刷新按钮）
   */
  function listTools(refresh) {
    return request("/tools/list" + (refresh ? "?refresh=1" : ""));
  }

  // ---------- 模型配置 ----------
  /**
   * 获取模型列表与指定角色（chat/compaction/title）的选择配置
   * @param {string} [role="chat_model"] 角色：chat_model / compaction_model / title_model
   */
  /**
   * 列出 models.json 中所有可用的 provider / model 组合，并返回指定角色的完整配置
   * 传 sessionId 时按「会话覆盖 → 全局默认」返回该会话的生效选择
   * （role_info 附加 is_overridden，响应含 session_selection/effective_selection/warning）
   * @param {string} [role] chat_model / compaction_model / title_model，默认 chat_model
   * @param {string} [sessionId] 会话 ID
   */
  function getModels(role, sessionId) {
    const params = new URLSearchParams();
    if (role) params.set("role", role);
    if (sessionId) params.set("session_id", sessionId);
    return request("/chat_config/models" + (params.toString() ? "?" + params.toString() : ""));
  }

  /**
   * 选择模型（可选参数）并保存配置
   * 不传 sessionId：写全局默认（models.json 顶层 model_selection）
   * 传 sessionId：写入该会话 _meta.model_selection（仅覆盖该角色）
   * @param {string} provider 服务商名（models.json provider 键）
   * @param {string} model 模型名（models.json models 键）
   * @param {string} [role="chat_model"] 角色
   * @param {object|null} [parameter] 生成参数（按 api_type 分桶）；null 表示仅切换模型、保留原参数桶
   * @param {string} [sessionId] 会话 ID
   * @param {boolean} [clear] 仅会话级有效：清除该会话当前角色的独立模型选择，恢复跟随全局默认
   */
  function selectModel(provider, model, role, parameter, sessionId, clear) {
    const body = { provider: provider, model: model };
    if (role) body.role = role;
    if (parameter !== undefined) body.parameter = parameter;
    if (sessionId) {
      body.session_id = sessionId;
      if (clear) body.clear = true;
    }
    return request("/chat_config/models/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function getHistoryCompactionConfig() {
    return request("/chat_config/history_compaction");
  }

  /**
   * 获取已保存的工具选择
   * 不传 sessionId：全局默认（setting/mcp_servers.json 的 inputs 键）
   * 传 sessionId：附加会话独立选择信息
   * （session_selection/effective_selection/is_overridden/warning，见 docs/api_docs.md）
   * @param {string} [sessionId] 会话 ID
   * @returns {Promise<{state, inputs: Object<string, string[]>, servers: string[]}>}
   */
  function getToolSelection(sessionId) {
    const query = sessionId ? ("?session_id=" + encodeURIComponent(sessionId)) : "";
    return request("/chat_config/tool_selection" + query);
  }

  /**
   * 保存工具选择
   * 不传 sessionId：写回全局默认 setting/mcp_servers.json 的 inputs 键（新建会话前的默认）
   * 传 sessionId：写入该会话 _meta.tool_selection（空 inputs 表示清除覆盖恢复跟随全局）
   * @param {Object<string, string[]>} inputs 服务名 -> 工具名数组；未提及的已配置服务保存为 []
   * @param {string} [sessionId] 会话 ID
   */
  function updateToolSelection(inputs, sessionId) {
    return request("/chat_config/tool_selection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sessionId ? { inputs: inputs || {}, session_id: sessionId } : { inputs: inputs || {} }),
    });
  }

  function updateHistoryCompactionConfig(config) {
    return request("/chat_config/history_compaction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  }

  function getContextReturnConfig() {
    return request("/chat_config/context_return");
  }

  /**
   * 获取工作目录配置；传 sessionId 时返回该会话的独立目录信息
   * （session_dir/is_overridden/effective_dir/warning，见 docs/api_docs.md）
   */
  function getWorkDirConfig(sessionId) {
    const query = sessionId ? ("?session_id=" + encodeURIComponent(sessionId)) : "";
    return request("/chat_config/work_dir" + query);
  }

  /**
   * 切换全局默认聊天工作目录（.env DEFAULT_CHAT_WORK_DIR，作为新会话初始目录）
   * @param {string} newDir 新的工作目录路径
   */
  function changeChatDir(newDir) {
    return request("/change_chat_dir?new_dir=" + encodeURIComponent(newDir), {
      method: "POST",
    });
  }

  /**
   * 设置/清除会话独立工作目录（写入会话 _meta.work_dir）
   * @param {string} sessionId 会话 ID
   * @param {string} workDir 工作目录；空串表示清除覆盖，恢复跟随全局默认
   */
  function setSessionWorkDir(sessionId, workDir) {
    return request("/chat_config/work_dir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, work_dir: workDir }),
    });
  }

  /**
   * 更新思考过程与历史工具结果的最大回传长度
   * @param {{reasoning_max_length: number, tool_result_max_length: number}} config 均为整数；0=不回传，负数=全部回传
   */
  function updateContextReturnConfig(config) {
    return request("/chat_config/context_return", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  }

  /**
   * 获取 MCP 工具执行配置（单次工具执行超时秒数）
   */
  function getMcpToolConfig() {
    return request("/chat_config/mcp_tools");
  }

  /**
   * 更新 MCP 工具执行超时秒数
   * @param {{call_timeout_seconds: number}} config 0=不限制，正数=超时秒数
   */
  function updateMcpToolConfig(config) {
    return request("/chat_config/mcp_tools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  }

  /**
   * 获取模型网络请求失败重试次数配置
   */
  function getNetworkRetryConfig() {
    return request("/chat_config/network_retry");
  }

  /**
   * 更新模型网络请求失败重试次数
   * @param {{max_attempts: number}} config 0 或负数=不限制（一直重试），正数=连续失败 N 次后终止任务
   */
  function updateNetworkRetryConfig(config) {
    return request("/chat_config/network_retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
  }

  // ---------- 停止 ----------
  function stopChat(sessionId) {
    return request("/stop_chat?session_id=" + encodeURIComponent(sessionId || "default"), { method: "POST" });
  }

  /**
   * 运行中注入用户消息（消息引导）：任务运行中投递给后端，生成循环在
   * 下一轮检查点（工具结果处理完毕后）取出并作为新一轮继续。
   * 返回 {ok: boolean}；ok=false 表示任务未运行（前端回退为普通发送）。
   */
  async function injectMessage(sessionId, content) {
    const res = await request("/inject_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, content: content }),
    });
    return res && typeof res === "object" ? res : { ok: false };
  }

  /**
   * 撤回一条尚未消费的注入消息（消息引导提示行 ×）：
   * 按文本匹配从后端注入队列移除；已被消费时 {ok:false}（不可撤回）。
   */
  async function cancelInjectMessage(sessionId, text) {
    const res = await request("/cancel_inject_message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text: text }),
    });
    return res && typeof res === "object" ? res : { ok: false };
  }

  // ---------- 会话文件 ----------
  function uploadSessionFiles(sessionId, fileList) {
    const fd = new FormData();
    Array.from(fileList).forEach(function (f) { fd.append("files", f); });
    return fetch(BASE + "/file/upload_session_files?session_id=" + encodeURIComponent(sessionId), {
      method: "POST",
      body: fd,
    }).then(function (res) {
      if (!res.ok) throw new Error("上传失败 " + res.status);
      return res.json();
    });
  }

  function getSessionFiles(sessionId) {
    return request("/file/get_session_file_memory?number=10&session_id=" + encodeURIComponent(sessionId));
  }

  function deleteSessionFile(filename, sessionId) {
    return request(
      "/file/delete_session_file_memory?filename=" + encodeURIComponent(filename) + "&session_id=" + encodeURIComponent(sessionId),
      { method: "DELETE" }
    );
  }

  /**
   * 读取 SSE 响应体：逐行解析 data: 帧，[DONE] 结束。
   * chatStream / compactContextStream 共用。
   */
  async function readSseResponse(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop(); // 最后一段可能不完整，留到下次

      for (const raw of lines) {
        const line = raw.trim();
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") {
          onEvent({ type: "done", data: null });
          return;
        }
        try {
          onEvent({ type: "data", data: JSON.parse(data) });
        } catch (_) { /* 跳过无法解析的行 */ }
      }
    }
    onEvent({ type: "done", data: null });
  }

  /**
   * 上传多媒体附件（图片/音频/视频）：POST /file/upload_session_media
   * 原始字节保存到 history_files/upload/<session>/media/，返回 media:// 引用
   * @param {string} sessionId 会话 ID
   * @param {File[]} files 媒体文件列表
   * @returns {Promise<{total:number, success:number, failed:number,
   *   results:Array<{filename,status,stored_name?,media_ref?,kind?,mime?,size?,message?}>, upload_id}>}
   */
  async function uploadSessionMedia(sessionId, files) {
    const params = new URLSearchParams();
    params.set("session_id", sessionId || "default");
    const form = new FormData();
    (files || []).forEach(function (file) {
      form.append("files", file, file.name);
    });
    const res = await fetch(BASE + "/file/upload_session_media?" + params.toString(), {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  /** 会话内媒体文件的访问 URL（消息气泡缩略图等用途） */
  function sessionMediaUrl(sessionId, storedName) {
    return BASE + "/file/get_session_media?session_id=" +
      encodeURIComponent(sessionId || "default") +
      "&name=" + encodeURIComponent(storedName || "");
  }

  /** 会话内文档原始文件的访问 URL（点击预览 PDF/文本/下载） */
  function sessionDocumentUrl(sessionId, storedName) {
    return BASE + "/file/get_session_document?session_id=" +
      encodeURIComponent(sessionId || "default") +
      "&name=" + encodeURIComponent(storedName || "");
  }

  /** 本地文件的访问 URL（媒体伪标签本地路径 src；相对路径由后端按会话工作目录解析） */
  function localFileUrl(sessionId, path) {
    return BASE + "/file/get_local_file?session_id=" +
      encodeURIComponent(sessionId || "default") +
      "&path=" + encodeURIComponent(path || "");
  }

  /**
   * 删除消息中的媒体伪标签：POST /chat_history/remove_media_tag
   * 把会话历史 JSONL 中该标签原文替换为占位说明（用户已删除/文件不存在）
   */
  async function removeMediaTag(sessionId, rawTag) {
    return request("/chat_history/remove_media_tag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId || "default",
        tag: rawTag || "",
        replacement: "用户已删除/文件不存在",
      }),
    });
  }

  /**
   * SSE 聊天：POST /chat_with_tool
   * @param {object} payload 请求体（messages/session_id/tool_names...）
   * @param {(event: {type:string, data:any}) => void} onEvent 每个 SSE 事件回调
   * @param {AbortSignal} [signal] 中止信号
   */
  async function chatStream(payload, onEvent, signal) {
    const res = await fetch(BASE + "/chat_with_tool", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: signal,
    });
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try {
        detail = JSON.stringify(await res.json());
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    await readSseResponse(res, onEvent);
  }

  /**
   * 手动压缩（流式）：POST /chat_context/compact_manual?stream=true
   * 事件结构与自动压缩一致：context_compaction 的 start/delta/done
   * （done 携带 summary_text），结尾附加 compaction_manual_result 结果帧。
   * @param {string} sessionId 会话 ID
   * @param {(event: {type:string, data:any}) => void} onEvent 每个 SSE 事件回调
   * @param {AbortSignal} [signal] 中止信号
   */
  async function compactContextStream(sessionId, onEvent, signal) {
    const params = new URLSearchParams();
    params.set("session_id", sessionId || "default");
    params.set("stream", "true");
    const res = await fetch(BASE + "/chat_context/compact_manual?" + params.toString(), {
      method: "POST",
      signal: signal,
    });
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try {
        detail = JSON.stringify(await res.json());
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    await readSseResponse(res, onEvent);
  }

  // ---------- Skills 提示词库 ----------
  function listPrompts() {
    return request("/prompts/list");
  }

  function readPrompt(name) {
    return request("/prompts/read?name=" + encodeURIComponent(name));
  }

  function createPrompt(name, content) {
    return request("/prompts/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, content: content || "" }),
    });
  }

  function savePrompt(name, content) {
    return request("/prompts/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, content: content }),
    });
  }

  function renamePrompt(oldName, newName) {
    return request("/prompts/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_name: oldName, new_name: newName }),
    });
  }

  function deletePrompt(name) {
    return request("/prompts/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name }),
    });
  }

  global.API = {
    BASE,
    listSessions,
    streamStatus,
    getSessionMeta,
    getContextTokenStats,
    updateSessionTitle,
    fetchSessionFile,
    deleteSession,
    uploadChatHistory,
    listTools,
    getModels,
    selectModel,
    getHistoryCompactionConfig,
    updateHistoryCompactionConfig,
    getToolSelection,
    updateToolSelection,
    getContextReturnConfig,
    updateContextReturnConfig,
    getMcpToolConfig,
    updateMcpToolConfig,
    getNetworkRetryConfig,
    updateNetworkRetryConfig,
    getWorkDirConfig,
    changeChatDir,
    setSessionWorkDir,
    stopChat,
    injectMessage,
    cancelInjectMessage,
    uploadSessionFiles,
    uploadSessionMedia,
    sessionMediaUrl,
    sessionDocumentUrl,
    localFileUrl,
    removeMediaTag,
    getSessionFiles,
    deleteSessionFile,
    chatStream,
    compactContextStream,
    listPrompts,
    readPrompt,
    createPrompt,
    savePrompt,
    renamePrompt,
    deletePrompt,
  };
})(window);
