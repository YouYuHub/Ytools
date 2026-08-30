/**
 * 主题管理：system / light / dark
 * 偏好存 localStorage('ytools-theme-preference')，有效主题写到 <html data-theme>
 */
(function (global) {
  const STORAGE_KEY = "ytools-theme-preference";
  const LEGACY_KEY = "theme-preference";
  const MEDIA = global.matchMedia("(prefers-color-scheme: dark)");
  const VALID = ["system", "light", "dark"];

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

  // 系统主题变化时，仅在偏好为 system 时跟随
  MEDIA.addEventListener("change", function () {
    if (getPreference() === "system") {
      apply("system");
      document.dispatchEvent(new CustomEvent("themechange", { detail: { preference: "system", effective: resolve("system") } }));
    }
  });

  // 支持 ?theme=light|dark|system 临时预览（不写入偏好）
  const urlTheme = new URLSearchParams(location.search).get("theme");
  if (VALID.includes(urlTheme)) {
    apply(urlTheme);
  } else {
    // 尽早应用，避免闪烁（需在 CSS 加载后、首绘前执行）
    apply(getPreference());
  }

  global.ThemeManager = { getPreference, setPreference, resolve };
})(window);
