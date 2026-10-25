# 项目维护与依赖更新

核对日期：2026-10-24。以下为当前维护约定；带日期的报告与测试数量保留为历史快照。

## 当前实现与使用入口

- 服务启动后访问 `/ui/` 登录；`/` 返回 JSON，`/docs` 仅管理员可读。账号、旧数据归属和 Linux HTTPS 部署见 [安全实施说明](security_implementation.md)。登录页面与 API 保持同源，直接打开本地文件只用于开发页面检查。
- 聊天中的静态 SVG 图片预览交给 DOMPurify 的 SVG 白名单净化；包含 SMIL/CSS 动画、样式或 `<use>` 等本地引用的 SVG 使用 `data:image/svg+xml` 图片环境显示，以保留动画并隔离脚本。若净化器不可用，预览显示提示，源码仍可在代码视图查看和复制。动画 SVG 的「复制图片」导出单帧 PNG 快照。`H5/js/vendor/dompurify/` 保留运行文件和许可证。
- `/chat_history/file` 与 `/chat_history/meta` 只打开已存在的会话。未知 ID 返回 404，不创建历史文件；只读查询不进入长期管理器缓存。可写管理器缓存最多保留 128 个空闲实例，活跃轮次与编辑状态不会被淘汰。
- 历史先加载最近完整轮次，早期区间及大工具结果按需读取，保留全量问题导航；大页面使用带版本的 Worker。DOM 继续使用可视窗口与占位，详见 [历史按需加载](history_paging.md)。检查点恢复和旧后端保留完整历史兼容路径。
- 侧栏主题入口与 `/主题` 共用选择弹窗；侧栏旧菜单已移除。
- 主任务状态区分执行阶段与网络连接；断线先确认后台状态再附接。流式渲染管线与内置工具静态定义已独立成模块，职责与验证见 [任务状态与模块拆分](page_maintenance_followup.md)。
- 侧边栏通过本地事件和一次批量状态查询标记前后台运行会话；等待/未知状态不持续转圈，结束后清除。轮询随页面可见性暂停，详见 [会话运行标记](session_activity.md)。
- 手动压缩完成时同步卡片、历史窗口缓存与任务状态；重连复用运行块，过程标题由内容重建，详见 [压缩收尾修复](compaction_lifecycle.md)。
- 多文件补丁沿现有 `file_diff` 通道展示逐文件差异与汇总；异常回滚明确标记需核对文件，详见 [文件差异 §10](file_diff.md#10-多文件补丁展示与失败回执2026-10-23)。
- MCP `sys_tools_server.py` 仅保留网页抓取和搜索。文件与终端工具由 `factory/agent_runtime/builtin_tools.py` 提供。
- 工具支持按需内容扫描与可选 `.gitignore`、文件枚举/搜索游标、多文件补丁、大文本压缩快照回退、终端短等待/增量输出及交互 stdin/PTY。用法见 [工具简表](builtin_tools.md)，实施及验证见 [首轮记录](tool_improvement_plan.md)、[进一步优化](tool_followup_plan.md)。
- 模型终端默认短等，查询只传 job_id 自动续读，输入只传 job_id/chars；按需重放、尾部采样与回执查询见 [终端说明](terminal_management.md)。

## 前端资源版本

修改前端资源后，在项目根目录执行（依赖已安装且只改 JS 时仅需最后一步）：

```powershell
npm.cmd ci --prefix H5
npm.cmd run build:css --prefix H5
python -B H5/version_assets.py
```

版本脚本按文件内容哈希更新 `H5/index.html`、`login.html`、`account.html` 中的 JS/CSS URL，重复运行结果相同。身份命名空间脚本和主题脚本同步执行，其他经典脚本 `defer` 并保持声明顺序。带身份数据的页面及 API 禁止缓存，版本化公共资源可长期缓存。

`watch:css` 只生成 CSS，不更新页面哈希。发布前同时核对 `python -B -m pytest test/test_frontend_asset_versions.py -q`；功能单元测试为 `npm.cmd test --prefix H5`，浏览器交互脚本另行运行。

## Python 依赖

日常检查与完整发布优先使用 [统一验收入口](release_validation.md)：`python scripts/release_check.py --profile quick` 或 `--profile release`；完整发布还检查浏览器实际运行、恢复用例与源码未改变。

`requirements.txt` 是可编辑的直接依赖清单；`requirements.lock.txt` 是以 Python 3.10 为最低目标、通用平台解析出的精确版本。部署优先用锁文件。升级时执行：

```powershell
uv pip compile requirements.txt --universal --python-version 3.10 -o requirements.lock.txt
python -m pytest test -q
cd H5
npm test
```

升级或改动 `H5/package.json` 后运行 `npm install` 更新 `H5/package-lock.json`；正式安装使用 `npm ci`。锁文件的可安装性还需在目标系统和 Python 版本上验证。

## 数据与排查产物

`.gitignore` 排除密钥配置、历史、上传、临时文件、日志及依赖目录。现有 `history_files/` 和 `tmp/` 没有自动删除；清理前应先确认备份和外部脚本引用。原分析报告保留在项目根目录的 [PROJECT_ANALYSIS_2026-10-18.md](../PROJECT_ANALYSIS_2026-10-18.md)。

## 文档维护

- 当前入口：根 [README](../README.md)、[项目结构](../project_structure.md)、[前端说明](../H5/README.md)。参数及接口分别以 [工具简表](builtin_tools.md) 和 [API 说明](api_docs.md) 为使用入口，变更时核对源码。
- 功能细节放在对应专题文档，入口只保留摘要与链接；避免多处复制完整参数、脚本清单和容易失效的模块/测试数量。
- 带日期的报告记录当时发现与验证，不改写旧结论来代表现状；页首注明快照性质，并链接后续使用或修复说明。未来计划不应写成已提供能力。
