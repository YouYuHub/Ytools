/**
 * 模型与参数面板（“参数”按钮）
 * - 四个角色（聊天/压缩/标题/子智能体模型）的模型选择与生成参数编辑
 * - 会话独立选择 vs 全局默认语义、api_type 参数分桶、恢复默认预设
 * 依赖：app/core.js、API；App.*：stats 模块（token 统计刷新）
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, MODEL_ROLES,
    ROLE_DEFAULT_PARAMS, API_TYPE_DEFAULT_PARAMS, enhancePanel, modelTabs,
    modelPicker, modelPickerLabel, modelList, reasoningEffort,
    enableThinking, chatParametersGroup, messagesParametersGroup, temperature, temperatureValue, maxTokens,
    maxTokensValue, topP, topPValue, presencePenalty,
    presencePenaltyValue, messagesTemperatureRow, messagesTemperature, messagesTemperatureValue,
    messagesTopPRow, messagesTopP, messagesTopPValue, messagesThinkingMode,
    messagesThinkingBudgetRow, messagesThinkingBudget, messagesThinkingBudgetValue, messagesThinkingHint,
    messagesEffort, messagesStopSequences, messagesExtraBody,
    enhanceCancel, enhanceConfirm, enhanceReset,
    enhanceClearOverride, enhanceHint,
    customHeadersRows, addHeaderRow
  } = App;

  const responseControls = ["responsesParametersGroup", "responsesMode", "responsesEffort", "responsesSummary", "responsesVerbosity", "responsesSampling", "responsesTemperatureRow", "responsesTemperature", "responsesTemperatureValue", "responsesTopPRow", "responsesTopP", "responsesTopPValue", "responsesInstructions", "responsesExtraBody", "responsesStateHint", "responsesRebuildSource", "responsesRebuild"];
  const R = {};
  let responsesStateVersion = 0;
  responseControls.forEach(function (key) { R[key] = App[key] || el("div"); });
  ["responsesModeHint", "responsesStatefulControls"].forEach(function (key) { R[key] = App[key] || el("div"); });

  function apiKindOf(model) {
    const raw = String(model && model.api_type || "").toLowerCase().replace(/_/g, "-");
    if (raw === "responses") return "responses";
    if (raw === "messages") return "messages";
    return "completions";
  }
  function isResponsesModel(model) {
    return apiKindOf(model) === "responses";
  }
  function responseParameters(role) {
    const info = state.modelConfigs[role] && state.modelConfigs[role].role_info;
    return cloneObject(info && info.selection && info.selection.parameter && info.selection.parameter.responses ||
      info && info.selection && info.selection.api_type === "responses" && info.effective_parameter || {});
  }
  function renderResponsesFields(params) {
    state.responsesParameterBase = cloneObject(params);
    R.responsesMode.value = params.responses_mode || "";
    R.responsesEffort.value = params.reasoning && params.reasoning.effort || params.reasoning_effort || "";
    R.responsesSummary.value = params.reasoning && params.reasoning.summary || "";
    R.responsesVerbosity.value = params.text && params.text.verbosity || "";
    R.responsesSampling.checked = params.temperature != null || params.top_p != null;
    R.responsesTemperature.value = String(params.temperature != null ? params.temperature : 1);
    R.responsesTopP.value = String(params.top_p != null ? params.top_p : 1);
    R.responsesInstructions.value = params.instructions || "";
    R.responsesExtraBody.value = params.extra_body ? JSON.stringify(params.extra_body, null, 2) : "";
  }
  function responsesDraft() {
    const result = { base: cloneObject(state.responsesParameterBase) };
    responseControls.forEach(function (key) { result[key] = R[key].value; });
    result.responsesSampling = Boolean(R.responsesSampling.checked);
    return result;
  }
  function restoreResponsesDraft(value) {
    if (!value) { renderResponsesFields(responseParameters(state.activeModelRole)); return; }
    state.responsesParameterBase = cloneObject(value.base);
    responseControls.forEach(function (key) { if (value[key] != null) R[key].value = value[key]; });
    R.responsesSampling.checked = Boolean(value.responsesSampling);
  }
  function responseParameterObject(draft, model) {
    const value = draft || responsesDraft();
    const params = cloneObject(value.base || {});
    delete params.reasoning_effort;
    delete params.max_tokens;
    if (value.responsesMode) params.responses_mode = value.responsesMode; else delete params.responses_mode;
    params.max_output_tokens = Number(draft ? draft.maxTokens : maxTokens.value);
    const reasoning = Object.assign({}, params.reasoning || {});
    if (value.responsesEffort) reasoning.effort = value.responsesEffort; else delete reasoning.effort;
    if (value.responsesSummary) reasoning.summary = value.responsesSummary; else delete reasoning.summary;
    if (Object.keys(reasoning).length) params.reasoning = reasoning; else delete params.reasoning;
    const text = Object.assign({}, params.text || {});
    if (value.responsesVerbosity) text.verbosity = value.responsesVerbosity; else delete text.verbosity;
    if (Object.keys(text).length) params.text = text; else delete params.text;
    delete params.temperature; delete params.top_p;
    if (value.responsesSampling) {
      if (supportsMessageParameter(model, "temperature")) params.temperature = Number(value.responsesTemperature);
      if (supportsMessageParameter(model, "top_p")) params.top_p = Number(value.responsesTopP);
    }
    if (value.responsesInstructions) params.instructions = value.responsesInstructions; else delete params.instructions;
    if (String(value.responsesExtraBody || "").trim()) {
      const extra = JSON.parse(value.responsesExtraBody);
      if (!extra || typeof extra !== "object" || Array.isArray(extra)) throw new Error("extra_body 须为 JSON 对象");
      params.extra_body = extra;
    } else delete params.extra_body;
    ["presence_penalty", "thinking", "output_config", "stop_sequences"].forEach(function (key) { delete params[key]; });
    return params;
  }
  function selectedResponsesMode() {
    const model = modelInfoByKey(state.activeModelRole, state.selectedModelKey);
    return R.responsesMode.value || model && model.responses_mode || "stateful";
  }
  async function refreshResponsesState() {
    const version = ++responsesStateVersion;
    const isMain = state.activeModelRole === "chat_model";
    const stateless = selectedResponsesMode() === "stateless";
    const modeLabel = stateless ? "无状态兼容 · 每次发送上下文" : "有状态 · 响应 ID 续聊";
    R.responsesModeHint.textContent = (R.responsesMode.value ? "当前选择：" : "跟随模型配置，当前为：") + modeLabel + "。保存后从下一次任务生效。";
    R.responsesStatefulControls.classList.toggle("hidden", stateless || !isMain);
    R.responsesRebuild.disabled = stateless || !isMain || !state.sessionId;
    R.responsesRebuildSource.disabled = stateless || !isMain || !state.sessionId;
    if (stateless) {
      R.responsesStateHint.textContent = "无状态兼容：每次发送当前本地上下文，工具结果随历史回传；使用本地压缩，无需准备重新同步。";
      return;
    }
    if (!isMain) {
      R.responsesStateHint.textContent = "该角色按独立任务维护 Responses 会话；重新同步入口用于聊天主会话。";
      return;
    }
    if (!state.sessionId || !API.getResponsesState) {
      R.responsesStateHint.textContent = "有状态续聊：首次发送时建立远端会话，之后通过响应 ID 发送新增输入。";
      return;
    }
    const session = state.sessionId;
    const role = state.activeModelRole;
    try {
      const data = await API.getResponsesState(session);
      if (version !== responsesStateVersion || state.sessionId !== session || state.activeModelRole !== role) return;
      const remote = data.responses_state || {};
      const labels = { new: "尚未建立远端会话", ready: "上次请求已完成；下次仍尝试响应 ID 续接", request_pending: "请求正在进行",
        recovery_required: "需要确认上次请求", rebuild_pending: "待下次发送重新同步", ready_to_rebase: "官方压缩完成，待下次发送启用", compacting: "正在压缩" };
      let hint = remote.responses_mode === "stateless"
        ? "上次调用使用无状态模式；保存当前选择后，下次建立有状态链"
        : labels[remote.status] || remote.status || "尚未建立远端会话";
      if (remote.age_risk) hint += " · 长期会话建议准备重新同步";
      const compressed = remote.last_compaction;
      if (compressed && compressed.phase === "error") hint += " · 官方压缩未完成";
      if (compressed && compressed.local_phase === "error") hint += " · 本地摘要未完成";
      if (compressed && compressed.phase === "done") {
        if (compressed.activation === "active") {
          hint += " · 已从官方压缩窗口建立新链";
          if (Number.isFinite(compressed.input_tokens_after)) hint += "（启用时输入 " + compressed.input_tokens_after.toLocaleString() + " tokens）";
        } else if (compressed.mode === "trigger") {
          hint += " · 压缩窗口待启用，下次发送自动建立新链";
        }
      }
      R.responsesStateHint.textContent = hint + "。上下文显示以本地数据为准。";
    } catch (error) {
      if (version === responsesStateVersion && state.sessionId === session && state.activeModelRole === role) R.responsesStateHint.textContent = "状态读取失败：" + error.message;
    }
  }
  R.responsesRebuild.addEventListener("click", async function () {
    if (selectedResponsesMode() === "stateless") { toast("无状态模式每次发送上下文，无需准备重新同步"); return; }
    if (state.activeModelRole !== "chat_model") { toast("请在聊天模型页重新同步主会话"); return; }
    if (!state.sessionId) { toast("请先建立会话"); return; }
    if (state.streaming || state.manualCompactRunning) { toast("请等待生成或压缩完成"); return; }
    const session = state.sessionId;
    try {
      await API.rebuildResponsesContext(session, R.responsesRebuildSource.value || "local");
      if (state.sessionId !== session) return;
      toast("已准备上下文，下次发送时同步一次");
      await refreshResponsesState();
    } catch (error) { toast("重新同步失败：" + error.message); }
  });
  responseControls.filter(function (key) { return ["responsesEffort", "responsesSummary", "responsesVerbosity", "responsesInstructions", "responsesExtraBody", "responsesTemperature", "responsesTopP"].includes(key); }).forEach(function (key) {
    R[key].addEventListener(key.includes("Temperature") || key.includes("TopP") || key.includes("Instructions") || key.includes("ExtraBody") ? "input" : "change", function () { markParamDirty(); syncRangeOutputs(); });
  });
  R.responsesSampling.addEventListener("change", function () { markParamDirty(); updateProtocolControls(); });
  R.responsesMode.addEventListener("change", function () { markParamDirty(); updateProtocolControls(); });
  App.refreshResponsesState = refreshResponsesState;

  let panelVersion = 0;
  let panelSessionId;

  // Messages 手动 thinking 预算约束（与 chat/protocols/messages.py 的发送前校验保持一致）
  const THINKING_BUDGET_MIN = 1024;
  const THINKING_BUDGET_STEP = 1024;
  const THINKING_BUDGET_SLIDER_MAX = 65536;

  function isCurrentPanel(version, sessionId) {
    return version === panelVersion && sessionId === (state.sessionId || undefined);
  }

  // ---------- 模型与参数面板 ----------
  // 当前角色的配置快照（GET /chat_config/models?role=... 的 role_info / selection / models）
  function roleConfig() {
    return state.modelConfigs[state.activeModelRole] || null;
  }

  function roleDefaultParams() {
    return ROLE_DEFAULT_PARAMS[state.activeModelRole] || ROLE_DEFAULT_PARAMS.chat_model;
  }

  // 该角色可选模型列表（优先 role_info.available_models，回退全量 models）
  function availableModelsForRole(role) {
    const cfg = state.modelConfigs[role];
    if (!cfg) return [];
    const info = cfg.role_info;
    return (info && info.available_models) || cfg.models || [];
  }

  // 该角色当前选中模型：provider::model 形式
  function currentModelKey(role) {
    const cfg = state.modelConfigs[role];
    const sel = cfg && cfg.role_info && cfg.role_info.selection;
    if (!sel) return "";
    const provider = sel.provider || sel.provider_name || sel.ownership_name || "";
    const model = sel.model || sel.model_name || "";
    return provider && model ? provider + "::" + model : "";
  }

  // 按 key 查找模型的完整信息（含 max_output_tokens / api_type）
  function modelInfoByKey(role, key) {
    if (!key) return null;
    const sep = key.indexOf("::");
    if (sep <= 0) return null;
    const provider = key.slice(0, sep);
    const model = key.slice(sep + 2);
    return availableModelsForRole(role).find(function (m) {
      return (m.provider_name || m.provider) === provider && (m.model_name || m.model) === model;
    }) || null;
  }

  function renderModelTabs() {
    modelTabs.querySelectorAll(".model-tab").forEach(function (tab) {
      tab.classList.toggle("active", tab.dataset.role === state.activeModelRole);
    });
  }

  function renderModelPicker() {
    // 面板草稿选中 = 服务端当前选择（仅初始化/切选项卡时重置；用户在列表中的选择保存在 selectedModelKey）
    state.selectedModelKey = currentModelKey(state.activeModelRole);
    state.modelSelectionTouched = false;
    modelList.classList.add("hidden");
    updateModelPickerLabel();
  }

  function updateModelPickerLabel() {
    const key = state.selectedModelKey;
    if (!key) {
      modelPickerLabel.textContent = "请选择模型";
      modelPickerLabel.classList.add("placeholder");
      if (modelPicker && modelPicker.dataset) modelPicker.dataset.api = "none";
      return;
    }
    const sep = key.indexOf("::");
    const name = key.slice(sep + 2);
    const model = modelInfoByKey(state.activeModelRole, key);
    modelPickerLabel.textContent = model && model.api_type ? name + " · " + model.api_type : name;
    modelPickerLabel.classList.remove("placeholder");
    // 选择按钮按 api 着色（completions 蓝 / messages 绿 / responses 粉），不再跟随全局强调色。
    if (modelPicker && modelPicker.dataset) modelPicker.dataset.api = apiKindOf(model);
  }

  // 模型选择列表在面板内展开（不依赖原生 select 弹出层，窄屏也能完整显示与滚动）
  function renderModelList() {
    const role = state.activeModelRole;
    modelList.innerHTML = "";
    const groups = new Map();
    availableModelsForRole(role).forEach(function (m) {
      const provider = m.provider_name || m.provider;
      if (!provider || !m.model_name) return;
      if (!groups.has(provider)) groups.set(provider, []);
      groups.get(provider).push(m);
    });

    if (!groups.size) {
      modelList.appendChild(el("div", "model-list-empty", "暂无可用模型"));
      return;
    }
    groups.forEach(function (items, provider) {
      modelList.appendChild(el("div", "model-list-group", provider));
      items.forEach(function (m) {
        const kind = apiKindOf(m);
        const item = el("button", "model-list-item api-" + kind, m.model_name);
        item.type = "button";
        item.dataset.key = provider + "::" + m.model_name;
        item.dataset.api = kind;
        const caps = [];
        if (m.vision) caps.push("视觉");
        if (m.tool_calling) caps.push("工具");
        const sub = [m.api_type, m.url].filter(Boolean).join(" · ");
        item.appendChild(el("span", "model-list-meta", (caps.length ? caps.join("、") + " · " : "") + sub));
        modelList.appendChild(item);
      });
    });
    modelList.querySelectorAll(".model-list-item").forEach(function (item) {
      item.classList.toggle("active", item.dataset.key === state.selectedModelKey);
    });
  }

  function selectModelKey(key) {
    const switched = key !== state.selectedModelKey;
    state.selectedModelKey = key;
    // 与全局当前模型相同也算明确选择，确认后建立会话独立配置。
    state.modelSelectionTouched = true;
    updateModelPickerLabel();
    updateProtocolControls();
    // 切换到不同模型后立即拉取一次 /chat_config/models，
    // 用服务端最新 max_output_tokens 校准“最大输出 token”：当前配置超过上限则压回上限，其余参数不动
    if (switched) refreshMaxTokensAfterSwitch(key);
  }

  function refreshMaxTokensAfterSwitch(key) {
    const role = state.activeModelRole;
    const version = panelVersion;
    const sessionId = panelSessionId;
    API.getModels(role, sessionId).then(function (data) {
      if (!isCurrentPanel(version, sessionId) || enhancePanel.classList.contains("hidden")) return;
      // 经单一入口写缓存：chat_model 角色顺带原子刷新状态条模型名，
      // 不再依赖"先赋值 state.modelConfigs 再手动调用刷新"的顺序约定
      if (role === "chat_model") App.setChatModelConfig(data);
      else state.modelConfigs[role] = data;
    }, function () { /* 拉取失败沿用缓存模型信息 */ }).then(function () {
      // 等待期间用户可能已切角色/换模型/关面板，过期结果不回填
      if (enhancePanel.classList.contains("hidden")) return;
      if (state.activeModelRole !== role || state.selectedModelKey !== key) return;
      if (!isCurrentPanel(version, sessionId)) return;
      setupMaxTokensRange(Number(maxTokens.value));
      syncRangeOutputs();
    });
  }

  // 输出 token 默认值：优先配置值，其次该模型 GET 接口的 max_output_tokens（默认最大）
  function defaultMaxTokens(model) {
    return model && model.max_output_tokens ? Number(model.max_output_tokens) : 8192;
  }

  function setupMaxTokensRange(value) {
    const model = modelInfoByKey(state.activeModelRole, state.selectedModelKey);
    const modelMax = model && model.max_output_tokens ? Number(model.max_output_tokens) : 131072;
    maxTokens.max = Math.max(4096, modelMax);
    maxTokens.min = 256;
    maxTokens.step = 256;
    maxTokens.value = String(Math.min(Math.max(256, value), Number(maxTokens.max)));
  }

  function formatNumber(n) {
    return String(Number(n).toFixed(2)).replace(/0+$/, "").replace(/\.$/, "");
  }

  function syncRangeOutputs() {
    syncManualBudgetRange(false);  // 先同步滑块上限再读取显示值：上限变小时浏览器可能下调 value
    temperatureValue.value = formatNumber(temperature.value);
    maxTokensValue.value = Number(maxTokens.value).toLocaleString("en-US");
    topPValue.value = formatNumber(topP.value);
    presencePenaltyValue.value = formatNumber(presencePenalty.value);
    R.responsesTemperatureValue.value = formatNumber(R.responsesTemperature.value);
    R.responsesTopPValue.value = formatNumber(R.responsesTopP.value);
    messagesTemperatureValue.value = formatNumber(messagesTemperature.value);
    messagesTopPValue.value = formatNumber(messagesTopP.value);
    messagesThinkingBudgetValue.value = Number(messagesThinkingBudget.value).toLocaleString("en-US");
    renderThinkingHint(modelInfoByKey(state.activeModelRole, state.selectedModelKey));
  }

  function cloneObject(value) {
    return value && typeof value === "object" ? JSON.parse(JSON.stringify(value)) : {};
  }

  function isMessagesModel(model) {
    return apiKindOf(model) === "messages";
  }

  function supportsMessageParameter(model, key) {
    const caps = model && (model.parameter_capabilities || model.parameterCapabilities);
    return !(caps && typeof caps === "object" && caps[key] === false);
  }

  function filterMessagesParameters(parameters, model) {
    const filtered = cloneObject(parameters);
    const caps = model && (model.parameter_capabilities || model.parameterCapabilities);
    Object.keys(filtered).forEach(function (key) {
      if (!supportsMessageParameter(model, key)) delete filtered[key];
    });
    if (caps && caps.thinking_budget === false && filtered.thinking && filtered.thinking.type === "enabled") {
      delete filtered.thinking;
    }
    return filtered;
  }

  function renderMessagesFields(parameters, modelKey) {
    const role = state.activeModelRole;
    const model = modelInfoByKey(role, modelKey || state.selectedModelKey);
    const native = filterMessagesParameters(parameters || {}, model);
    messagesTemperature.value = String(native.temperature != null ? native.temperature : 0.7);
    messagesTopP.value = String(native.top_p != null ? native.top_p : 1);
    const thinking = native.thinking && typeof native.thinking === "object" ? native.thinking : {};
    messagesThinkingMode.value = thinking.type === "adaptive" || thinking.type === "enabled" ? thinking.type : "";
    messagesThinkingBudget.value = String(thinking.budget_tokens != null ? thinking.budget_tokens : 8192);
    const output = native.output_config && typeof native.output_config === "object" ? native.output_config : {};
    messagesEffort.value = output.effort || "";
    messagesStopSequences.value = Array.isArray(native.stop_sequences) ? native.stop_sequences.join("\n") : "";
    messagesExtraBody.value = native.extra_body && typeof native.extra_body === "object"
      ? JSON.stringify(native.extra_body, null, 2) : "";
    syncMessagesThinkingControls(model);
    syncRangeOutputs();
  }

  function syncMessagesThinkingControls(model) {
    const thinkingOption = messagesThinkingMode.querySelector && messagesThinkingMode.querySelector('option[value="enabled"]');
    const budgetUnsupported = !supportsMessageParameter(model, "thinking_budget");
    if (thinkingOption) thinkingOption.disabled = budgetUnsupported;
    if (budgetUnsupported && messagesThinkingMode.value === "enabled") messagesThinkingMode.value = "";
    messagesThinkingBudgetRow.classList.toggle("hidden", messagesThinkingMode.value !== "enabled" || budgetUnsupported);
    syncManualBudgetRange(false);
    renderThinkingHint(model);
  }

  // 手动预算上限：小于最大输出 token 的最大 1024 倍数，且不超过滑块上限
  function manualBudgetLimit(maxOutput) {
    if (!Number.isFinite(maxOutput)) return 0;
    const cap = Math.floor((maxOutput - 1) / THINKING_BUDGET_STEP) * THINKING_BUDGET_STEP;
    return Math.min(THINKING_BUDGET_SLIDER_MAX, cap);
  }

  // 滑块上限随最大输出 token 联动。clamp=true（用户调整最大输出 token）时，超出上限的预算下调到上限，
  // 返回是否发生下调；调用方据此记为改动，保存时生效。
  function syncManualBudgetRange(clamp) {
    const limit = manualBudgetLimit(Number(maxTokens.value));
    messagesThinkingBudget.max = String(Math.max(THINKING_BUDGET_MIN, limit));
    if (!clamp || messagesThinkingMode.value !== "enabled" || limit < THINKING_BUDGET_MIN) return false;
    if (Number(messagesThinkingBudget.value) <= limit) return false;
    messagesThinkingBudget.value = String(limit);
    return true;
  }

  // 说明 Thinking 当前可用性：模型不支持手动预算时给出原因；手动预算时给出可选范围
  function renderThinkingHint(model) {
    let text = "";
    if (!supportsMessageParameter(model, "thinking_budget")) {
      text = "该模型不支持手动思考预算，已禁用 Enabled。请选择 Adaptive，并用输出 effort 调节思考深度。";
    } else if (messagesThinkingMode.value === "enabled") {
      const max = Number(maxTokens.value);
      const limit = manualBudgetLimit(max);
      text = limit >= THINKING_BUDGET_MIN
        ? "手动预算可选 " + THINKING_BUDGET_MIN.toLocaleString("en-US") + " – " + limit.toLocaleString("en-US") +
          "（需小于最大输出 token " + max.toLocaleString("en-US") + "）"
        : "最大输出 token 需大于 " + THINKING_BUDGET_MIN.toLocaleString("en-US") + " 才能使用手动预算";
    }
    messagesThinkingHint.textContent = text;
    messagesThinkingHint.classList.toggle("hidden", !text);
  }

  // 发送前组合校验（与 chat/protocols/messages.py 一致）：保存时即给出可操作的提示，避免等到上游返回 400
  function validateMessagesParameter(param) {
    const thinking = param.thinking;
    if (!thinking || typeof thinking !== "object") return;
    if (thinking.type === "enabled") {
      const budget = thinking.budget_tokens;
      if (!Number.isInteger(budget) || budget < THINKING_BUDGET_MIN || budget >= param.max_tokens) {
        throw new Error("手动 thinking 预算须为不小于 " + THINKING_BUDGET_MIN.toLocaleString("en-US") +
          " 且小于最大输出 token（" + param.max_tokens.toLocaleString("en-US") + "）的整数，请调整预算或最大输出 token");
      }
    }
    if (thinking.type === "enabled" || thinking.type === "adaptive") {
      if (param.temperature !== undefined && param.temperature !== 1) {
        throw new Error("开启 Thinking 时 Temperature 须为 1（供应商限制），请把 Temperature 调回 1 或关闭 Thinking");
      }
      if (param.top_p !== undefined && !(param.top_p >= 0.95 && param.top_p <= 1)) {
        throw new Error("开启 Thinking 时 Top P 须在 0.95 到 1 之间（供应商限制），请调整或关闭 Thinking");
      }
    }
  }

  // 将服务端生效参数回填到表单（未配置时用角色内置默认）
  function renderEnhanceFields() {
    const cfg = roleConfig();
    const defaults = roleDefaultParams();
    const info = cfg && cfg.role_info;
    const params = (info && info.effective_parameter) || {};
    const maxTokensValueInit = params.max_tokens != null ? Number(params.max_tokens)
      : (params.max_output_tokens != null ? Number(params.max_output_tokens) : null);
    const model = modelInfoByKey(state.activeModelRole, state.selectedModelKey);
    const initMaxTokens = maxTokensValueInit != null ? maxTokensValueInit : defaultMaxTokens(model);

    temperature.value = String(params.temperature != null ? Number(params.temperature) : defaults.temperature);
    setupMaxTokensRange(initMaxTokens);
    topP.value = String(params.top_p != null ? Number(params.top_p) : defaults.top_p);
    presencePenalty.value = String(params.presence_penalty != null ? Number(params.presence_penalty) : defaults.presence_penalty);
    reasoningEffort.value = params.reasoning_effort || defaults.reasoning_effort;
    const extra = params.extra_body || {};
    enableThinking.checked = extra.enable_thinking != null ? Boolean(extra.enable_thinking) : defaults.enable_thinking;
    // 自定义请求头按该角色生效值回填（会话覆盖 → 全局默认）
    renderHeaderRows(info && info.effective_headers);
    renderResponsesFields(responseParameters(state.activeModelRole));
    syncRangeOutputs();
    state.modelParamDirty = false;
    state.messagesParameterBase = nativeParameters(state.activeModelRole);
    state.messagesParameterTouched = {};
    state.messagesParametersReset = false;
    renderMessagesFields(state.messagesParameterBase);
    updateProtocolControls();
  }

  function updateProtocolControls() {
    const model = modelInfoByKey(state.activeModelRole, state.selectedModelKey);
    const kind = apiKindOf(model);
    const messages = kind === "messages";
    const responses = kind === "responses";
    const completions = kind === "completions";
    chatParametersGroup.classList.toggle("hidden", messages || responses);
    R.responsesParametersGroup.classList.toggle("hidden", !responses);
    // 三色模式互斥：completions 蓝 / messages 绿 / responses 粉，不再依赖全局强调色。
    enhancePanel.classList.toggle("completions-mode", completions);
    enhancePanel.classList.toggle("messages-mode", messages);
    enhancePanel.classList.toggle("responses-mode", responses);
    if (enhancePanel.dataset) enhancePanel.dataset.api = kind;
    if (modelPicker && modelPicker.dataset) modelPicker.dataset.api = kind;
    R.responsesTemperatureRow.classList.toggle("hidden", !responses || !R.responsesSampling.checked || !supportsMessageParameter(model, "temperature"));
    R.responsesTopPRow.classList.toggle("hidden", !responses || !R.responsesSampling.checked || !supportsMessageParameter(model, "top_p"));
    if (responses) refreshResponsesState();
    else {
      ++responsesStateVersion;
      R.responsesStatefulControls.classList.add("hidden");
      R.responsesRebuild.disabled = true;
      R.responsesRebuildSource.disabled = true;
    }
    messagesParametersGroup.classList.toggle("hidden", !messages);
    syncMessagesThinkingControls(model);
    [temperature, topP, presencePenalty, reasoningEffort, enableThinking].forEach(function (control) {
      control.disabled = Boolean(messages || responses);
      const row = control.closest && control.closest("label");
      if (row) row.classList.toggle("hidden", Boolean(messages || responses));
    });
    messagesTemperatureRow.classList.toggle("hidden", !messages || !supportsMessageParameter(model, "temperature"));
    messagesTopPRow.classList.toggle("hidden", !messages || !supportsMessageParameter(model, "top_p"));
  }

  function markParamDirty() {
    state.modelParamDirty = true;
  }

  // ---------- 面板草稿（按角色保留未保存的编辑） ----------
  // 切换角色页签时不再从服务端重新回填（那会清掉用户的未保存编辑）：
  // 切走前把当前表单存入草稿，切回时恢复草稿。面板打开、保存成功、
  // 清除覆盖时才以服务端当前配置重建基线；模型"选择"变更时的重新拉取
  // 校准由 selectModelKey → refreshMaxTokensAfterSwitch 承担。
  function drafts() {
    if (!state.modelPanelDrafts) state.modelPanelDrafts = {};
    return state.modelPanelDrafts;
  }

  // 把当前展示中的角色表单存入草稿（仅激活角色，切走前调用）
  function saveRoleDraft(role) {
    if (role !== state.activeModelRole) return;
    drafts()[role] = {
      selectedModelKey: state.selectedModelKey,
      temperature: temperature.value,
      maxTokens: maxTokens.value,
      topP: topP.value,
      presencePenalty: presencePenalty.value,
      reasoningEffort: reasoningEffort.value,
      enableThinking: enableThinking.checked,
      responses: responsesDraft(),
      messagesParameterBase: cloneObject(state.messagesParameterBase),
      messagesParameterTouched: Object.assign({}, state.messagesParameterTouched || {}),
      messagesParametersReset: Boolean(state.messagesParametersReset),
      messagesTemperature: messagesTemperature.value,
      messagesTopP: messagesTopP.value,
      messagesThinkingMode: messagesThinkingMode.value,
      messagesThinkingBudget: messagesThinkingBudget.value,
      messagesEffort: messagesEffort.value,
      messagesStopSequences: messagesStopSequences.value,
      messagesExtraBody: messagesExtraBody.value,
      headers: collectHeaderRows(),
      dirty: state.modelParamDirty,
      selectionTouched: state.modelSelectionTouched,
    };
  }

  // 恢复角色草稿（含模型选择、参数、请求头与 dirty 标记）
  function applyRoleDraft(draft) {
    state.selectedModelKey = draft.selectedModelKey;
    modelList.classList.add("hidden");
    temperature.value = draft.temperature;
    setupMaxTokensRange(draft.maxTokens);
    topP.value = draft.topP;
    presencePenalty.value = draft.presencePenalty;
    reasoningEffort.value = draft.reasoningEffort;
    enableThinking.checked = draft.enableThinking;
    restoreResponsesDraft(draft.responses);
    state.messagesParameterBase = cloneObject(draft.messagesParameterBase);
    state.messagesParameterTouched = Object.assign({}, draft.messagesParameterTouched || {});
    state.messagesParametersReset = Boolean(draft.messagesParametersReset);
    messagesTemperature.value = draft.messagesTemperature || "0.7";
    messagesTopP.value = draft.messagesTopP || "1";
    messagesThinkingMode.value = draft.messagesThinkingMode || "";
    messagesThinkingBudget.value = draft.messagesThinkingBudget || "8192";
    messagesEffort.value = draft.messagesEffort || "";
    messagesStopSequences.value = draft.messagesStopSequences || "";
    messagesExtraBody.value = draft.messagesExtraBody || "";
    messagesThinkingBudgetRow.classList.toggle("hidden", messagesThinkingMode.value !== "enabled");
    renderHeaderRows(draft.headers);
    syncRangeOutputs();
    updateModelPickerLabel();
    state.modelParamDirty = draft.dirty;
    state.modelSelectionTouched = Boolean(draft.selectionTouched);
    updateProtocolControls();
  }

  // ---------- 自定义请求头编辑器（键值两列，可增减行） ----------
  // 渲染 effective_headers 回填键值行；收集/删除行都会置 dirty，
  // 确定时随 parameter 一并提交（headers 全量替换语义）
  function renderHeaderRows(headers) {
    customHeadersRows.innerHTML = "";
    (Array.isArray(headers) ? headers : []).forEach(function (item) {
      addHeaderRowInputs(item.name || "", item.value || "");
    });
  }

  function addHeaderRowInputs(name, value) {
    const row = el("div", "enhance-header-row");
    const nameInput = el("input", "enhance-header-name");
    nameInput.type = "text";
    nameInput.placeholder = "头名称（如 x-opencode-session）";
    nameInput.value = name || "";
    const valueInput = el("input", "enhance-header-value");
    valueInput.type = "text";
    valueInput.placeholder = "值";
    valueInput.value = value || "";
    const removeBtn = el("button", "enhance-header-remove");
    removeBtn.type = "button";
    removeBtn.title = "删除该行";
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", function () {
      row.remove();
      markParamDirty();
    });
    nameInput.addEventListener("input", markParamDirty);
    valueInput.addEventListener("input", markParamDirty);
    row.appendChild(nameInput);
    row.appendChild(valueInput);
    row.appendChild(removeBtn);
    customHeadersRows.appendChild(row);
  }

  function collectHeaderRows() {
    const headers = [];
    customHeadersRows.querySelectorAll(".enhance-header-row").forEach(function (row) {
      const name = (row.querySelector(".enhance-header-name") || {}).value || "";
      const value = (row.querySelector(".enhance-header-value") || {}).value || "";
      if (name.trim()) headers.push({ name: name.trim(), value: value });
    });
    return headers;
  }

  function nativeParameters(role, modelKey) {
    const cfg = state.modelConfigs[role];
    const info = cfg && cfg.role_info;
    const buckets = info && info.selection && info.selection.parameter;
    const native = (buckets && buckets.messages) || (info && info.selection && String(info.selection.api_type).replace(/-/g, "_") === "messages" && info.effective_parameter) || {};
    const params = {};
    ["thinking", "output_config", "stop_sequences", "temperature", "top_p", "extra_body"].forEach(function (key) {
      if (native[key] != null) params[key] = native[key];
    });
    return filterMessagesParameters(params, modelInfoByKey(role, modelKey || currentModelKey(role)));
  }

  // 按角色草稿对应模型的 api_type 分桶组装 parameter（responses 协议用 max_output_tokens）
  function buildParameterObject(role, draft) {
    const modelKey = draft ? draft.selectedModelKey : state.selectedModelKey;
    const model = modelInfoByKey(role, modelKey);
    const apiType = model && model.api_type ? String(model.api_type).replace(/-/g, "_").toLowerCase() : "chat_completions";
    if (apiType === "responses") return responseParameterObject(draft ? Object.assign({}, draft.responses, { maxTokens: draft.maxTokens }) : null, model);
    if (apiType === "messages") {
      const modelInfo = model;
      const base = draft ? draft.messagesParameterBase : state.messagesParameterBase;
      const touched = draft ? draft.messagesParameterTouched || {} : state.messagesParameterTouched || {};
      const reset = draft ? draft.messagesParametersReset : state.messagesParametersReset;
      const native = reset ? {} : filterMessagesParameters(base || nativeParameters(role), modelInfo);
      const hasTouch = function (key) { return Boolean(touched[key]); };
      const setValue = function (key, value) {
        if (!supportsMessageParameter(modelInfo, key)) { delete native[key]; return; }
        if (value == null) delete native[key]; else native[key] = value;
      };
      if (hasTouch("temperature")) setValue("temperature", Number(draft ? draft.messagesTemperature : messagesTemperature.value));
      if (hasTouch("top_p")) setValue("top_p", Number(draft ? draft.messagesTopP : messagesTopP.value));
      if (hasTouch("thinking")) {
        const mode = draft ? draft.messagesThinkingMode : messagesThinkingMode.value;
        if (mode === "adaptive") setValue("thinking", { type: "adaptive" });
        else if (mode === "enabled") {
          if (!supportsMessageParameter(modelInfo, "thinking_budget")) throw new Error("该模型声明不支持手动 thinking 预算（thinking_budget: false），请改用 Adaptive");
          const budget = Number(draft ? draft.messagesThinkingBudget : messagesThinkingBudget.value);
          setValue("thinking", { type: "enabled", budget_tokens: budget });
        } else setValue("thinking", null);
      }
      if (hasTouch("output_config")) {
        const effort = draft ? draft.messagesEffort : messagesEffort.value;
        const output = Object.assign({}, native.output_config || {});
        if (effort) output.effort = effort; else delete output.effort;
        setValue("output_config", Object.keys(output).length ? output : null);
      }
      if (hasTouch("stop_sequences")) {
        const text = draft ? draft.messagesStopSequences : messagesStopSequences.value;
        const stops = String(text || "").split(/\r?\n/).map(function (item) { return item.trim(); }).filter(Boolean);
        setValue("stop_sequences", stops.length ? stops : null);
      }
      if (hasTouch("extra_body")) {
        const text = String(draft ? draft.messagesExtraBody : messagesExtraBody.value || "").trim();
        let extra = null;
        if (text) {
          try { extra = JSON.parse(text); } catch (_) { throw new Error("extra_body 必须为合法 JSON"); }
          if (!extra || typeof extra !== "object" || Array.isArray(extra)) throw new Error("extra_body 须为 JSON 对象");
        }
        setValue("extra_body", extra);
      }
      const param = { max_tokens: Number(draft ? draft.maxTokens : maxTokens.value) };
      Object.keys(native).forEach(function (key) { if (native[key] != null) param[key] = native[key]; });
      validateMessagesParameter(param);
      return param;
    }
    const param = {
      temperature: Number(draft ? draft.temperature : temperature.value),
      top_p: Number(draft ? draft.topP : topP.value),
      presence_penalty: Number(draft ? draft.presencePenalty : presencePenalty.value),
      reasoning_effort: draft ? draft.reasoningEffort : reasoningEffort.value,
      extra_body: { enable_thinking: draft ? draft.enableThinking : enableThinking.checked },
    };
    const maxTokensDraft = draft ? draft.maxTokens : maxTokens.value;
    if (apiType === "responses") param.max_output_tokens = Number(maxTokensDraft);
    else param.max_tokens = Number(maxTokensDraft);
    return param;
  }

  async function loadModelConfigs(version, sessionId) {
    // 会话级模型选择：携带当前会话加载生效配置（未设置会话时展示全局默认）
    const tasks = MODEL_ROLES.map(function (r) {
      return API.getModels(r.role, sessionId).then(
        function (data) { return { role: r.role, data: data, error: null }; },
        function (err) { return { role: r.role, data: null, error: err.message }; }
      );
    });
    const results = await Promise.all(tasks);
    // 旧面板/旧会话的在途响应不能回写当前缓存或状态栏。
    if (!isCurrentPanel(version, sessionId)) return null;
    results.forEach(function (res) {
      state.modelConfigs[res.role] = res.error ? null : res.data;
      state.modelOverridden[res.role] = Boolean(
        res.data && res.data.role_info && res.data.role_info.is_overridden
      );
    });
    // 三个角色的缓存已由本流程整体写入：直接按缓存重绘状态条模型名。
    // 这里不再调用 refreshChatModelLabel()（无参调用会隐含"缓存必须先被写好"
    // 的时序约定，语义上等价于纯渲染，显式调用渲染函数更不容易被后续误用）。
    App.renderChatModelLabel();
    return results.some(function (res) { return res.error; });
  }

  // 面板提示：区分"会话独立选择 / 跟随全局默认 / 新对话保存为全局默认"三种语义
  function updateModelPanelHint() {
    if (App.enhanceScope) {
      App.enhanceScope.textContent = !state.sessionId ? "全局默认 · 新对话使用"
        : state.modelOverridden[state.activeModelRole] ? "当前会话 · 独立配置" : "当前会话 · 跟随全局，保存后独立生效";
    }
    if (state.modelLoadFailed) {
      enhanceHint.textContent = "部分模型配置加载失败，请检查后端服务";
      return;
    }
    const base = "参数未修改时切换模型会保留原参数；保存后从下一次任务生效";
    let scopeTip;
    if (!state.sessionId) {
      scopeTip = "新对话中的修改将保存为全局默认";
    } else if (roleConfig() && roleConfig().role_info && roleConfig().role_info.selection_invalid) {
      scopeTip = "原会话模型已失效，当前显示全局回退模型；确定后保存当前选择";
    } else if (state.modelOverridden[state.activeModelRole]) {
      scopeTip = "当前为本会话独立模型选择，仅对本会话生效";
    } else {
      scopeTip = "当前跟随全局默认，确定后保存为本会话独立选择";
    }
    let roleTip = "";
    if (state.activeModelRole === "sub_agent_model") {
      roleTip = "；未选择时子智能体继承聊天模型";
    }
    enhanceHint.textContent = base + "；" + scopeTip + roleTip;
    // 仅会话内且已设置覆盖时提供"清除会话覆盖"入口
    enhanceClearOverride.classList.toggle(
      "hidden",
      !(state.sessionId && (state.modelOverridden[state.activeModelRole]
        || (roleConfig() && roleConfig().role_info && roleConfig().role_info.selection_invalid)))
    );
  }

  function initModelPanel() {
    renderModelTabs();
    const draft = drafts()[state.activeModelRole];
    if (draft) {
      applyRoleDraft(draft);   // 切回页签：恢复未保存的编辑，不重新拉取回填
    } else {
      renderModelPicker();
      renderEnhanceFields();
      saveRoleDraft(state.activeModelRole);  // 以服务端回填为初始草稿基线
    }
    updateModelPanelHint();
    (App.enhanceBody || enhancePanel).scrollTop = 0;
  }

  function openModelPanel() {
    if (App.showModelPanelModal) App.showModelPanelModal();
    const version = ++panelVersion;
    const sessionId = state.sessionId || undefined;
    panelSessionId = sessionId;
    state.modelPanelDrafts = {};   // 每次打开都以服务端当前配置为基线
    // 先用缓存渲染，再异步刷新服务端配置
    initModelPanel();
    loadModelConfigs(version, sessionId).then(function (failed) {
      if (!isCurrentPanel(version, sessionId) || enhancePanel.classList.contains("hidden")) return;
      state.modelLoadFailed = failed;
      // 只刷新未编辑的缓存基线；加载期间修改过的各角色草稿必须保留。
      saveRoleDraft(state.activeModelRole);
      const edited = {};
      Object.keys(drafts()).forEach(function (role) {
        const draft = drafts()[role];
        if (draft.dirty || draft.selectionTouched) edited[role] = draft;
      });
      state.modelPanelDrafts = edited;
      // initModelPanel 内会按加载结果刷新提示文案与"清除会话覆盖"按钮可见性
      initModelPanel();
    });
  }

  modelTabs.addEventListener("click", function (e) {
    const tab = e.target.closest(".model-tab");
    if (!tab || tab.dataset.role === state.activeModelRole) return;
    saveRoleDraft(state.activeModelRole);   // 切走前保留当前页签的未保存编辑
    state.activeModelRole = tab.dataset.role;
    renderModelTabs();
    initModelPanel();
  });

  modelPicker.addEventListener("click", function () {
    renderModelList();
    modelList.classList.toggle("hidden");
  });

  modelList.addEventListener("click", function (e) {
    const item = e.target.closest(".model-list-item");
    if (!item) return;
    selectModelKey(item.dataset.key);
    modelList.classList.add("hidden");
  });

  // 点击面板其他区域时收起模型列表
  enhancePanel.addEventListener("click", function (e) {
    if (e.target.closest("#modelPicker") || e.target.closest("#modelList")) return;
    modelList.classList.add("hidden");
  });

  function touchMessagesParameter(key) {
    if (!state.messagesParameterTouched) state.messagesParameterTouched = {};
    state.messagesParameterTouched[key] = true;
    markParamDirty();
  }

  temperature.addEventListener("input", function () { markParamDirty(); syncRangeOutputs(); });
  maxTokens.addEventListener("input", function () {
    markParamDirty();
    // 最大输出 token 调小到不再大于手动预算时，预算自动下调到合法上限，并记为改动（保存时生效）
    if (syncManualBudgetRange(true)) touchMessagesParameter("thinking");
    syncRangeOutputs();
  });
  topP.addEventListener("input", function () { markParamDirty(); syncRangeOutputs(); });
  presencePenalty.addEventListener("input", function () { markParamDirty(); syncRangeOutputs(); });
  reasoningEffort.addEventListener("change", markParamDirty);
  enableThinking.addEventListener("change", markParamDirty);
  messagesTemperature.addEventListener("input", function () { touchMessagesParameter("temperature"); syncRangeOutputs(); });
  messagesTopP.addEventListener("input", function () { touchMessagesParameter("top_p"); syncRangeOutputs(); });
  messagesThinkingMode.addEventListener("change", function () {
    syncMessagesThinkingControls(modelInfoByKey(state.activeModelRole, state.selectedModelKey));
    touchMessagesParameter("thinking");
  });
  messagesThinkingBudget.addEventListener("input", function () { touchMessagesParameter("thinking"); syncRangeOutputs(); });
  messagesEffort.addEventListener("change", function () { touchMessagesParameter("output_config"); });
  messagesStopSequences.addEventListener("input", function () { touchMessagesParameter("stop_sequences"); });
  messagesExtraBody.addEventListener("input", function () { touchMessagesParameter("extra_body"); });
  // 自定义请求头：添加空行（输入/删除行时各自置 dirty）
  addHeaderRow.addEventListener("click", function () {
    addHeaderRowInputs("", "");
    markParamDirty();
  });

  function closeModelPanel() {
    if (enhancePanel.inert) return; // 保存过程中不关闭或丢失待完成的配置。
    ++panelVersion;
    ++responsesStateVersion;
    enhancePanel.classList.add("hidden");
    if (App.hideModelPanelModal) App.hideModelPanelModal();
  }
  enhanceCancel.addEventListener("click", closeModelPanel);
  if (App.enhanceClose) App.enhanceClose.addEventListener("click", closeModelPanel);

  // 恢复默认：按选中模型的 api_type 取预设默认参数（角色默认覆盖协议默认），
  // Messages 清除显式可选参数，使用供应商默认值。
  // 自定义请求头不属于"参数默认"范畴，恢复默认不触碰
  function applyRoleDefaults() {
    const model = modelInfoByKey(state.activeModelRole, state.selectedModelKey);
    const apiType = model && model.api_type
      ? String(model.api_type).replace(/-/g, "_").toLowerCase() : "chat_completions";
    const preset = API_TYPE_DEFAULT_PARAMS[apiType] || API_TYPE_DEFAULT_PARAMS.chat_completions;
    const merged = Object.assign({}, preset, apiType === "messages" ? {} : ROLE_DEFAULT_PARAMS[state.activeModelRole] || {});

    setupMaxTokensRange(defaultMaxTokens(model));
    if (apiType !== "messages") {
      temperature.value = String(merged.temperature);
      topP.value = String(merged.top_p);
      presencePenalty.value = String(merged.presence_penalty);
      reasoningEffort.value = merged.reasoning_effort;
      enableThinking.checked = merged.enable_thinking;
    }
    if (apiType === "responses") { renderResponsesFields({}); updateProtocolControls(); }
    if (apiType === "messages") {
      state.messagesParameterBase = {};
      state.messagesParameterTouched = {};
      state.messagesParametersReset = true;
      renderMessagesFields({}, state.selectedModelKey);
    }
    syncRangeOutputs();
    state.modelParamDirty = true;
    toast("已恢复为 " + apiType + " 默认参数");
  }

  enhanceReset.addEventListener("click", applyRoleDefaults);

  enhanceConfirm.addEventListener("click", async function () {
    if (panelSessionId !== (state.sessionId || undefined)) {
      toast("当前会话已切换，请重新打开参数面板后保存");
      return;
    }
    // 确认时先把当前页最新表单也写入草稿，再检查四个角色的草稿。
    // 页签切换只切换表单视图，未保存改动一直由 modelPanelDrafts 按角色持有。
    saveRoleDraft(state.activeModelRole);
    const changes = [];
    for (const item of MODEL_ROLES) {
      const role = item.role;
      const draft = drafts()[role];
      if (!draft) continue;
      const key = String(draft.selectedModelKey || "");
      const cfg = state.modelConfigs[role];
      // 服务端显示的是有效回退值，不能据此把修复失效覆盖判为“未变化”。
      const invalidOverride = Boolean(state.sessionId && cfg && cfg.role_info && cfg.role_info.selection_invalid);
      const modelChanged = key !== currentModelKey(role) || invalidOverride;
      const headersNow = JSON.stringify(Array.isArray(draft.headers) ? draft.headers : []);
      const headersBefore = JSON.stringify(
        (cfg && cfg.role_info && cfg.role_info.effective_headers) || []
      );
      const headersChanged = headersNow !== headersBefore;
      const paramsChanged = Boolean(draft.dirty || headersChanged);
      const establishOverride = Boolean(state.sessionId && draft.selectionTouched && !state.modelOverridden[role]);
      if (!modelChanged && !paramsChanged && !establishOverride) continue;
      if (!key) {
        toast("请先为「" + item.label + "」选择模型");
        return;
      }
      const sep = key.indexOf("::");
      if (sep <= 0) {
        toast("「" + item.label + "」的模型选择无效");
        return;
      }
      let parameter = null;
      try {
        parameter = paramsChanged ? buildParameterObject(role, draft) : null;
      } catch (error) {
        toast("「" + item.label + "」参数无效：" + error.message);
        return;
      }
      changes.push({
        role: role,
        label: item.label,
        key: key,
        provider: key.slice(0, sep),
        model: key.slice(sep + 2),
        modelChanged: modelChanged,
        parameter: parameter,
        headers: headersChanged ? draft.headers : null,
      });
    }
    if (!changes.length) {
      closeModelPanel();
      toast("模型与参数均未变化");
      return;
    }
    enhanceConfirm.disabled = true;
    const wasInert = enhancePanel.inert;
    enhancePanel.inert = true; // 保存请求期间冻结表单，避免快照之后的新编辑被关闭面板丢弃
    const saved = [];
    const sessionId = state.sessionId || undefined;
    const version = ++panelVersion; // 保存期间使之前的读取失效。
    try {
      // 同一全局配置文件/会话元数据由多个角色共写，顺序提交避免并发覆盖。
      for (const change of changes) {
        await API.selectModel(
          change.provider,
          change.model,
          change.role,
          change.parameter,
          sessionId,
          false,
          change.headers
        );
        saved.push(change);
      }
      if (!isCurrentPanel(version, sessionId)) return;
      const failed = await loadModelConfigs(version, sessionId);
      if (!isCurrentPanel(version, sessionId)) return;
      state.modelLoadFailed = failed;
      state.modelPanelDrafts = {};   // 保存成功：服务端状态已变化，草稿基线作废
      enhancePanel.inert = wasInert;
      enhancePanel.classList.add("hidden");
      if (App.hideModelPanelModal) App.hideModelPanelModal();
      toast("已保存：" + saved.map(function (item) { return item.label; }).join("、"));
      // 仅聊天模型更换才影响 token 估算口径；其他角色不影响聊天上下文统计。
      if (saved.some(function (item) { return item.role === "chat_model" && item.modelChanged; })) {
        App.refreshContextTokenStats(state.sessionId);
      }
    } catch (err) {
      const savedLabels = saved.map(function (item) { return item.label; });
      toast(saved.length
        ? "保存中断，已保存「" + savedLabels.join("、") + "」；未完成的草稿仍保留，可重新确定。原因：" + err.message
        : "保存失败：" + err.message);
    } finally {
      enhancePanel.inert = wasInert;
      enhanceConfirm.disabled = false;
    }
  });

  // 清除当前角色的会话独立模型选择，恢复跟随全局默认（仅会话内且已覆盖时可见）
  enhanceClearOverride.addEventListener("click", async function () {
    if (!state.sessionId) return;
    if (panelSessionId !== state.sessionId) return;
    const sessionId = state.sessionId;
    const role = state.activeModelRole;
    const version = ++panelVersion;
    enhanceClearOverride.disabled = true;
    try {
      const res = await API.selectModel("", "", role, null, sessionId, true);
      if (!isCurrentPanel(version, sessionId)) return;
      toast(res.message || "已恢复跟随全局默认模型");
      state.modelOverridden[role] = false;
      const data = await API.getModels(role, sessionId);
      if (!isCurrentPanel(version, sessionId)) return;
      // 写缓存 + 状态条模型名原子更新（单一入口，chat_model 角色才影响状态条）
      if (role === "chat_model") App.setChatModelConfig(data);
      else if (data) state.modelConfigs[role] = data;
      state.modelLoadFailed = false;
      if (state.modelPanelDrafts) delete state.modelPanelDrafts[role];
      initModelPanel();
      if (role === "chat_model") {
        App.refreshContextTokenStats(sessionId);
      }
    } catch (err) {
      toast("清除会话覆盖失败：" + err.message);
    } finally {
      enhanceClearOverride.disabled = false;
    }
  });


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.openModelPanel = openModelPanel;
  App.closeModelPanel = closeModelPanel;
})(window.App);
