"use strict";
(function (global) {
    "use strict";
    const doc = global.document;
    if (!doc || typeof MutationObserver !== "function")
        return;
    const COARSE = !!(global.matchMedia && global.matchMedia("(pointer: coarse)").matches);
    const registry = new Map();
    let openState = null;
    let uid = 0;
    const CHEVRON = '<svg class="yt-select-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
    const CHECK = '<svg class="yt-select-check" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 5 5 9-10"/></svg>';
    function accessibleName(select) {
        const aria = select.getAttribute("aria-label");
        if (aria)
            return aria;
        if (select.title)
            return select.title;
        const id = select.id;
        let label = id
            ? doc.querySelector('label[for="' + id + '"]')
            : null;
        if (!label)
            label = select.closest("label");
        if (label) {
            const span = label.querySelector("span");
            const text = (span ? span.textContent : label.textContent) || "";
            return text.replace(/\s+/g, " ").trim();
        }
        return "";
    }
    function selectedText(select) {
        const opt = select.options[select.selectedIndex];
        if (opt)
            return opt.textContent || "";
        return select.options.length ? (select.options[0].textContent || "") : "";
    }
    function sync(enh) {
        const { select, button, label } = enh;
        const text = selectedText(select);
        if (label.textContent !== text)
            label.textContent = text;
        button.disabled = select.disabled;
        // 镜像 class（去掉内部标记）与内联样式，保证布局规则继续命中
        const cls = ("yt-select " + select.className).replace(/\s*yt-select-native\s*/g, " ").trim();
        if (button.className !== cls)
            button.className = cls;
        const style = select.getAttribute("style") || "";
        if (button.getAttribute("style") !== style) {
            if (style)
                button.setAttribute("style", style);
            else
                button.removeAttribute("style");
        }
        const hidden = select.hidden || !select.offsetParent && getComputedStyle(select).display === "none";
        button.hidden = !!hidden;
        const name = accessibleName(select);
        if (name)
            button.setAttribute("aria-label", name);
        else
            button.removeAttribute("aria-label");
        if (openState && openState.enh === enh) {
            // 选项结构变化才重建；否则只刷新选中标记，保留键盘高亮位置
            if (openState.sig !== optionsSig(select))
                rebuildRows(openState);
            else
                markSelected(openState);
        }
    }
    function optionsSig(select) {
        let sig = String(select.selectedIndex);
        Array.prototype.forEach.call(select.options, function (o) {
            sig += "~" + (o.textContent || "") + "|" + (o.disabled ? 1 : 0);
        });
        return sig;
    }
    function markSelected(st) {
        const idx = st.enh.select.selectedIndex;
        st.rows.forEach(function (row, i) {
            row.classList.toggle("is-selected", i === idx);
            row.setAttribute("aria-selected", String(i === idx));
        });
    }
    function optionIndex(select, option) {
        return Array.prototype.indexOf.call(select.options, option);
    }
    function firstEnabled(select, from, dir) {
        const n = select.options.length;
        if (!n)
            return -1;
        let i = from;
        for (let step = 0; step < n; step++) {
            i = (i + dir + n) % n;
            if (!select.options[i].disabled)
                return i;
        }
        return -1;
    }
    function closeMenu(refocus) {
        if (!openState)
            return;
        const st = openState;
        openState = null;
        if (st.typeTimer != null)
            global.clearTimeout(st.typeTimer);
        st.menu.remove();
        st.enh.button.classList.remove("is-open");
        st.enh.button.setAttribute("aria-expanded", "false");
        st.enh.button.removeAttribute("aria-activedescendant");
        doc.removeEventListener("pointerdown", onDocPointerDown, true);
        doc.removeEventListener("keydown", onDocKeyDown, true);
        global.removeEventListener("resize", onViewportChange);
        doc.removeEventListener("scroll", onDocScroll, true);
        if (refocus)
            st.enh.button.focus();
    }
    function onViewportChange() { closeMenu(false); }
    function onDocScroll(ev) {
        // 菜单自身滚动不关闭；外部（页面/弹窗体）滚动才关闭
        if (openState && ev.target instanceof Node && openState.menu.contains(ev.target))
            return;
        closeMenu(false);
    }
    function onDocPointerDown(ev) {
        if (!openState)
            return;
        const target = ev.target;
        if (openState.menu.contains(target) || openState.enh.button.contains(target))
            return;
        closeMenu(false);
    }
    function setActive(st, index, scroll) {
        if (index < 0 || index >= st.rows.length)
            return;
        st.active = index;
        st.rows.forEach(function (row, i) { row.classList.toggle("is-active", i === index); });
        const row = st.rows[index];
        st.enh.button.setAttribute("aria-activedescendant", row.id);
        if (scroll)
            row.scrollIntoView({ block: "nearest" });
    }
    function commit(st, index) {
        const select = st.enh.select;
        const opt = select.options[index];
        if (!opt || opt.disabled)
            return;
        if (select.selectedIndex !== index) {
            select.selectedIndex = index;
            select.dispatchEvent(new Event("input", { bubbles: true }));
            select.dispatchEvent(new Event("change", { bubbles: true }));
        }
        sync(st.enh);
        closeMenu(true);
    }
    function onDocKeyDown(ev) {
        if (!openState)
            return;
        const st = openState;
        const select = st.enh.select;
        const key = ev.key;
        if (key === "Escape") {
            ev.preventDefault();
            ev.stopPropagation();
            closeMenu(true);
            return;
        }
        if (key === "Tab") {
            closeMenu(false);
            return;
        }
        if (key === "Enter" || key === " ") {
            ev.preventDefault();
            ev.stopPropagation();
            commit(st, st.active);
            return;
        }
        if (key === "ArrowDown") {
            ev.preventDefault();
            setActive(st, firstEnabled(select, st.active, 1), true);
            return;
        }
        if (key === "ArrowUp") {
            ev.preventDefault();
            setActive(st, firstEnabled(select, st.active, -1), true);
            return;
        }
        if (key === "Home") {
            ev.preventDefault();
            setActive(st, firstEnabled(select, -1, 1), true);
            return;
        }
        if (key === "End") {
            ev.preventDefault();
            setActive(st, firstEnabled(select, select.options.length, -1), true);
            return;
        }
        if (key === "PageDown") {
            ev.preventDefault();
            setActive(st, firstEnabled(select, Math.min(st.active + 8, select.options.length - 1), 1), true);
            return;
        }
        if (key === "PageUp") {
            ev.preventDefault();
            setActive(st, firstEnabled(select, Math.max(st.active - 8, 0), -1), true);
            return;
        }
        if (key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
            ev.preventDefault();
            st.typeBuffer += key.toLowerCase();
            if (st.typeTimer != null)
                global.clearTimeout(st.typeTimer);
            st.typeTimer = global.setTimeout(function () { if (openState === st)
                st.typeBuffer = ""; }, 600);
            const n = select.options.length;
            for (let step = 1; step <= n; step++) {
                const i = (st.active + step) % n;
                const text = (select.options[i].textContent || "").trim().toLowerCase();
                if (!select.options[i].disabled && text.indexOf(st.typeBuffer) === 0) {
                    setActive(st, i, true);
                    return;
                }
            }
        }
    }
    function rebuildRows(st) {
        const select = st.enh.select;
        st.menu.innerHTML = "";
        st.rows = [];
        const baseId = st.menu.id;
        function addOption(opt) {
            const index = optionIndex(select, opt);
            const row = doc.createElement("div");
            row.className = "yt-select-option";
            row.id = baseId + "-o" + index;
            row.setAttribute("role", "option");
            row.setAttribute("aria-selected", String(index === select.selectedIndex));
            if (opt.disabled)
                row.setAttribute("aria-disabled", "true");
            if (index === select.selectedIndex)
                row.classList.add("is-selected");
            const check = doc.createElement("span");
            check.className = "yt-select-option-check";
            check.innerHTML = CHECK;
            const text = doc.createElement("span");
            text.className = "yt-select-option-text";
            text.textContent = opt.textContent || "";
            row.append(check, text);
            row.addEventListener("click", function () { commit(st, index); });
            row.addEventListener("pointerenter", function () { if (!opt.disabled)
                setActive(st, index, false); });
            st.menu.appendChild(row);
            st.rows.push(row);
        }
        Array.prototype.forEach.call(select.children, function (child) {
            if (child.tagName === "OPTGROUP") {
                const group = doc.createElement("div");
                group.className = "yt-select-group";
                group.setAttribute("role", "group");
                group.setAttribute("aria-label", child.getAttribute("label") || "");
                const head = doc.createElement("div");
                head.className = "yt-select-group-label";
                head.textContent = child.getAttribute("label") || "";
                group.appendChild(head);
                Array.prototype.forEach.call(child.children, function (opt) {
                    if (opt.tagName === "OPTION") {
                        // 组内 option 也放进 menu 顶层 rows 顺序：用临时容器保持键盘顺序一致
                        addOptionTo(group, opt);
                    }
                });
                st.menu.appendChild(group);
            }
            else if (child.tagName === "OPTION") {
                addOption(child);
            }
        });
        function addOptionTo(group, opt) {
            const before = st.rows.length;
            addOption(opt);
            const row = st.rows[before];
            // addOption 追加到 menu 顶层；移入 group 保持视觉分组，rows 顺序不变
            group.appendChild(row);
        }
        st.sig = optionsSig(select);
        const active = select.selectedIndex >= 0 ? select.selectedIndex : firstEnabled(select, -1, 1);
        setActive(st, active, false);
        const row = st.rows[active];
        if (row)
            row.scrollIntoView({ block: "nearest" });
    }
    function openMenu(enh) {
        closeMenu(false);
        const select = enh.select;
        if (select.disabled || !select.options.length)
            return;
        const menu = doc.createElement("div");
        menu.className = "yt-select-menu";
        menu.id = "yt-select-menu-" + (++uid);
        menu.setAttribute("role", "listbox");
        const name = accessibleName(select);
        if (name)
            menu.setAttribute("aria-label", name);
        doc.body.appendChild(menu);
        const st = { enh, menu, rows: [], active: -1, sig: "", typeBuffer: "", typeTimer: null };
        openState = st;
        rebuildRows(st);
        // 定位：默认按钮下方，空间不足翻转到上方；水平夹紧在视口内
        const rect = enh.button.getBoundingClientRect();
        const vw = global.innerWidth, vh = global.innerHeight;
        menu.style.minWidth = Math.ceil(rect.width) + "px";
        menu.style.maxWidth = Math.max(220, vw - 16) + "px";
        const maxH = Math.min(320, vh - 16);
        menu.style.maxHeight = maxH + "px";
        const mh = menu.offsetHeight, mw = menu.offsetWidth;
        let top = rect.bottom + 6;
        let placement = "bottom";
        if (top + mh > vh - 8 && rect.top - 6 - mh >= 8) {
            top = rect.top - 6 - mh;
            placement = "top";
        }
        top = Math.max(8, Math.min(top, vh - mh - 8));
        let left = rect.left;
        left = Math.max(8, Math.min(left, vw - mw - 8));
        menu.style.top = Math.round(top) + "px";
        menu.style.left = Math.round(left) + "px";
        menu.dataset.placement = placement;
        enh.button.classList.add("is-open");
        enh.button.setAttribute("aria-expanded", "true");
        enh.button.setAttribute("aria-controls", menu.id);
        doc.addEventListener("pointerdown", onDocPointerDown, true);
        doc.addEventListener("keydown", onDocKeyDown, true);
        global.addEventListener("resize", onViewportChange);
        doc.addEventListener("scroll", onDocScroll, true);
    }
    function onButtonKeyDown(enh, ev) {
        if (openState)
            return; // 打开时由 onDocKeyDown 处理
        const select = enh.select;
        const key = ev.key;
        if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " " || key === "Home" || key === "End") {
            ev.preventDefault();
            openMenu(enh);
            return;
        }
        // 关闭态直接按键改值（原生行为）：方向键步进
        if (key === "ArrowLeft" || key === "ArrowRight")
            return;
        if (key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
            const n = select.options.length;
            const ch = key.toLowerCase();
            for (let step = 1; step <= n; step++) {
                const i = (select.selectedIndex + step) % n;
                const text = (select.options[i].textContent || "").trim().toLowerCase();
                if (!select.options[i].disabled && text.indexOf(ch) === 0) {
                    select.selectedIndex = i;
                    select.dispatchEvent(new Event("input", { bubbles: true }));
                    select.dispatchEvent(new Event("change", { bubbles: true }));
                    sync(enh);
                    ev.preventDefault();
                    return;
                }
            }
        }
    }
    function enhance(select) {
        if (COARSE || registry.has(select) || select.multiple || select.size > 1)
            return;
        if (!select.parentNode)
            return;
        const button = doc.createElement("button");
        button.type = "button";
        button.className = "yt-select";
        button.setAttribute("aria-haspopup", "listbox");
        button.setAttribute("aria-expanded", "false");
        const label = doc.createElement("span");
        label.className = "yt-select-label";
        button.appendChild(label);
        button.insertAdjacentHTML("beforeend", CHEVRON);
        select.classList.add("yt-select-native");
        select.tabIndex = -1;
        select.setAttribute("aria-hidden", "true");
        select.parentNode.insertBefore(button, select.nextSibling);
        const enh = { select, button, label, mo: null };
        button.addEventListener("click", function (ev) {
            ev.preventDefault();
            ev.stopPropagation();
            if (openState && openState.enh === enh)
                closeMenu(true);
            else
                openMenu(enh);
        });
        button.addEventListener("keydown", function (ev) { onButtonKeyDown(enh, ev); });
        select.addEventListener("change", function () { sync(enh); });
        select.addEventListener("yt-select-sync", function () { sync(enh); });
        enh.mo = new MutationObserver(function () { sync(enh); });
        enh.mo.observe(select, { attributes: true, attributeFilter: ["disabled", "hidden", "class", "style", "aria-label"], childList: true, subtree: true, characterData: true });
        registry.set(select, enh);
        sync(enh);
    }
    function cleanup(select) {
        const enh = registry.get(select);
        if (!enh)
            return;
        if (openState && openState.enh === enh)
            closeMenu(false);
        enh.mo.disconnect();
        enh.button.remove();
        registry.delete(select);
    }
    function scan(root) {
        if (COARSE)
            return;
        const selects = root.querySelectorAll ? root.querySelectorAll("select") : [];
        Array.prototype.forEach.call(selects, function (s) {
            if (s.tagName === "SELECT")
                enhance(s);
        });
        if (root instanceof HTMLSelectElement)
            enhance(root);
    }
    function refresh() {
        registry.forEach(function (enh, select) {
            if (!select.isConnected)
                cleanup(select);
            else
                sync(enh);
        });
        scan(doc);
    }
    // 程序化赋值同步：拦截 value / selectedIndex setter，派发内部事件
    try {
        const proto = HTMLSelectElement.prototype;
        ["value", "selectedIndex"].forEach(function (prop) {
            const desc = Object.getOwnPropertyDescriptor(proto, prop);
            if (!desc || !desc.set || !desc.get)
                return;
            Object.defineProperty(proto, prop, {
                configurable: true,
                enumerable: desc.enumerable,
                get: desc.get,
                set: function (v) {
                    desc.set.call(this, v);
                    this.dispatchEvent(new Event("yt-select-sync"));
                },
            });
        });
    }
    catch (_) { /* 原型不可写时退回轮询同步 */ }
    // 兜底轮询：覆盖 .disabled 属性赋值、option 文本变更等无事件场景
    global.setInterval(function () {
        if (doc.hidden)
            return;
        registry.forEach(function (enh, select) {
            if (!select.isConnected)
                cleanup(select);
            else
                sync(enh);
        });
    }, 500);
    doc.addEventListener("reset", function () { global.setTimeout(refresh, 0); }, true);
    const bodyObserver = new MutationObserver(function (muts) {
        let touched = false;
        muts.forEach(function (m) {
            m.addedNodes.forEach(function (n) {
                if (n.nodeType === 1) {
                    scan(n);
                    touched = true;
                }
            });
            m.removedNodes.forEach(function (n) {
                if (n.nodeType !== 1)
                    return;
                const el = n;
                if (el.tagName === "SELECT")
                    cleanup(el);
                else if (el.querySelectorAll)
                    el.querySelectorAll("select").forEach(function (s) { cleanup(s); });
            });
        });
        if (touched)
            refresh();
    });
    function init() {
        scan(doc);
        bodyObserver.observe(doc.body, { childList: true, subtree: true });
    }
    if (doc.readyState === "loading")
        doc.addEventListener("DOMContentLoaded", init);
    else
        init();
    global.YtoolsSelectMenu = {
        refresh, close: function () { closeMenu(false); },
    };
})(window);
