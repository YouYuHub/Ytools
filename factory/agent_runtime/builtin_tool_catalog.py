"""Builtin tool names and model-facing schemas. Execution stays in builtin_tools."""
import copy


TODO_TOOL_NAME = "todo_write"
ASK_USER_TOOL_NAME = "ask_user"
WRITE_FILE_NAME = "write_file"
EDIT_FILE_NAME = "edit_file"
APPLY_PATCH_NAME = "apply_patch"
READ_FILE_NAME = "read_file"
SEARCH_FILES_NAME = "search_files"
LIST_FILES_NAME = "list_files"
READ_MEDIA_NAME = "read_media"
READ_DOCUMENT_NAME = "read_document"
SUB_AGENT_TOOL_NAME = "sub_agent"
RUN_COMMAND_NAME = "run_command"
POLL_COMMAND_NAME = "poll_command"
STOP_COMMAND_NAME = "stop_command"
WRITE_STDIN_NAME = "write_stdin"
COMMAND_TOOL_NAMES = frozenset({RUN_COMMAND_NAME, POLL_COMMAND_NAME, STOP_COMMAND_NAME, WRITE_STDIN_NAME})
BUILTIN_TOOL_SERVER_KEY = "__builtin__"


SELECTABLE_BUILTIN_TOOL_NAMES = (
    TODO_TOOL_NAME, ASK_USER_TOOL_NAME, WRITE_FILE_NAME, EDIT_FILE_NAME,
    READ_FILE_NAME, SEARCH_FILES_NAME, READ_MEDIA_NAME, READ_DOCUMENT_NAME,
    SUB_AGENT_TOOL_NAME, RUN_COMMAND_NAME, APPLY_PATCH_NAME,
)


TODO_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'todo_write',
              'description': '更新多步骤计划；每次提交完整 todos，最多 20 项，空数组清空。保持步骤 id，仅在计划或状态变化时调用。',
              'parameters': {'type': 'object',
                             'properties': {'todos': {'type': 'array',
                                                      'description': '完整计划；最多 20 项，[] 清空。',
                                                      'maxItems': 20,
                                                      'items': {'type': 'object',
                                                                'properties': {'id': {'type': 'string',
                                                                                      'description': '步骤标识，创建后保持不变；省略时自动分配。',
                                                                                      'maxLength': 64},
                                                                               'content': {'type': 'string',
                                                                                           'description': '步骤内容（一句话，祈使句，≤200 '
                                                                                                          '字符）',
                                                                                           'minLength': 1,
                                                                                           'maxLength': 200},
                                                                               'status': {'type': 'string',
                                                                                          'enum': ['pending',
                                                                                                   'in_progress',
                                                                                                   'done'],
                                                                                          'description': 'pending=待办 '
                                                                                                         'in_progress=进行中 '
                                                                                                         'done=已完成'}},
                                                                'required': ['content', 'status'],
                                                                'additionalProperties': False}}},
                             'required': ['todos'],
                             'additionalProperties': False}}}


ASK_USER_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'ask_user',
              'description': '向用户提出 1-5 个必要问题并暂停，回答作为下一条用户消息到达。需确认或缺少必要信息时使用；同轮其他工具会延后。',
              'parameters': {'type': 'object',
                             'properties': {'questions': {'type': 'array',
                                                          'description': '1-5 个问题；每题可自由输入或选择选项。',
                                                          'minItems': 1,
                                                          'maxItems': 5,
                                                          'items': {'type': 'object',
                                                                    'properties': {'question': {'type': 'string',
                                                                                                'description': '具体问题，最多 '
                                                                                                               '200 '
                                                                                                               '字符。',
                                                                                                'minLength': 1,
                                                                                                'maxLength': 200},
                                                                                   'options': {'type': 'array',
                                                                                               'description': '可选 '
                                                                                                              '2-6 '
                                                                                                              '项；用户也可自由输入。',
                                                                                               'maxItems': 6,
                                                                                               'items': {'type': 'string',
                                                                                                         'minLength': 1,
                                                                                                         'maxLength': 100}},
                                                                                   'multiple': {'type': 'boolean',
                                                                                                'description': '是否多选，默认 '
                                                                                                               'false。',
                                                                                                'default': False}},
                                                                    'required': ['question'],
                                                                    'additionalProperties': False}}},
                             'required': ['questions'],
                             'additionalProperties': False}}}


SUB_AGENT_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'sub_agent',
              'description': '派发独立子任务；同轮多个调用并发执行，只返回最终报告。子任务看不到对话历史，不能提问或再次派发；task 写全目标、路径、事实和限制。',
              'parameters': {'type': 'object',
                             'properties': {'task': {'type': 'string',
                                                     'maxLength': 20000,
                                                     'description': '自包含任务：目标、验收标准、绝对路径、已知事实、限制和所需证据。'},
                                            'todo': {'type': 'array',
                                                     'description': '可选初始计划，最多 20 项。',
                                                     'maxItems': 20,
                                                     'items': {'type': 'object',
                                                               'properties': {'id': {'type': 'string',
                                                                                     'description': '步骤标识，创建后保持不变；省略时自动分配。',
                                                                                     'maxLength': 64},
                                                                              'content': {'type': 'string',
                                                                                          'description': '步骤内容（一句话，≤200 '
                                                                                                         '字符）',
                                                                                          'minLength': 1,
                                                                                          'maxLength': 200},
                                                                              'status': {'type': 'string',
                                                                                         'enum': ['pending',
                                                                                                  'in_progress',
                                                                                                  'done'],
                                                                                         'description': 'pending=待办 '
                                                                                                        'in_progress=进行中 '
                                                                                                        'done=已完成'}},
                                                               'required': ['content', 'status'],
                                                               'additionalProperties': False}}},
                             'required': ['task'],
                             'additionalProperties': False}}}


WRITE_FILE_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'write_file',
              'description': '写入文本并自动建目录。mode=create 新建、overwrite 整文件覆盖、append 追加；mode 必填。局部修改用 edit_file。',
              'parameters': {'type': 'object',
                             'properties': {'path': {'type': 'string',
                                                     'description': '文件路径；相对路径基于会话工作目录',
                                                     'maxLength': 4096},
                                            'content': {'type': 'string',
                                                        'description': '要写入的完整内容（覆盖模式会清空原内容）',
                                                        'maxLength': 2097152},
                                            'encoding': {'type': 'string',
                                                         'description': '文件编码；新文件默认 utf-8，追加已有文件时自动识别',
                                                         'maxLength': 256},
                                            'mode': {'type': 'string',
                                                     'enum': ['create', 'overwrite', 'append'],
                                                     'description': '写入模式；新建用 create，覆盖已有文件须明确使用 overwrite',
                                                     'maxLength': 256},
                                            'expected_hash': {'type': 'string',
                                                              'description': '可选：read_file 返回的全文 '
                                                                             'content_hash，内容变化时拒绝写入',
                                                              'maxLength': 16,
                                                              'pattern': '^[0-9a-f]{16}$'}},
                             'required': ['path', 'content', 'mode'],
                             'additionalProperties': False}}}


EDIT_FILE_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'edit_file',
              'description': '按精确原文替换。单次用 old_string/new_string，多处用 edits，二者不可混用。匹配多处需加长原文或 '
                             'replace_all=true；任一项失败不写入。',
              'parameters': {'type': 'object',
                             'properties': {'path': {'type': 'string',
                                                     'description': '文件路径',
                                                     'maxLength': 4096},
                                            'old_string': {'type': 'string',
                                                           'description': '原文，须与文件内容一致；与 new_string 成对，不可与 '
                                                                          'edits 同用。',
                                                           'maxLength': 2097152,
                                                           'minLength': 1},
                                            'new_string': {'type': 'string',
                                                           'description': '替换文本，空串表示删除；与 old_string 成对。',
                                                           'maxLength': 2097152},
                                            'replace_all': {'type': 'boolean',
                                                            'description': '默认 false；多处替换在 edits 各项内设置。',
                                                            'default': False},
                                            'encoding': {'type': 'string',
                                                         'description': '指定文件编码（如 gbk）；留空按 BOM、UTF-8 '
                                                                        '和常见本地编码自动识别',
                                                         'default': '',
                                                         'maxLength': 256},
                                            'expected_hash': {'type': 'string',
                                                              'description': '可选：read_file 返回的全文 '
                                                                             'content_hash，内容变化时拒绝编辑',
                                                              'maxLength': 16,
                                                              'pattern': '^[0-9a-f]{16}$'},
                                            'edits': {'type': 'array',
                                                      'minItems': 1,
                                                      'maxItems': 100,
                                                      'description': '只传 edits：按顺序匹配前项修改后的文本；每项含 '
                                                                     'old_string/new_string，可选 replace_all。',
                                                      'items': {'type': 'object',
                                                                'properties': {'old_string': {'type': 'string',
                                                                                              'maxLength': 2097152,
                                                                                              'minLength': 1},
                                                                               'new_string': {'type': 'string',
                                                                                              'maxLength': 2097152},
                                                                               'replace_all': {'type': 'boolean',
                                                                                               'default': False}},
                                                                'required': ['old_string', 'new_string'],
                                                                'additionalProperties': False}}},
                             'required': ['path'],
                             'additionalProperties': False}}}


APPLY_PATCH_TOOL_DEFINITION = {'type': 'function', 'function': {
    'name': APPLY_PATCH_NAME,
    'description': '上下文补丁修改 1–20 个文本文件，单文件最多 8 MiB；支持 Add/Update/Delete File。修改前读原文，更新须有唯一上下文。先校验再提交；失败检查 files/rollback_complete，needs_attention 文件须先重读核对。',
    'parameters': {'type': 'object', 'properties': {
        'patch': {'type': 'string', 'maxLength': 2097152,
                  'description': '*** Begin Patch\n*** Update File: 路径\n@@\n-原行\n+新行\n*** End Patch。上下文行前加空格；新建每行加 +；删除仅写 Delete File 头。'}},
        'required': ['patch'], 'additionalProperties': False}}}


READ_FILE_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'read_file',
              'description': '分段读取文本，默认带行号。按 next_start_line 续读；单行超长时同时传原 start_line 和 next_char_offset。返回全文 '
                             'content_hash 可用于编辑校验。',
              'parameters': {'type': 'object',
                             'properties': {'path': {'type': 'string',
                                                     'description': '文件路径；相对路径基于会话工作目录；传入目录会报错',
                                                     'maxLength': 4096},
                                            'start_line': {'type': 'integer',
                                                           'description': '起始行（从 1 开始）',
                                                           'default': 1,
                                                           'minimum': 1},
                                            'limit': {'type': 'integer',
                                                      'minimum': 1,
                                                      'maximum': 2000,
                                                      'description': '本次最多读取的连续行数',
                                                      'default': 200},
                                            'encoding': {'type': 'string',
                                                         'description': '指定文件编码（如 utf-8、gbk）；留空按 BOM、UTF-8 '
                                                                        '和常见本地编码自动识别',
                                                         'default': '',
                                                         'maxLength': 256},
                                            'show_line_numbers': {'type': 'boolean',
                                                                  'description': '是否显示行号，默认 true；编辑时复制原文即可。',
                                                                  'default': True},
                                            'char_offset': {'type': 'integer',
                                                            'description': '仅单行超长时使用的字符偏移（按返回的 '
                                                                           'next_char_offset 续读）',
                                                            'default': 0,
                                                            'minimum': 0}},
                             'required': ['path'],
                             'additionalProperties': False}}}


SEARCH_FILES_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'search_files',
              'description': '跨文件搜索内容；只搜索内容，不枚举文件名。file_pattern 只筛选待搜索文件。默认普通文本，正则须 '
                             'is_regex=true；结果不完整时不能判断目标不存在。',
              'parameters': {'type': 'object',
                             'properties': {'pattern': {'type': 'string',
                                                        'description': '要查找的文本；is_regex=true 时为正则表达式',
                                                        'maxLength': 8192},
                                            'dir_path': {'type': 'string',
                                                         'description': '搜索根目录',
                                                         'default': '.',
                                                         'maxLength': 4096},
                                            'file_pattern': {'type': 'string',
                                                             'description': '筛选文件，如 *.py 或 src/**/*.py；不返回文件清单。',
                                                             'default': '',
                                                             'maxLength': 4096},
                                            'ignore_case': {'type': 'boolean',
                                                            'description': '是否忽略大小写',
                                                            'default': False},
                                            'is_regex': {'type': 'boolean',
                                                         'description': 'pattern 是否按正则解析，默认按普通文本匹配',
                                                         'default': False},
                                            'context_lines': {'type': 'integer',
                                                              'description': '每处匹配附带上下文行数（0-5）',
                                                              'default': 0,
                                                              'minimum': 0,
                                                              'maximum': 5},
                                            'max_results': {'type': 'integer',
                                                            'description': '最多返回的匹配区块数（1-200）',
                                                            'default': 50,
                                                            'minimum': 1,
                                                            'maximum': 200},
                                            'max_depth': {'type': 'integer',
                                                          'description': '递归深度，0=仅当前目录',
                                                          'default': 6,
                                                          'minimum': 0, 'maximum': 128},
                                            'max_scanned_files': {'type': 'integer',
                                                                  'minimum': 1,
                                                                  'maximum': 20000,
                                                                  'default': 3000,
                                                                  'description': '候选文件扫描上限；未扫完会标记部分结果'},
                                            'max_scan_seconds': {'type': 'number',
                                                                 'minimum': 0.1,
                                                                 'maximum': 120,
                                                                 'default': 10,
                                                                 'description': '扫描时间上限（秒）'},
                                            'max_file_mb': {'type': 'number',
                                                            'default': 2,
                                                            'description': '单文件上限 MiB，默认 2；0 使用硬上限 16。',
                                                            'minimum': 0,
                                                            'maximum': 16},
                                            'exclude_dirs': {'type': 'array',
                                                             'items': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 256},
                                                             'description': '追加排除的目录名，支持通配符；[] 不改变默认排除。',
                                                             'maxItems': 64},
                                            'no_default_excludes': {'type': 'boolean',
                                                                    'default': False,
                                                                    'description': 'true 关闭默认排除；仍应用 '
                                                                                   'exclude_dirs。默认 false。'}},
                             'required': ['pattern'],
                             'additionalProperties': False}}}


SEARCH_FILES_TOOL_DEFINITION['function']['description'] += ' 按 next_cursor 续读时保留原查询。'


SEARCH_FILES_TOOL_DEFINITION['function']['parameters']['properties']['cursor'] = {
    'type': 'string', 'maxLength': 256, 'description': '上次返回的 next_cursor；失效或文件改变后重新搜索。'}


SEARCH_FILES_TOOL_DEFINITION['function']['parameters']['properties'].update({
    'count_total': {'type': 'boolean', 'default': False,
                    'description': '默认按需扫描、尽快返回；true 在预算内扫描并统计全部命中。'},
    'respect_gitignore': {'type': 'boolean', 'default': False,
                         'description': '应用搜索根及下级 .gitignore；不读取根外规则，仍保留默认排除。'},
})


LIST_FILES_TOOL_DEFINITION = {'type': 'function', 'function': {
    'name': LIST_FILES_NAME,
    'description': '按名称列出文件路径，不读内容。pattern 为文件名或相对路径 glob；按 next_cursor 续读，保留原查询。',
    'parameters': {'type': 'object', 'properties': {
        **{name: copy.deepcopy(SEARCH_FILES_TOOL_DEFINITION['function']['parameters']['properties'][name])
           for name in ('dir_path', 'max_results', 'max_depth', 'max_scanned_files', 'max_scan_seconds',
                        'exclude_dirs', 'no_default_excludes', 'respect_gitignore', 'cursor')},
        'pattern': {'type': 'string', 'default': '*', 'minLength': 1, 'maxLength': 4096,
                    'description': '如 *.py、src/**/*.py；默认 * 列出范围内全部文件。'},
    }, 'additionalProperties': False}}}


READ_DOCUMENT_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'read_document',
              'description': '读取上传文件或本地文档。auto 自动选择；text 按 offset/limit 字符续读；table 按 next_row/next_sheet '
                             '续读。PDF 页段须同时指定 page_start/page_count；失败不改变读取范围。原生文件仅供下一次请求。',
              'parameters': {'type': 'object',
                             'properties': {'filename': {'type': 'string',
                                                         'description': '会话上传文件名，或本地相对/绝对路径。',
                                                         'maxLength': 4096},
                                            'mode': {'type': 'string',
                                                     'enum': ['auto', 'text', 'table', 'native'],
                                                     'description': 'auto 自动选择；text 字符分页；table 表格行分页；native '
                                                                    '原生文件。',
                                                     'maxLength': 256,
                                                     'default': 'auto'},
                                            'offset': {'type': 'integer',
                                                       'description': '文本起始字符，从 0 开始；auto 指定后使用文本模式。',
                                                       'minimum': 0},
                                            'limit': {'type': 'integer',
                                                      'description': '文本/表格字符预算，默认 10000，最大 20000；可能按上下文收紧。',
                                                      'minimum': 1,
                                                      'maximum': 20000,
                                                      'default': 10000},
                                            'sheet': {'type': 'string',
                                                      'description': '表格工作表名称；默认第一个，返回 sheets 列出全部工作表',
                                                      'maxLength': 256},
                                            'header_row': {'type': 'integer',
                                                           'minimum': 0,
                                                           'description': '表格表头行号，从 1 开始，默认 1；0 表示无表头，使用列 '
                                                                          'A/B 等且保留首行数据'},
                                            'start_row': {'type': 'integer',
                                                          'minimum': 1,
                                                          'description': '表格数据起始行号，从 1 开始；默认表头之后，续读用 '
                                                                         'next_row；与 offset 互斥'},
                                            'row_limit': {'type': 'integer',
                                                          'minimum': 1,
                                                          'maximum': 200,
                                                          'description': '表格最多读取的数据行数，默认 100，最多 200；同时受 '
                                                                         'limit 字符预算约束'},
                                            'page_start': {'type': 'integer',
                                                           'description': '原生 PDF 起始页码（从 1 开始）；与 page_count '
                                                                          '同时指定',
                                                           'minimum': 1},
                                            'page_count': {'type': 'integer',
                                                           'description': '原生 PDF 读取页数（1–20）；与 page_start '
                                                                          '同时指定',
                                                           'minimum': 1,
                                                           'maximum': 20}},
                             'required': ['filename'],
                             'additionalProperties': False}}}


RUN_COMMAND_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'run_command',
              'description': '运行命令，每次新建 shell，默认短等 1000ms；未结束返回 job_id。'
                             '用 poll_command(job_id) 续取输出，stop_command(job_id) 停止。'
                             '需输入设 interactive=true，需终端再加 tty=true；独立服务用 background=true。文件读写优先专用工具。',
              'parameters': {'type': 'object',
                             'properties': {'command': {'type': 'string',
                                                        'description': '目标 shell 的命令原文；语法须与 shell 一致。cmd 用 '
                                                                       '&& 或 & 串联。',
                                                        'maxLength': 200000},
                                            'shell': {'type': 'string',
                                                      'enum': ['auto',
                                                               'cmd',
                                                               'powershell',
                                                               'pwsh',
                                                               'bash',
                                                               'sh',
                                                               'zsh'],
                                                      'description': '默认 auto：Windows PowerShell（不可用时 '
                                                                     'cmd），其他平台 sh。',
                                                      'default': 'auto',
                                                      'maxLength': 256},
                                            'work_dir': {'type': 'string',
                                                         'description': '工作目录；默认会话目录。每次调用独立，cd 不会影响下一次调用。',
                                                         'default': '',
                                                         'maxLength': 4096},
                                            'timeout_seconds': {'type': 'number',
                                                                'minimum': 1,
                                                                'maximum': 1800,
                                                                'description': '仅前台有效，默认 120 秒，范围 '
                                                                               '1-1800；含预处理和启动，超时终止进程树。',
                                                                'default': 120.0},
                                            'background': {'type': 'boolean',
                                                           'description': 'true 启动后台任务并返回 job_id；不受 '
                                                                          'timeout_seconds 限制，结束后仍可查询日志。',
                                                           'default': False},
                                            'yield_time_ms': {'type': 'integer', 'minimum': 0, 'maximum': 30000,
                                                              'description': '启动后最多等待多久，默认 1000ms，0 立即返回。与 background 互斥；总超时仍由 timeout_seconds 控制。'},
                                            'interactive': {'type': 'boolean', 'default': False,
                                                            'description': '保持 UTF-8 stdin 可写；用 write_stdin 继续输入。与 background 互斥，任务结束时停止。'},
                                            'tty': {'type': 'boolean', 'default': False,
                                                    'description': '启用伪终端（120×30），自动启用 interactive。用于 REPL/需 TTY 的程序；输出可能含 ANSI 控制码。'}},
                             'required': ['command'],
                             'additionalProperties': False}}}


POLL_COMMAND_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'poll_command',
              'description': '续取命令的新增输出；通常只传 job_id，自动记住当前任务/代理的读取位置。'
                             'status=running 或 has_more=true 时继续查询；退出码确认执行结果。',
              'parameters': {'type': 'object',
                             'properties': {'job_id': {'type': 'string',
                                                       'description': 'run_command 返回的 job_id',
                                                       'maxLength': 256,
                                                       'pattern': '^bg_[A-Za-z0-9_]+$'},
                                            'tail_lines': {'type': 'integer',
                                                           'minimum': 1,
                                                           'maximum': 200,
                                                           'description': '可选尾部预览：最近 1-200 行，不推进读取位置；与 cursor/wait_seconds/encoding 互斥。'},
                                            'cursor': {'type': 'string', 'maxLength': 512,
                                                       'description': '通常省略。空字符串从头重读；传 next_cursor 从指定位置读，之后自动续读。'},
                                            'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 30, 'default': 1,
                                                             'description': '无新增输出时最多等待秒数，默认 1；0 立即查询。'},
                                            'encoding': {'type': 'string', 'maxLength': 64,
                                                         'description': '默认自动识别；乱码时指定如 gbk，后续沿用；空字符串恢复自动识别。'}},
                             'required': ['job_id'],
                             'additionalProperties': False}}}


STOP_COMMAND_TOOL_DEFINITION = {'type': 'function',
 'function': {'name': 'stop_command',
              'description': '停止当前会话命令的进程树并保留日志；任务已结束时返回其当前状态。',
              'parameters': {'type': 'object',
                             'properties': {'job_id': {'type': 'string',
                                                       'description': 'run_command 返回的 job_id',
                                                       'maxLength': 256,
                                                       'pattern': '^bg_[A-Za-z0-9_]+$'}},
                             'required': ['job_id'],
                             'additionalProperties': False}}}


WRITE_STDIN_TOOL_DEFINITION = {'type': 'function', 'function': {
    'name': WRITE_STDIN_NAME,
    'description': '向当前任务/代理的交互命令输入 UTF-8 文本，自动续取输出；换行须显式写入 chars。空输入等同 poll_command。',
    'parameters': {'type': 'object', 'properties': {
        'job_id': {'type': 'string', 'maxLength': 256, 'pattern': '^bg_[A-Za-z0-9_]+$',
                   'description': 'run_command 返回的 job_id；发送输入须已启用 interactive'},
        'chars': {'type': 'string', 'default': '', 'maxLength': 16384,
                  'description': '原样发送；提交一行加换行。最多 16384 字符/64KiB。'},
        'eof': {'type': 'boolean', 'default': False,
                'description': '发送完 chars 后关闭管道 stdin；PTY 不支持此选项。'},
        'cursor': {'type': 'string', 'maxLength': 512,
                   'description': '通常省略，与 poll_command 共用读取位置；空字符串重读，next_cursor 指定位置。'},
        'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 30, 'default': 1,
                         'description': '发送后无新增输出时最多等待秒数，默认 1，范围 0-30。'},
        'request_id': {'type': 'string', 'maxLength': 128, 'minLength': 1,
                       'description': '可选去重标识；重试同一输入沿用它。只传 job_id/request_id 查回执，不重发；queued/writing 时先查询，勿换标识重发。'},
    }, 'required': ['job_id'], 'additionalProperties': False}}}
