/**
 * Prism markup 扩展：恢复 <script>/<style> 块内嵌语法高亮
 * （1.29 的 markup 组件已移除该能力，仅保留 addAttribute API）。
 * <script> 内容 → javascript，<style> 内容 → css；同时启用 style="..." 属性的内联 css 高亮。
 * 需在 prism-markup 之后加载（依赖 javascript / css 语法，请确保已先加载）。
 */
(function (Prism) {
  if (!Prism || !Prism.languages || !Prism.languages.markup) return;

  // 插到 cdata（tag 之前），让 script/style 块整体先于 tag 规则匹配
  Prism.languages.insertBefore("markup", "cdata", {
    style: {
      pattern: /<style(?:\s[^>]*)?>[\s\S]*?<\/style>/i,
      inside: {
        "style-tag": {
          pattern: /<\/?style[^>]*>/i,
          inside: Prism.languages.markup.tag.inside
        },
        "language-css": {
          pattern: /[\s\S]+/,
          inside: Prism.languages.css
        }
      }
    },
    script: {
      pattern: /<script(?:\s[^>]*)?>[\s\S]*?<\/script>/i,
      inside: {
        "script-tag": {
          pattern: /<\/?script[^>]*>/i,
          inside: Prism.languages.markup.tag.inside
        },
        "language-javascript": {
          pattern: /[\s\S]+/,
          inside: Prism.languages.javascript
        }
      }
    }
  });

  // 内联 style="..." 属性内的 css 高亮（官方 addAttribute API）
  if (Prism.languages.markup.tag && Prism.languages.markup.tag.addAttribute) {
    Prism.languages.markup.tag.addAttribute("style", "css");
  }
})(window.Prism);
