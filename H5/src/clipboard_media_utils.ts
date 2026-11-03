/** 剪贴板媒体条目形态（DataTransfer 的结构化子集，兼容测试替身）。 */
interface ClipboardItemLike {
  kind?: unknown;
  type?: unknown;
  // 注意：必须是方法调用 item.getAsFile()，不能摘出来裸调——
  // 浏览器原生 DataTransferItem.getAsFile 依赖 this，裸调会抛 Illegal invocation。
  getAsFile?: () => File | null;
}

interface ClipboardDataLike {
  items?: ArrayLike<ClipboardItemLike> | null;
  files?: ArrayLike<File> | null;
}

interface ClipboardFileEntry {
  file: File;
  type?: unknown;
}

/** File 构造器形态（与 DOM File 构造签名一致，测试可注入替身）。 */
type FileConstructorLike = new (bits: BlobPart[], name: string, options?: FilePropertyBag) => File;

interface ClipboardMediaApi {
  mediaFiles: (data: unknown, kindOf: unknown, FileType?: FileConstructorLike | null) => File[];
}

/** Clipboard files shared by the composer and historical message editor. */
(function (root: typeof globalThis, factory: () => ClipboardMediaApi) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else (root as unknown as { ClipboardMediaUtils: ClipboardMediaApi }).ClipboardMediaUtils = api;
})(typeof self !== "undefined" ? self : globalThis, function (): ClipboardMediaApi {
  "use strict";
  const imageExtensions: Record<string, string | undefined> = { "image/png": "png", "image/x-png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp", "image/x-ms-bmp": "bmp",
    "image/tiff": "tiff", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico" };

  function mediaFiles(data: unknown, kindOf: unknown, FileType?: FileConstructorLike | null): File[] {
    if (!data || typeof kindOf !== "function") return [];
    const input = data as ClipboardDataLike;
    const kindFn = kindOf as (fileName: string) => unknown;
    const candidates: ClipboardFileEntry[] = [];
    for (const item of Array.from(input.items || [])) {
      if (item.kind !== "file" || typeof item.getAsFile !== "function") continue;
      const file = item.getAsFile();
      if (file) candidates.push({ file, type: item.type });
    }
    // Some browsers expose only files; don't add both representations twice.
    if (!candidates.length) Array.from(input.files || []).forEach(file => candidates.push({ file }));
    const result: File[] = [], stamp = Date.now() + "-" + Math.random().toString(36).slice(2, 8);
    for (const entry of candidates) {
      let file = entry.file;
      const mime = String(file.type || entry.type || "").toLowerCase();
      const ext = imageExtensions[mime], kind = kindFn(file.name || "");
      if (ext && (kind !== "image" || !file.name || file.name.toLowerCase() === "image.png")) {
        const Constructor = FileType || (typeof File !== "undefined" ? File : null);
        if (Constructor) {
          try { file = new Constructor([file], "clipboard-" + stamp + "-" + result.length + "." + ext, { type: mime }); }
          catch (_) { /* Preserve already usable named files when File is unavailable. */ }
        }
      }
      if (kindFn(file.name || "")) result.push(file);
    }
    return result;
  }
  return { mediaFiles };
});
