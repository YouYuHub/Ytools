(function (global) {
  "use strict";
  function create(api, changed) {
    const records = new Map(), versions = new Map(), inFlight = new Map(), inFlightKeys = new Map(), retired = new Set();
    const known = new Map(), states = new Map();
    function setState(a) { states.delete(a.id); states.set(a.id, a); }
    const key = a => a.task_id && a.tool_call_id ? [a.task_id, a.agent_id, a.tool_call_id].join("\n") : a.id;
    function pendingId(id) {
      if (inFlight.has(id)) return id;
      const a = records.get(id);
      return a && Array.from(inFlightKeys).find(([, value]) => value === key(a))?.[0];
    }
    function touch(sid) { versions.set(sid, (versions.get(sid) || 0) + 1); if (changed) changed(); }
    function apply(event) {
      const a = event && event.approval;
      if (!a || !a.id) return;
      if (event.phase === "requested" && retired.has(a.id)) return;
      known.set(a.id, a); setState({ ...a, state: event.phase === "cancelled" ? "cancelled" : a.state });
      if (event.phase === "requested" && !retired.has(a.id)) records.set(a.id, a);
      else { records.delete(a.id); retired.add(a.id); }
      touch(a.session_id);
    }
    async function refresh(sid) {
      const version = versions.get(sid) || 0;
      const ids = Array.from(known.values()).filter(a => a.session_id === sid).map(a => a.id);
      const result = await api.pendingApprovals(sid, ids);
      if ((versions.get(sid) || 0) !== version) return null;
      for (const [id, a] of records) if (a.session_id === sid &&
        !(result.approvals || []).some(next => next.id === id) && !(result.states || []).some(next => next.id === id)) {
        setState({ ...a, state: "unavailable" });
        // The backend no longer reports this card: retire it so a replayed
        // "requested" event can never resurrect it as actionable.
        retired.add(id);
      }
      for (const [id, a] of records) if (a.session_id === sid) records.delete(id);
      (result.states || []).concat(result.approvals || []).forEach(a => {
        if (a.state === "pending" && retired.has(a.id)) return;
        known.set(a.id, a); setState(a);
        if (["cancelled", "completed", "invalidated"].includes(a.state)) retired.add(a.id);
      });
      (result.approvals || []).forEach(a => {
        if (!retired.has(a.id) || a.task_state === "suspended") records.set(a.id, a);
      });
      touch(sid);
      return result;
    }
    function decide(id, decision) {
      const pending = pendingId(id);
      if (pending) return inFlight.get(pending);
      const approval = records.get(id);
      if (!approval) return Promise.reject(new Error("审批已失效，请刷新状态"));
      const promise = Promise.resolve().then(() => api.decideApproval(approval, decision)).then(result => {
        records.delete(id); retired.add(id);
        const next = result.approval || { ...approval, state: decision === "approve" ? "approved" : "denied" };
        known.set(next.id, next); setState(next);
        if (result.approval) { records.delete(result.approval.id); retired.add(result.approval.id); }
        return result;
      }).finally(() => { inFlight.delete(id); inFlightKeys.delete(id); touch(approval.session_id); });
      inFlight.set(id, promise); inFlightKeys.set(id, key(approval)); touch(approval.session_id);
      return promise;
    }
    return { apply, refresh, decide, busy: id => Boolean(pendingId(id)),
      track: a => { if (a && a.id) known.set(a.id, a); },
      states: sid => Array.from(states.values()).filter(a => !sid || a.session_id === sid),
      items: sid => Array.from(records.values()).filter(a => !sid || a.session_id === sid) };
  }
  const exported = { create };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else global.ToolApprovalState = exported;
})(typeof window !== "undefined" ? window : globalThis);
