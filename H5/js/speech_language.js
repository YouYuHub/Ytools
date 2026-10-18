/** Browser speech language choices and language-specific text boundaries. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.SpeechLanguage = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  const DEFAULT_LANGUAGE = "zh-CN";
  const STORAGE_KEY = "ytools-speech-language";
  const languages = [
    { code: "zh-CN", label: "普通话（中国大陆）", badge: "中" },
    { code: "zh-TW", label: "中文（台湾）", badge: "繁" },
    { code: "en-US", label: "英语 · English（美国）", badge: "EN" },
    { code: "en-GB", label: "英语 · English（英国）", badge: "EN" },
    { code: "fr-FR", label: "法语 · Français", badge: "FR" },
    { code: "de-DE", label: "德语 · Deutsch", badge: "DE" },
    { code: "ja-JP", label: "日语 · 日本語", badge: "日" },
    { code: "ko-KR", label: "韩语 · 한국어", badge: "韩" },
    { code: "es-ES", label: "西班牙语 · Español", badge: "ES" },
    { code: "pt-BR", label: "葡萄牙语 · Português（巴西）", badge: "PT" },
    { code: "ru-RU", label: "俄语 · Русский", badge: "RU" },
    { code: "it-IT", label: "意大利语 · Italiano", badge: "IT" },
    { code: "ar-SA", label: "阿拉伯语 · العربية（沙特阿拉伯）", badge: "AR" },
    { code: "hi-IN", label: "印地语 · हिन्दी", badge: "HI" },
    { code: "bn-BD", label: "孟加拉语 · বাংলা", badge: "BN" },
    { code: "ur-PK", label: "乌尔都语 · اردو", badge: "UR" },
    { code: "id-ID", label: "印尼语 · Bahasa Indonesia", badge: "ID" },
    { code: "vi-VN", label: "越南语 · Tiếng Việt", badge: "VI" },
    { code: "th-TH", label: "泰语 · ไทย", badge: "TH" },
    { code: "tr-TR", label: "土耳其语 · Türkçe", badge: "TR" },
  ].map(Object.freeze);
  Object.freeze(languages);

  function get(code) {
    return languages.find(item => item.code === code) || languages[0];
  }

  function read(storage) {
    try { return get(storage.getItem(STORAGE_KEY)).code; }
    catch (_) { return DEFAULT_LANGUAGE; }
  }

  function save(storage, code) {
    try { storage.setItem(STORAGE_KEY, get(code).code); return true; }
    catch (_) { return false; }
  }

  function normalize(text, code) {
    // Only trim the transcript's outer horizontal padding. Word spaces, French
    // punctuation spacing, line breaks and RTL-language text remain intact.
    text = text.replace(/^[ \t\u00a0\u3000]+|[ \t\u00a0\u3000]+$/g, "");
    const language = get(code).code;
    if (language.startsWith("zh-")) {
      text = text.replace(/(\p{Script=Han})[ \t\u00a0\u3000]+(?=\p{Script=Han})/gu, "$1");
    } else if (language === "ja-JP") {
      text = text.replace(/([\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}])[ \t\u00a0\u3000]+(?=[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}])/gu, "$1");
    } else return text;
    return text
      .replace(/[ \t\u00a0\u3000]+([，。！？；：、（）《》【】「」『』])/gu, "$1")
      .replace(/([（《【「『])[ \t\u00a0\u3000]+/gu, "$1")
      .replace(/([，。！？；：、）》】」』])[ \t\u00a0\u3000]+(?=[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}])/gu, "$1");
  }

  function separator(left, right, code) {
    if (!left || !right || /\s$/.test(left) || /^\s/.test(right)) return "";
    const language = get(code).code;
    if (language.startsWith("zh-") && (/\p{Script=Han}$/u.test(left) || /^\p{Script=Han}/u.test(right))) return "";
    if (language === "ja-JP" && (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]$/u.test(left) || /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]/u.test(right))) return "";
    if (language === "th-TH" && (/\p{Script=Thai}$/u.test(left) || /^\p{Script=Thai}/u.test(right))) return "";
    if (/[([{“‘（《【「『]$/.test(left) || /^[,.;:!?%…)}\]”’，。！？；：、）》】」』]/.test(right)) return "";
    return " ";
  }

  return { DEFAULT_LANGUAGE, STORAGE_KEY, languages, get, read, save, normalize, separator };
});
