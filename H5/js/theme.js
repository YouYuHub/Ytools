/**
 * 主题管理：system / light / dark；强调色：blue / green / purple / pink / yellow / none
 * 偏好存 localStorage('ytools-theme-preference' / 'ytools-accent-preference')，
 * 有效主题写到 <html data-theme>，强调色写到 <html data-accent>
 */
(function (global) {
  const STORAGE_KEY = "ytools-theme-preference";
  const LEGACY_KEY = "theme-preference";
  const ACCENT_KEY = "ytools-accent-preference";
  const MEDIA = global.matchMedia("(prefers-color-scheme: dark)");
  const VALID = ["system", "light", "dark"];
  const ACCENTS = ["blue", "green", "purple", "pink", "yellow", "none"];

  function readStored() {
    const current = localStorage.getItem(STORAGE_KEY);
    if (current != null) return current;
    // 迁移旧键名（避免与其他同名网页项目共用键值互相影响）
    const legacy = localStorage.getItem(LEGACY_KEY);
    if (legacy != null) {
      localStorage.setItem(STORAGE_KEY, legacy);
      localStorage.removeItem(LEGACY_KEY);
      return legacy;
    }
    return null;
  }

  function getPreference() {
    const saved = readStored();
    return VALID.includes(saved) ? saved : "system";
  }

  function resolve(pref) {
    if (pref === "system") return MEDIA.matches ? "dark" : "light";
    return pref;
  }

  function apply(pref) {
    document.documentElement.dataset.theme = resolve(pref);
  }

  function setPreference(pref) {
    if (!VALID.includes(pref)) return;
    localStorage.setItem(STORAGE_KEY, pref);
    apply(pref);
    document.dispatchEvent(new CustomEvent("themechange", { detail: { preference: pref, effective: resolve(pref) } }));
  }

  function getAccentPreference() {
    const saved = localStorage.getItem(ACCENT_KEY);
    return ACCENTS.includes(saved) ? saved : "blue";
  }

  function applyAccent(accent) {
    document.documentElement.dataset.accent = accent;
  }

  function setAccentPreference(accent) {
    if (!ACCENTS.includes(accent)) return;
    localStorage.setItem(ACCENT_KEY, accent);
    applyAccent(accent);
    document.dispatchEvent(new CustomEvent("accentchange", { detail: { accent: accent } }));
  }

  // 系统主题变化时，仅在偏好为 system 时跟随
  MEDIA.addEventListener("change", function () {
    if (getPreference() === "system") {
      apply("system");
      document.dispatchEvent(new CustomEvent("themechange", { detail: { preference: "system", effective: resolve("system") } }));
    }
  });

  // 支持 ?theme=light|dark|system 临时预览（不写入偏好）
  const params = new URLSearchParams(location.search);
  const urlTheme = params.get("theme");
  if (VALID.includes(urlTheme)) {
    apply(urlTheme);
  } else {
    // 尽早应用，避免闪烁（需在 CSS 加载后、首绘前执行）
    apply(getPreference());
  }
  // 支持 ?accent=green|purple|pink|yellow|blue|none 临时预览（不写入偏好）
  const urlAccent = params.get("accent");
  applyAccent(ACCENTS.includes(urlAccent) ? urlAccent : getAccentPreference());

  global.ThemeManager = { getPreference, setPreference, resolve, getAccentPreference, setAccentPreference, ACCENTS };
})(window);
