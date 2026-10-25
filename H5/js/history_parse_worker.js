/* 大历史解析移出 UI 线程。与主页面共用同一份解析逻辑。 */
importScripts("history_parser.js?v=a1f33616c31b");

self.onmessage = function (event) {
  try {
    const data = event.data;
    self.postMessage({ ok: true, result: self.HistoryParser.parseHistory(typeof data === "string" ? data : data.text, typeof data === "string" ? undefined : data.options) });
  } catch (error) {
    self.postMessage({ ok: false, error: String(error && error.message || error) });
  }
};
