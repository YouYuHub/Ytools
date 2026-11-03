interface RecoveryCodesApi {
  bindDownload: (button: HTMLElement, source: HTMLElement) => void;
}

/* Export only codes already displayed to the user, after an explicit click. */
(function (global: Window) {
  "use strict";
  // global.URL 经测试替身注入（w.URL），必须保持 global.URL.xxx 方法调用形态。
  const URLns = (global as unknown as { URL: typeof URL }).URL;
  function bindDownload(button: HTMLElement, source: HTMLElement): void {
    const urls = new Set<string>();
    function revoke(url: string): void { URLns.revokeObjectURL(url); urls.delete(url); }
    button.addEventListener("click", () => {
      const codes = (source.textContent as string).trim();
      if (!codes) return;
      const content = "Ytools recovery codes\nEach code is single-use. Store separately from your authenticator.\n\n" + codes + "\n";
      const url = URLns.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
      urls.add(url);
      const link = document.createElement("a"); link.href = url; link.download = "ytools-recovery-codes.txt";
      link.hidden = true; document.body.appendChild(link); link.click(); link.remove();
      global.setTimeout(() => { if (urls.has(url)) revoke(url); }, 1000);
    });
    global.addEventListener("pagehide", () => { for (const url of urls) revoke(url); });
  }
  (global as unknown as { YtoolsRecoveryCodes: RecoveryCodesApi }).YtoolsRecoveryCodes = { bindDownload };
})(window);
