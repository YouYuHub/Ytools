/**
 * 媒体与文档
 * - 媒体/文档预览模态框（图片/视频/音频/PDF/文本/下载兜底，media:// 解析）
 * - 待发送媒体附件（粘贴/选择）：数量与大小限制、缩略图预览、统一附件区渲染
 * 依赖：app/core.js、API；App.*：composer 模块（发送按钮状态）
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, $,
    composerAttachments, composer, input, MAX_PENDING_MEDIA,
    MEDIA_SIZE_LIMITS, MEDIA_EXTENSIONS, mediaPreviewModal, mediaPreviewBody,
    mediaPreviewTitle
  } = App;

  // ---------- 媒体/文档预览模态框 ----------
  const PREVIEW_IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"];
  const PREVIEW_VIDEO_EXTS = ["mp4", "webm", "mov"];
  const PREVIEW_AUDIO_EXTS = ["wav", "mp3", "m4a", "ogg", "flac"];
  const PREVIEW_TEXT_EXTS = ["txt", "md", "json", "log", "csv"];

  function previewKindForName(name) {
    const ext = (String(name || "").split(".").pop() || "").toLowerCase();
    if (PREVIEW_IMAGE_EXTS.indexOf(ext) >= 0) return "image";
    if (PREVIEW_VIDEO_EXTS.indexOf(ext) >= 0) return "video";
    if (PREVIEW_AUDIO_EXTS.indexOf(ext) >= 0) return "audio";
    if (ext === "pdf") return "pdf";
    if (PREVIEW_TEXT_EXTS.indexOf(ext) >= 0) return "text";
    return "download";
  }

  function closeMediaPreview() {
    // 销毁内部媒体元素：暂停并移除 src，关闭预览即停止播放/收听
    mediaPreviewBody.querySelectorAll("video, audio").forEach(function (media) {
      try { media.pause(); } catch (_) { /* ignore */ }
      media.removeAttribute("src");
      try { media.load(); } catch (_) { /* ignore */ }
    });
    mediaPreviewBody.innerHTML = "";
    mediaPreviewTitle.textContent = "";
    mediaPreviewModal.classList.add("hidden");
    mediaPreviewModal.setAttribute("aria-hidden", "true");
  }

  function openMediaPreview(options) {
    const type = options.type || "download";
    mediaPreviewTitle.textContent = options.title || "";
    mediaPreviewBody.innerHTML = "";
    if (type === "image") {
      const img = el("img", "media-preview-image");
      img.src = options.src;
      img.alt = options.title || "图片预览";
      mediaPreviewBody.appendChild(img);
    } else if (type === "video") {
      const video = el("video", "media-preview-video");
      video.src = options.src;
      video.controls = true;
      video.autoplay = true;
      video.playsInline = true;
      mediaPreviewBody.appendChild(video);
    } else if (type === "audio") {
      const wrap = el("div", "media-preview-audio");
      wrap.appendChild(el("div", "media-preview-audio-icon", "🎵"));
      wrap.appendChild(el("div", "media-preview-audio-name", options.title || "音频"));
      const audio = el("audio");
      audio.src = options.src;
      audio.controls = true;
      audio.autoplay = true;
      wrap.appendChild(audio);
      mediaPreviewBody.appendChild(wrap);
    } else if (type === "pdf") {
      // 浏览器原生 PDF 查看器
      const frame = el("iframe", "media-preview-frame");
      frame.src = options.src;
      mediaPreviewBody.appendChild(frame);
    } else if (type === "text") {
      const pre = el("pre", "media-preview-text", "加载中…");
      mediaPreviewBody.appendChild(pre);
      fetch(options.src).then(function (res) {
        if (!res.ok) throw new Error(res.status + " " + res.statusText);
        return res.text();
      }).then(function (text) {
        pre.textContent = text || "（空文件）";
      }).catch(function (err) {
        pre.textContent = "读取失败：" + err.message;
      });
    } else {
      const wrap = el("div", "media-preview-download");
      wrap.appendChild(el("div", "media-preview-download-tip", "该文件类型不支持在线预览，可下载后查看"));
      const btn = el("a", "media-preview-download-btn", "下载文件");
      btn.href = options.src;
      btn.download = options.title || "file";
      wrap.appendChild(btn);
      mediaPreviewBody.appendChild(wrap);
    }
    mediaPreviewModal.classList.remove("hidden");
    mediaPreviewModal.setAttribute("aria-hidden", "false");
  }

  // media:// 引用 / data: / http(s) → 可访问的资源 URL
  function resolveMediaSrc(mediaRef, sessionId) {
    if (typeof mediaRef !== "string" || !mediaRef) return null;
    if (mediaRef.indexOf("media://") === 0) {
      return API.sessionMediaUrl(sessionId || state.sessionId, mediaRef.slice("media://".length));
    }
    if (/^(data:|https?:)/.test(mediaRef)) return mediaRef;
    return null;
  }

  // 媒体伪标签 src → 可访问 URL（markdown.js 以 (kind, src) 调用）：
  // media:// → 会话媒体端点；http(s)/data 原样；其它按本地路径交给
  // /file/get_local_file（后端按会话生效工作目录解析相对路径）
  function resolveMediaTagSrc(kind, src) {
    void kind;
    if (typeof src !== "string" || !src) return "";
    if (src.indexOf("media://") === 0) {
      return API.sessionMediaUrl(state.sessionId, src.slice("media://".length));
    }
    if (/^(data:|https?:)/i.test(src)) return src;
    return API.localFileUrl(state.sessionId, src);
  }

  function openMediaPreviewForDocument(doc) {
    if (!doc) return;
    if (!doc.stored_name) {
      toast("该文件上传于旧版本（未保存原始文件），无法预览");
      return;
    }
    const url = API.sessionDocumentUrl(state.sessionId, doc.stored_name);
    const kind = previewKindForName(doc.stored_name);
    openMediaPreview({ type: kind, src: url, title: doc.filename });
  }

  $("#mediaPreviewClose").addEventListener("click", closeMediaPreview);
  $("#mediaPreviewBackdrop").addEventListener("click", closeMediaPreview);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !mediaPreviewModal.classList.contains("hidden")) closeMediaPreview();
  });

  // ---------- 待发送媒体附件（粘贴/选择） ----------
  function mediaKindOf(filename) {
    const ext = (filename.split(".").pop() || "").toLowerCase();
    for (const kind in MEDIA_EXTENSIONS) {
      if (MEDIA_EXTENSIONS[kind].indexOf(ext) >= 0) return kind;
    }
    return null;
  }

  // ---------- 发送前图片压缩 ----------
  // 与后端 IMAGE_THUMBNAIL_* 一致：>2MB 的图片在发送前用 canvas 重采样为
  // 长边 ≤1568px、JPEG 质量 0.85，通常可缩到原图的 1/5~1/10；GIF 跳过
  // （canvas 只取第一帧），压缩结果反而更大时保留原文件。
  const COMPRESS_THRESHOLD_BYTES = 2 * 1024 * 1024;
  const COMPRESS_MAX_EDGE = 1568;
  const COMPRESS_JPEG_QUALITY = 0.85;

  function compressImageFile(file) {
    if (!file || file.size <= COMPRESS_THRESHOLD_BYTES) return Promise.resolve(file);
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if (ext === "gif") return Promise.resolve(file);
    if (typeof createImageBitmap === "undefined" || typeof document === "undefined") {
      return Promise.resolve(file);
    }
    return createImageBitmap(file).then(function (bitmap) {
      try {
        const scale = Math.min(1, COMPRESS_MAX_EDGE / Math.max(bitmap.width, bitmap.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(bitmap.width * scale));
        canvas.height = Math.max(1, Math.round(bitmap.height * scale));
        const ctx = canvas.getContext("2d");
        ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
        bitmap.close();
        return new Promise(function (resolve) {
          canvas.toBlob(function (blob) {
            if (!blob || blob.size >= file.size) {
              resolve(file);  // 压缩无收益：保留原文件
              return;
            }
            const baseName = file.name.replace(/\.[^.]+$/, "");
            resolve(new File([blob], baseName + "_compressed.jpg", {
              type: "image/jpeg", lastModified: Date.now(),
            }));
          }, "image/jpeg", COMPRESS_JPEG_QUALITY);
        });
      } catch (_) {
        try { bitmap.close(); } catch (_e) { /* ignore */ }
        return file;
      }
    }).catch(function () { return file; });
  }

  async function addPendingMediaFiles(files) {
    const added = [];
    for (const file of files || []) {
      if (!file) continue;
      if (state.pendingMedia.length >= MAX_PENDING_MEDIA) {
        toast("最多附加 " + MAX_PENDING_MEDIA + " 个媒体文件");
        break;
      }
      const kind = mediaKindOf(file.name || "");
      if (!kind) {
        toast("不支持的媒体类型：" + file.name);
        continue;
      }
      let finalFile = file;
      if (kind === "image") {
        finalFile = await compressImageFile(file);
      }
      const sizeLimit = MEDIA_SIZE_LIMITS[kind] || MEDIA_SIZE_LIMITS.image;
      if (finalFile.size > sizeLimit) {
        toast("文件超过 " + Math.round(sizeLimit / (1024 * 1024)) + "MB 限制：" + file.name);
        continue;
      }
      state.pendingMedia.push({
        id: Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8),
        file: finalFile,
        name: finalFile.name,
        kind: kind,
        dataUrl: "",
      });
      added.push(state.pendingMedia[state.pendingMedia.length - 1]);
    }
    if (added.length) {
      // 图片读成 dataURL 供预览；音频/视频只显示名称徽标
      added.forEach(function (item) {
        if (item.kind !== "image") return;
        const reader = new FileReader();
        reader.onload = function () {
          item.dataUrl = String(reader.result || "");
          renderComposerAttachments();
        };
        reader.readAsDataURL(item.file);
      });
    }
    renderComposerAttachments();
    return added.length;
  }

  // 统一附件区：待发送媒体（随消息上传，media:// 进内容部件）+ 会话已解析文档
  // （file_memory，后端注入系统提示词、跨消息生效）
  function renderComposerAttachments() {
    if (!state.pendingMedia.length && !state.sessionDocs.length) {
      composerAttachments.innerHTML = "";
      composerAttachments.classList.add("hidden");
      composer.classList.remove("has-media");
      App.refreshComposerButtons();
      return;
    }
    composerAttachments.classList.remove("hidden");
    // 附件行占满输入行上方（.has-media 走 flex 换行），输入框区域随之变高
    composer.classList.add("has-media");
    composerAttachments.innerHTML = "";
    state.pendingMedia.forEach(function (item) {
      const chip = el("div", "media-chip");
      if (item.kind === "image" && item.dataUrl) {
        const thumb = el("img", "media-thumb", "");
        thumb.src = item.dataUrl;
        thumb.alt = item.name;
        thumb.title = "点击预览";
        thumb.classList.add("clickable");
        thumb.addEventListener("click", function () {
          openMediaPreview({ type: "image", src: item.dataUrl, title: item.name });
        });
        chip.appendChild(thumb);
      } else {
        // 音频/视频无预览帧：显示类别占位，点击在预览框中播放/收听
        const placeholder = el("div", "media-thumb media-thumb-placeholder",
          item.kind === "audio" ? "🎵 音频" : "🎬 视频");
        placeholder.title = "点击预览";
        placeholder.classList.add("clickable");
        placeholder.addEventListener("click", function () {
          if (!item.objUrl) item.objUrl = URL.createObjectURL(item.file);
          openMediaPreview({
            type: item.kind === "audio" ? "audio" : "video",
            src: item.objUrl,
            title: item.name,
          });
        });
        chip.appendChild(placeholder);
      }
      const removeBtn = el("button", "media-remove", "✕");
      removeBtn.type = "button";
      removeBtn.title = "移除";
      removeBtn.addEventListener("click", function () {
        state.pendingMedia = state.pendingMedia.filter(function (m) { return m.id !== item.id; });
        renderComposerAttachments();
      });
      chip.appendChild(removeBtn);
      composerAttachments.appendChild(chip);
    });
    state.sessionDocs.forEach(function (doc) {
      const chip = el("div", "doc-chip");
      chip.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/></svg>';
      chip.appendChild(el("span", "doc-chip-name", doc.filename || "未命名"));
      chip.title = "已解析文档：内容将注入对话上下文；点击预览";
      chip.classList.add("clickable");
      chip.addEventListener("click", function () {
        openMediaPreviewForDocument(doc);
      });
      const del = el("button", "media-remove", "✕");
      del.type = "button";
      del.title = "移除文件";
      del.addEventListener("click", async function (e) {
        e.stopPropagation();
        try {
          await API.deleteSessionFile(doc.filename, state.sessionId);
          state.sessionDocs = state.sessionDocs.filter(function (d) { return d.filename !== doc.filename; });
          renderComposerAttachments();
        } catch (err) {
          toast("移除失败：" + err.message);
        }
      });
      chip.appendChild(del);
      composerAttachments.appendChild(chip);
    });
    App.refreshComposerButtons();
  }

  function clearPendingMedia() {
    // 释放待发媒体的对象 URL
    state.pendingMedia.forEach(function (item) {
      if (item.objUrl) {
        try { URL.revokeObjectURL(item.objUrl); } catch (_) { /* ignore */ }
      }
    });
    state.pendingMedia = [];
    renderComposerAttachments();
  }

  // 粘贴剪贴板图片：加入待发送附件并在输入框上方预览
  input.addEventListener("paste", function (e) {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const mediaFiles = [];
    for (const item of items) {
      if (item.kind !== "file") continue;
      const file = item.getAsFile && item.getAsFile();
      if (file && mediaKindOf(file.name || "")) mediaFiles.push(file);
    }
    if (!mediaFiles.length) return;
    e.preventDefault();
    // 剪贴板截图常无扩展名：按 MIME 推断补一个
    const named = mediaFiles.map(function (file, index) {
      if (file.name && file.name !== "image.png" && file.name.indexOf(".") >= 0) return file;
      const mime = file.type || "image/png";
      const ext = (mime.split("/")[1] || "png").replace("jpeg", "jpg");
      const name = "clipboard-" + Date.now() + (mediaFiles.length > 1 ? "-" + index : "") + "." + ext;
      try {
        return new File([file], name, { type: mime });
      } catch (_) {
        return file;
      }
    });
    addPendingMediaFiles(named);
  });


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.mediaKindOf = mediaKindOf;
  App.addPendingMediaFiles = addPendingMediaFiles;
  App.renderComposerAttachments = renderComposerAttachments;
  App.clearPendingMedia = clearPendingMedia;
  App.openMediaPreview = openMediaPreview;
  App.resolveMediaSrc = resolveMediaSrc;
  App.resolveMediaTagSrc = resolveMediaTagSrc;
  App.openMediaPreviewForDocument = openMediaPreviewForDocument;

  // 注册 markdown 伪标签 src 解析器（markdown.js 先于本模块加载）
  if (globalThis.Markdown && typeof Markdown.setMediaResolver === "function") {
    Markdown.setMediaResolver(resolveMediaTagSrc);
  }
})(window.App);
