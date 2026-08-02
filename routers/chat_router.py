from typing import Optional, List
# fastapi 库导入
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

# 自定义模块导入
from config import ChatLLMRequest
from factory.chat_factory import tool_chat_server, stop_chat_task
from memory.chat_memory import ChatMemoryManager, get_chat_memory_manager
from routers.chat_config_router import get_chat_work_dir_config

# 创建 API 路由器实例
api_chat_router = APIRouter()


# 聊天主接口
@api_chat_router.post('/chat_with_tool')
async def chat_with_tool(request: ChatLLMRequest):
    """ 用户聊天信息，流式响应 """
    return StreamingResponse(tool_chat_server(request), media_type='text/event-stream')


# 停止当前聊天任务接口
@api_chat_router.post('/stop_chat')
async def stop_chat(session_id: str = "default"):
    """ 停止当前聊天任务 """
    try:
        await stop_chat_task(session_id)
        return {'stop_chat': 'stopped'}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.get('/chat_history/sessions')
async def list_chat_history_sessions():
    """
    列出所有聊天历史会话文件
    返回:
        会话文件名列表
    """
    try:
        sessions = ChatMemoryManager.list_chat_sessions()
        return JSONResponse(content=[session.name for session in sessions])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.get('/chat_history/file')
async def get_chat_history_file(session_id: str = "default"):
    """
    下载指定会话的聊天历史文件
    参数:
        session_id: 会话ID
    返回:
        文件响应
    """
    return ChatMemoryManager.get_chat_session_file(session_id)


@api_chat_router.get('/chat_history/meta')
async def get_chat_history_meta(session_id: str = "default"):
    """
    获取指定会话的聊天历史元数据（jsonl 首行 _meta）
    参数:
        session_id: 会话ID
    返回:
        元数据字典
    """
    try:
        manager = await get_chat_memory_manager(session_id)
        meta = await manager.get_session_meta()
        return JSONResponse(content=meta)
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_router.delete('/chat_history/delete_file')
async def delete_chat_history(session_id: str = "default"):
    """
    删除指定会话的聊天历史文件
    参数:
        session_id: 会话ID
    返回:
        操作结果
    """
    result = ChatMemoryManager.delete_chat_session_file(session_id)
    return JSONResponse(content=result)


@api_chat_router.delete("/chat_history/delete_lines")
async def delete_chat_history_lines(
    startline: int = Query(..., description="要删除的起始行号(1-based)", ge=1),
    endline: int = Query(..., description="要删除的结束行号(1-based,包含)", ge=1),
    session_id: str = "default"
):
    """
    删除指定会话的聊天历史文件中的指定行范围
    参数:
        startline: 要删除的起始行号(从1开始)
        endline: 要删除的结束行号(从1开始,包含该行)
        session_id: 会话ID
    返回:
        操作结果
    """
    if startline > endline:
        raise HTTPException(status_code=400, detail=f"起始行号({startline})不能大于结束行号({endline})")
    try:
        result = ChatMemoryManager.delete_chat_session_file_line(session_id, startline, endline)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content=result)

