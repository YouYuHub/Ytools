/** read_media 结果条目（后端载荷的宽松形态）。 */
interface ReadMediaEntry {
  reference?: unknown;
  kind?: unknown;
  mime?: unknown;
  converted?: { from_video?: unknown } | null;
  video?: { duration?: unknown; read?: { seconds?: unknown } } | null;
  reason?: unknown;
  message?: unknown;
  [key: string]: unknown;
}

interface ParsedReadMedia {
  loaded: ReadMediaEntry[];
  skipped: ReadMediaEntry[];
}

/** 图片条磁贴项（buildStripItems 输出）。 */
interface StripItem {
  tile: string;
  name: string;
  url: string | null;
  previewType: string | null;
  reason: string;
  duration: string;
}

/** URL 解析依赖（API 方法必须方法调用，宿主方法可能依赖 this）。 */
interface ReferenceUrlApi {
  sessionMediaUrl: (sessionId: unknown, name: string) => string;
  localFileUrl?: (sessionId: unknown, ref: string) => string;
}

/** DOM 构建依赖（el 返回 HTMLElement，测试注入假节点）。 */
interface StripDeps {
  el: (tag: string, cls?: string, text?: string) => HTMLElement;
  onPreview: (item: StripItem) => void;
}

/** 延迟设置 src 的媒体节点（仅 image/video 磁贴入列，url 恒非空）。 */
interface DeferredMedia {
  node: { src: string };
  url: string;
}

interface ReadMediaStripApi {
  parseReadMediaResult: (resultText: unknown) => ParsedReadMedia | null;
  displayNameForReference: (ref: unknown) => string;
  resolveReferenceUrl: (api: ReferenceUrlApi, ref: unknown, sessionId: unknown) => string | null;
  previewTypeForKind: (kind: unknown, mime: unknown) => string;
  formatDuration: (seconds: unknown) => string;
  headerTextForCounts: (loaded: ReadMediaEntry[], skipped: ReadMediaEntry[]) => string;
  buildStripItems: (api: ReferenceUrlApi, parsed: ParsedReadMedia, sessionId: unknown) => { headerText: string; items: StripItem[] };
  buildMediaStrip: (deps: StripDeps, headerText: string, items: StripItem[]) => HTMLElement;
}

(function (global: typeof globalThis) {
  "use strict";
  // read_media 工具结果的图片条：解析结果 JSON → 可展开的缩略图网格。
  // - loaded 项按引用解析出可访问 URL（media:// 走会话媒体端点，http(s)/
  //   data 原样，本地路径走 /file/get_local_file），图片/视频/音频分别建块；
  // - 默认折叠，首次展开时才设置媒体 src（避免外部 URL 自动请求与预加载开销）；
  // - 加载失败（文件已删除）就地替换为“找不到图片”占位；skipped 项展示原因。
  // 纯函数 + 注入式 DOM 构建（deps.el），可直接单测。
  function parseReadMediaResult(resultText: unknown): ParsedReadMedia | null {
    let obj: unknown = resultText;
    if (typeof obj === "string") {
      try {
        obj = JSON.parse(obj);
      } catch (_) {
        return null;
      }
    }
    if (!obj || typeof obj !== "object") return null;
    const raw = obj as { loaded?: unknown; skipped?: unknown };
    const loaded = Array.isArray(raw.loaded) ? raw.loaded as ReadMediaEntry[] : [];
    const skipped = Array.isArray(raw.skipped) ? raw.skipped as ReadMediaEntry[] : [];
    if (!loaded.length && !skipped.length) return null;
    return { loaded: loaded, skipped: skipped };
  }

  function displayNameForReference(ref: unknown): string {
    if (typeof ref !== "string" || !ref) return "未知来源";
    if (ref.indexOf("media://") === 0) return ref.slice("media://".length) || ref;
    const path = ref.split(/[?#]/)[0];
    const parts = path.split(/[/\\]/);
    return parts.pop() || ref;
  }

  function resolveReferenceUrl(API: ReferenceUrlApi, ref: unknown, sessionId: unknown): string | null {
    if (typeof ref !== "string" || !ref) return null;
    if (ref.indexOf("media://") === 0) {
      return API.sessionMediaUrl(sessionId, ref.slice("media://".length));
    }
    if (/^https?:\/\//i.test(ref) || /^data:/i.test(ref)) return ref;
    if (API && typeof API.localFileUrl === "function") return API.localFileUrl(sessionId, ref);
    return null;
  }

  function previewTypeForKind(kind: unknown, mime: unknown): string {
    if (kind === "video") return "video";
    if (kind === "audio") return "audio";
    if (kind === "image") return "image";
    if (typeof mime === "string") {
      if (mime.indexOf("video/") === 0) return "video";
      if (mime.indexOf("audio/") === 0) return "audio";
      if (mime.indexOf("image/") === 0) return "image";
    }
    return "image";
  }

  function formatDuration(seconds: unknown): string {
    if (seconds == null || seconds === "") return "";
    const total = Math.floor(Number(seconds));
    if (!Number.isFinite(total) || total < 0) return "";
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return minutes + ":" + (rest < 10 ? "0" + rest : rest);
  }

  function effectiveKind(entry: ReadMediaEntry): string {
    // 视频关键帧拼图（converted.from_video）：注入的是图片，但来源是视频，
    // 展示口径按视频算（视频磁贴+时长徽标），避免 <img> 去加载 mp4 原片挂错。
    if (entry && entry.converted && entry.converted.from_video) return "video";
    return (entry && (entry.kind as string)) || "image";
  }

  function headerTextForCounts(loaded: ReadMediaEntry[], skipped: ReadMediaEntry[]): string {
    const kinds = loaded.map(function (item) { return effectiveKind(item); });
    const count = function (kind: string): number { return kinds.filter(function (k) { return k === kind; }).length; };
    const parts: string[] = [];
    if (count("image")) parts.push(count("image") + " 张图像");
    if (count("video")) parts.push(count("video") + " 个视频");
    if (count("audio")) parts.push(count("audio") + " 个音频");
    const others = loaded.length - count("image") - count("video") - count("audio");
    if (others > 0) parts.push(others + " 项媒体");
    let text = parts.length ? "已查看 " + parts.join("、") : "媒体未读取";
    if (skipped.length) text += "（" + skipped.length + " 项未读取）";
    return text;
  }

  function videoDurationOf(entry: ReadMediaEntry): string {
    const video = entry && entry.video;
    const seconds = video && (video.duration ?? (video.read && video.read.seconds));
    return formatDuration(seconds);
  }

  // parsed: parseReadMediaResult 的返回值 → { headerText, items }。
  // item: { tile: image|video|audio|missing|skipped, name, url, previewType, reason, duration }
  function buildStripItems(API: ReferenceUrlApi, parsed: ParsedReadMedia, sessionId: unknown): { headerText: string; items: StripItem[] } {
    const items: StripItem[] = [];
    for (const entry of parsed.loaded) {
      const ref = entry && entry.reference;
      const kind = effectiveKind(entry);
      const url = resolveReferenceUrl(API, ref, sessionId);
      const name = displayNameForReference(ref);
      const framesNote = entry && entry.converted && entry.converted.from_video
        ? "已转为关键帧拼图发送给模型" : "";
      if (!url) {
        items.push({ tile: "missing", name: name, url: null, previewType: null, reason: "无法解析该引用", duration: "" });
        continue;
      }
      const tile = kind === "video" ? "video" : kind === "audio" ? "audio" : "image";
      items.push({ tile: tile, name: name, url: url,
        previewType: previewTypeForKind(kind, entry && entry.mime),
        reason: framesNote, duration: tile === "video" ? videoDurationOf(entry) : "" });
    }
    for (const entry of parsed.skipped) {
      const reason = (entry && (entry.reason || entry.message)) || "未读取";
      items.push({ tile: "skipped", name: displayNameForReference(entry && entry.reference),
        url: null, previewType: null, reason: String(reason), duration: "" });
    }
    return { headerText: headerTextForCounts(parsed.loaded, parsed.skipped), items: items };
  }

  function markTileMissing(deps: StripDeps, tile: HTMLElement, name: string): void {
    tile.classList.remove("is-image", "is-video", "is-audio");
    tile.classList.add("is-missing");
    tile.textContent = "";
    tile.appendChild(deps.el("div", "read-media-missing-icon", "🖼️"));
    tile.appendChild(deps.el("div", "read-media-missing-text", "找不到图片，可能已删除"));
    tile.appendChild(deps.el("div", "read-media-name", name));
  }

  function buildTile(deps: StripDeps, item: StripItem, deferred: DeferredMedia[]): HTMLElement {
    const tile = deps.el("div", "read-media-tile is-" + item.tile);
    tile.title = item.reason ? item.name + "\n" + item.reason : item.name;
    if (item.tile === "image" || item.tile === "video") {
      const media = deps.el(item.tile === "video" ? "video" : "img", "read-media-thumb") as HTMLVideoElement & HTMLImageElement;
      if (item.tile === "video") {
        media.muted = true;
        media.preload = "metadata";
        media.playsInline = true;
      } else {
        media.alt = item.name;
        media.loading = "lazy";
      }
      media.addEventListener("error", function () { markTileMissing(deps, tile, item.name); });
      deferred.push({ node: media, url: item.url as string });
      tile.appendChild(media);
      if (item.tile === "video" && item.duration) {
        tile.appendChild(deps.el("div", "read-media-badge", item.duration));
      }
    } else if (item.tile === "audio") {
      tile.appendChild(deps.el("div", "read-media-audio-icon", "🎵"));
    } else if (item.tile === "skipped") {
      tile.appendChild(deps.el("div", "read-media-missing-icon", "⚠️"));
      tile.appendChild(deps.el("div", "read-media-missing-text", item.reason || "未读取"));
    } else {
      tile.appendChild(deps.el("div", "read-media-missing-icon", "🖼️"));
      tile.appendChild(deps.el("div", "read-media-missing-text", item.reason || "找不到图片，可能已删除"));
    }
    if (item.tile === "image" || item.tile === "video" || item.tile === "audio") {
      tile.classList.add("is-clickable");
      tile.addEventListener("click", function () { deps.onPreview(item); });
    }
    tile.appendChild(deps.el("div", "read-media-name", item.name));
    return tile;
  }

  // deps: { el, onPreview(item) }。返回 strip 根节点；默认折叠，首次展开
  // 时才给媒体节点设置 src（避免折叠态预加载外部资源）。
  function buildMediaStrip(deps: StripDeps, headerText: string, items: StripItem[]): HTMLElement {
    const strip = deps.el("div", "read-media-strip");
    const head = deps.el("button", "read-media-head") as HTMLButtonElement;
    head.type = "button";
    head.appendChild(deps.el("span", "read-media-label", headerText));
    head.appendChild(deps.el("span", "read-media-chevron", "∨"));
    const grid = deps.el("div", "read-media-grid");
    grid.hidden = true;
    const deferred: DeferredMedia[] = [];
    for (const item of items) grid.appendChild(buildTile(deps, item, deferred));
    let hydrated = false;
    head.addEventListener("click", function () {
      grid.hidden = !grid.hidden;
      strip.classList.toggle("open", !grid.hidden);
      if (!grid.hidden && !hydrated) {
        hydrated = true;
        for (const pending of deferred) pending.node.src = pending.url;
      }
    });
    strip.appendChild(head);
    strip.appendChild(grid);
    return strip;
  }

  const exported: ReadMediaStripApi = {
    parseReadMediaResult: parseReadMediaResult,
    displayNameForReference: displayNameForReference,
    resolveReferenceUrl: resolveReferenceUrl,
    previewTypeForKind: previewTypeForKind,
    formatDuration: formatDuration,
    headerTextForCounts: headerTextForCounts,
    buildStripItems: buildStripItems,
    buildMediaStrip: buildMediaStrip,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else (global as unknown as { ReadMediaStrip: ReadMediaStripApi }).ReadMediaStrip = exported;
})(typeof window !== "undefined" ? window : globalThis);
