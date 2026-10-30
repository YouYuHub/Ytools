/* 小历史直接解析；大历史用 Worker，避免 JSONL 解析长时间阻塞交互。 */
(function (global) {
  "use strict";
  const WORKER_THRESHOLD = 1024 * 1024;

  function parse(text, options) {
    if (typeof global.Worker !== "function" || text.length < WORKER_THRESHOLD) {
      return Promise.resolve(global.HistoryParser.parseHistory(text, options));
    }
    return new Promise(function (resolve, reject) {
      let worker;
      try {
        worker = new global.Worker("js/history_parse_worker.js?v=ea9b2dcf6fb6");
      } catch (_) {
        resolve(global.HistoryParser.parseHistory(text, options));
        return;
      }
      worker.onmessage = function (event) {
        worker.terminate();
        if (event.data && event.data.ok) resolve(event.data.result);
        else reject(new Error(event.data && event.data.error || "历史解析失败"));
      };
      worker.onerror = function (event) {
        worker.terminate();
        try { resolve(global.HistoryParser.parseHistory(text, options)); }
        catch (error) { reject(error); }
      };
      worker.postMessage(options ? {text:text,options:options} : text);
    });
  }

  global.HistoryParseAsync = { parse: parse, WORKER_THRESHOLD: WORKER_THRESHOLD };
})(typeof self !== "undefined" ? self : globalThis);
