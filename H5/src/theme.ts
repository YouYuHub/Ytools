interface ThemeManagerApi {
  getPreference: () => string;
  setPreference: (pref: string) => void;
  resolve: (pref: string) => string;
  getAccentPreference: () => string;
  setAccentPreference: (accent: string) => void;
  ACCENTS: string[];
}

/**
 * 主题管理：system / light / dark；强调色：blue / green / purple / pink / yellow / none
 * 偏好存 localStorage('ytools-theme-preference' / 'ytools-accent-preference')，
 * 有效主题写到 <html data-theme>，强调色写到 <html data-accent>
 */
(function (global: Window) {
  const STORAGE_KEY = "ytools-theme-preference";
  const ACCENT_KEY = "ytools-accent-preference";
  const MEDIA = global.matchMedia("(prefers-color-scheme: dark)");
  const VALID = ["system", "light", "dark"];
  const ACCENTS = ["blue", "green", "purple", "pink", "yellow", "none"];

  function readStored(): string | null {
    return localStorage.getItem(STORAGE_KEY);
  }

  function getPreference(): string {
    const saved = readStored();
    return VALID.includes(saved as string) ? (saved as string) : "system";
  }

  function resolve(pref: string): string {
    if (pref === "system") return MEDIA.matches ? "dark" : "light";
    return pref;
  }

  function apply(pref: string): void {
    document.documentElement.dataset.theme = resolve(pref);
  }

  function setPreference(pref: string): void {
    if (!VALID.includes(pref)) return;
    localStorage.setItem(STORAGE_KEY, pref);
    apply(pref);
    document.dispatchEvent(new CustomEvent("themechange", { detail: { preference: pref, effective: resolve(pref) } }));
  }

  function getAccentPreference(): string {
    const saved = localStorage.getItem(ACCENT_KEY);
    return ACCENTS.includes(saved as string) ? (saved as string) : "blue";
  }

  function applyAccent(accent: string): void {
    document.documentElement.dataset.accent = accent;
  }

  function setAccentPreference(accent: string): void {
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
  if (VALID.includes(urlTheme as string)) {
    apply(urlTheme as string);
  } else {
    // 尽早应用，避免闪烁（需在 CSS 加载后、首绘前执行）
    apply(getPreference());
  }
  // 支持 ?accent=green|purple|pink|yellow|blue|none 临时预览（不写入偏好）
  const urlAccent = params.get("accent");
  applyAccent(ACCENTS.includes(urlAccent as string) ? (urlAccent as string) : getAccentPreference());

  (global as unknown as { ThemeManager: ThemeManagerApi }).ThemeManager = { getPreference, setPreference, resolve, getAccentPreference, setAccentPreference, ACCENTS };
  global.addEventListener("ytools-preferences-change", function () {
    if (!VALID.includes(urlTheme as string)) apply(getPreference());
    if (!ACCENTS.includes(urlAccent as string)) applyAccent(getAccentPreference());
    document.dispatchEvent(new CustomEvent("themechange", { detail: { preference: getPreference(), effective: resolve(getPreference()) } }));
    document.dispatchEvent(new CustomEvent("accentchange", { detail: { accent: getAccentPreference() } }));
  });
})(window);
