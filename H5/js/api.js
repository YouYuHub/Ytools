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
  function listTools() {
    return request("/tools/list");
  }

  // ---------- 模型配置 ----------
  /**
   * 获取模型列表与指定角色（chat/compaction/title）的选择配置
   * @param {string} [role="chat_model"] 角色：chat_model / compaction_model / title_model
   */
  function getModels(role) {
    const params = new URLSearchParams();
    if (role) params.set("role", role);
    return request("/chat_config/models" + (params.toString() ? "?" + params.toString() : ""));
  }

  /**
   * 选择模型（可选参数）并保存配置
   * @param {string} provider 服务商名（models.json provider 键）
   * @param {string} model 模型名（models.json models 键）
   * @param {string} [role="chat_model"] 角色
   * @param {object|null} [parameter] 生成参数（按 api_type 分桶）；null 表示仅切换模型、保留原参数桶
   */
  function selectModel(provider, model, role, parameter) {
    const body = { provider: provider, model: model };
    if (role) body.role = role;
    if (parameter !== undefined) body.parameter = parameter;
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
   * 获取已保存的工具选择（setting/mcp_servers.json 的 inputs 键）
   * @returns {Promise<{state, inputs: Object<string, string[]>, servers: string[]}>}
   */
  function getToolSelection() {
    return request("/chat_config/tool_selection");
  }

  /**
   * 保存工具选择（后端实时更新内存并写回 setting/mcp_servers.json）
   * @param {Object<string, string[]>} inputs 服务名 -> 工具名数组；未提及的已配置服务保存为 []
   */
  function updateToolSelection(inputs) {
    return request("/chat_config/tool_selection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ inputs: inputs || {} }),
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
   * 获取当前工作目录及项目 .env 中保存的路径
   */
  function getWorkDirConfig() {
    return request("/chat_config/work_dir");
  }

  /**
   * 切换聊天工作目录（写入 .env 并实时生效）
   * @param {string} newDir 新的工作目录路径
   */
  function changeChatDir(newDir) {
    return request("/change_chat_dir?new_dir=" + encodeURIComponent(newDir), {
      method: "POST",
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

  // ---------- 停止 ----------
  function stopChat(sessionId) {
    return request("/stop_chat?session_id=" + encodeURIComponent(sessionId || "default"), { method: "POST" });
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
    getWorkDirConfig,
    changeChatDir,
    stopChat,
    uploadSessionFiles,
    uploadSessionMedia,
    sessionMediaUrl,
    sessionDocumentUrl,
    getSessionFiles,
    deleteSessionFile,
    chatStream,
    compactContextStream,
  };
})(window);
