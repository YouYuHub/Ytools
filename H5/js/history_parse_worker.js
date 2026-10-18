/* 大历史解析移出 UI 线程。与主页面共用同一份解析逻辑。 */
importScripts("history_parser.js");

self.onmessage = function (event) {
  try {
    self.postMessage({ ok: true, result: self.HistoryParser.parseHistory(event.data) });
  } catch (error) {
    self.postMessage({ ok: false, error: String(error && error.message || error) });
  }
};
