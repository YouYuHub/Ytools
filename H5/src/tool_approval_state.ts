/** 审批记录（后端字段宽松，UI 只读 id/session_id/state 等少量键）。 */
interface ApprovalRecord {
  id: string;
  session_id?: string;
  task_id?: string;
  agent_id?: string;
  tool_call_id?: string;
  state?: string;
  task_state?: string;
  [key: string]: unknown;
}

interface ApprovalEvent {
  approval?: ApprovalRecord | null;
  phase?: string;
}

interface ApprovalResult {
  approvals?: ApprovalRecord[];
  states?: ApprovalRecord[];
  approval?: ApprovalRecord;
  [key: string]: unknown;
}

interface ApprovalApi {
  pendingApprovals: (sessionId: unknown, ids: unknown) => Promise<ApprovalResult>;
  decideApproval: (approval: ApprovalRecord, decision: string) => Promise<ApprovalResult>;
}

interface ApprovalStore {
  apply: (event: ApprovalEvent | null | undefined) => void;
  refresh: (sessionId: string) => Promise<ApprovalResult | null>;
  decide: (id: string, decision: string) => Promise<ApprovalResult>;
  busy: (id: string) => boolean;
  track: (approval: ApprovalRecord | null | undefined) => void;
  states: (sessionId?: string | null) => ApprovalRecord[];
  items: (sessionId?: string | null) => ApprovalRecord[];
}

interface ToolApprovalStateApi {
  create: (api: ApprovalApi, changed?: (() => void) | null) => ApprovalStore;
}

(function (global: typeof globalThis, factory: () => ToolApprovalStateApi) {
  const exported = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else (global as unknown as { ToolApprovalState: ToolApprovalStateApi }).ToolApprovalState = exported;
})(typeof self !== "undefined" ? self : globalThis, function (): ToolApprovalStateApi {
  "use strict";
  function create(api: ApprovalApi, changed?: (() => void) | null): ApprovalStore {
    const records = new Map<string, ApprovalRecord>(), versions = new Map<string, number>(),
      inFlight = new Map<string, Promise<ApprovalResult>>(), inFlightKeys = new Map<string, string>(),
      retired = new Set<string>();
    const known = new Map<string, ApprovalRecord>(), states = new Map<string, ApprovalRecord>();
    function setState(a: ApprovalRecord): void { states.delete(a.id); states.set(a.id, a); }
    const key = (a: ApprovalRecord): string => a.task_id && a.tool_call_id
      ? [a.task_id, a.agent_id, a.tool_call_id].join("\n") : a.id;
    function pendingId(id: string): string | undefined {
      if (inFlight.has(id)) return id;
      const a = records.get(id);
      if (!a) return undefined;
      const wanted = key(a);
      const hit = Array.from(inFlightKeys).find(([, value]) => value === wanted);
      return hit ? hit[0] : undefined;
    }
    function touch(sessionId: any): void {
      versions.set(sessionId, (versions.get(sessionId) || 0) + 1);
      if (changed) changed();
    }
    function apply(event: ApprovalEvent | null | undefined): void {
      const a = event && event.approval;
      if (!a || !a.id) return;
      if (event.phase === "requested" && retired.has(a.id)) return;
      known.set(a.id, a);
      setState({ ...a, state: event.phase === "cancelled" ? "cancelled" : a.state });
      if (event.phase === "requested" && !retired.has(a.id)) records.set(a.id, a);
      else { records.delete(a.id); retired.add(a.id); }
      touch(a.session_id);
    }
    async function refresh(sid: string): Promise<ApprovalResult | null> {
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
        if (["cancelled", "completed", "invalidated"].includes(a.state as string)) retired.add(a.id);
      });
      (result.approvals || []).forEach(a => {
        if (!retired.has(a.id) || a.task_state === "suspended") records.set(a.id, a);
      });
      touch(sid);
      return result;
    }
    function decide(id: string, decision: string): Promise<ApprovalResult> {
      const pending = pendingId(id);
      if (pending) return inFlight.get(pending) as Promise<ApprovalResult>;
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
    return { apply, refresh, decide, busy: (id: string) => Boolean(pendingId(id)),
      track: (a: ApprovalRecord | null | undefined) => { if (a && a.id) known.set(a.id, a); },
      states: (sid?: string | null) => Array.from(states.values()).filter(a => !sid || a.session_id === sid),
      items: (sid?: string | null) => Array.from(records.values()).filter(a => !sid || a.session_id === sid) };
  }
  return { create };
});
