/** 输入框开头的 / 快捷命令：光标位置判断与文本替换。 */
(function (global) {
  "use strict";

  function readTrigger(value, selectionStart, selectionEnd) {
    if (typeof value !== "string" || value.charAt(0) !== "/") return null;
    if (!Number.isInteger(selectionStart) || selectionStart < 1 || selectionStart > value.length ||
        selectionStart !== selectionEnd) return null;
    const query = value.slice(1, selectionStart);
    if (/\s/.test(query)) return null;
    return { query: query, end: selectionStart };
  }

  function removeTrigger(value, trigger) {
    if (!trigger || typeof value !== "string" || value.charAt(0) !== "/") return value;
    return value.slice(trigger.end).replace(/^[ \t]/, "");
  }

  function filterCommands(commands, query) {
    const needle = String(query || "").toLocaleLowerCase().trim();
    if (!needle) return commands.slice();
    return commands.filter(function (command) {
      const haystack = [command.id, command.title, command.description]
        .concat(command.aliases || []).join(" ").toLocaleLowerCase();
      return haystack.includes(needle);
    });
  }

  const api = { readTrigger: readTrigger, removeTrigger: removeTrigger, filterCommands: filterCommands };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else global.SlashCommandUtils = api;
})(typeof self !== "undefined" ? self : globalThis);
