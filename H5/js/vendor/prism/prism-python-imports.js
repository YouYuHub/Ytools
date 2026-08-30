/**
 * Prism python 扩展：补两处默认 grammar 不覆盖的类名着色
 * 1) `from fastapi import FastAPI, Depends` / `import FastAPI` 引入的类名 → class-name
 * 2) 代码中的 PascalCase 标识符（except HTTPException、class X(Exception): 基类、
 *    类型注解 x: FastAPI、isinstance 等）→ class-name（alias 着色）
 * 需在 prism-python 之后加载。
 */
(function (Prism) {
  if (!Prism || !Prism.languages || !Prism.languages.python) return;
  var g = Prism.languages.python;

  g["class-name"] = [
    g["class-name"],
    {
      // `from ... import ` / `import ` 之后的大写开头名字（可逗号分隔多个）
      pattern: /(\b(?:from\s+[.\w]+\s+import\s+|import\s+))([A-Z]\w*(?:\s*,\s*[A-Z]\w*)*)/,
      lookbehind: true,
      inside: {
        punctuation: /,/
      }
    }
  ];

  // PascalCase 兜底：插在 builtin 之后（True/False/None 在 boolean 规则中，
  // 用负向前瞻排除，保持原有颜色不变）
  Prism.languages.insertBefore("python", "boolean", {
    "pascalcase-class": {
      pattern: /\b(?!(?:True|False|None)\b)[A-Z][A-Za-z0-9_]*\b/,
      alias: "class-name"
    }
  });
})(window.Prism);
