/**
 * Token 统计
 * - 会话累计用量（usage 归一化交给 FormatUtils）与顶栏徽标
 * - 上下文 token 估算状态栏（防抖合并刷新、请求序号丢弃过期响应）
 * 依赖：app/core.js、API、FormatUtils、SessionUtils
 */
(function (App) {
  "use strict";
  const {
    state, tokenTotal, contextTokenStatus, contextTokenSummary
  } = App;

  // ---------- token 统计 ----------
  // 用量归一化 / 文案见 FormatUtils（js/format_utils.js）
  function setSessionUsage(usage) {
    state.sessionUsage = FormatUtils.normalizeUsage(usage);
    state.sessionTotalTokens = state.sessionUsage.total_tokens;
    tokenTotal.innerHTML =
      "<span>" + FormatUtils.fmtNum(state.sessionUsage.total_tokens) + " tokens</span>" +
      "<span>输入 " + FormatUtils.fmtNum(state.sessionUsage.prompt_tokens) +
      " · 输出 " + FormatUtils.fmtNum(state.sessionUsage.completion_tokens) + "</span>";
    tokenTotal.classList.toggle("hidden", !state.sessionUsage.total_tokens);
  }

  function setSessionTotalTokens(n) {
    setSessionUsage({ total_tokens: n || 0 });
  }

  function addSessionUsage(usage) {
    const current = state.sessionUsage;
    const next = FormatUtils.normalizeUsage(usage);
    setSessionUsage({
      prompt_tokens: current.prompt_tokens + next.prompt_tokens,
      completion_tokens: current.completion_tokens + next.completion_tokens,
      total_tokens: current.total_tokens + next.total_tokens,
    });
  }

  async function refreshSessionUsage(sessionId) {
    if (!sessionId && !state.sessionId) return false;
    const targetSessionId = SessionUtils.sanitizeSessionId(sessionId || state.sessionId);
    try {
      const meta = await API.getSessionMeta(targetSessionId);
      if (state.sessionId === targetSessionId && meta && meta.usage) {
        // 会话文件是累计用量的权威来源，覆盖流式期间的临时累计值。
        setSessionUsage(meta.usage);
        return true;
      }
    } catch (_) {
      // 流结束后的校准是辅助操作，失败时保留本地流式累计值。
    }
    return false;
  }

  // ---------- 上下文 token 统计 ----------
  // token_stats 会读取历史文件并重新估算消息，因此流式期间不按固定间隔轮询，
  // 只在 usage / 压缩完成等会实际改变上下文的事件后刷新。
  const CONTEXT_STATS_EVENT_DEBOUNCE_MS = 120;
  let contextStatsTimer = null;
  let contextStatsInFlight = false;
  let contextStatsRefreshQueued = false;
  let contextStatsQueuedSession = null;
  let contextStatsRequestSeq = 0;

  function hideContextTokenStatus() {
    state.contextTokenStats = null;
    contextTokenSummary.textContent = "";
    contextTokenSummary.title = "";
    // 只隐藏 token 摘要；工作路径不受会话上下文影响，保持常显。
    // 注意移除全局工具类 hidden（display:none !important），避免容器被藏住。
    contextTokenStatus.classList.remove("hidden");
    contextTokenStatus.classList.add("ctx-hidden");
    contextTokenStatus.classList.remove("is-warning", "is-danger");
  }

  function resetContextTokenStats() {
    contextStatsRequestSeq++;
    contextStatsRefreshQueued = false;
    contextStatsQueuedSession = null;
    clearContextStatsTimer();
    hideContextTokenStatus();
  }

  function clearContextStatsTimer() {
    if (contextStatsTimer === null) return;
    clearTimeout(contextStatsTimer);
    contextStatsTimer = null;
  }

  function finiteNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function formatContextPercent(ratio) {
    const percent = Math.max(0, ratio * 100);
    const digits = percent >= 10 ? 1 : 2;
    return percent.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "") + "%";
  }

  function formatTokensK(value) {
    // 统一保留 1 位小数再去尾零：100000 -> 100k，30105 -> 30.1k，105317 -> 105.3k
    const text = (value / 1000).toFixed(1);
    return text.replace(/0+$/, "").replace(/\.$/, "") + "k";
  }

  function renderContextTokenStats(stats) {
    const limit = finiteNumber(stats && stats.context_token_limit, 0);
    const used = finiteNumber(stats && stats.request_context_tokens, 0);
    const statsRounds = stats && stats.rounds && typeof stats.rounds === "object"
      ? stats.rounds : {};
    const hasConversationContext = state.hasConversation || state.streaming ||
      finiteNumber(statsRounds.total, 0) > 0 || Boolean(stats && stats.has_context_summary);
    if (!hasConversationContext) {
      hideContextTokenStatus();
      return;
    }
    if (limit <= 0 || used <= 0) {
      hideContextTokenStatus();
      return;
    }

    const ratioValue = finiteNumber(stats.estimated_budget_ratio, used / limit);
    const ratio = ratioValue >= 0 ? ratioValue : used / limit;
    const roundTokens = Array.isArray(stats.round_tokens) ? stats.round_tokens : [];
    const recentTokens = roundTokens.reduce(function (total, round) {
      return total + Math.max(0, finiteNumber(round && round.tokens, 0));
    }, 0);
    const latestRound = roundTokens.length ? roundTokens[roundTokens.length - 1] : null;
    const rounds = statsRounds;
    const recentCount = roundTokens.length;
    const recentLimit = Math.max(0, finiteNumber(rounds.max_rounds, 0));
    const summarizedCount = Math.max(0, finiteNumber(rounds.summarized, 0));

    // 常态只显示 30.1k/100k (30.1%)；小屏（≤420px）仅显示百分比
    contextTokenSummary.innerHTML =
      '<span class="cts-nums">' + formatTokensK(used) + "/" + formatTokensK(limit) + "</span>" +
      " (" + formatContextPercent(ratio) + ")";

    // 轮次详情不再常态占位：hover 摘要块时通过 title 查看
    const roundParts = [];
    if (recentCount) {
      const recentLabel = recentLimit > recentCount
        ? "近 " + recentCount + "/" + recentLimit + " 轮估算"
        : "近 " + recentCount + " 轮估算";
      roundParts.push(recentLabel + " " + FormatUtils.fmtNum(recentTokens));
      if (latestRound) {
        roundParts.push("最近轮估算 " + FormatUtils.fmtNum(Math.max(0, finiteNumber(latestRound.tokens, 0))));
      }
    }
    if (summarizedCount) roundParts.push("已摘要 " + FormatUtils.fmtNum(summarizedCount) + " 轮");
    const roundsDetail = roundParts.join(" · ");

    const messageTokens = Math.max(0, finiteNumber(stats.messages_tokens, 0));
    const systemTokens = Math.max(0, finiteNumber(stats.system_prompt_tokens, 0));
    const toolTokens = Math.max(0, finiteNumber(stats.tool_definition_tokens, 0));
    const compressionCount = Math.max(0, finiteNumber(stats.context_compress_count, 0));
    contextTokenSummary.title = [
      "模型上下文 token 估算",
      "消息 " + FormatUtils.fmtNum(messageTokens) +
      " · 系统提示词 " + FormatUtils.fmtNum(systemTokens) +
      " · 工具定义 " + FormatUtils.fmtNum(toolTokens),
      "请求上下文 " + FormatUtils.fmtNum(used) + " / " + FormatUtils.fmtNum(limit) +
      "（" + formatContextPercent(ratio) + "）",
      recentCount ? "最近 " + recentCount + " 轮合计 " + FormatUtils.fmtNum(recentTokens) + " tokens" : "暂无可展开轮次",
      compressionCount ? "单轮压缩 " + FormatUtils.fmtNum(compressionCount) + " 次" : "未发生单轮压缩",
    ].join("\n");
    contextTokenStatus.setAttribute("aria-label",
      "请求上下文 " + formatContextPercent(ratio) +
      (roundsDetail ? "，" + roundsDetail : ""));
    contextTokenStatus.classList.toggle("is-warning", ratio >= 0.8 && ratio < 1);
    contextTokenStatus.classList.toggle("is-danger", ratio >= 1);
    contextTokenStatus.classList.remove("hidden");
    contextTokenStatus.classList.remove("ctx-hidden");
    state.contextTokenStats = stats;
  }

  function scheduleContextTokenStatsRefresh(delay, sessionId) {
    // 尚未开始的会话（sessionId 为 null）不产生任何按会话的请求，
    // 否则后端会为不存在的会话创建只有 _meta 的空文件
    if (!sessionId && !state.sessionId) return;
    const targetSessionId = SessionUtils.sanitizeSessionId(sessionId || state.sessionId);
    if (targetSessionId !== state.sessionId) return;
    if (contextStatsTimer !== null) return;
    contextStatsTimer = setTimeout(function () {
      contextStatsTimer = null;
      refreshContextTokenStats(targetSessionId);
    }, Math.max(0, Number(delay) || 0));
  }

  async function refreshContextTokenStats(sessionId) {
    // 同上：待开始会话不发请求，选工具/改参数等操作静默跳过
    if (!sessionId && !state.sessionId) return;
    const targetSessionId = SessionUtils.sanitizeSessionId(sessionId || state.sessionId);
    if (targetSessionId !== state.sessionId) return;
    // 事件触发的立即刷新优先于已经排队的防抖刷新，避免同一事件产生两个请求。
    clearContextStatsTimer();
    if (contextStatsInFlight) {
      contextStatsRefreshQueued = true;
      contextStatsQueuedSession = targetSessionId;
      return;
    }

    const requestSeq = ++contextStatsRequestSeq;
    contextStatsInFlight = true;
    try {
      const toolNames = targetSessionId === state.sessionId
        ? Array.from(state.selectedTools)
        : [];
      const stats = await API.getContextTokenStats(targetSessionId, null, toolNames);
      if (requestSeq === contextStatsRequestSeq && targetSessionId === state.sessionId) {
        renderContextTokenStats(stats);
      }
    } catch (_) {
      // 统计是辅助信息，后端暂时不可用时保留已有数据显示，不阻塞聊天。
    } finally {
      contextStatsInFlight = false;
      const queued = contextStatsRefreshQueued;
      const queuedSession = contextStatsQueuedSession;
      contextStatsRefreshQueued = false;
      contextStatsQueuedSession = null;
      if (queued && queuedSession === state.sessionId && !contextStatsTimer) {
        // 请求结束后再合并处理事件期间积累的刷新请求，避免连续事件形成请求风暴。
        scheduleContextTokenStatsRefresh(CONTEXT_STATS_EVENT_DEBOUNCE_MS, queuedSession);
      }
    }
  }


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.setSessionUsage = setSessionUsage;
  App.setSessionTotalTokens = setSessionTotalTokens;
  App.addSessionUsage = addSessionUsage;
  App.refreshSessionUsage = refreshSessionUsage;
  App.resetContextTokenStats = resetContextTokenStats;
  App.clearContextStatsTimer = clearContextStatsTimer;
  App.scheduleContextTokenStatsRefresh = scheduleContextTokenStatsRefresh;
  App.refreshContextTokenStats = refreshContextTokenStats;
  App.CONTEXT_STATS_EVENT_DEBOUNCE_MS = CONTEXT_STATS_EVENT_DEBOUNCE_MS;
  App.renderContextTokenStats = renderContextTokenStats;
})(window.App);
