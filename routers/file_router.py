# coding: utf-8
# fastapi 库导入
from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from typing import List
from pathlib import Path
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# 自定义模块导入
from memory.file_memory import (
    get_file_memory_manager,
    get_upload_dir_name,
    media_kind,
    media_mime_type,
    media_size_limit,
    resolve_document_path,
    resolve_media_path,
    save_session_document,
    save_session_media,
    save_session_media_stream,
)
from memory.chat_memory import get_chat_memory_manager, normalize_session_id
from factory.file_factory import extract_text_from_bytes

# 创建 API 路由器实例
api_file_router = APIRouter(prefix="/file")

# 创建线程池用于并发处理文件解析
file_parse_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="file_parse")


async def _record_session_upload_id(session_id: str) -> str | None:
    """把上传目录名记录到会话 _meta.upload_id，供删除会话时连带清理。

    记录失败只告警不阻断（文件本身已保存成功）。
    """
    try:
        upload_id = get_upload_dir_name(session_id)
        chat_manager = await get_chat_memory_manager(normalize_session_id(session_id))
        await chat_manager.update_session_upload_id(upload_id)
        return upload_id
    except Exception as record_error:
        print(f"[WARN] 记录 upload_id 失败: {record_error}")
        return None


def _parse_single_file(file_info: dict) -> dict:
    """
    解析单个文件的辅助函数（在线程池中执行）
    Args:
        file_info: 包含 filename, content_bytes 的字典
    Returns:
        解析结果字典
    """
    filename = file_info['filename']
    file_content = file_info['content_bytes']
    try:
        # 获取文件扩展名和类型
        _, ext = os.path.splitext(filename.lower())
        file_type = ext.lstrip('.') if ext else "unknown"
        # 解析文件内容
        text_content = extract_text_from_bytes(filename, file_content)
        return {
            "filename": filename,
            "status": "success",
            "type": file_type,
            "content_length": len(text_content),
            "text_content": text_content,  # 返回解析后的文本
            "size": len(file_content)
        }
    except Exception as parse_error:
        return {
            "filename": filename,
            "status": "failed",
            "message": f"文件解析失败: {str(parse_error)}"
        }


@api_file_router.post("/upload_session_files")
async def upload_files(
    files: List[UploadFile] = File(...),
    session_id: str = "default"  # 会话ID，用于隔离不同用户的文件历史
):
    """
    上传文件并解析内容，将文件信息记录到 file_memory 中
    使用线程池并发处理多个文件的解析
    参数:
        files: 上传的文件列表，支持多个文件，最多10个
        session_id: 会话ID，用于隔离不同会话的文件历史
    返回:
        JSON格式的结果，包含每个文件的处理状态
    """
    # 验证文件数量
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="最多只能上传10个文件")
    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件")
    results = []
    success_count = 0
    failed_count = 0
    # 第一步：读取所有文件内容（异步操作）
    file_data_list = []
    for file in files:
        try:
            # 验证文件名
            if not file.filename:
                results.append({
                    "filename": "未知文件",
                    "status": "failed",
                    "message": "文件名缺失"
                })
                failed_count += 1
                continue
            # 读取文件内容
            file_content = await file.read()
            # 验证文件大小（限制为10MB）
            max_file_size = 10 * 1024 * 1024  # 10MB
            if len(file_content) > max_file_size:
                results.append({
                    "filename": file.filename,
                    "status": "failed",
                    "message": f"文件大小超过限制（最大{max_file_size // (1024*1024)}MB）"
                })
                failed_count += 1
                continue
            file_data_list.append({
                "filename": file.filename,
                "content_bytes": file_content
            })
        except Exception as e:
            results.append({
                "filename": file.filename if file.filename else "未知文件",
                "status": "failed",
                "message": f"读取文件失败: {str(e)}"
            })
            failed_count += 1
    # 第二步：并发解析文件内容（CPU密集型操作）
    if file_data_list:
        # 提交所有文件到线程池进行并发解析
        future_to_file = {
            file_parse_executor.submit(_parse_single_file, file_data): file_data 
            for file_data in file_data_list
        }
        # 获取当前会话的记忆管理器
        session_file_memory = await get_file_memory_manager(session_id)
        # 等待所有解析任务完成
        for future in as_completed(future_to_file):
            file_data = future_to_file[future]
            try:
                parse_result = future.result()
                if parse_result["status"] == "success":
                    # 构建文件信息字典（不包含原始二进制内容）
                    file_info = {
                        "filename": parse_result["filename"],
                        "type": parse_result["type"],
                        "content": parse_result["text_content"],  # 只保存文本内容
                        "size": parse_result["size"]
                    }
                    # 同时保存原始字节（history_files/upload/<session>/files/），
                    # 供前端点击预览 PDF/文本/下载原文件；保存失败只告警不影响解析结果
                    stored_name = None
                    try:
                        doc_saved = save_session_document(
                            session_id, parse_result["filename"], file_data["content_bytes"]
                        )
                        stored_name = doc_saved["stored_name"]
                        file_info["stored_name"] = stored_name
                    except Exception as save_error:
                        print(f"[WARN] 保存文档原始字节失败（不影响解析文本）: {save_error}")
                    # 添加到 file_memory（使用session_id隔离）
                    add_result = session_file_memory.add_file_memory(file_info, max_items=10)
                    results.append({
                        "filename": parse_result["filename"],
                        "status": "success",
                        "message": add_result,
                        "type": parse_result["type"],
                        "content_length": parse_result["content_length"],
                        "stored_name": stored_name,
                    })
                    success_count += 1
                else:
                    results.append(parse_result)
                    failed_count += 1
            except Exception as e:
                results.append({
                    "filename": file_data["filename"],
                    "status": "failed",
                    "message": f"解析任务异常: {str(e)}"
                })
                failed_count += 1
    # 有成功保存的文件时，把上传目录名记录到会话 _meta.upload_id：
    # 上传目录按前端传入的 session_id 命名，可能与会话文件名不一致，
    # 记录后删除会话时可连带清理上传目录（见 /chat_history/delete_file）。
    upload_id = None
    if success_count:
        upload_id = await _record_session_upload_id(session_id)
    # 返回处理结果
    return JSONResponse(content={
        "total": len(files),
        "success": success_count,
        "failed": failed_count,
        "results": results,
        "upload_id": upload_id,
    })


@api_file_router.post("/upload_session_media")
async def upload_session_media(
    files: List[UploadFile] = File(...),
    session_id: str = "default",
):
    """
    上传多媒体附件（图片/音频/视频），原始字节保存到
    history_files/upload/<session>/media/，供聊天多模态消息以
    media://<stored_name> 引用（发送上游前由后端解析为 data URL / base64）。
    参数:
        files: 媒体文件列表，最多 10 个；单文件上限按类别：图片/音频 20MB、视频 500MB
               （视频流式落盘，不整体读入内存）；扩展名白名单校验
        session_id: 会话ID，用于隔离不同会话的媒体目录
    返回:
        {total, success, failed, results: [{filename, stored_name, media_ref, kind, mime, size}], upload_id}
    """
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="最多只能上传10个文件")
    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件")
    results = []
    success_count = 0
    for file in files:
        filename = file.filename or ""
        try:
            if not filename:
                results.append({"filename": "", "status": "failed", "message": "文件名缺失"})
                continue
            kind = media_kind(filename)
            if kind is None:
                results.append({
                    "filename": filename,
                    "status": "failed",
                    "message": "不支持的媒体类型（仅支持图片 png/jpg/jpeg/gif/webp/bmp/ico/tif/tiff"
                               "——ico/tif/tiff 会自动转为 png、"
                               "音频 wav/mp3/m4a/ogg/flac、视频 mp4/webm/mov/mkv）",
                })
                continue
            size_limit = media_size_limit(kind)
            if kind == "video":
                # 大文件流式落盘：边拷贝边计数、超限即中止，不整体读入内存
                saved = save_session_media_stream(session_id, filename, file.file)
                results.append({"status": "success", **saved})
                success_count += 1
                continue
            data = await file.read()
            if len(data) > size_limit:
                results.append({
                    "filename": filename,
                    "status": "failed",
                    "message": f"文件大小超过限制（{kind} 最大{size_limit // (1024 * 1024)}MB）",
                })
                continue
            saved = save_session_media(session_id, filename, data)
            results.append({"status": "success", **saved})
            success_count += 1
        except Exception as exc:
            results.append({
                "filename": filename,
                "status": "failed",
                "message": f"保存失败: {str(exc)}",
            })
    upload_id = None
    if success_count:
        upload_id = await _record_session_upload_id(session_id)
    return JSONResponse(content={
        "total": len(files),
        "success": success_count,
        "failed": len(files) - success_count,
        "results": results,
        "upload_id": upload_id,
    })


def _parse_range_header(range_header: str | None, total: int) -> tuple[int, int] | None:
    """解析 Range 请求头（bytes=start-end / bytes=-N），返回闭区间 (start, end)。

    不支持多区间（回退全量）；非法或完全越界返回 None（调用方回退 200 全量）。
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes="):].strip()
    if not spec or "," in spec:
        return None
    start_txt, _, end_txt = spec.partition("-")
    try:
        if not start_txt:
            # bytes=-N：末尾 N 字节
            tail = int(end_txt)
            if tail <= 0:
                return None
            return (max(0, total - tail), total - 1)
        start = int(start_txt)
        if start < 0 or start >= total:
            return None
        end = int(end_txt) if end_txt else total - 1
        end = min(end, total - 1)
        if end < start:
            return None
        return (start, end)
    except ValueError:
        return None


def _file_response_with_range(request_headers, path: Path, media_type: str) -> Response:
    """按 Range 请求头返回 206 切片或 200 全量；支持音频/视频拖动进度条即时跳转。

    浏览器 seek 到未缓冲位置时会发 Range 请求——不支持 Range 的全量 200
    只能顺序缓冲（进度条浅白色部分），缓冲完才能跳转。
    """
    total = path.stat().st_size
    range_header = request_headers.get("range")
    span = _parse_range_header(range_header, total)
    base_headers = {"Accept-Ranges": "bytes"}
    if span is None:
        with path.open("rb") as fp:
            data = fp.read()
        return Response(
            content=data,
            media_type=media_type,
            status_code=200,
            headers={**base_headers, "Content-Length": str(total)},
        )
    start, end = span
    length = end - start + 1
    with path.open("rb") as fp:
        fp.seek(start)
        data = fp.read(length)
    return Response(
        content=data,
        media_type=media_type,
        status_code=206,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{total}",
            "Content-Length": str(length),
        },
    )


@api_file_router.get("/get_session_media")
async def get_session_media(request: Request, name: str, session_id: str = "default"):
    """
    读取会话内已上传的媒体文件原始字节（前端消息气泡缩略图等用途）。
    支持 HTTP Range 请求（206 Partial Content）：音频/视频进度条可即时跳转，
    无需等待整体缓冲完成。
    参数:
        name: 保存时返回的 stored_name（不含 media:// 前缀，禁止路径分隔符）
        session_id: 会话ID
    返回:
        文件字节流（Content-Type 按扩展名推断；带 Range 时为 206 切片）
    """
    path = resolve_media_path(session_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    return _file_response_with_range(request.headers, path, media_mime_type(path.name))


@api_file_router.get("/get_session_document")
async def get_session_document(request: Request, name: str, session_id: str = "default"):
    """
    读取会话内已上传文档的原始字节（前端点击预览 PDF/文本/下载原文件）。
    支持 HTTP Range 请求（206），大 PDF 可按需加载。
    参数:
        name: 上传时返回的 stored_name（files/ 目录下，禁止路径分隔符）
        session_id: 会话ID
    返回:
        文件字节流（Content-Type 按扩展名推断；带 Range 时为 206 切片）；不存在 404
    """
    path = resolve_document_path(session_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="文档文件不存在")
    return _file_response_with_range(request.headers, path, media_mime_type(path.name))


@api_file_router.get("/get_session_file_memory")
async def get_file_history(
    number: int = 10,
    session_id: str = "default"
):
    """
    获取最近的文件上传历史
    参数:
        number: 获取的记录数量，默认10条，范围1-10
        session_id: 会话ID
    返回:
        文件历史记录列表
    """
    # 验证参数范围
    if number < 1 or number > 10:
        number = 10
    # 获取当前会话的记忆管理器
    session_file_memory = await get_file_memory_manager(session_id)
    history = session_file_memory.get_file_memory_all(number)
    return JSONResponse(content={
        "total": len(history),
        "files": history
    })


@api_file_router.get("/get_session_file_text")
async def get_file_history_text(
    number: int = 10,
    max_total_chars: int = 3000,
    session_id: str = "default"
):
    """
    获取最近文件历史的纯文本表示（用于LLM上下文）
    参数:
        number: 获取的记录数量，默认10条，范围1-10
        max_total_chars: 最大总字符数，默认3000
        session_id: 会话ID
    返回:
        纯文本格式的文件历史摘要
    """
    # 验证参数范围
    if number < 1 or number > 10:
        number = 10
    # 获取当前会话的记忆管理器
    session_file_memory = await get_file_memory_manager(session_id)
    text_summary = session_file_memory.get_file_memory_text(number, max_total_chars)
    return JSONResponse(content={
        "summary": text_summary,
        "length": len(text_summary)
    })


@api_file_router.delete("/delete_session_file_memory")
async def delete_file_history(
    filename: str,
    session_id: str = "default"
):
    """
    删除指定文件上传历史
    参数:
        filename: 文件名
        session_id: 会话ID
    返回:
        操作结果
    """
    # 获取当前会话的记忆管理器
    session_file_memory = await get_file_memory_manager(session_id)
    deleted_count = session_file_memory.delete_file_memory(filename)
    return JSONResponse(content={
        "message": f"已删除 {deleted_count} 个文件记录" if deleted_count > 0 else "未找到匹配的文件",
        "deleted_count": deleted_count
    })


@api_file_router.delete("/clear_session_file_memorys")
async def clear_file_history(
    session_id: str = "default"
):
    """
    清空指定会话的所有文件上传历史
    参数:
        session_id: 会话ID
    返回:
        操作结果
    """
    # 获取当前会话的记忆管理器
    session_file_memory = await get_file_memory_manager(session_id)
    result = session_file_memory.clear_file_memory()
    return JSONResponse(content={
        "message": result
    })

