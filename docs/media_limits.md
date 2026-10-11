# 供应商多模态输入类型与数量限制（mediaLimits）

`vision=true` 不代表同时支持图片、音频与视频，也不代表单个请求支持任意数量的媒体。动图 GIF 会转换为视频，可能触发供应商 `At most 0 video(s)` 错误；这种情况只发送一个视频仍然不合法。

## 配置

可以在 `models.json` 供应商条目或某个模型条目添加 `mediaLimits`。模型字段覆盖供应商字段。未配置的类型沿用既有能力与限制，不统一降级所有模型。

```json
"mediaLimits": {
  "total": 1,
  "video": 0
}
```

- `total`：整份 API 请求中媒体部件总数量上限，包含历史消息中的实际媒体。
- `image`、`video`、`audio`、`document`：各类型的请求数量上限。
- 非负整数有效；`0` 禁止该类型，`1` 每次只发送一个；省略表示本机制不额外限制。
- 文档仅统计原生 `file` 部件，解析后的文本不计入媒体数量。

### 配置归属与文档工具的关系

`mediaLimits` 属于供应商/模型的**整份 API 请求配置**，不是 `read_media` 的参数。名称中的 media 包含图片、音频、视频以及原生文档输入。它在发送请求前统一检查部件，因此 `read_document` 发送的原生文档也使用 `document` 和 `total` 配额；无需把 `document` 移入某个工具的配置，也无需新增一套重复的文档供应商限制。

- `read_media` 读取图片、音频、视频，不读取文档；其工具参数没有 `document`。
- `read_document(mode="native")` 或 `auto` 选择原生输入时，一个 `type=file` 部件计为一个文档。一次 PDF 页段读取生成的子 PDF 也计为一个文档，不按页数计数。
- `read_document(mode="text")` 的解析结果不占 `document` 或 `total`，但仍消耗上下文 token。`document=0` 禁止发送原生文档，不会禁用文档工具或禁止文本解析。
- `supportDocTypes` 声明支持哪些原生文档格式，`document` 限制一次发送多少个；数量配置不会赋予模型文档能力。
- 文档工具的 `NATIVE_DOC_MAX_ITEMS`、`NATIVE_DOC_MAX_BYTES`、`NATIVE_DOC_TOTAL_MAX_BYTES` 是服务端读取预算；`mediaLimits` 是供应商请求配额，两层均需满足。例如服务端允许 3 个文件而 `document=1`，最终请求仍最多发送 1 个原生文件。

目前供应商配额在最终请求层裁剪原生文档，**不会因此自动重新调用解析器或回退文本**。模型会收到未发送占位及配额提示；需要文本时应调用 `read_document(mode="text")`，需要原生内容时可分次读取或切换支持的模型。

例如只限制文档，不限制图片数量：

```json
"mediaLimits": {"document": 1}
```

如果配置 `{"total": 1, "document": 1}`，一张图片和一个原生文档也不能同时发送，因为二者共享 `total`。

如果只是“不支持视频，但可以多张图片”，应配置 `{"video": 0}`，无需配置 `total=1`。真实支持情况由供应商端点决定，不能仅按模型名字推断。

### 数量计算示例

| 配置 | 允许的组合示例 | 被限制的组合 |
|---|---|---|
| `{"video": 0}` | 多张图片，受其他既有上限约束 | 任意视频或转换为视频的 GIF |
| `{"image": 3, "video": 0}` | 最多 3 张图片 | 第 4 张图片、任意视频 |
| `{"total": 1}` | 1 张图片，或 1 个音频，或 1 个原生文档 | 图片＋音频、2 张图片 |
| `{"total": 3, "image": 2, "video": 0}` | 2 张图片＋1 个音频 | 3 张图片、任何视频、4 个媒体 |

`total` 与类型配额同时生效，不能互相抵消。限制单位为最终 API 媒体部件数量，不是字节数、token 数或工具调用次数。未配置 `image` 表示不额外限制图片数量，图片能力仍取决于 `vision` 及真实供应商能力。字符串 `"1"`、负数、布尔值等不是有效配额，应写 JSON 整数。

### 供应商示例配置

以下是项目 `models.json` 可使用的结构示例。域名、API key 和模型 ID 均为占位，按真实服务替换；配额是假设的限制示例，不代表某个商业供应商的实际承诺。将供应商条目合并到现有文件，不要覆盖已有 `model_selection` 和其他供应商。

```json
{
  "Example Completions": {
    "vendor": "openai",
    "url": "https://api.example.com/v1/chat/completions",
    "apiKey": "REPLACE_WITH_YOUR_KEY",
    "apiType": "chat-completions",
    "mediaLimits": {
      "total": 1,
      "video": 0
    },
    "models": {
      "Single Media Model": {
        "id": "replace-single-media-model-id",
        "vision": true,
        "toolCalling": true
      },
      "Three Image Model": {
        "id": "replace-three-image-model-id",
        "vision": true,
        "toolCalling": true,
        "mediaLimits": {
          "total": 3,
          "image": 3,
          "video": 0,
          "audio": 0,
          "document": 0
        }
      }
    }
  }
}
```

- `Single Media Model` 继承供应商限制：每次最多一个媒体，禁止视频；图片与音频是否实际可用仍需供应商支持。
- `Three Image Model` 使用模型自己的限制：最多三张图片，同时禁止视频、音频与原生文档。普通文档解析文本仍可使用。
- **模型的 `mediaLimits` 整个对象覆盖供应商对象，不做逐字段合并。** 例如供应商设置 `{"total": 1, "video": 0}`，模型只写 `{"image": 3}`，就不会继承供应商的 `total` 和 `video`。需要的限制必须在模型对象中完整重写。
- 模型省略该字段时继承供应商；写 `{}` 可取消继承的配置配额，但不会立刻清除自动学习到的更严格限制。
- 若供应商仅“不支持视频”，在供应商层使用 `"mediaLimits": {"video": 0}` 即可，不必限制全部媒体为一个。

配置调整按现有 `models.json` 热加载链路生效。自动学习的限制会与配置取更严格值；如果供应商已放宽限制，当前进程缓存可能仍保留旧限制，等待一小时过期或重建相关 worker / 重启后端后再验证。

工具参数、分段读取、去重、base64 回传和历史存档规则见 [read_media 使用说明](read_media.md)。

## 自动降级

同步/异步 Chat Completions SSE 收到 HTTP 400 或供应商 SSE 错误帧时，识别明确的 `At most N image(s)/video(s)/audio(s)/file(s)/document(s)/media(s)` 数量限制。只有限额能实际改变当前请求，且 SSE 尚无正文/思考/工具调用输出时，才裁剪请求并重新发送；避免重复生成。非流式 HTTP 400 也支持该降级。

限制按端点、供应商、模型记录在当前进程内，缓存最多 128 项、有效期 1 小时，不自动写 `models.json`。会话 worker 重建或后端重启后需重新探测；已知限制推荐写入配置。与类型/数量无关的错误沿用原错误处理，不猜测删媒体。

裁剪作用于请求副本，保留工具调用与结果配对、正文及历史原文，不改 JSONL。优先保留最新消息中的媒体，同批保留靠前的媒体；旧媒体及超过配额的媒体转换为明确的“未发送”文本占位。另附模型提示：限额为 1 时逐个读取，限额为 0 时更换模型，不能声称已看过未发送内容；读取新媒体前先记录已有观察。

父任务和子任务的 `read_media` 使用同一套已知限制，成功列表与注入去重集合不包含被跳过的来源。旧媒体从请求中退出后允许重新读取。首次探测失败时工具加载结果可能已落盘，但后续请求会明确告知模型哪些数据实际未发送。

用户直接上传的媒体、`read_media` 注入和原生文档最终都受请求侧限制保护。普通文档解析文本继续正常发送。不会自动把不支持的视频转换成静态帧，避免把部分内容当完整视频交给模型。

## 测试

`test/test_media_limits.py` 覆盖 HTTP/SSE 自动降级、异步 SSE、非流式、视频上限 0、图片上限 1、总配额、历史去重与重读、请求副本隔离、端点隔离、工具跳过结果，以及已经输出正文时禁止整请求重试。
