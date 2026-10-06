# 工具文本编码

## 乱码出现的位置

终端涉及文件字节 → shell 文本 → 输出字节 → 后端文本四个步骤。PowerShell 5.1 的文件读取默认编码与项目 UTF-8 不一致时，中文会先被读错；仅调整终端输出解码无法恢复原文。已经包含 `�` 的输出说明信息可能丢失，不应作为修改文件的依据。

## 当前策略

- PowerShell / pwsh：前台直接执行和 Windows 中转脚本均设置控制台输入、输出及 `$OutputEncoding` 为 UTF-8。`Get-Content`、`Select-String`、`Set-Content`、`Add-Content`、`Out-File` 的子会话默认编码为 UTF-8；显式 `-Encoding` 优先。不修改系统设置或用户 profile。PowerShell 5.1 写 UTF-8 文件可能带 BOM。
- 终端输出：BOM 优先识别 UTF-8、UTF-16、UTF-32；其余按行先严格尝试 UTF-8，再尝试系统 ANSI/OEM/控制台代码页和常见中文编码。支持不同行分别使用 UTF-8 与 GBK。合法 UTF-8 不再依据汉字“生僻程度”重新猜成 GBK。
- 文本及 CSV 文档：采用统一 BOM/UTF-8/常见中文编码回退，CSV 先解码再解析，不通过跳过坏行掩盖编码错误。带 Unicode BOM 的文本不再因含空字节被误判为二进制。
- `.doc` 外部转换器：先收集原始字节再解码，不再以 `errors=ignore` 静默丢字。PDF、DOCX、Excel 的正文由各自格式解析库提取，不对已解析字符串猜测转码。
- 网页抓取及两种搜索引擎：使用统一解码策略，参考 HTTP/meta 声明；BOM 和严格合法 UTF-8 优先。`fetch_url.encoding` 显式指定时优先采用该值。
- 图片/音频/视频转换器的错误输出也走文本解码；原始媒体与文档 base64 不参与文本转码。
- `read_file` 的显式编码仍严格校验，错误编码直接报错；编辑工具仍坚持无损解码后才修改文件。

## 旧编码文件

对无 BOM 的系统 ANSI 文件，PowerShell 5.1 可显式使用 `Get-Content -Encoding Default` 或 `Select-String -Encoding Default`。`Default` 指系统 ANSI 编码，不能保证在所有 Windows 上都是 GBK。固定 GBK 文件可用 `read_file` 的 `encoding="gbk"` 核验；网页可用 `fetch_url.encoding` 指定真实编码。

无 BOM 编码无法普遍准确识别，尤其是同一行混合编码、GBK/Big5 均能合法解码、源文件已经损坏等情况。程序不尝试修改历史 JSONL 中已有的乱码，也不将 base64 当文本解码。对替换字符应核验原始文件或字节，而不是反复转码猜测还原。

## 验证

`test/test_text_encoding.py` 覆盖 UTF-8/BOM/GB18030、CSV、Word 转换器输出、网页声明冲突、显式编码、PowerShell 中转脚本及真实 Windows PowerShell 中文路径搜索。现有终端测试覆盖混合 UTF-8/GBK 输出和替换字符提示。
