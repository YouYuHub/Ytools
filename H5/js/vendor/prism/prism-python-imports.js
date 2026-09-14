/**
 * Prism python 扩展：对齐 VS Code（Light+ / Dark Modern）的 Python 着色语义。
 * 在 prism-python.min.js 之后加载；颜色见 _prism.scss 的 python 调色板：
 * 1) docstring：三引号字符串整体着色（浅色蓝 / 深色绿，同编辑器 docstring 观感）
 * 2) 内建类型/内建函数（str/set/list/dict/enumerate/print…）→ builtin（VSCode 青绿），
 *    并移除 python 自带的 py2 遗留 builtin 长名单
 * 3) None → constant（蓝）；True/False 保留 boolean 规则（同为蓝）
 * 4) 模块级全大写常量（UPPER_CASE）→ caps-constant（蓝）
 * 5) 类名：import 引入名 + PascalCase 兜底 → class-name（class 定义着色由原
 *    class-name 规则负责）
 * 按 VS Code 语义不染色：变量/参数/赋值目标/for 迭代变量 = 默认前景色。
 *
 * 实现注意（Prism 源码行为）：
 * - insertBefore 会整表重建 grammar 对象，因此禁止缓存旧引用后再赋值/删除；
 *   全部通过 Prism.languages.insertBefore 或每次现取 Prism.languages.python。
 * - 对已存在键（class-name）直接赋值不改变键序（ECMAScript 规范），import 规则
 *   借此保持在 keyword 之前，lookbehind 才能匹配到未被 keyword 切走的 `import `。
 * - f-string 插值的 rest 引用（string-interpolation.inside…rest）由 insertBefore
 *   内置 DFS 自动更新到最新对象，内层插值同样吃到新规则。
 */
(function (Prism) {
  if (!Prism || !Prism.languages || !Prism.languages.python) return;

  // 1) docstring：插到 triple-quoted-string 之前使其优先命中；f-string 三引号
  //    仍走 string-interpolation（插值语义优先，可接受）
  Prism.languages.insertBefore("python", "triple-quoted-string", {
    "py-docstring": {
      pattern: /(?:[rub]|br|rb)?("""|''')[\s\S]*?\1/i,
      greedy: true
    }
  });

  // 2) None / 内建类型/函数：插到 boolean 之前（= keyword 之后，不影响关键字；
  //    comment/string/decorator/function 等在其之前，裸标识符才轮到这里）
  var PY_BUILTIN_TYPES =
    "bool|int|float|complex|str|bytes|bytearray|list|tuple|dict|set|frozenset|" +
    "type|object|slice|range|memoryview|super|property|staticmethod|classmethod";
  var PY_BUILTIN_FUNCS =
    "__import__|abs|all|any|ascii|bin|callable|chr|delattr|dir|divmod|enumerate|" +
    "eval|exec|filter|format|getattr|globals|hasattr|hash|help|hex|id|input|" +
    "isinstance|issubclass|iter|len|locals|map|max|min|next|oct|open|ord|pow|" +
    "print|repr|reversed|round|setattr|sorted|sum|vars|zip";
  Prism.languages.insertBefore("python", "boolean", {
    "py-none": {
      pattern: /\bNone\b/,
      alias: "constant"
    },
    "py-builtin-type": {
      pattern: new RegExp("\\b(?:" + PY_BUILTIN_TYPES + "|" + PY_BUILTIN_FUNCS + ")\\b"),
      alias: "builtin"
    }
  });

  // 3) 全大写常量：插到 class-name 之前（= function 之后，不干扰 def 函数名的
  //    lookbehind 匹配；全大写 def 名被 caps 先切走的场景罕见，可接受）
  Prism.languages.insertBefore("python", "class-name", {
    "caps-constant": {
      pattern: /\b[A-Z][A-Z0-9_]{2,}\b/,
      alias: "constant"
    }
  });

  // 4) PascalCase 类名兜底：沿用旧版键名 pascalcase-class，插到 boolean 之前
  //    （位于 decorator/keyword/number/operator 之后，裸词才轮到这里；全大写
  //    常量已由 caps-constant 命中，负前瞻排除避免双重 token）
  Prism.languages.insertBefore("python", "boolean", {
    "pascalcase-class": {
      pattern: /\b(?!None\b|True\b|False\b)(?![A-Z][A-Z0-9_]+\b)[A-Z][a-z0-9_]*\w*\b/,
      alias: "class-name"
    }
  });

  // 5) import 引入名：替换已存在的 class-name 键（直接赋值保序，仍在 keyword 之前）
  Prism.languages.python["class-name"] = [
    Prism.languages.python["class-name"],
    {
      // from ... import Foo, Bar / import Foo（大写开头引入名）
      pattern: /(\b(?:from\s+[.\w]+\s+import\s+|import\s+))([A-Z]\w*(?:\s*,\s*[A-Z]\w*)*)/,
      lookbehind: true,
      inside: {
        punctuation: /,/
      }
    }
  ];

  // 6) 移除 python 自带的 builtin 长名单（含 py2 遗留名；新名单见 py-builtin-type，
  //    覆盖 int/str/list… 与 print/enumerate… 全套 VSCode 内建口径）
  delete Prism.languages.python.builtin;
})(window.Prism);
