# API 发送性能（2026-10-24）

## 保持的行为

不裁剪正文、工具结果、原生签名或协议状态；保留预算、窗口和附件限制、重试、停止、用量计数与断线恢复。字符 token 估算仍为 `max(1, ASCII字符数 // 4 + 非ASCII字符数)`，空文本仍为 0；这不是服务商 tokenizer。

## 实现

1. `util/token_estimate.py` 用 C 层字符统计替换 Python 逐字符循环，两个原估算入口共用它。重复扫描暂不引入跨请求文本缓存，避免保留大段提示词或错误复用可变上下文。
2. INFO 级别跳过 DEBUG 消息摘要构造。Messages 在每次实际发送前估算一次、校验一次，校验使用最终发送字节检查体积。公开 `build_payload()` 仍默认校验；续传、配额降级和重试后的 payload 仍重新校验。
3. Chat Completions / Messages 的同步、异步 SSE 读取都增量处理 HTTP chunk。完整 SSE 行到达即产出，UTF-8 解码仍在完整行重组之后。
4. 三个协议的异步路径复用 HTTP/1.1 连接。池按事件循环、主机、端口与 TLS 上下文隔离，不并发使用同一连接、不自动重发 POST；每次请求仍登记原有网络尝试和预算。
   - 最多 8 条空闲连接、同源最多 2 条、空闲 20 秒回收。
   - 只复用完整 Content-Length 或 chunked 响应。未知边界、额外字节、服务端 close、错误连接和用户停止均不复用。
   - SSE 提前结束只消费已缓冲的尾部，不等待远端补尾；未完成边界直接关闭。
   - 主服务和会话 worker 退出时关闭空闲池。
5. 同步与异步路径复用默认 TLS 上下文，证书与主机名验证仍开启。CA 环境变量、CA 文件/目录变化时刷新，最长 5 分钟刷新一次系统证书状态；同步路径仍使用原有短连接。
6. Responses 热路径将公开状态镜像原子写入 `history_files/sidecars/<会话文件名>.responses`。协议恢复仍以原有私有 journal 为准。
   - 镜像绑定原始 `_meta` 的摘要；历史修改/替换后，旧镜像不覆盖新状态。
   - 首行元数据读取、完整历史读取均合并有效镜像；正常历史写入和原始 JSONL 下载会折回最新状态。
   - 写入失败保留上一版本，沿用原子替换重试；删除会话同时清理镜像与临时文件。
7. 相同轮次不再重复更新任务用量记录；预算事务、数据库同步级别不变。

## 分段计时

服务端 INFO 日志搜索 `API latency`。每次模型调用一条记录，不包含 URL、密钥、消息正文或工具结果；最近 100 条另存于进程内有界队列，单次最多保留最近 10 次网络尝试。

流式路径记录以下字段，单位毫秒，均相对于最近一次工具结果发出（首次请求则为入口时间）：

| 字段 | 含义 |
| --- | --- |
| `meter_ready_ms` | 本地入口与用量估算准备完成 |
| `request_ready_ms` | 协议组装、检查和序列化完成 |
| `connect_start_ms` / `connected_ms` | 连接阶段；`connection_reused` 标记复用 |
| `request_sent_ms` | 请求写入并完成 drain |
| `headers_received_ms` | 收齐 HTTP 响应头 |
| `first_upstream_event_ms` | 首个可解析的上游事件，可能只有状态信息 |
| `first_visible_delta_ms` | 首个可转发正文、思考或工具增量；不是浏览器实际绘制时间 |

例如 `first_upstream_event_ms - request_sent_ms` 包含网络往返和服务端等待；`first_visible_delta_ms - first_upstream_event_ms` 能识别“已有状态事件、尚无内容”的时段。重试会分别记录，GET 恢复查询标记 `method`。这些数据才能用于判断真实慢请求的占比。

## 离线性能对比

运行 `python scripts/archive/benchmark_api_send.py`，结果写入 `tmp/api-send-perf-after.json`。参考流程重建原字符统计与重复校验顺序，不调用服务商；导入时间不计。以下为本机中位数，公共流程和 Messages 流程有重叠，不能相加：

| 合成样本 | 参考流程 | 优化后 |
| --- | ---: | ---: |
| 约 7.8 万 tokens，公共准备 | 48.8 ms | 2.2 ms |
| 约 46.6 万 tokens，公共准备 | 286.7 ms | 13.9 ms |
| 约 46.6 万 tokens，Messages 适配 | 291.4 ms | 9.2 ms |
| 7.66 MiB 历史状态镜像 | 33.2 ms | 2.0 ms |
| 首 SSE 行已到、HTTP 块剩余部分延迟 150ms | 150.9 ms | 0.6 ms |

两种准备流程都断言新旧发送正文逐字节一致。历史实验只操作临时目录，并断言热路径不改变 JSONL 字节。这些结果不包含真实模型推理、网络或服务商排队，不能作为整体首帧提速倍数。

## 分步验证

- 第一步：143 passed / 9 skipped（估算、预算硬限制、Messages、原生回放和附件）。
- 第二步：204 passed / 9 skipped（增量 SSE、跨块中文、截断、供应商错误与重试）。
- 第三步：221 passed（连接复用、恢复、用量计数）；补充三个协议回环与真实 HTTPS 证书/主机名校验。
- 第四步：141 passed（Responses 镜像、原子写入、历史替换、下载及协议恢复）。
- 最终 Windows Python 全量：2086 passed / 11 skipped，见 `tmp/api-perf-full-final.log`。
- Linux / WSL：203 passed / 9 skipped，覆盖本次新增用例、三个协议回环、HTTPS、Messages 附件/回放、Responses 恢复与预算，见 `tmp/api-perf-linux.log`。
- 新增性能边界用例及其他分步结果见本次验证日志 `tmp/api-perf-*.log`。

新增实现仅使用 Python 标准库，无操作系统专属分支；Windows、Linux 已运行回环 HTTP/HTTPS 验证，macOS 尚未实机验证。
