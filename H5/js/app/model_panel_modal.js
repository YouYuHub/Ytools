/** Modal mechanics shared by empty/chat views; form drafts live in model_panel.js. */
(function (App) {
  "use strict";
  const { enhanceModal: modal, enhancePanel: panel, enhanceClose: close, boostBtn } = App;
  let opened = false;
  let returnFocus = null;
  let background = [];
  let hadScrollLock = false;

  function fit() {
    if (!opened) return;
    const viewport = window.visualViewport;
    modal.style.setProperty("--model-viewport-height", (viewport ? viewport.height : window.innerHeight) + "px");
    modal.style.setProperty("--model-viewport-top", (viewport ? viewport.offsetTop : 0) + "px");
  }

  function focusClose() {
    if (!panel.inert) close.focus({ preventScroll: true });
  }

  function show() {
    if (opened) { fit(); return; }
    opened = true;
    returnFocus = document.activeElement;
    background = Array.from(document.body.children).filter(function (node) {
      return node !== modal && !["SCRIPT", "STYLE", "LINK"].includes(node.tagName);
    }).map(function (node) { const before = node.inert; node.inert = true; return { node: node, before: before }; });
    hadScrollLock = document.body.classList.contains("model-settings-open");
    document.body.classList.add("model-settings-open");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    panel.classList.remove("hidden");
    boostBtn.setAttribute("aria-expanded", "true");
    fit();
    // Focus a button so opening settings never summons the phone keyboard.
    focusClose();
  }

  function hide() {
    if (!opened) return;
    opened = false;
    panel.classList.add("hidden");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    boostBtn.setAttribute("aria-expanded", "false");
    background.forEach(function (entry) { entry.node.inert = entry.before; });
    background = [];
    if (!hadScrollLock) document.body.classList.remove("model-settings-open");
    const target = returnFocus && returnFocus.isConnected ? returnFocus : boostBtn;
    returnFocus = null;
    if (target && !target.inert) target.focus({ preventScroll: true });
  }

  modal.addEventListener("click", function (event) {
    // The backdrop deliberately does not discard edits or close other menus.
    event.stopPropagation();
  });
  modal.addEventListener("keydown", function (event) {
    if (!opened || event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (App.closeModelPanel) App.closeModelPanel();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(panel.querySelectorAll(
      'button, input:not([type="hidden"]), select, textarea, summary, a[href], [tabindex]'
    )).filter(function (node) {
      return !node.disabled && node.tabIndex >= 0 && !node.closest("[inert]") && node.getClientRects().length;
    });
    if (!focusable.length) { event.preventDefault(); return; }
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
  });
  document.addEventListener("focusin", function (event) {
    if (opened && !modal.contains(event.target)) focusClose();
  });
  window.addEventListener("resize", fit);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fit);
    window.visualViewport.addEventListener("scroll", fit);
  }
  boostBtn.setAttribute("aria-haspopup", "dialog");
  boostBtn.setAttribute("aria-controls", "enhancePanel");
  boostBtn.setAttribute("aria-expanded", "false");
  App.showModelPanelModal = show;
  App.hideModelPanelModal = hide;
  App.fitEnhancePanel = fit;
})(window.App);
