/** 分组注册表原始条目（后端/存储的宽松形态）。 */
interface RawGroup {
  id?: unknown;
  name?: unknown;
  collapsed?: unknown;
  created_at?: unknown;
  [key: string]: unknown;
}

/** normalizeGroups 输出的规整分组。 */
interface NormalizedGroup {
  id: string;
  name: string;
  collapsed: boolean;
  createdAt: string;
}

type AssignmentMap = Record<string, string>;

interface SessionRowLike {
  id?: unknown;
  title?: unknown;
  [key: string]: unknown;
}

interface GroupBucket {
  group: NormalizedGroup;
  sessions: SessionRowLike[];
}

interface BulkBucket {
  key: string;
  name: string;
  sessions: SessionRowLike[];
}

interface GroupNameCheck {
  ok: boolean;
  name: string;
  error: string;
}

interface SessionGroupUtilsApi {
  GROUP_NAME_MAX: number;
  UNGROUPED_KEY: string;
  normalizeGroups: (raw: unknown) => NormalizedGroup[];
  normalizeAssignments: (raw: unknown) => Record<string, string>;
  bucketSessions: (rows: unknown, groups: unknown, assignments: unknown) => GroupBucket[];
  bucketForBulk: (rows: unknown, groups: unknown, assignments: unknown) => BulkBucket[];
  validateGroupName: (raw: unknown) => GroupNameCheck;
}

/**
 * 会话分组工具（纯逻辑，无 DOM 依赖，可单元测试）
 * - 规整分组注册表 / 会话归属映射（非法条目过滤、稳定排序）
 * - 会话按归属分桶（未知分组自动归为未分组）
 * - 分组名校验（与后端 _normalize_group_name 规则一致）
 * 浏览器挂 window.SessionGroupUtils；Node 下 module.exports，便于测试。
 */
(function (global: typeof globalThis) {
  "use strict";

  const GROUP_NAME_MAX = 40;
  // 多选分组视图的「未分组」伪桶 key（非真实分组 id，仅前端展示用）
  const UNGROUPED_KEY = "__ungrouped__";

  /**
   * 规整分组列表：过滤非法条目、按 order/created_at 稳定排序。
   * 输出结构统一为 { id, name, collapsed, order, createdAt }。
   */
  function normalizeGroups(raw: unknown): NormalizedGroup[] {
    const list: unknown[] = Array.isArray(raw) ? raw : [];
    const seen: Record<string, boolean> = Object.create(null);
    const out: NormalizedGroup[] = [];
    list.forEach(function (item: unknown) {
      if (!item || typeof item !== "object") return;
      const entry = item as RawGroup;
      const id = String(entry.id == null ? "" : entry.id).trim();
      const name = String(entry.name == null ? "" : entry.name).trim();
      if (!id || !name || seen[id]) return;
      seen[id] = true;
      out.push({
        id: id,
        name: name.slice(0, GROUP_NAME_MAX),
        collapsed: Boolean(entry.collapsed),
        createdAt: String(entry.created_at == null ? "" : entry.created_at),
      });
    });
    out.sort(function (a, b) {
      if (a.createdAt === b.createdAt) return 0;
      return a.createdAt < b.createdAt ? -1 : 1;
    });
    return out;
  }

  /**
   * 规整归属映射：{sessionId: groupId}；过滤空值与非字符串，id 去空白。
   */
  function normalizeAssignments(raw: unknown): Record<string, string> {
    const out: Record<string, string> = Object.create(null);
    if (!raw || typeof raw !== "object") return out;
    const src = raw as Record<string, unknown>;
    Object.keys(src).forEach(function (sid) {
      const gid = src[sid];
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
  function bucketSessions(rows: unknown, groups: unknown, assignments: unknown): GroupBucket[] {
    const buckets: Record<string, GroupBucket> = Object.create(null);
    const out: GroupBucket[] = [];
    ((groups as NormalizedGroup[] | null) || []).forEach(function (g) {
      const bucket = { group: g, sessions: [] as SessionRowLike[] };
      buckets[g.id] = bucket;
      out.push(bucket);
    });
    ((rows as SessionRowLike[] | null) || []).forEach(function (row) {
      if (!row || !row.id) return;
      const table = assignments as AssignmentMap | null | undefined;
      const gid = table ? table[row.id as string] : null;
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
  function bucketForBulk(rows: unknown, groups: unknown, assignments: unknown): BulkBucket[] {
    const list: NormalizedGroup[] = Array.isArray(groups) ? groups : [];
    const known: Record<string, boolean> = Object.create(null);
    const byKey: Record<string, BulkBucket> = Object.create(null);
    const out: BulkBucket[] = [];
    list.forEach(function (g) {
      if (!g || !g.id || known[g.id]) return;
      known[g.id] = true;
      const bucket = { key: g.id, name: g.name, sessions: [] as SessionRowLike[] };
      byKey[g.id] = bucket;
      out.push(bucket);
    });
    const ungrouped: BulkBucket = { key: UNGROUPED_KEY, name: "未分组", sessions: [] };
    ((rows as SessionRowLike[] | null) || []).forEach(function (row) {
      if (!row || !row.id) return;
      const table = assignments as AssignmentMap | null | undefined;
      const gid = table ? table[row.id as string] : null;
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
  function validateGroupName(raw: unknown): GroupNameCheck {
    const name = String(raw == null ? "" : raw).replace(/[\r\n\t]+/g, " ").trim();
    if (!name) return { ok: false, name: "", error: "分组名不能为空" };
    return { ok: true, name: name.slice(0, GROUP_NAME_MAX), error: "" };
  }

  const api: SessionGroupUtilsApi = {
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
    (global as unknown as { SessionGroupUtils: SessionGroupUtilsApi }).SessionGroupUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
