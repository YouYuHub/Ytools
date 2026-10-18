# 项目维护与依赖更新

## 本次调整（2026-10-18）

- 聊天中的静态 SVG 图片预览交给 DOMPurify 的 SVG 白名单净化；包含 SMIL/CSS 动画、样式或 `<use>` 等本地引用的 SVG 使用 `data:image/svg+xml` 图片环境显示，以保留动画并隔离脚本。若净化器不可用，预览显示提示，源码仍可在代码视图查看和复制。动画 SVG 的「复制图片」导出单帧 PNG 快照。`H5/js/vendor/dompurify/` 保留运行文件和许可证。
- `/chat_history/file` 与 `/chat_history/meta` 只打开已存在的会话。未知 ID 返回 404，不创建历史文件；只读查询不进入长期管理器缓存。可写管理器缓存最多保留 128 个空闲实例，活跃轮次与编辑状态不会被淘汰。
- 大于 1 MiB 的历史在 Web Worker 中解析，前端仍一次下载完整 JSONL，再按已有分段方式渲染。浏览器不支持 Worker 或本地文件限制 Worker 时自动回退同步解析。超大会话仍可能受下载量和渲染内存限制。
- 侧栏主题入口与 `/主题` 共用选择弹窗；侧栏旧菜单已移除。
- MCP `sys_tools_server.py` 仅保留网页抓取和搜索。文件与终端工具由 `factory/agent_runtime/builtin_tools.py` 提供。
- 已修复测试清理中的未等待协程，并新增只读接口、缓存、SVG 和历史 Worker 回归测试。

## 前端资源版本

修改 `H5/js/` 或 SCSS 后，在项目根目录执行：

```powershell
cd H5
npm ci
npm run build:css
cd ..
python H5/version_assets.py
```

版本脚本按文件内容哈希更新 `H5/index.html` 中 JS/CSS URL，重复运行结果相同。底部经典脚本统一 `defer`，保持声明顺序加载。`/ui/` 页面和未版本化资源每次校验；带内容哈希的资源可长期缓存。直接打开本地 `index.html` 时浏览器仍可读取同一组相对文件。

## Python 依赖

`requirements.txt` 是可编辑的直接依赖清单；`requirements.lock.txt` 是以 Python 3.10 为最低目标、通用平台解析出的精确版本。部署优先用锁文件。升级时执行：

```powershell
uv pip compile requirements.txt --universal --python-version 3.10 -o requirements.lock.txt
python -m pytest test -q
cd H5
npm test
```

升级或改动 `H5/package.json` 后运行 `npm install` 更新 `H5/package-lock.json`；正式安装使用 `npm ci`。锁文件的可安装性还需在目标系统和 Python 版本上验证。

## 数据与排查产物

`.gitignore` 排除密钥配置、历史、上传、临时文件、日志及依赖目录。现有 `history_files/` 和 `tmp/` 没有自动删除；清理前应先确认备份和外部脚本引用。原分析报告保留在项目根目录的 `PROJECT_ANALYSIS_2026-10-18.md`。
