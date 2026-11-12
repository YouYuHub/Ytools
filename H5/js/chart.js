"use strict";
/**
 * 数据图表渲染器（```chart 代码块 → SVG）。
 * 手写 SVG、零依赖、离线可用：bar（柱状/直方图）/ hbar（横条）/ line（折线）/ pie（饼图）。
 * 纯字符串构建（不碰 DOM），可在 Node 单测里直接跑。
 * 配色说明：数据色用固定色板（深浅主题通用）；文字/坐标轴用 currentColor
 * 跟随主题——导出 PNG 时 currentColor 按黑色光栅化，依然可读；绝不在 SVG
 * 内使用 var(--*)（隔离导出上下文解析不了）。
 */
(function (global) {
    var MAX_CATEGORIES = 64; // bar/line/hbar 类目上限
    var MAX_HBAR_CATEGORIES = 40; // hbar 纵向空间大，单独收紧
    var MAX_SERIES = 8;
    var MAX_SLICES = 32; // 饼图扇区上限
    var MAX_LABEL_CHARS = 24; // 类目/图例单标签截断
    var MAX_TITLE_CHARS = 80;
    // 色盲友好色板（Okabe-Ito 系），深浅主题通用
    var PALETTE = [
        "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
        "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
    ];
    function esc(text) {
        return String(text == null ? "" : text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }
    function toNum(v) {
        if (typeof v === "number")
            return isFinite(v) ? v : NaN;
        if (typeof v === "string" && v.trim() !== "") {
            var n = Number(v);
            return isFinite(n) ? n : NaN;
        }
        return NaN;
    }
    function fmtNum(n) {
        if (!isFinite(n))
            return "0";
        if (Math.abs(n) < 1e-15)
            return "0";
        if (Number.isInteger(n) && Math.abs(n) < 1e15)
            return String(n);
        return String(parseFloat(n.toFixed(4)));
    }
    function fmtCompact(n) {
        var a = Math.abs(n);
        var sign = n < 0 ? "-" : "";
        function trim(x) {
            return String(parseFloat(x.toFixed(2)));
        }
        if (a >= 1e9)
            return sign + trim(a / 1e9) + "B";
        if (a >= 1e6)
            return sign + trim(a / 1e6) + "M";
        if (a >= 1e3)
            return sign + trim(a / 1e3) + "k";
        return fmtNum(n);
    }
    function truncLabel(s, max) {
        var t = String(s == null ? "" : s);
        if (t.length <= max)
            return t;
        return t.slice(0, Math.max(0, max - 1)) + "…";
    }
    // 粗估文本像素宽（12px 字号）：ASCII 按 7px、CJK 按 13px
    function estTextWidth(s) {
        var w = 0;
        for (var i = 0; i < s.length; i++) {
            w += s.charCodeAt(i) < 128 ? 7 : 13;
        }
        return w;
    }
    // 友好刻度：返回 {ticks, step}，ticks 覆盖 [min, max]
    function niceTicks(min, max, count) {
        if (!isFinite(min) || !isFinite(max)) {
            min = 0;
            max = 1;
        }
        if (min === max) {
            if (min === 0) {
                max = 1;
            }
            else {
                var d = Math.abs(min) * 0.1 || 1;
                min -= d;
                max += d;
            }
        }
        var span = max - min;
        var rawStep = span / Math.max(1, count);
        var mag = Math.pow(10, Math.floor(Math.log(rawStep) / Math.LN10));
        var norm = rawStep / mag;
        var mult = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
        var step = mult * mag;
        var ticks = [];
        var t = Math.ceil(min / step - 1e-9) * step;
        var guard = 0;
        while (t <= max + step * 0.5 + 1e-9 && guard++ < 32) {
            ticks.push(Math.abs(t) < step * 1e-9 ? 0 : t);
            t += step;
        }
        if (!ticks.length)
            ticks = [min, max];
        return { ticks: ticks, step: step, lo: Math.min(ticks[0], min), hi: Math.max(ticks[ticks.length - 1], max) };
    }
    function normalizeSpec(input) {
        var warnings = [];
        var raw = input;
        if (typeof raw === "string") {
            try {
                raw = JSON.parse(raw);
            }
            catch (e) {
                return { ok: false, error: "JSON 解析失败：" + (e && e.message || e) };
            }
        }
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
            return { ok: false, error: "图表配置必须是 JSON 对象（含 type/series 等字段）" };
        }
        var type = String(raw.type == null ? "" : raw.type).trim().toLowerCase();
        // 直方图按离散分箱表达：复用柱状图（xAxis 放分箱标签）
        if (type === "histogram" || type === "hist")
            type = "bar";
        if (type !== "bar" && type !== "hbar" && type !== "line" && type !== "pie") {
            return { ok: false, error: "未知图表类型：" + (raw.type == null ? "（空）" : JSON.stringify(raw.type)) + "，仅支持 bar / hbar / line / pie" };
        }
        var title = truncLabel(String(raw.title == null ? "" : raw.title).trim(), MAX_TITLE_CHARS);
        var yLabel = truncLabel(String(raw.yLabel == null ? "" : raw.yLabel).trim(), MAX_LABEL_CHARS);
        if (type === "pie") {
            var items = [];
            if (Array.isArray(raw.data)) {
                raw.data.forEach(function (d, i) {
                    if (d != null && typeof d === "object" && !Array.isArray(d)) {
                        items.push({ name: d.name == null ? "扇区" + (i + 1) : d.name, value: d.value });
                    }
                    else {
                        var v = toNum(d);
                        items.push({ name: "扇区" + (i + 1), value: isNaN(v) ? 0 : v });
                        if (isNaN(v))
                            warnings.push("第 " + (i + 1) + " 个值非数字，已置 0");
                    }
                });
            }
            else if (raw.series && Array.isArray(raw.series) && raw.series.length) {
                var s0 = raw.series[0] || {};
                var labels = Array.isArray(raw.xAxis) ? raw.xAxis : [];
                (Array.isArray(s0.data) ? s0.data : []).forEach(function (v, i) {
                    items.push({ name: labels[i] != null ? labels[i] : "扇区" + (i + 1), value: v });
                });
            }
            if (!items.length)
                return { ok: false, error: "饼图缺少数据：请提供 data（[{name, value}]）或 series[0].data" };
            var bad = 0;
            items.forEach(function (it) {
                var v = toNum(it.value);
                if (isNaN(v) || v < 0) {
                    bad++;
                    v = 0;
                }
                it.value = v;
                it.name = truncLabel(it.name, MAX_LABEL_CHARS);
            });
            if (bad)
                warnings.push(bad + " 个扇区值非法/为负，已置 0");
            if (items.length > MAX_SLICES) {
                warnings.push("扇区超 " + MAX_SLICES + " 个，已截断");
                items = items.slice(0, MAX_SLICES);
            }
            var total = 0;
            items.forEach(function (it) { total += it.value; });
            if (!(total > 0))
                return { ok: false, error: "饼图数值全为 0，无可绘制内容" };
            return { ok: true, spec: { type: type, title: title, items: items, total: total }, warnings: warnings };
        }
        // 笛卡尔系：bar / hbar / line
        var xAxis = Array.isArray(raw.xAxis) ? raw.xAxis.map(function (x) {
            return truncLabel(x, MAX_LABEL_CHARS);
        }) : [];
        var seriesRaw = Array.isArray(raw.series) ? raw.series : [];
        if (!seriesRaw.length)
            return { ok: false, error: "缺少 series：请提供 [{name, data}]（data 为数字数组）" };
        if (seriesRaw.length > MAX_SERIES) {
            warnings.push("系列超 " + MAX_SERIES + " 个，已截断");
            seriesRaw = seriesRaw.slice(0, MAX_SERIES);
        }
        var series = seriesRaw.map(function (s, i) {
            var data = s != null && Array.isArray(s.data) ? s.data : [];
            var nums = [];
            var badCount = 0;
            data.forEach(function (v) {
                var n = toNum(v);
                if (isNaN(n)) {
                    badCount++;
                    n = 0;
                }
                nums.push(n);
            });
            if (badCount)
                warnings.push("系列“" + (s && s.name != null ? s.name : "系列" + (i + 1)) + "”有 " + badCount + " 个非数字，已置 0");
            return { name: truncLabel(s != null && s.name != null ? s.name : "系列" + (i + 1), MAX_LABEL_CHARS), data: nums };
        });
        var maxLen = 0;
        series.forEach(function (s) { maxLen = Math.max(maxLen, s.data.length); });
        if (!xAxis.length) {
            // 未给类目：按序号生成
            for (var i = 0; i < maxLen; i++)
                xAxis.push("#" + (i + 1));
        }
        var catCap = type === "hbar" ? MAX_HBAR_CATEGORIES : MAX_CATEGORIES;
        if (xAxis.length > catCap) {
            warnings.push("类目超 " + catCap + " 个，已截断");
            xAxis = xAxis.slice(0, catCap);
        }
        if (!xAxis.length)
            return { ok: false, error: "没有可用类目数据" };
        // 数据与类目对齐：短补 0、长截断
        series.forEach(function (s) {
            while (s.data.length < xAxis.length)
                s.data.push(0);
            if (s.data.length > xAxis.length)
                s.data = s.data.slice(0, xAxis.length);
        });
        return { ok: true, spec: { type: type, title: title, yLabel: yLabel, xAxis: xAxis, series: series }, warnings: warnings };
    }
    function svgText(x, y, text, attrs) {
        return '<text x="' + x + '" y="' + y + '"' + (attrs || "") + ">" + esc(text) + "</text>";
    }
    function renderCartesian(spec, warnings) {
        var W = 640, H = 400;
        var horizontal = spec.type === "hbar";
        var n = spec.xAxis.length, m = spec.series.length;
        var hasTitle = !!spec.title;
        var top = hasTitle ? 48 : 20;
        var right = 16;
        var bottom = 24;
        var left = 56;
        var rotateX = false;
        if (!horizontal) {
            rotateX = n > 8 || spec.xAxis.some(function (x) { return x.length > 8; });
            bottom = rotateX ? 74 : 46;
            if (spec.yLabel)
                left += 20;
        }
        else {
            var maxW = 0;
            spec.xAxis.forEach(function (x) { maxW = Math.max(maxW, estTextWidth(x)); });
            left = Math.min(200, Math.max(64, maxW + 16));
            bottom = 30;
            H = Math.max(220, top + n * 26 + bottom + (m > 1 ? 30 : 0));
        }
        // 图例占位（多系列才画）
        var legendH = m > 1 ? 30 : 0;
        if (!horizontal)
            H = 400;
        var plotW = W - left - right;
        var plotH = H - top - bottom - legendH;
        if (plotW < 80)
            plotW = 80;
        if (plotH < 80)
            plotH = 80;
        var all = [];
        spec.series.forEach(function (s) { s.data.forEach(function (v) { all.push(v); }); });
        var dmin = Math.min.apply(null, all.concat([0]));
        var dmax = Math.max.apply(null, all.concat([0]));
        var ticks = niceTicks(dmin, dmax, 4);
        var lo = ticks.lo, hi = ticks.hi;
        function pos(v) {
            if (hi === lo)
                return horizontal ? left : top + plotH;
            var k = (v - lo) / (hi - lo);
            return horizontal ? left + k * plotW : top + plotH - k * plotH;
        }
        var zero = pos(0);
        var parts = [];
        if (hasTitle) {
            parts.push(svgText(W / 2, 26, spec.title, ' text-anchor="middle" font-size="15" font-weight="600" fill="currentColor"'));
        }
        // 网格 + 刻度
        ticks.ticks.forEach(function (t) {
            if (horizontal) {
                var gx = pos(t);
                parts.push('<line x1="' + gx + '" y1="' + top + '" x2="' + gx + '" y2="' + (top + plotH) + '" stroke="currentColor" stroke-opacity="0.14"/>');
                parts.push(svgText(gx, top + plotH + 18, fmtCompact(t), ' text-anchor="middle" font-size="11" fill="currentColor" opacity="0.75"'));
            }
            else {
                var gy = pos(t);
                parts.push('<line x1="' + left + '" y1="' + gy + '" x2="' + (left + plotW) + '" y2="' + gy + '" stroke="currentColor" stroke-opacity="0.14"/>');
                parts.push(svgText(left - 8, gy + 4, fmtCompact(t), ' text-anchor="end" font-size="11" fill="currentColor" opacity="0.75"'));
            }
        });
        // 轴线
        if (horizontal) {
            parts.push('<line x1="' + left + '" y1="' + top + '" x2="' + left + '" y2="' + (top + plotH) + '" stroke="currentColor" stroke-opacity="0.35"/>');
            parts.push('<line x1="' + left + '" y1="' + (top + plotH) + '" x2="' + (left + plotW) + '" y2="' + (top + plotH) + '" stroke="currentColor" stroke-opacity="0.35"/>');
        }
        else {
            parts.push('<line x1="' + left + '" y1="' + top + '" x2="' + left + '" y2="' + (top + plotH) + '" stroke="currentColor" stroke-opacity="0.35"/>');
            parts.push('<line x1="' + left + '" y1="' + (top + plotH) + '" x2="' + (left + plotW) + '" y2="' + (top + plotH) + '" stroke="currentColor" stroke-opacity="0.35"/>');
        }
        if (spec.yLabel && !horizontal) {
            parts.push('<text x="16" y="' + (top + plotH / 2) + '" text-anchor="middle" font-size="11" fill="currentColor" opacity="0.75" transform="rotate(-90 16 ' + (top + plotH / 2) + ')">' + esc(spec.yLabel) + "</text>");
        }
        var showValues = n <= 12;
        if (spec.type === "line") {
            spec.series.forEach(function (s, si) {
                var color = PALETTE[si % PALETTE.length];
                var pts = [];
                s.data.forEach(function (v, i) {
                    var cx = left + plotW * (n === 1 ? 0.5 : i / (n - 1));
                    var cy = pos(v);
                    pts.push(cx.toFixed(1) + "," + cy.toFixed(1));
                });
                if (pts.length > 1) {
                    parts.push('<polyline points="' + pts.join(" ") + '" fill="none" stroke="' + color + '" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>');
                }
                s.data.forEach(function (v, i) {
                    var cx = left + plotW * (n === 1 ? 0.5 : i / (n - 1));
                    var cy = pos(v);
                    parts.push('<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="3.5" fill="' + color + '"><title>' + esc(s.name + " · " + spec.xAxis[i] + "：" + fmtNum(v)) + "</title></circle>");
                });
            });
            // x 类目
            spec.xAxis.forEach(function (x, i) {
                var cx = left + plotW * (n === 1 ? 0.5 : i / (n - 1));
                if (rotateX) {
                    parts.push('<text x="' + cx.toFixed(1) + '" y="' + (top + plotH + 14) + '" text-anchor="end" font-size="11" fill="currentColor" transform="rotate(-25 ' + cx.toFixed(1) + " " + (top + plotH + 14) + ')"><title>' + esc(x) + "</title>" + esc(x) + "</text>");
                }
                else {
                    parts.push(svgText(cx.toFixed(1), top + plotH + 18, x, ' text-anchor="middle" font-size="11" fill="currentColor"'));
                }
            });
        }
        else if (!horizontal) {
            // bar：分组柱
            var groupW = plotW / n;
            var barW = Math.min(56, Math.max(4, (groupW * 0.72) / m));
            spec.series.forEach(function (s, si) {
                var color = PALETTE[si % PALETTE.length];
                s.data.forEach(function (v, i) {
                    var gx = left + i * groupW + (groupW - barW * m) / 2 + si * barW;
                    var vy = pos(v);
                    var y = Math.min(vy, zero), h = Math.max(1, Math.abs(vy - zero));
                    if (h < 1)
                        h = 1;
                    parts.push('<rect x="' + gx.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + barW.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="2" fill="' + color + '"><title>' + esc(s.name + " · " + spec.xAxis[i] + "：" + fmtNum(v)) + "</title></rect>");
                    if (showValues) {
                        var ly = v >= 0 ? y - 5 : y + h + 13;
                        parts.push(svgText((gx + barW / 2).toFixed(1), ly.toFixed(1), fmtNum(v), ' text-anchor="middle" font-size="10.5" fill="currentColor"'));
                    }
                });
            });
            spec.xAxis.forEach(function (x, i) {
                var cx = left + i * groupW + groupW / 2;
                if (rotateX) {
                    parts.push('<text x="' + cx.toFixed(1) + '" y="' + (top + plotH + 14) + '" text-anchor="end" font-size="11" fill="currentColor" transform="rotate(-25 ' + cx.toFixed(1) + " " + (top + plotH + 14) + ')"><title>' + esc(x) + "</title>" + esc(x) + "</text>");
                }
                else {
                    parts.push(svgText(cx.toFixed(1), top + plotH + 18, x, ' text-anchor="middle" font-size="11" fill="currentColor"'));
                }
            });
        }
        else {
            // hbar：分组横条
            var rowH = plotH / n;
            var hbH = Math.min(22, Math.max(4, (rowH * 0.72) / m));
            spec.series.forEach(function (s, si) {
                var color = PALETTE[si % PALETTE.length];
                s.data.forEach(function (v, i) {
                    var ry = top + i * rowH + (rowH - hbH * m) / 2 + si * hbH;
                    var vx = pos(v);
                    var x = Math.min(vx, zero), w = Math.max(1, Math.abs(vx - zero));
                    parts.push('<rect x="' + x.toFixed(1) + '" y="' + ry.toFixed(1) + '" width="' + w.toFixed(1) + '" height="' + hbH.toFixed(1) + '" rx="2" fill="' + color + '"><title>' + esc(s.name + " · " + spec.xAxis[i] + "：" + fmtNum(v)) + "</title></rect>");
                    if (showValues) {
                        var lx = v >= 0 ? x + w + 5 : x - 5;
                        parts.push(svgText(lx.toFixed(1), (ry + hbH / 2 + 4).toFixed(1), fmtNum(v), ' text-anchor="' + (v >= 0 ? "start" : "end") + '" font-size="10.5" fill="currentColor"'));
                    }
                });
            });
            spec.xAxis.forEach(function (x, i) {
                parts.push('<text x="' + (left - 8) + '" y="' + (top + i * rowH + rowH / 2 + 4).toFixed(1) + '" text-anchor="end" font-size="11" fill="currentColor"><title>' + esc(x) + "</title>" + esc(x) + "</text>");
            });
        }
        // 图例（多系列）
        if (m > 1) {
            var ly = H - 12;
            var items = spec.series.map(function (s, si) {
                return { name: s.name, color: PALETTE[si % PALETTE.length], w: estTextWidth(s.name) + 26 };
            });
            var totalW = 0;
            items.forEach(function (it) { totalW += it.w + 14; });
            var lx = (W - totalW + 14) / 2;
            items.forEach(function (it) {
                parts.push('<rect x="' + lx.toFixed(1) + '" y="' + (ly - 11) + '" width="12" height="12" rx="2" fill="' + it.color + '"/>');
                parts.push(svgText((lx + 16).toFixed(1), (ly - 1).toFixed(1), it.name, ' font-size="11" fill="currentColor"'));
                lx += it.w + 14;
            });
        }
        if (warnings.length) {
            parts.push(svgText(W - 10, H - (m > 1 ? 26 : 8), warnings.join("；"), ' text-anchor="end" font-size="10.5" fill="currentColor" opacity="0.55"'));
        }
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + " " + H + '" role="img">' +
            (hasTitle ? "<title>" + esc(spec.title) + "</title>" : "") + parts.join("") + "</svg>";
    }
    function renderPie(spec, warnings) {
        var W = 640;
        var rowH = 22;
        var legendRows = spec.items.length;
        var H = Math.max(400, 90 + legendRows * rowH + 30);
        var cx = 240, cy = Math.min(H / 2 + 10, 230), r = 140;
        var parts = [];
        if (spec.title) {
            parts.push(svgText(W / 2, 26, spec.title, ' text-anchor="middle" font-size="15" font-weight="600" fill="currentColor"'));
        }
        var angle = -Math.PI / 2;
        spec.items.forEach(function (it, i) {
            var frac = it.value / spec.total;
            var a0 = angle, a1 = angle + frac * Math.PI * 2;
            angle = a1;
            var color = PALETTE[i % PALETTE.length];
            if (frac <= 0)
                return;
            var large = frac > 0.5 ? 1 : 0;
            var x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
            var x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
            var d;
            if (frac >= 0.9999) {
                d = "M " + (cx - r) + " " + cy + " A " + r + " " + r + " 0 1 0 " + (cx + r) + " " + cy +
                    " A " + r + " " + r + " 0 1 0 " + (cx - r) + " " + cy + " Z";
            }
            else {
                d = "M " + cx + " " + cy + " L " + x0.toFixed(1) + " " + y0.toFixed(1) +
                    " A " + r + " " + r + " 0 " + large + " 1 " + x1.toFixed(1) + " " + y1.toFixed(1) + " Z";
            }
            var pct = frac * 100;
            parts.push('<path d="' + d + '" fill="' + color + '" stroke="#ffffff" stroke-width="1" stroke-opacity="0.85"><title>' +
                esc(it.name + "：" + fmtNum(it.value) + "（" + pct.toFixed(1) + "%）") + "</title></path>");
            if (pct >= 6) {
                var mid = (a0 + a1) / 2;
                var lr = r * 0.68;
                parts.push(svgText((cx + lr * Math.cos(mid)).toFixed(1), (cy + lr * Math.sin(mid) + 4).toFixed(1), pct.toFixed(pct >= 10 ? 0 : 1) + "%", ' text-anchor="middle" font-size="11" font-weight="600" fill="#ffffff"'));
            }
        });
        // 右侧图例：色块 + 名 + 值 + 百分比
        var lx = 440, ly0 = Math.max(70, cy - (legendRows * rowH) / 2);
        spec.items.forEach(function (it, i) {
            var y = ly0 + i * rowH;
            var pct = (it.value / spec.total) * 100;
            parts.push('<rect x="' + lx + '" y="' + (y - 11) + '" width="12" height="12" rx="2" fill="' + PALETTE[i % PALETTE.length] + '"/>');
            parts.push(svgText(lx + 16, y - 1, it.name + " " + fmtNum(it.value) + "（" + pct.toFixed(1) + "%）", ' font-size="11" fill="currentColor"'));
        });
        if (warnings.length) {
            parts.push(svgText(W - 10, H - 8, warnings.join("；"), ' text-anchor="end" font-size="10.5" fill="currentColor" opacity="0.55"'));
        }
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + W + " " + H + '" role="img">' +
            (spec.title ? "<title>" + esc(spec.title) + "</title>" : "") + parts.join("") + "</svg>";
    }
    function renderChart(code) {
        var parsed = normalizeSpec(code);
        if (!parsed.ok)
            return { error: parsed.error };
        try {
            var svg = parsed.spec.type === "pie"
                ? renderPie(parsed.spec, parsed.warnings)
                : renderCartesian(parsed.spec, parsed.warnings);
            return { svg: svg };
        }
        catch (e) {
            return { error: "图表渲染失败：" + (e && e.message || e) };
        }
    }
    var api = {
        renderChart: renderChart,
        normalizeSpec: normalizeSpec,
        MAX_CATEGORIES: MAX_CATEGORIES,
        MAX_SERIES: MAX_SERIES,
        MAX_SLICES: MAX_SLICES,
    };
    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    }
    else {
        global.ChartRender = api;
    }
})(typeof self !== "undefined" ? self : globalThis);
