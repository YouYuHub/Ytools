/* Export only codes already displayed to the user, after an explicit click. */
(function (global) {
  "use strict";
  function bindDownload(button, source) {
    const urls = new Set();
    function revoke(url) { global.URL.revokeObjectURL(url); urls.delete(url); }
    button.addEventListener("click", () => {
      const codes = source.textContent.trim();
      if (!codes) return;
      const content = "Ytools recovery codes\nEach code is single-use. Store separately from your authenticator.\n\n" + codes + "\n";
      const url = global.URL.createObjectURL(new Blob([content], { type: "text/plain;charset=utf-8" }));
      urls.add(url);
      const link = document.createElement("a"); link.href = url; link.download = "ytools-recovery-codes.txt";
      link.hidden = true; document.body.appendChild(link); link.click(); link.remove();
      global.setTimeout(() => { if (urls.has(url)) revoke(url); }, 1000);
    });
    global.addEventListener("pagehide", () => { for (const url of urls) revoke(url); });
  }
  global.YtoolsRecoveryCodes = { bindDownload };
})(window);
