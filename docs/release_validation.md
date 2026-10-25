# 统一验证与发布验收

## 安装测试依赖

使用独立虚拟环境，按锁文件安装生产依赖，再安装测试依赖：

```text
python -m pip install -r requirements.lock.txt -r requirements-dev.txt
npm ci --prefix H5
npm ci --prefix browser
node browser/node_modules/playwright/cli.js install chromium
```

Linux 若缺 Chromium 系统库，按 Playwright 要求安装系统依赖；CI 使用 `install --with-deps chromium`。本地无需重复安装已有依赖。修改前端后先执行 `python H5/version_assets.py`。

`H5/package-lock.json` 纳入版本管理供 `npm ci` 使用。静态资源版本取决于文件原始字节，`.gitattributes` 对 `H5/` 禁用 Git 自动换行转换，保证 Windows／Linux 检出后资源哈希仍一致。

## 一条命令验证

```text
python scripts/release_check.py --profile quick
python scripts/release_check.py --profile release
```

| 模式 | 内容 | 用途 |
|---|---|---|
| `quick` | 核心后端、全部前端、4 个关键浏览器流程、容量统计对照基准 | 开发阶段；不代表完整发布通过 |
| `release` | 全部自动化后端、全部前端、全部跨页面浏览器流程、容量统计对照基准 | 正式发布验收 |

账号存储回归包含一致性备份、校验、恢复及恢复副本登录／读取验证；发布门禁检查恢复用例确实运行通过。恢复夹具不覆盖真实数据。浏览器依赖缺失、浏览器用例被跳过、必需报告缺失、阶段失败或验证期间源码改变，都不能给出发布通过。

结果默认写入 `tmp/validation/<时间>/`，包含 `summary.json`、各阶段日志及 JUnit XML。可用 `--report-dir <新目录>` 指定位置，已有目录拒绝复用，避免旧日志冒充新结果；`--dry-run` 只显示执行计划。

后端仍可能有与平台或可选功能有关的跳过项，数量保留在报告中供核对；浏览器门禁不允许跳过。容量基准核对新旧统计值一致并记录耗时，不用不同 CI 硬件上的固定毫秒数代替正确性测试。

## CI

`.github/workflows/validate.yml` 在 Windows、Ubuntu 分别执行完整门禁，使用 Python 3.14、Node 24 和依赖锁文件。Actions 固定提交 SHA，仓库权限仅 `contents: read`；不部署服务，不载入真实模型密钥或账号库。报告保留 7 天。配置提交到启用了 Actions 的 GitHub 仓库后才会运行，不能把本地验证当作 GitHub 已执行。

Actions 的用法核对自官方仓库：[checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[setup-node](https://github.com/actions/setup-node)、[upload-artifact](https://github.com/actions/upload-artifact)。

## 发布与恢复顺序

1. 完成 `release` 门禁，保存当前版本的通过报告。后续代码改变需要重新验证。
2. 进入维护窗口，停止接收新任务，等待真实任务和写入工具结束。只停止已经核实属于本实例的服务；不能按记录年龄判定活进程已经结束。
3. 停写后生成本实例的完整备份，校验并恢复到新目录演练，不能覆盖生产：

   ```text
   python scripts/manage_storage.py backup --archive tmp/before-release.zip
   python scripts/manage_storage.py verify --archive tmp/before-release.zip
   python scripts/manage_storage.py restore --archive tmp/before-release.zip --target tmp/release-restore-drill
   ```

   使用新的归档／恢复目录名。备份包括账号库、MFA 密钥、归属、聊天、附件、版本、控制状态和配置，必须私有保存；程序代码及外部工作区另行备份，详见 [账号存储](account_storage.md)。
4. 用原服务配置启动已验证代码。Linux 按既有 systemd 方案运行；本地保持原监听地址、解释器和配置。即使启动后检查失败，也应恢复可用服务，不遗留停服。
5. 执行上线只读检查：

   ```text
   python scripts/verify_deployment.py --origin http://127.0.0.1:48621 --release-report tmp/validation/<时间>/summary.json --output tmp/deployment-check.json
   ```

   远程实例使用实际 HTTPS origin。脚本检查当前代码对应的完整通过报告、功能能力、匿名 API 拒绝、私有页登录跳转，以及所有入口页／Worker 资源的内容哈希和线上字节。它不登录、不会重置凭据或重启服务。

6. 核对账号库／MFA／归属指纹及实例配置保持一致，再退出维护。服务器实际证书、反向代理 SSE 和防火墙仍需在目标环境验收。

回滚时使用匹配的代码版本，将完整备份恢复到独立目录验证后离线切换；不要把旧数据库直接解压覆盖到运行实例，也不要把账号库和 MFA 密钥拆开恢复。
