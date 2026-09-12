"""read_media 内置工具单元测试。

覆盖：
- 工具定义/注入/选择名单（include_read_media / SELECTABLE / is_builtin_tool）
- normalize_read_media_args 参数规整（media:// 剥离、去重、quality 校验）
- execute_read_media：当前任务范围安全边界、单轮 ≤5 上限、重复读取去重、
  加载失败/越界跳过、loaded 元信息（不携带 base64 数据本体）、quality 透传
- roll_recent_media_parts：任务内累计滚动窗口（多次读取只保留最近 5 个坐标）
- load_session_media_model_part：小图原图、大图降采样 + quality 参数、
  GIF 跳过降采样、非法引用返回 None、音频/视频部件形态
- collect_media_references：安全边界数据源收集（必须先于 resolve 的顺序约束）
- 本地/网络/会话统一入口：register_media_source（下载/读取 → 魔数嗅探 →
  入库复用上传规则）、load_any_media_model_part 三类引用、
  execute_read_media 无白名单边界（available=None 时全放行）
"""
import base64
import io
import json
import os
import shutil
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools as bt  # noqa: E402
from memory import file_memory as fm  # noqa: E402

TEST_SESSION = "read_media_ut_session"


def _cleanup_test_session():
    upload_dir = fm.HISTORY_ROOT / TEST_SESSION
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)
    fm.cleanup_file_memory_manager(TEST_SESSION)


def _make_png_bytes(width: int, height: int) -> bytes:
    """用 PIL 构造一张指定尺寸的合法 PNG（纯色压缩后体积很小）。"""
    import io as _io
    from PIL import Image as _Image

    out = _io.BytesIO()
    _Image.new("RGB", (width, height), (119, 119, 119)).save(out, format="PNG")
    return out.getvalue()


def _fake_loader(session_id, reference, quality=None):
    """给 execute_read_media 的加载桩：返回可注入部件形态（含实际生效质量）。"""
    if reference == "media://fail.png":
        return None
    mime = "image/jpeg" if quality is not None else "image/png"
    part = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,QUJD"}}
    return {
        "part": part,
        "kind": "image",
        "mime": mime,
        "size": 123,
        "downsampled": quality is not None,
        "quality": quality if quality is not None else None,
    }


class _StubMediaHandler(BaseHTTPRequestHandler):
    """最小 HTTP 媒体桩：/image/<name> 返回类属性 payload；其余 404。"""

    payload: dict = {}

    def do_GET(self):  # noqa: N802
        type(self).payload["hits"] = type(self).payload.get("hits", 0) + 1
        if self.path in ("/image/photo.png", "/image/raw"):
            body = type(self).payload.get("payload", b"")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        return


class DefinitionAndInjectionTests(unittest.TestCase):
    """工具定义、注入与内置名单。"""

    def test_selectable_names_include_read_media(self):
        self.assertIn("read_media", bt.SELECTABLE_BUILTIN_TOOL_NAMES)
        self.assertTrue(bt.is_builtin_tool("read_media"))

    def test_inject_read_media(self):
        tools, servers = bt.inject_builtin_tools([], {}, include_read_media=True)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("read_media", names)
        self.assertEqual(servers["read_media"], "__builtin__")
        tools2, _ = bt.inject_builtin_tools([], {})
        self.assertNotIn("read_media", {t["function"]["name"] for t in tools2})


class NormalizeArgsTests(unittest.TestCase):
    """normalize_read_media_args 参数规整。"""

    def test_strips_media_scheme_and_dedupes(self):
        references, quality, error = bt.normalize_read_media_args({
            "references": ["media://a.png", " media://a.png ", "media://b.jpg", ""],
        })
        self.assertEqual(references, ["media://a.png", "media://b.jpg"])
        self.assertIsNone(quality)
        self.assertIsNone(error)

    def test_single_string_wrapped(self):
        references, _q, error = bt.normalize_read_media_args({"references": "media://a.png"})
        self.assertEqual(references, ["media://a.png"])
        self.assertIsNone(error)

    def test_quality_bounds(self):
        _, quality, error = bt.normalize_read_media_args({"references": ["media://a.png"], "quality": 50})
        self.assertEqual(quality, 50)
        self.assertIsNone(error)
        _, _, error = bt.normalize_read_media_args({"references": ["media://a.png"], "quality": 49})
        self.assertIn("超出范围", error)
        _, _, error = bt.normalize_read_media_args({"references": ["media://a.png"], "quality": 101})
        self.assertIn("超出范围", error)
        _, _, error = bt.normalize_read_media_args({"references": ["media://a.png"], "quality": "abc"})
        self.assertIn("无效", error)

    def test_empty_references_rejected(self):
        _, _, error = bt.normalize_read_media_args({"references": []})
        self.assertIn("不能为空", error)
        _, _, error = bt.normalize_read_media_args({"references": 123})
        self.assertIn("无效", error)
        # 单个字符串自动包列表（宽容处理，不算错误）
        references, _, error = bt.normalize_read_media_args({"references": "not-a-list"})
        self.assertEqual(references, ["not-a-list"])
        self.assertIsNone(error)


class ExecuteReadMediaTests(unittest.TestCase):
    """execute_read_media 安全边界与数据流（媒体坐标而非数据本体）。"""

    def test_success_returns_loaded_meta_without_base64(self):
        result = bt.execute_read_media(
            {"references": ["media://a.png"]},
            "session", {"media://a.png"}, None, load_media=_fake_loader,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["injected_references"], ["media://a.png"])
        loaded = result["loaded"][0]
        self.assertEqual(loaded["reference"], "media://a.png")
        self.assertEqual(loaded["kind"], "image")
        # 结果 dict 不得包含 base64 数据本体（防止进入工具结果文本落盘）
        self.assertNotIn("media_parts", result)
        self.assertNotIn("part", loaded)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("base64", serialized)
        self.assertNotIn("data:image", serialized)

    def test_reference_outside_current_task_rejected(self):
        result = bt.execute_read_media(
            {"references": ["media://evil.png", "media://a.png"]},
            "session", {"media://a.png"}, None, load_media=_fake_loader,
        )
        self.assertTrue(result["ok"])
        reasons = {s["reference"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons["media://evil.png"], "not_in_current_task")
        self.assertEqual(
            [item["reference"] for item in result["loaded"]],
            ["media://a.png"],
        )

    def test_already_injected_skipped(self):
        result = bt.execute_read_media(
            {"references": ["media://a.png"]},
            "session", {"media://a.png"}, {"media://a.png"}, load_media=_fake_loader,
        )
        self.assertFalse(result["ok"])
        reasons = {s["reference"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons["media://a.png"], "already_injected")
        self.assertEqual(result["loaded"], [])

    def test_limit_five_per_round(self):
        refs = [f"media://r{i}.png" for i in range(8)]
        result = bt.execute_read_media(
            {"references": refs},
            "session", set(refs), None, load_media=_fake_loader,
        )
        self.assertEqual(len(result["loaded"]), 5)
        over_limit = {s["reference"] for s in result["skipped"] if s["reason"] == "limit_5_per_round"}
        self.assertEqual(over_limit, set(refs[5:]))
        # 传入 already_injected=结果里已注入的引用，下一轮调用读取剩余 3 个
        follow_up = bt.execute_read_media(
            {"references": refs},
            "session", set(refs), set(result["injected_references"]), load_media=_fake_loader,
        )
        self.assertEqual(len(follow_up["loaded"]), 3)
        already = [s["reference"] for s in follow_up["skipped"] if s["reason"] == "already_injected"]
        self.assertEqual(set(already), set(refs[:5]))

    def test_quality_recorded_in_loaded_meta(self):
        result = bt.execute_read_media(
            {"references": ["media://a.png"], "quality": 70},
            "session", {"media://a.png"}, None, load_media=_fake_loader,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["loaded"][0]["quality"], 70)
        no_quality = bt.execute_read_media(
            {"references": ["media://a.png"]},
            "session", {"media://a.png"}, None, load_media=_fake_loader,
        )
        self.assertIsNone(no_quality["loaded"][0]["quality"])

    def test_load_failed_recorded(self):
        result = bt.execute_read_media(
            {"references": ["media://fail.png"]},
            "session", {"media://fail.png"}, None, load_media=_fake_loader,
        )
        self.assertFalse(result["ok"])
        reasons = {s["reference"]: s["reason"] for s in result["skipped"]}
        # media:// 引用加载失败按新语义给出"未找到，可能已删除"提示
        self.assertIn("未找到", reasons["media://fail.png"])

    def test_invalid_args_error(self):
        result = bt.execute_read_media(
            {"references": [], "quality": 999},
            "session", set(), None, load_media=_fake_loader,
        )
        self.assertFalse(result["ok"])
        self.assertIn("error", result)


class RollRecentMediaPartsTests(unittest.TestCase):
    """任务内累计滚动窗口：多次读取只保留最近 5 个部件坐标。"""

    def test_under_limit_keeps_all(self):
        pairs = [(f"media://a{i}.png", i) for i in range(5)]
        evicted: set[str] = set()
        trimmed = bt.roll_recent_media_parts(pairs, evicted=evicted)
        self.assertEqual(trimmed, pairs)
        self.assertEqual(evicted, set())

    def test_over_limit_keeps_recent_five_and_reports_evicted(self):
        pairs = [(f"media://a{i}.png", i) for i in range(8)]
        evicted: set[str] = set()
        trimmed = bt.roll_recent_media_parts(pairs, evicted=evicted)
        self.assertEqual([ref for ref, _ in trimmed], [f"media://a{i}.png" for i in range(3, 8)])
        self.assertEqual(evicted, {f"media://a{i}.png" for i in range(3)})

    def test_evicted_refs_can_be_re_injected(self):
        # 模拟 chat_factory 接线：被挤出的引用从 injected 集合移除后可再次读取
        injected = {f"media://a{i}.png" for i in range(8)}
        pairs = [(f"media://a{i}.png", i) for i in range(8)]
        evicted: set[str] = set()
        bt.roll_recent_media_parts(pairs, evicted=evicted)
        injected.difference_update(evicted)
        self.assertEqual(
            injected,
            {f"media://a{i}.png" for i in range(3, 8)},
        )


class LoadMediaModelPartTests(unittest.TestCase):
    """load_session_media_model_part 的真实文件行为（含降采样与 quality）。"""

    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    def test_small_image_returns_original_png(self):
        saved = fm.save_session_media(TEST_SESSION, "小图.png", _make_png_bytes(40, 30))
        info = fm.load_session_media_model_part(TEST_SESSION, saved["media_ref"])
        self.assertFalse(info["downsampled"])
        url = info["part"]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), _make_png_bytes(40, 30))

    def test_big_image_downsampled_with_quality(self):
        big = _make_png_bytes(2400, 800)
        saved = fm.save_session_media(TEST_SESSION, "大图.png", big)
        with patch.object(fm, "IMAGE_THUMBNAIL_THRESHOLD_BYTES", 16):
            info = fm.load_session_media_model_part(TEST_SESSION, saved["media_ref"], quality=50)
        self.assertTrue(info["downsampled"])
        url = info["part"]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        from PIL import Image
        raw = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as img:
            self.assertEqual(max(img.size), fm.MODEL_MEDIA_MAX_EDGE)

    def test_gif_skips_downsample(self):
        import io as _io
        from PIL import Image as _Image
        out = _io.BytesIO()
        _Image.new("P", (64, 48), color=3).save(out, format="GIF")
        gif_bytes = out.getvalue()
        saved = fm.save_session_media(TEST_SESSION, "动图.gif", gif_bytes)
        info = fm.load_session_media_model_part(TEST_SESSION, saved["media_ref"], quality=50)
        self.assertFalse(info["downsampled"])
        url = info["part"]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/gif;base64,"))

    def test_invalid_reference_returns_none(self):
        self.assertIsNone(fm.load_session_media_model_part(TEST_SESSION, "media://ghost.png"))
        self.assertIsNone(fm.load_session_media_model_part(TEST_SESSION, "../escape.png"))

    def test_describe_session_media_file(self):
        saved = fm.save_session_media(TEST_SESSION, "描述.png", _make_png_bytes(40, 30))
        info = fm.describe_session_media_file(TEST_SESSION, saved["media_ref"])
        self.assertEqual(info["kind"], "image")
        self.assertEqual(info["size"], len(_make_png_bytes(40, 30)))
        self.assertIsNone(fm.describe_session_media_file(TEST_SESSION, "media://ghost.png"))


class CollectMediaReferencesTests(unittest.TestCase):
    """collect_media_references：read_media 安全边界数据源（chat_factory 接线口径）。"""

    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    def test_collects_user_media_refs_in_order_and_dedupes(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [
                {"type": "text", "text": "看看这些"},
                {"type": "image_url", "image_url": {"url": "media://a.png"}},
                {"type": "image_url", "image_url": {"url": "media://a.png"}},
                {"type": "video_url", "video_url": {"url": "media://b.mp4"}},
                {"type": "input_audio", "input_audio": {"data": "media://c.mp3", "format": "mp3"}},
            ]},
        ]
        self.assertEqual(
            bt.collect_media_references(messages),
            ["media://a.png", "media://b.mp4", "media://c.mp3"],
        )

    def test_ignores_non_user_roles_and_non_media_urls(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "image_url", "image_url": {"url": "media://x.png"}},
            ]},
            {"role": "user", "content": "纯字符串 content"},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "image_url", "image_url": {"url": "media://y.png"}},
            ]},
        ]
        self.assertEqual(bt.collect_media_references(messages), ["media://y.png"])

    def test_collect_before_resolve_regression(self):
        """顺序守卫：先收集、后 resolve_message_media_refs（就地改写）。

        历史 bug：chat_factory 曾把收集排在解析之后——解析把 media:// 就地
        改写为 data:，收集永远为空，read_media 把所有引用误判
        not_in_current_task。此测试锁定"先收集后解析"的顺序。
        """
        saved = fm.save_session_media(TEST_SESSION, "守卫.png", _make_png_bytes(20, 16))
        ref = saved["media_ref"]
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "图"},
            {"type": "image_url", "image_url": {"url": ref}},
        ]}]
        collected_before = bt.collect_media_references(messages)
        unresolved = fm.resolve_message_media_refs(TEST_SESSION, messages)
        collected_after = bt.collect_media_references(messages)
        self.assertEqual(unresolved, [])
        self.assertEqual(collected_before, [ref])
        # 解析后就地改写为 data:，再收集永远为空——收集必须发生在解析之前
        self.assertEqual(collected_after, [])
        # 用"解析前收集"的引用集调用 execute_read_media（走真实加载路径）
        result = bt.execute_read_media({"references": [ref]}, TEST_SESSION, collected_before)
        self.assertTrue(result["ok"])
        self.assertEqual(result["injected_references"], [ref])


class LoadAnyMediaTests(unittest.TestCase):
    """register_media_source / load_any_media_model_part：本地/网络/会话统一入口。"""

    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    def test_register_local_source_reuses_upload_rules(self):
        png = _make_png_bytes(30, 20)
        tmp_dir = fm.HISTORY_ROOT / "register_local_probe"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / "本地图.png"
        tmp_path.write_bytes(png)
        try:
            registered = bt.register_media_source(
                TEST_SESSION, str(tmp_path), "local"
            )
            self.assertTrue(registered["ok"])
            self.assertEqual(registered["source_type"], "local")
            self.assertEqual(registered["kind"], "image")
            self.assertTrue(registered["reference"].startswith("media://"))
            # 注册后与用户上传同链路：可被会话加载函数读取
            info = fm.load_session_media_model_part(TEST_SESSION, registered["reference"])
            self.assertIsNotNone(info)
            self.assertEqual(info["size"], len(png))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_register_rejects_non_media_and_missing_file(self):
        tmp_dir = fm.HISTORY_ROOT / "register_probe2"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        txt = tmp_dir / "note.txt"
        txt.write_text("plain text", encoding="utf-8")
        try:
            missing = bt.register_media_source(
                TEST_SESSION, str(tmp_dir / "ghost.png"), "local"
            )
            self.assertFalse(missing["ok"])
            self.assertIn("不存在", missing["error"])
            bad = bt.register_media_source(TEST_SESSION, str(txt), "local")
            self.assertFalse(bad["ok"])
            self.assertIn("不支持的媒体类型", bad["error"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_sniff_calibrates_extension(self):
        # PNG 字节伪装成 .bin 扩展名：入库时按魔数校准为 .png
        png = _make_png_bytes(20, 14)
        tmp_dir = fm.HISTORY_ROOT / "register_probe3"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / "伪装.bin"
        tmp_path.write_bytes(png)
        try:
            registered = bt.register_media_source(TEST_SESSION, str(tmp_path), "local")
            self.assertTrue(registered["ok"])
            self.assertEqual(registered["kind"], "image")
            self.assertEqual(registered["mime"], "image/png")
            self.assertTrue(registered["stored_name"].endswith(".png"))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_load_any_media_session_reference(self):
        saved = fm.save_session_media(TEST_SESSION, "会话图.png", _make_png_bytes(24, 18))
        info = bt.load_any_media_model_part(TEST_SESSION, saved["media_ref"])
        self.assertIsNotNone(info)
        self.assertEqual(info["kind"], "image")
        self.assertIn("data:image/png;base64,", info["part"]["image_url"]["url"])

    def test_load_any_media_local_path(self):
        png = _make_png_bytes(32, 20)
        tmp_dir = fm.HISTORY_ROOT / "load_any_probe"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / "直达图.png"
        tmp_path.write_bytes(png)
        try:
            info = bt.load_any_media_model_part(TEST_SESSION, str(tmp_path))
            self.assertIsNotNone(info)
            self.assertEqual(info["kind"], "image")
            # 本地路径直读真实位置：不复制落盘（会话媒体库保持干净）
            self.assertGreater(info["size"], 0)
            self.assertEqual(info["source_type"], "local")
            self.assertEqual(info["source"], str(tmp_path))
            media_files = list(fm._media_dir(TEST_SESSION).glob("*.png"))
            self.assertEqual(media_files, [])
            # 文件不存在：返回"未找到，可能已删除"提示（不再静默 None）
            ghost = bt.load_any_media_model_part(
                TEST_SESSION, str(tmp_dir / "ghost.png")
            )
            self.assertIsNotNone(ghost)
            self.assertIn("未找到", ghost["error"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_load_any_media_network_url(self):
        # URL 直传：不再下载/入库，原样把 URL 作为多模态部件透传给上游
        png = _make_png_bytes(40, 30)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StubMediaHandler)
        server_bytes = {}
        server_bytes["payload"] = png
        server_bytes["hits"] = 0
        server.RequestHandlerClass.payload = server_bytes
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/image/photo.png"
            info = bt.load_any_media_model_part(TEST_SESSION, url)
            self.assertIsNotNone(info)
            self.assertEqual(info["kind"], "image")
            self.assertEqual(info["source_type"], "network")
            # 部件携带原始 URL，而非 data: base64
            self.assertEqual(info["part"]["image_url"]["url"], url)
            # 扩展名缺失（无 .png 路径）：回退按图片处理，URL 原样透传
            raw_url = f"http://127.0.0.1:{port}/image/raw"
            info2 = bt.load_any_media_model_part(TEST_SESSION, raw_url)
            self.assertIsNotNone(info2)
            self.assertEqual(info2["part"]["image_url"]["url"], raw_url)
            # 全程零请求：URL 不经服务器下载（404 与否由模型供应商处理）
            self.assertEqual(server_bytes["hits"], 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_execute_without_boundary_reads_local(self):
        """available_references=None（无白名单）时本地路径直接可读。"""
        png = _make_png_bytes(28, 22)
        tmp_dir = fm.HISTORY_ROOT / "exec_local_probe"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / "工具入参图.png"
        tmp_path.write_bytes(png)
        try:
            result = bt.execute_read_media(
                {"references": [str(tmp_path)]}, TEST_SESSION, None
            )
            self.assertTrue(result["ok"])
            loaded = result["loaded"][0]
            self.assertEqual(loaded["source_type"], "local")
            self.assertEqual(loaded["kind"], "image")
            # 无 base64 本体落盘
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("base64", serialized)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_execute_mixed_sources(self):
        """media:// / 本地路径混用（available=None，网络 stub 验证见上）。"""
        saved = fm.save_session_media(TEST_SESSION, "混用图.png", _make_png_bytes(20, 16))
        png = _make_png_bytes(20, 16)
        tmp_dir = fm.HISTORY_ROOT / "exec_mixed_probe"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / "本地混用.png"
        tmp_path.write_bytes(png)
        try:
            result = bt.execute_read_media(
                {"references": [saved["media_ref"], str(tmp_path)]},
                TEST_SESSION, None, load_media=_fake_loader,
            )
            self.assertTrue(result["ok"])
            refs = result["injected_references"]
            self.assertEqual(refs, [saved["media_ref"], str(tmp_path)])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_execute_media_ref_outside_collection_when_boundary_given(self):
        """传入集合时 media:// 引用仍按旧语义过滤（本地/网络不受影响）。"""
        png = _make_png_bytes(20, 16)
        tmp_dir = fm.HISTORY_ROOT / "exec_bound_probe"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / "边界外.png"
        tmp_path.write_bytes(png)
        try:
            result = bt.execute_read_media(
                {"references": ["media://evil.png", str(tmp_path)]},
                TEST_SESSION, {"media://other.png"}, None, load_media=_fake_loader,
            )
            self.assertTrue(result["ok"])
            reasons = {s["reference"]: s["reason"] for s in result["skipped"]}
            self.assertEqual(reasons.get("media://evil.png"), "not_in_current_task")
            self.assertEqual(result["injected_references"], [str(tmp_path)])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
