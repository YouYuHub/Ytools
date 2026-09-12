/**
 * 模型与参数面板（“参数”按钮）
 * - 三个角色（聊天/压缩/标题模型）的模型选择与生成参数编辑
 * - 会话独立选择 vs 全局默认语义、api_type 参数分桶、恢复默认预设
 * 依赖：app/core.js、API；App.*：stats 模块（token 统计刷新）
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, MODEL_ROLES,
    ROLE_DEFAULT_PARAMS, API_TYPE_DEFAULT_PARAMS, enhancePanel, modelTabs,
    modelPicker, modelPickerLabel, modelList, reasoningEffort,
    enableThinking, temperature, temperatureValue, maxTokens,
    maxTokensValue, topP, topPValue, presencePenalty,
    presencePenaltyValue, enhanceCancel, enhanceConfirm, enhanceReset,
    enhanceClearOverride, enhanceHint
  } = App;

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
    modelList.classList.add("hidden");
    updateModelPickerLabel();
  }

  function updateModelPickerLabel() {
    const key = state.selectedModelKey;
    if (!key) {
      modelPickerLabel.textContent = "请选择模型";
      modelPickerLabel.classList.add("placeholder");
      return;
    }
    const sep = key.indexOf("::");
    const name = key.slice(sep + 2);
    const model = modelInfoByKey(state.activeModelRole, key);
    modelPickerLabel.textContent = model && model.api_type ? name + " · " + model.api_type : name;
    modelPickerLabel.classList.remove("placeholder");
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
        const item = el("button", "model-list-item", m.model_name);
        item.type = "button";
        item.dataset.key = provider + "::" + m.model_name;
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
    updateModelPickerLabel();
    // 切换到不同模型后立即拉取一次 /chat_config/models，
    // 用服务端最新 max_output_tokens 校准“最大输出 token”：当前配置超过上限则压回上限，其余参数不动
    if (switched) refreshMaxTokensAfterSwitch(key);
  }

  function refreshMaxTokensAfterSwitch(key) {
    const role = state.activeModelRole;
    API.getModels(role, state.sessionId || undefined).then(function (data) {
      state.modelConfigs[role] = data;
    }, function () { /* 拉取失败沿用缓存模型信息 */ }).then(function () {
      // 等待期间用户可能已切角色/换模型/关面板，过期结果不回填
      if (enhancePanel.classList.contains("hidden")) return;
      if (state.activeModelRole !== role || state.selectedModelKey !== key) return;
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
    temperatureValue.value = formatNumber(temperature.value);
    maxTokensValue.value = Number(maxTokens.value).toLocaleString("en-US");
    topPValue.value = formatNumber(topP.value);
    presencePenaltyValue.value = formatNumber(presencePenalty.value);
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
    syncRangeOutputs();
    state.modelParamDirty = false;
  }

  function markParamDirty() {
    state.modelParamDirty = true;
  }

  // 按选中模型的 api_type 分桶组装 parameter（responses 协议用 max_output_tokens）
  function buildParameterObject() {
    const model = modelInfoByKey(state.activeModelRole, state.selectedModelKey);
    const apiType = model && model.api_type ? String(model.api_type).replace(/-/g, "_").toLowerCase() : "chat_completions";
    const param = {
      temperature: Number(temperature.value),
      top_p: Number(topP.value),
      presence_penalty: Number(presencePenalty.value),
      reasoning_effort: reasoningEffort.value,
      extra_body: { enable_thinking: enableThinking.checked },
    };
    if (apiType === "responses") param.max_output_tokens = Number(maxTokens.value);
    else param.max_tokens = Number(maxTokens.value);
    return param;
  }

  async function loadModelConfigs() {
    // 会话级模型选择：携带当前会话加载生效配置（未设置会话时展示全局默认）
    const sessionId = state.sessionId || undefined;
    const tasks = MODEL_ROLES.map(function (r) {
      return API.getModels(r.role, sessionId).then(
        function (data) { return { role: r.role, data: data, error: null }; },
        function (err) { return { role: r.role, data: null, error: err.message }; }
      );
    });
    const results = await Promise.all(tasks);
    results.forEach(function (res) {
      state.modelConfigs[res.role] = res.error ? null : res.data;
      state.modelOverridden[res.role] = Boolean(
        res.data && res.data.role_info && res.data.role_info.is_overridden
      );
    });
    return results.some(function (res) { return res.error; });
  }

  // 面板提示：区分"会话独立选择 / 跟随全局默认 / 新对话保存为全局默认"三种语义
  function updateModelPanelHint() {
    if (state.modelLoadFailed) {
      enhanceHint.textContent = "部分模型配置加载失败，请检查后端服务";
      return;
    }
    const base = "参数未修改时仅切换模型、保留原参数";
    let scopeTip;
    if (!state.sessionId) {
      scopeTip = "新对话中的修改将保存为全局默认";
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
      !(state.sessionId && state.modelOverridden[state.activeModelRole])
    );
  }

  function initModelPanel() {
    renderModelTabs();
    renderModelPicker();
    renderEnhanceFields();
    updateModelPanelHint();
    enhancePanel.scrollTop = 0;
  }

  function openModelPanel() {
    // 先用缓存渲染，再异步刷新服务端配置
    initModelPanel();
    loadModelConfigs().then(function (failed) {
      if (enhancePanel.classList.contains("hidden")) return;
      state.modelLoadFailed = failed;
      // initModelPanel 内会按加载结果刷新提示文案与"清除会话覆盖"按钮可见性
      initModelPanel();
    });
  }

  modelTabs.addEventListener("click", function (e) {
    const tab = e.target.closest(".model-tab");
    if (!tab || tab.dataset.role === state.activeModelRole) return;
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

  temperature.addEventListener("input", function () { markParamDirty(); syncRangeOutputs(); });
  maxTokens.addEventListener("input", function () {
    markParamDirty();
    syncRangeOutputs();
  });
  topP.addEventListener("input", function () { markParamDirty(); syncRangeOutputs(); });
  presencePenalty.addEventListener("input", function () { markParamDirty(); syncRangeOutputs(); });
  reasoningEffort.addEventListener("change", markParamDirty);
  enableThinking.addEventListener("change", markParamDirty);

  enhanceCancel.addEventListener("click", function () { enhancePanel.classList.add("hidden"); });

  // 恢复默认：按选中模型的 api_type 取预设默认参数（角色默认覆盖协议默认），
  // 后端暂未按 api_type 细分适配，预设先占位，后续随协议设计调整
  function applyRoleDefaults() {
    const model = modelInfoByKey(state.activeModelRole, state.selectedModelKey);
    const apiType = model && model.api_type
      ? String(model.api_type).replace(/-/g, "_").toLowerCase() : "chat_completions";
    const preset = API_TYPE_DEFAULT_PARAMS[apiType] || API_TYPE_DEFAULT_PARAMS.chat_completions;
    const merged = Object.assign({}, preset, ROLE_DEFAULT_PARAMS[state.activeModelRole] || {});

    temperature.value = String(merged.temperature);
    setupMaxTokensRange(defaultMaxTokens(model));
    topP.value = String(merged.top_p);
    presencePenalty.value = String(merged.presence_penalty);
    reasoningEffort.value = merged.reasoning_effort;
    enableThinking.checked = merged.enable_thinking;
    syncRangeOutputs();
    state.modelParamDirty = true;
    toast("已恢复为 " + apiType + " 默认参数");
  }

  enhanceReset.addEventListener("click", applyRoleDefaults);

  enhanceConfirm.addEventListener("click", async function () {
    const key = state.selectedModelKey;
    if (!key) { toast("请先选择模型"); return; }
    const sep = key.indexOf("::");
    if (sep <= 0) return;
    const provider = key.slice(0, sep);
    const model = key.slice(sep + 2);
    // 先与面板打开时快照的服务端当前选择比对：模型未更换且参数未改动时
    // 不再调用切换接口，避免每次确定都弹"已切换"提示。
    // 快照缺失（配置加载失败）时按有变化处理，仍允许显式保存
    const modelChanged = key !== currentModelKey(state.activeModelRole);
    const paramsChanged = state.modelParamDirty;
    if (!modelChanged && !paramsChanged) {
      enhancePanel.classList.add("hidden");
      toast("模型与参数均未变化");
      return;
    }
    enhanceConfirm.disabled = true;
    try {
      const parameter = paramsChanged ? buildParameterObject() : null;
      // 会话内保存为该会话独立模型选择；新对话（无 sessionId）保存为全局默认
      const res = await API.selectModel(provider, model, state.activeModelRole, parameter, state.sessionId || undefined);
      // 模型未更换（仅保存参数）时不用后端的"已切换"文案，避免误导
      toast(modelChanged ? (res.message || "已保存") : "模型未更换，参数已保存");
      enhancePanel.classList.add("hidden");
      const data = await API.getModels(state.activeModelRole, state.sessionId || undefined);
      if (data) state.modelConfigs[state.activeModelRole] = data;
      // 仅模型更换才影响 token 估算口径（窗口取自模型定义，参数不影响）：
      // 切换聊天模型后立即刷新统计标签；压缩/标题模型不影响统计，无需刷新
      if (modelChanged && state.activeModelRole === "chat_model") {
        App.refreshContextTokenStats(state.sessionId);
      }
    } catch (err) {
      toast("保存失败：" + err.message);
    } finally {
      enhanceConfirm.disabled = false;
    }
  });

  // 清除当前角色的会话独立模型选择，恢复跟随全局默认（仅会话内且已覆盖时可见）
  enhanceClearOverride.addEventListener("click", async function () {
    if (!state.sessionId) return;
    enhanceClearOverride.disabled = true;
    try {
      const res = await API.selectModel("", "", state.activeModelRole, null, state.sessionId, true);
      toast(res.message || "已恢复跟随全局默认模型");
      state.modelOverridden[state.activeModelRole] = false;
      const data = await API.getModels(state.activeModelRole, state.sessionId);
      if (data) state.modelConfigs[state.activeModelRole] = data;
      state.modelLoadFailed = false;
      initModelPanel();
      if (state.activeModelRole === "chat_model") {
        App.refreshContextTokenStats(state.sessionId);
      }
    } catch (err) {
      toast("清除会话覆盖失败：" + err.message);
    } finally {
      enhanceClearOverride.disabled = false;
    }
  });


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.openModelPanel = openModelPanel;
})(window.App);
