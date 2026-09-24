/**
 * 会话分组工具（纯逻辑，无 DOM 依赖，可单元测试）
 * - 规整分组注册表 / 会话归属映射（非法条目过滤、稳定排序）
 * - 会话按归属分桶（未知分组自动归为未分组）
 * - 分组名校验（与后端 _normalize_group_name 规则一致）
 * 浏览器挂 window.SessionGroupUtils；Node 下 module.exports，便于测试。
 */
(function (global) {
  "use strict";

  const GROUP_NAME_MAX = 40;
  // 多选分组视图的「未分组」伪桶 key（非真实分组 id，仅前端展示用）
  const UNGROUPED_KEY = "__ungrouped__";

  /**
   * 规整分组列表：过滤非法条目、按 order/created_at 稳定排序。
   * 输出结构统一为 { id, name, collapsed, order, createdAt }。
   */
  function normalizeGroups(raw) {
    const list = Array.isArray(raw) ? raw : [];
    const seen = Object.create(null);
    const out = [];
    list.forEach(function (item) {
      if (!item || typeof item !== "object") return;
      const id = String(item.id == null ? "" : item.id).trim();
      const name = String(item.name == null ? "" : item.name).trim();
      if (!id || !name || seen[id]) return;
      seen[id] = true;
      out.push({
        id: id,
        name: name.slice(0, GROUP_NAME_MAX),
        collapsed: Boolean(item.collapsed),
        order: Number.isFinite(item.order) ? item.order : 0,
        createdAt: String(item.created_at == null ? "" : item.created_at),
      });
    });
    out.sort(function (a, b) {
      if (a.order !== b.order) return a.order - b.order;
      if (a.createdAt === b.createdAt) return 0;
      return a.createdAt < b.createdAt ? -1 : 1;
    });
    return out;
  }

  /**
   * 规整归属映射：{sessionId: groupId}；过滤空值与非字符串，id 去空白。
   */
  function normalizeAssignments(raw) {
    const out = Object.create(null);
    if (!raw || typeof raw !== "object") return out;
    Object.keys(raw).forEach(function (sid) {
      const gid = raw[sid];
      if (typeof gid !== "string" || !gid.trim()) return;
      out[String(sid)] = gid.trim();
    });
    return out;
  }

  /**
   * 把会话行分桶到各分组。
   * rows: [{ id, title }]（调用方已按「最近」规则排序）
   * groups: normalizeGroups 输出
   * assignments: normalizeAssignments 输出
   * 返回 [{ group, sessions }]：
   * - 归属指向已删除/不存在分组的会话自动归为未分组（不出现在任何桶）；
   * - 会话顺序保持入参顺序。
   */
  function bucketSessions(rows, groups, assignments) {
    const buckets = Object.create(null);
    const out = [];
    (groups || []).forEach(function (g) {
      const bucket = { group: g, sessions: [] };
      buckets[g.id] = bucket;
      out.push(bucket);
    });
    (rows || []).forEach(function (row) {
      if (!row || !row.id) return;
      const gid = assignments ? assignments[row.id] : null;
      if (!gid) return;
      const bucket = buckets[gid];
      if (!bucket) return; // 未知分组：归为未分组
      bucket.sessions.push(row);
    });
    return out;
  }

  /**
   * 多选视图分桶：已定义分组（按注册表顺序，空组保留）+ 末尾「未分组」桶。
   * rows: [{ id, title }]（调用方已按「最近」规则排序）
   * groups: normalizeGroups 输出
   * assignments: normalizeAssignments 输出
   * 返回 [{ key, name, sessions }]：
   * - key 为 group.id 或 UNGROUPED_KEY（未分组）；
   * - 归属指向已删除/不存在分组的会话归入「未分组」；
   * - 「未分组」桶仅在存在未分组会话时返回（避免空噪音）；
   * - 会话顺序保持入参顺序（最近在前）。
   */
  function bucketForBulk(rows, groups, assignments) {
    const list = Array.isArray(groups) ? groups : [];
    const known = Object.create(null);
    const byKey = Object.create(null);
    const out = [];
    list.forEach(function (g) {
      if (!g || !g.id || known[g.id]) return;
      known[g.id] = true;
      const bucket = { key: g.id, name: g.name, sessions: [] };
      byKey[g.id] = bucket;
      out.push(bucket);
    });
    const ungrouped = { key: UNGROUPED_KEY, name: "未分组", sessions: [] };
    (rows || []).forEach(function (row) {
      if (!row || !row.id) return;
      const gid = assignments ? assignments[row.id] : null;
      if (gid && known[gid]) {
        byKey[gid].sessions.push(row);
      } else {
        ungrouped.sessions.push(row);
      }
    });
    if (ungrouped.sessions.length) out.push(ungrouped);
    return out;
  }

  /**
   * 校验/规整分组名（与后端一致：控制字符折叠、trim、上限 40）。
   * 返回 { ok, name, error }。
   */
  function validateGroupName(raw) {
    const name = String(raw == null ? "" : raw).replace(/[\r\n\t]+/g, " ").trim();
    if (!name) return { ok: false, name: "", error: "分组名不能为空" };
    return { ok: true, name: name.slice(0, GROUP_NAME_MAX), error: "" };
  }

  const api = {
    GROUP_NAME_MAX: GROUP_NAME_MAX,
    UNGROUPED_KEY: UNGROUPED_KEY,
    normalizeGroups: normalizeGroups,
    normalizeAssignments: normalizeAssignments,
    bucketSessions: bucketSessions,
    bucketForBulk: bucketForBulk,
    validateGroupName: validateGroupName,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.SessionGroupUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
