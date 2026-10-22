/** Clipboard files shared by the composer and historical message editor. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ClipboardMediaUtils = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const imageExtensions = { "image/png": "png", "image/x-png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp", "image/x-ms-bmp": "bmp",
    "image/tiff": "tiff", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico" };

  function mediaFiles(data, kindOf, FileType) {
    if (!data || typeof kindOf !== "function") return [];
    const candidates = [];
    for (const item of Array.from(data.items || [])) {
      if (item.kind !== "file" || typeof item.getAsFile !== "function") continue;
      const file = item.getAsFile(); if (file) candidates.push({ file, type: item.type });
    }
    // Some browsers expose only files; don't add both representations twice.
    if (!candidates.length) Array.from(data.files || []).forEach(file => candidates.push({ file }));
    const result = [], stamp = Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    for (const entry of candidates) {
      let file = entry.file;
      const mime = String(file.type || entry.type || "").toLowerCase();
      const ext = imageExtensions[mime], kind = kindOf(file.name || "");
      if (ext && (kind !== "image" || !file.name || file.name.toLowerCase() === "image.png")) {
        const Constructor = FileType || (typeof File !== "undefined" ? File : null);
        if (Constructor) {
          try { file = new Constructor([file], "clipboard-" + stamp + "-" + result.length + "." + ext, { type: mime }); }
          catch (_) { /* Preserve already usable named files when File is unavailable. */ }
        }
      }
      if (kindOf(file.name || "")) result.push(file);
    }
    return result;
  }
  return { mediaFiles };
});
