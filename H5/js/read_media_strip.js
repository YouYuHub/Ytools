(function (global) {
  "use strict";
  // read_media 工具结果的图片条：解析结果 JSON → 可展开的缩略图网格。
  // - loaded 项按引用解析出可访问 URL（media:// 走会话媒体端点，http(s)/
  //   data 原样，本地路径走 /file/get_local_file），图片/视频/音频分别建块；
  // - 默认折叠，首次展开时才设置媒体 src（避免外部 URL 自动请求与预加载开销）；
  // - 加载失败（文件已删除）就地替换为“找不到图片”占位；skipped 项展示原因。
  // 纯函数 + 注入式 DOM 构建（deps.el），可直接单测。
  function parseReadMediaResult(resultText) {
    let obj = resultText;
    if (typeof obj === "string") {
      try {
        obj = JSON.parse(obj);
      } catch (_) {
        return null;
      }
    }
    if (!obj || typeof obj !== "object") return null;
    const loaded = Array.isArray(obj.loaded) ? obj.loaded : [];
    const skipped = Array.isArray(obj.skipped) ? obj.skipped : [];
    if (!loaded.length && !skipped.length) return null;
    return { loaded: loaded, skipped: skipped };
  }

  function displayNameForReference(ref) {
    if (typeof ref !== "string" || !ref) return "未知来源";
    if (ref.indexOf("media://") === 0) return ref.slice("media://".length) || ref;
    const path = ref.split(/[?#]/)[0];
    const parts = path.split(/[/\\]/);
    return parts.pop() || ref;
  }

  function resolveReferenceUrl(API, ref, sessionId) {
    if (typeof ref !== "string" || !ref) return null;
    if (ref.indexOf("media://") === 0) {
      return API.sessionMediaUrl(sessionId, ref.slice("media://".length));
    }
    if (/^https?:\/\//i.test(ref) || /^data:/i.test(ref)) return ref;
    if (API && typeof API.localFileUrl === "function") return API.localFileUrl(sessionId, ref);
    return null;
  }

  function previewTypeForKind(kind, mime) {
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

  function formatDuration(seconds) {
    if (seconds == null || seconds === "") return "";
    const total = Math.floor(Number(seconds));
    if (!Number.isFinite(total) || total < 0) return "";
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return minutes + ":" + (rest < 10 ? "0" + rest : rest);
  }

  function effectiveKind(entry) {
    // 视频关键帧拼图（converted.from_video）：注入的是图片，但来源是视频，
    // 展示口径按视频算（视频磁贴+时长徽标），避免 <img> 去加载 mp4 原片挂错。
    if (entry && entry.converted && entry.converted.from_video) return "video";
    return (entry && entry.kind) || "image";
  }

  function headerTextForCounts(loaded, skipped) {
    const kinds = loaded.map(function (item) { return effectiveKind(item); });
    const count = function (kind) { return kinds.filter(function (k) { return k === kind; }).length; };
    const parts = [];
    if (count("image")) parts.push(count("image") + " 张图像");
    if (count("video")) parts.push(count("video") + " 个视频");
    if (count("audio")) parts.push(count("audio") + " 个音频");
    const others = loaded.length - count("image") - count("video") - count("audio");
    if (others > 0) parts.push(others + " 项媒体");
    let text = parts.length ? "已查看 " + parts.join("、") : "媒体未读取";
    if (skipped.length) text += "（" + skipped.length + " 项未读取）";
    return text;
  }

  function videoDurationOf(entry) {
    const video = entry && entry.video;
    const seconds = video && (video.duration ?? (video.read && video.read.seconds));
    return formatDuration(seconds);
  }

  // parsed: parseReadMediaResult 的返回值 → { headerText, items }。
  // item: { tile: image|video|audio|missing|skipped, name, url, previewType, reason, duration }
  function buildStripItems(API, parsed, sessionId) {
    const items = [];
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

  function markTileMissing(deps, tile, name) {
    tile.classList.remove("is-image", "is-video", "is-audio");
    tile.classList.add("is-missing");
    tile.textContent = "";
    tile.appendChild(deps.el("div", "read-media-missing-icon", "🖼️"));
    tile.appendChild(deps.el("div", "read-media-missing-text", "找不到图片，可能已删除"));
    tile.appendChild(deps.el("div", "read-media-name", name));
  }

  function buildTile(deps, item, deferred) {
    const tile = deps.el("div", "read-media-tile is-" + item.tile);
    tile.title = item.reason ? item.name + "\n" + item.reason : item.name;
    if (item.tile === "image" || item.tile === "video") {
      const media = item.tile === "video" ? deps.el("video", "read-media-thumb") : deps.el("img", "read-media-thumb");
      if (item.tile === "video") {
        media.muted = true;
        media.preload = "metadata";
        media.playsInline = true;
      } else {
        media.alt = item.name;
        media.loading = "lazy";
      }
      media.addEventListener("error", function () { markTileMissing(deps, tile, item.name); });
      deferred.push({ node: media, url: item.url });
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
  function buildMediaStrip(deps, headerText, items) {
    const strip = deps.el("div", "read-media-strip");
    const head = deps.el("button", "read-media-head");
    head.type = "button";
    head.appendChild(deps.el("span", "read-media-label", headerText));
    head.appendChild(deps.el("span", "read-media-chevron", "∨"));
    const grid = deps.el("div", "read-media-grid");
    grid.hidden = true;
    const deferred = [];
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

  const exported = {
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
  else global.ReadMediaStrip = exported;
})(typeof window !== "undefined" ? window : globalThis);
