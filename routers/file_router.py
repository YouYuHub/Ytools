# coding: utf-8
# fastapi 库导入
from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import List
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# 自定义模块导入
from memory.file_memory import get_file_memory_manager
from factory.file_factory import extract_text_from_bytes

# 创建 API 路由器实例
api_file_router = APIRouter(prefix="/file")

# 创建线程池用于并发处理文件解析
file_parse_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="file_parse")


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


@api_file_router.post("/upload")
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
        session_file_memory = get_file_memory_manager(session_id)
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
                    # 添加到 file_memory（使用session_id隔离）
                    add_result = session_file_memory.add_file_memory(file_info, max_items=10)
                    results.append({
                        "filename": parse_result["filename"],
                        "status": "success",
                        "message": add_result,
                        "type": parse_result["type"],
                        "content_length": parse_result["content_length"]
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
    # 返回处理结果
    return JSONResponse(content={
        "total": len(files),
        "success": success_count,
        "failed": failed_count,
        "results": results
    })


@api_file_router.get("/memory")
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
    session_file_memory = get_file_memory_manager(session_id)
    history = session_file_memory.get_file_memory(number)
    return JSONResponse(content={
        "total": len(history),
        "files": history
    })


@api_file_router.get("/memory/text")
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
    session_file_memory = get_file_memory_manager(session_id)
    text_summary = session_file_memory.get_file_memory_text(number, max_total_chars)
    return JSONResponse(content={
        "summary": text_summary,
        "length": len(text_summary)
    })


@api_file_router.delete("/memory/{filename}")
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
    session_file_memory = get_file_memory_manager(session_id)
    deleted_count = session_file_memory.delete_file_memory(filename)
    return JSONResponse(content={
        "message": f"已删除 {deleted_count} 个文件记录" if deleted_count > 0 else "未找到匹配的文件",
        "deleted_count": deleted_count
    })


@api_file_router.delete("/memory")
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
    session_file_memory = get_file_memory_manager(session_id)
    result = session_file_memory.clear_file_memory()
    return JSONResponse(content={
        "message": result
    })

