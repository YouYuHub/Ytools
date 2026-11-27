"""内联代码（python -c / node -e）引号嵌套回归测试。

覆盖：C 风格 \\" 反转义、PS ""/反引号/单引号转义、$ 展开、原生边界剥引号、
cmd 单引号静默、; 链式定界。含纯函数单测与真实执行矩阵（严格断言精确输出）。
"""

import unittest

from factory.agent_runtime import builtin_tools as bt


class UnescapeInlineCodeTests(unittest.TestCase):
    def test_c_style_double_quote_cmd(self):
        self.assertEqual(
            bt._unescape_inline_code('print(\\"hello\\")', 'cmd', '"'),
            'print("hello")')

    def test_c_style_double_quote_ps(self):
        self.assertEqual(
            bt._unescape_inline_code('print(\\"a\\", \\"b\\")', 'powershell', '"'),
            'print("a", "b")')

    def test_backslashes_kept_verbatim(self):
        # 路径/正则的反斜杠原样保留（只处理引号前的转义位）
        self.assertEqual(
            bt._unescape_inline_code('print("C:\\\\temp")', 'cmd', '"'),
            'print("C:\\\\temp")')
        self.assertEqual(
            bt._unescape_inline_code("re.search('\\\\d+', s)", 'powershell', '"'),
            "re.search('\\\\d+', s)")

    def test_escaped_backslash_before_quote(self):
        # \\\"：奇数反斜杠折半（bash/MSVCRT/C 一致语义），偶数个原样保留
        self.assertEqual(
            bt._unescape_inline_code('print(\\\\\\"hi\\\\\\")', 'cmd', '"'),
            'print(\\"hi\\")')
        self.assertEqual(
            bt._unescape_inline_code('print(\\\\"hi\\\\")', 'cmd', '"'),
            'print(\\\\"hi\\\\")')

    def test_ps_doubled_quote_collapses_when_verbatim_broken(self):
        self.assertEqual(
            bt._unescape_inline_code('print(""hi"")', 'powershell', '"'),
            'print("hi")')

    def test_empty_string_preserved(self):
        # 空字符串：折叠会破坏解析，原样保留
        code = 'print({"k": 1}.get("k", ""))'
        self.assertEqual(bt._unescape_inline_code(code, 'powershell', '"'), code)
        self.assertEqual(bt._unescape_inline_code(code, 'cmd', '"'), code)

    def test_adjacent_strings_preserved(self):
        # 相邻串拼接：原样能解析，保持今日行为
        code = 'print("a" "")'
        self.assertEqual(bt._unescape_inline_code(code, 'cmd', '"'), code)

    def test_bare_nested_kept(self):
        # 贪心嵌套定位进来的孤立引号就是代码本身
        code = 'print("hi")'
        self.assertEqual(bt._unescape_inline_code(code, 'cmd', '"'), code)

    def test_node_double_quotes_verbatim(self):
        # node："" 一律原样（JS 空字符串优先）
        code = 'console.log(""hi"")'
        self.assertEqual(bt._unescape_inline_code(code, 'cmd', '"', 'node'), code)
        # 但 C 风格 \" 仍还原
        self.assertEqual(
            bt._unescape_inline_code('console.log(\\"hi\\")', 'cmd', '"', 'node'),
            'console.log("hi")')

    def test_ps_backtick_quote(self):
        self.assertEqual(
            bt._unescape_inline_code('print(`"hi`")', 'powershell', '"'),
            'print("hi")')

    def test_ps_backtick_unknown_kept(self):
        self.assertEqual(
            bt._unescape_inline_code('a`qb', 'powershell', '"'), 'a`qb')

    def test_cmd_backtick_untouched(self):
        # cmd 里反引号是普通字符
        self.assertEqual(
            bt._unescape_inline_code('print(`"hi`")', 'cmd', '"'),
            'print(`"hi`")')

    def test_ps_single_quote_doubling(self):
        self.assertEqual(
            bt._unescape_inline_code("print(''hi'')", 'powershell', "'"),
            "print('hi')")

    def test_cmd_single_quote_verbatim(self):
        code = "print('hi')"
        self.assertEqual(bt._unescape_inline_code(code, 'cmd', "'"), code)


class NeedsScriptTriggerTests(unittest.TestCase):
    def test_ps_single_with_dquote(self):
        self.assertTrue(bt._inline_code_needs_script(
            'print("x")', False, 'powershell', "'"))

    def test_ps_single_without_dquote(self):
        self.assertFalse(bt._inline_code_needs_script(
            'print(1)', False, 'powershell', "'"))

    def test_ps_double_with_dollar(self):
        self.assertTrue(bt._inline_code_needs_script(
            "print('$x')", False, 'powershell', '"'))
        self.assertTrue(bt._inline_code_needs_script(
            'print($(x))', False, 'pwsh', '"'))

    def test_ps_double_lone_dollar_no_rewrite(self):
        self.assertFalse(bt._inline_code_needs_script(
            "print('cost: $')", False, 'powershell', '"'))

    def test_ps_percent_still_native(self):
        self.assertFalse(bt._inline_code_needs_script(
            "print('100%')", False, 'powershell', '"'))

    def test_cmd_single_always_rewrites(self):
        self.assertTrue(bt._inline_code_needs_script('print(1)', False, 'cmd', "'"))

    def test_cmd_dollar_stays_native(self):
        self.assertFalse(bt._inline_code_needs_script(
            "print('$x')", False, 'cmd', '"'))


class RewriteContentTests(unittest.TestCase):
    def _rewritten_file_content(self, command, shell):
        new_cmd, notes = bt._rewrite_inline_code_to_script(command, shell)
        self.assertNotEqual(new_cmd, command)
        self.assertTrue(notes)
        # 新命令形如 `<interp> <脚本路径>[尾部]`：提取引号/首个 token 路径
        rest = new_cmd.split(None, 1)[1]
        if rest.startswith('"'):
            path = rest[1:rest.index('"', 1)]
        else:
            path = rest.split(None, 1)[0].split(';')[0].strip()
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_rewrite_restores_c_escapes(self):
        content = self._rewritten_file_content(
            'python -c "print(\\"hello\\")"', 'powershell')
        self.assertEqual(content, 'print("hello")\n')

    def test_rewrite_keeps_trailing_chain(self):
        new_cmd, _ = bt._rewrite_inline_code_to_script(
            'python -c "print(\\"x\\")"; Write-Output done', 'powershell')
        self.assertIn('; Write-Output done', new_cmd)

    def test_rewrite_rejects_multiline_semicolon(self):
        # 多行 + ; 仍拒绝（定界歧义）
        with self.assertRaisesRegex(ValueError, '命令未执行'):
            bt._rewrite_inline_code_to_script(
                'python -c "print(1)\nprint(2)" ; echo done', 'powershell')

    def test_rewrite_ignores_semicolon_cmd(self):
        # cmd 里 ; 不是分隔符：单行不断言改写，仅不断言崩溃即可覆盖分支
        new_cmd, _ = bt._rewrite_inline_code_to_script(
            'python -c "print(1)" ; echo done', 'cmd')
        self.assertEqual(new_cmd, 'python -c "print(1)" ; echo done')


# 真实执行矩阵：(shell, command, expected_stdout_stripped)
EXEC_MATRIX = [
    ('powershell', 'python -c "print(\\"hello\\")"', 'hello'),
    ('powershell', 'python -c "print(\\"a\\", \\"b\\")"', 'a b'),
    ('powershell', 'python -c "print(\'single\')"', 'single'),
    ('powershell', 'python -c \'print("double")\'', 'double'),
    ('powershell', 'python -c "d = {\\"k\\": \\"v\\"}; print(d[\\"k\\"])"', 'v'),
    ('powershell', 'python -c "import json; print(json.dumps({\\"a\\": 1}))"', '{"a": 1}'),
    ('powershell', 'python -c "print(""hi"")"', 'hi'),
    ('powershell', 'python -c "x=5; print(f\\"val={x}\\")"', 'val=5'),
    ('powershell', 'python -c "print("hi")"', 'hi'),
    ('powershell', 'python -c "print({\\"k\\": 1}.get(\\"k\\", \\"\\"))"', '1'),
    ('powershell', 'python -c "print(`"hi`")"', 'hi'),
    ('powershell', 'python -c "print(\'$x\')"', '$x'),
    ('powershell', 'python -c "print(\\"a\\" \\"\\")"', 'a'),
    ('powershell', 'python -c \'print(\'\'hi\'\')\'', 'hi'),
    ('powershell', 'node -e \'console.log("hi")\'', 'hi'),
    ('powershell', 'node -e "console.log(\'hi\')"', 'hi'),
    ('powershell', 'python -c "import sys; print(sys.platform)"', 'win32'),
    ('powershell', 'python -c "import re; print(re.search(\'\\\\d+\', \'abc123\').group())"', '123'),
    ('powershell', 'python -c "print(\\"C:\\\\temp\\")"', 'C:\\temp'),
    ('powershell', 'python -c "print(\\"x\\")"; Write-Output done', 'x\ndone'),
    ('powershell', 'python -c "import json; print(json.loads(\\"{\\\\\\"k\\\\\\": 1}\\")[\\"k\\"])"', '1'),
    ('cmd', 'python -c "print(\\"hello\\")"', 'hello'),
    ('cmd', 'python -c "print(\\"a\\", \\"b\\")"', 'a b'),
    ('cmd', 'python -c "print(\'single\')"', 'single'),
    ('cmd', 'python -c "d = {\\"k\\": \\"v\\"}; print(d[\\"k\\"])"', 'v'),
    ('cmd', 'python -c "import json; print(json.dumps({\\"a\\": 1}))"', '{"a": 1}'),
    ('cmd', 'python -c "print(\'%d\' % 5)"', '5'),
    ('cmd', 'python -c "print(\\"x\\")" & echo done', 'x\ndone'),
    ('cmd', 'python -c "print("hi")"', 'hi'),
    ('cmd', 'python -c "print({\\"k\\": 1}.get(\\"k\\", \\"\\"))"', '1'),
    ('cmd', 'python -c \'print(1)\'', '1'),
    ('cmd', 'node -e \'console.log(1+2)\'', '3'),
    ('cmd', 'python -c "print(\'$x\')"', '$x'),
    ('cmd', 'python -c "print(\\"a\\" \\"\\")"', 'a'),
    ('cmd', 'python -c "import re; print(re.search(\'\\\\d+\', \'abc123\').group())"', '123'),
    ('cmd', 'python -c "print(\\"x\\")" && echo done', 'x\ndone'),
    ('cmd', 'python -c "print(\\"%d\\" % 5)"', '5'),
    ('cmd', 'python -c "import json; print(json.loads(\'{"a": 1}\')[\\"a\\"])"', '1'),
    ('cmd', 'python -c "import json; print(json.loads(\\"{\\\\\\"k\\\\\\": 1}\\")[\\"k\\"])"', '1'),
]


class InlineCodeExecMatrixTests(unittest.TestCase):
    def test_exec_matrix(self):
        for shell, command, expect in EXEC_MATRIX:
            with self.subTest(shell=shell, command=command):
                r = bt.try_execute_builtin_command_tool('run_command', {
                    'command': command, 'shell': shell,
                    'timeout_seconds': 60, '_session_id': 'quote-matrix-test'})
                out = (r.get('output', '') or '').strip().replace('\r\n', '\n')
                self.assertEqual(out, expect)
                self.assertIn('exit=0', r.get('message', ''))


if __name__ == '__main__':
    unittest.main()
