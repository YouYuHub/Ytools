/** Chat settings presentation: tabs, focus, viewport and save lock. Config IO stays in composer.js. */
(function (App: any) {
  "use strict";
  const modal = App.chatSettingsModal;
  const body = modal.querySelector(".chat-settings-body");
  const tablist = modal.querySelector('[role="tablist"]');
  const tabs: any[] = Array.from(tablist.querySelectorAll('[role="tab"]'));
  const panels: any[] = tabs.map((tab: any) => document.getElementById(tab.getAttribute("aria-controls")));
  const close = App.chatSettingsClose;
  const scrollPositions = new Map<number, number>();
  let opened = false, current = 0, returnFocus: Element | null = null,
    background: Array<{ node: HTMLElement; before: boolean }> = [], hadScrollLock = false;

  function select(index: number, focus: unknown): void {
    if (index < 0 || index >= tabs.length) return;
    scrollPositions.set(current, body.scrollTop);
    current = index;
    tabs.forEach(function (tab: any, i: number) {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      panels[i].hidden = i !== index;
    });
    body.scrollTop = scrollPositions.get(index) || 0;
    if (focus) tabs[index].focus({ preventScroll: true });
  }

  tabs.forEach(function (tab: any, index: number) {
    tab.addEventListener("click", function () { select(index, false); });
    tab.addEventListener("keydown", function (event: KeyboardEvent) {
      if (event.isComposing) return;
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
        : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
      if (next < 0) return;
      event.preventDefault(); select(next, true);
    });
  });

  function fit(): void {
    if (!opened) return;
    const viewport = window.visualViewport;
    modal.style.setProperty("--settings-viewport-height", (viewport ? viewport.height : window.innerHeight) + "px");
    modal.style.setProperty("--settings-viewport-top", (viewport ? viewport.offsetTop : 0) + "px");
  }

  App.showChatSettingsModal = function (): void {
    if (opened) { fit(); return; }
    opened = true;
    returnFocus = document.activeElement;
    background = Array.from(document.body.children).filter(function (node) {
      return node !== modal && !["SCRIPT", "STYLE", "LINK"].includes(node.tagName);
    }).map(function (node) { const before = (node as HTMLElement).inert; (node as HTMLElement).inert = true; return { node: node as HTMLElement, before }; });
    hadScrollLock = document.body.classList.contains("chat-settings-open");
    document.body.classList.add("chat-settings-open");
    modal.classList.remove("hidden"); modal.setAttribute("aria-hidden", "false");
    select(current, false); fit();
    // Button focus avoids opening the mobile keyboard on entry.
    close.focus({ preventScroll: true });
  };

  App.hideChatSettingsModal = function (): void {
    if (!opened) return;
    opened = false;
    background.forEach(function (entry) { entry.node.inert = entry.before; });
    background = [];
    if (!hadScrollLock) document.body.classList.remove("chat-settings-open");
    const target = returnFocus && returnFocus.isConnected && returnFocus.getClientRects().length ? returnFocus : App.plusBtn;
    returnFocus = null;
    if (target && !target.inert) target.focus({ preventScroll: true });
  };

  App.setChatSettingsBusy = function (saving: boolean): void {
    App.chatSettingsSaving = saving;
    modal.setAttribute("aria-busy", String(saving));
    body.inert = saving; tablist.inert = saving;
    [App.chatSettingsReset, App.chatSettingsCancel, close].forEach(function (button: any) { button.disabled = saving; });
    App.chatSettingsConfirm.textContent = saving ? "保存中…" : "确定";
  };

  App.revealInvalidChatSetting = function (name: string): void {
    const aliases: Record<string, string> = { "MCP 工具并发线程数": "mcpToolWorkers", "子智能体轮次上限": "subAgentMaxRounds", "子智能体整体超时": "subAgentTimeoutSeconds" };
    const field = aliases[name] ? document.getElementById(aliases[name]) : Array.from<any>(body.querySelectorAll(".chat-setting-field")).find(function (row: any) {
      return row.querySelector("span").textContent.startsWith(name);
    })?.querySelector("input");
    if (!field) return;
    const index = panels.indexOf(field.closest('[role="tabpanel"]'));
    select(index, false); field.focus();
  };

  modal.addEventListener("keydown", function (event: KeyboardEvent) {
    if (!opened || event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); App.closeChatSettings(); return;
    }
    if (event.key !== "Tab") return;
    const nodes: any[] = Array.from(modal.querySelectorAll('button, input, select, [tabindex]')).filter(function (node: any) {
      return !node.disabled && node.tabIndex >= 0 && !node.closest("[inert]") && node.getClientRects().length;
    });
    if (!nodes.length) { event.preventDefault(); return; }
    const first = nodes[0], last = nodes[nodes.length - 1];
    if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
  });
  document.addEventListener("focusin", function (event: FocusEvent) {
    if (opened && !modal.contains(event.target) && !close.disabled) close.focus({ preventScroll: true });
  });
  window.addEventListener("resize", fit);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fit);
    window.visualViewport.addEventListener("scroll", fit);
  }
})((window as unknown as { App: any }).App);
