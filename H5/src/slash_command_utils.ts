interface SlashTrigger {
  query: string;
  end: number;
}

interface SlashCommand {
  id?: unknown;
  title?: unknown;
  description?: unknown;
  aliases?: unknown;
}

interface SlashCommandUtilsApi {
  readTrigger: (value: unknown, selectionStart: unknown, selectionEnd: unknown) => SlashTrigger | null;
  removeTrigger: (value: unknown, trigger: { end: number } | null | undefined) => unknown;
  filterCommands: (commands: SlashCommand[], query: unknown) => SlashCommand[];
}

/** 输入框开头的 / 快捷命令：光标位置判断与文本替换。 */
(function (global: typeof globalThis) {
  "use strict";

  function readTrigger(value: unknown, selectionStart: unknown, selectionEnd: unknown): SlashTrigger | null {
    if (typeof value !== "string" || value.charAt(0) !== "/") return null;
    if (typeof selectionStart !== "number" || !Number.isInteger(selectionStart) ||
        selectionStart < 1 || selectionStart > value.length ||
        selectionStart !== selectionEnd) return null;
    const query = value.slice(1, selectionStart);
    if (/\s/.test(query)) return null;
    return { query: query, end: selectionStart };
  }

  function removeTrigger(value: unknown, trigger: { end: number } | null | undefined): unknown {
    if (!trigger || typeof value !== "string" || value.charAt(0) !== "/") return value;
    return value.slice(trigger.end).replace(/^[ \t]/, "");
  }

  function filterCommands(commands: SlashCommand[], query: unknown): SlashCommand[] {
    const needle = String(query || "").toLocaleLowerCase().trim();
    if (!needle) return commands.slice();
    return commands.filter(function (command) {
      const haystack = [command.id, command.title, command.description]
        .concat(command.aliases || []).join(" ").toLocaleLowerCase();
      return haystack.includes(needle);
    });
  }

  const api: SlashCommandUtilsApi = { readTrigger: readTrigger, removeTrigger: removeTrigger, filterCommands: filterCommands };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else (global as unknown as { SlashCommandUtils: SlashCommandUtilsApi }).SlashCommandUtils = api;
})(typeof self !== "undefined" ? self : globalThis);
