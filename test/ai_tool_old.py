import asyncio
import functools
import inspect
# import json
from typing import (
    Any, 
    Callable, 
    Dict, 
    List, 
    Optional, 
    Type, 
    Union, 
    get_type_hints,
    get_args,
    get_origin,
)
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
import re


@dataclass
class FieldInfo:
    """字段信息，类似 Pydantic 的 Field"""
    default: Any = ...
    description: str = ""
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    ge: Optional[Union[int, float]] = None  # greater or equal
    le: Optional[Union[int, float]] = None  # less or equal
    gt: Optional[Union[int, float]] = None  # greater than
    lt: Optional[Union[int, float]] = None  # less than


def Field(
    default: Any = ...,
    *,
    description: str = "",
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    ge: Optional[Union[int, float]] = None,
    le: Optional[Union[int, float]] = None,
    gt: Optional[Union[int, float]] = None,
    lt: Optional[Union[int, float]] = None,
) -> FieldInfo:
    """
    定义字段的验证规则
    
    参数:
        default: 默认值
        description: 字段描述
        min_length: 最小长度（用于字符串/列表）
        max_length: 最大长度（用于字符串/列表）
        ge: 大于等于（用于数字）
        le: 小于等于（用于数字）
        gt: 大于（用于数字）
        lt: 小于（用于数字）
    
    返回:
        FieldInfo 实例
    """
    return FieldInfo(
        default=default,
        description=description,
        min_length=min_length,
        max_length=max_length,
        ge=ge,
        le=le,
        gt=gt,
        lt=lt,
    )


class ValidationError(Exception):
    """参数验证错误异常"""
    def __init__(self, errors: List[Dict[str, Any]]):
        self.errors = errors
        error_messages = "\n".join([f"- {e['field']}: {e['message']}" for e in errors])
        super().__init__(f"参数验证失败:\n{error_messages}")


class SimpleSchema:
    """
    简单的 Schema 类，用于参数验证
    替代 Pydantic BaseModel，仅使用标准库
    """
    
    def __init__(self, **kwargs):
        self._fields = {}
        self._field_info = {}
        
        # 获取子类的字段定义
        if hasattr(self.__class__, '__annotations__'):
            for field_name, field_type in self.__class__.__annotations__.items():
                field_value = getattr(self.__class__, field_name, ...)
                if isinstance(field_value, FieldInfo):
                    self._field_info[field_name] = field_value
                    if field_value.default != ...:
                        self._fields[field_name] = field_value.default
                else:
                    self._field_info[field_name] = FieldInfo(default=field_value if field_value != ... else ...)
        
        # 设置传入的值
        for key, value in kwargs.items():
            setattr(self, key, value)
    
    def __setattr__(self, name: str, value: Any):
        if name.startswith('_'):
            super().__setattr__(name, value)
            return
        
        # 验证值
        if hasattr(self, '_field_info') and name in self._field_info:
            field_info = self._field_info[name]
            errors = []
            
            # 类型检查
            if hasattr(self.__class__, '__annotations__'):
                expected_type = self.__class__.__annotations__.get(name)
                if expected_type and not self._check_type(value, expected_type):
                    errors.append({
                        "field": name,
                        "message": f"期望类型 {expected_type}, 得到 {type(value).__name__}"
                    })
            
            # 验证规则
            if field_info:
                if field_info.min_length is not None and hasattr(value, '__len__'):
                    if len(value) < field_info.min_length:
                        errors.append({
                            "field": name,
                            "message": f"长度不能小于 {field_info.min_length}"
                        })
                
                if field_info.max_length is not None and hasattr(value, '__len__'):
                    if len(value) > field_info.max_length:
                        errors.append({
                            "field": name,
                            "message": f"长度不能大于 {field_info.max_length}"
                        })
                
                if isinstance(value, (int, float)):
                    if field_info.ge is not None and value < field_info.ge:
                        errors.append({
                            "field": name,
                            "message": f"值不能小于 {field_info.ge}"
                        })
                    if field_info.le is not None and value > field_info.le:
                        errors.append({
                            "field": name,
                            "message": f"值不能大于 {field_info.le}"
                        })
                    if field_info.gt is not None and value <= field_info.gt:
                        errors.append({
                            "field": name,
                            "message": f"值必须大于 {field_info.gt}"
                        })
                    if field_info.lt is not None and value >= field_info.lt:
                        errors.append({
                            "field": name,
                            "message": f"值必须小于 {field_info.lt}"
                        })
            
            if errors:
                raise ValidationError(errors)
        
        super().__setattr__(name, value)
    
    def _check_type(self, value: Any, expected_type: Type) -> bool:
        """检查值的类型是否匹配"""
        # 处理 Optional
        origin = get_origin(expected_type)
        if origin is Union:
            args = get_args(expected_type)
            if type(None) in args:
                if value is None:
                    return True
                # 获取非 None 类型
                non_none_args = [a for a in args if a is not type(None)]
                if non_none_args:
                    expected_type = non_none_args[0]
                else:
                    return False
            
            origin = get_origin(expected_type)
            if origin:
                args = get_args(expected_type)
                return any(self._check_type(value, arg) for arg in args)
        
        # 处理泛型
        if origin is not None:
            if origin is list and isinstance(value, list):
                item_type = get_args(expected_type)[0] if get_args(expected_type) else Any
                return all(self._check_type(item, item_type) for item in value)
            elif origin is dict and isinstance(value, dict):
                return True  # 简化处理
            elif origin is tuple and isinstance(value, tuple):
                return True  # 简化处理
            return isinstance(value, origin)
        
        # 基本类型检查
        if expected_type == int:
            return isinstance(value, int) and not isinstance(value, bool)
        elif expected_type == float:
            return isinstance(value, (int, float))
        elif expected_type == str:
            return isinstance(value, str)
        elif expected_type == bool:
            return isinstance(value, bool)
        elif expected_type == list:
            return isinstance(value, list)
        elif expected_type == dict:
            return isinstance(value, dict)
        elif expected_type == Any:
            return True
        else:
            return isinstance(value, expected_type)
    
    def model_dump(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {}
        for name in self._field_info.keys():
            if hasattr(self, name):
                result[name] = getattr(self, name)
        return result
    
    @classmethod
    def model_json_schema(cls) -> Dict[str, Any]:
        """生成 JSON Schema"""
        properties = {}
        required = []
        
        if hasattr(cls, '__annotations__'):
            for field_name, field_type in cls.__annotations__.items():
                field_value = getattr(cls, field_name, ...)
                
                # 获取字段信息
                if isinstance(field_value, FieldInfo):
                    field_info = field_value
                    default = field_info.default
                else:
                    field_info = FieldInfo()
                    default = field_value
                
                # 判断是否必填
                if default == ...:
                    required.append(field_name)
                
                # 转换类型为 JSON Schema 格式
                json_type = cls._type_to_json_type(field_type)
                
                prop = {
                    "title": field_name.replace('_', ' ').title(),
                    "type": json_type,
                }
                
                if field_info.description:
                    prop["description"] = field_info.description
                
                if default != ...:
                    prop["default"] = default
                
                if field_info.min_length is not None:
                    prop["minLength"] = field_info.min_length
                
                if field_info.max_length is not None:
                    prop["maxLength"] = field_info.max_length
                
                if field_info.ge is not None:
                    prop["minimum"] = field_info.ge
                
                if field_info.le is not None:
                    prop["maximum"] = field_info.le
                
                properties[field_name] = prop
        
        schema = {
            "title": cls.__name__,
            "type": "object",
            "properties": properties,
        }
        
        if required:
            schema["required"] = required
        
        return schema
    
    @staticmethod
    def _type_to_json_type(python_type: Type) -> str:
        """将 Python 类型转换为 JSON Schema 类型"""
        origin = get_origin(python_type)
        
        if python_type == int:
            return "integer"
        elif python_type == float:
            return "number"
        elif python_type == str:
            return "string"
        elif python_type == bool:
            return "boolean"
        elif python_type == list or origin is list:
            return "array"
        elif python_type == dict or origin is dict:
            return "object"
        elif origin is Union:
            args = get_args(python_type)
            non_none = [a for a in args if a is not type(None)]
            if non_none:
                return SimpleSchema._type_to_json_type(non_none[0])
            return "null"
        elif python_type == type(None):
            return "null"
        else:
            return "string"  # 默认


class BaseTool(ABC):
    """工具基类，定义工具的标准接口"""
    
    name: str
    """工具的唯一名称"""
    
    description: str
    """工具的描述，告诉 AI 这个工具做什么"""
    
    args_schema: Optional[Type[SimpleSchema]] = None
    """参数的 Schema"""
    
    return_direct: bool = False
    """是否直接返回结果（用于 Agent）"""
    
    @abstractmethod
    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """同步执行工具"""
        pass
    
    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """异步执行工具（默认转为同步调用）"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(self._run, *args, **kwargs))
    
    def invoke(self, input_data: Dict[str, Any]) -> Any:
        """
        使用给定参数调用工具
        参数:
            input_data: 包含参数的字典
        返回:
            工具执行结果
        """
        if self.args_schema:
            # 验证并解析参数
            validated_input = self.args_schema(**input_data)
            return self._run(**validated_input.model_dump())
        else:
            return self._run(**input_data)
    
    async def ainvoke(self, input_data: Dict[str, Any]) -> Any:
        """
        异步调用工具
        参数:
            input_data: 包含参数的字典
        返回:
            工具执行结果
        """
        if self.args_schema:
            validated_input = self.args_schema(**input_data)
            return await self._arun(**validated_input.model_dump())
        else:
            return await self._arun(**input_data)
    
    @property
    def args(self) -> Dict[str, Any]:
        """返回工具的 JSON Schema 格式参数定义"""
        if self.args_schema:
            return self.args_schema.model_json_schema()
        return {}
    
    def __repr__(self) -> str:
        return f"Tool(name='{self.name}', description='{self.description}')"


class StructuredTool(BaseTool):
    """结构化实现，支持更复杂的配置"""
    
    def __init__(
        self,
        name: str,
        description: str,
        func: Callable,
        args_schema: Optional[Type[SimpleSchema]] = None,
        return_direct: bool = False,
        coroutine_func: Optional[Callable] = None,
    ):
        self.name = name
        self.description = description
        self.func = func
        self.args_schema = args_schema
        self.return_direct = return_direct
        self.coroutine_func = coroutine_func
        
        # 如果没有提供异步函数，检查原函数是否是异步的
        # if not coroutine_func and asyncio.iscoroutinefunction(func): # 在 py3.14 以下可用
        if not coroutine_func and inspect.iscoroutinefunction(func):
            self.coroutine_func = func
    
    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """同步执行"""
        return self.func(*args, **kwargs)
    
    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        """异步执行"""
        if self.coroutine_func:
            return await self.coroutine_func(*args, **kwargs)
        else:
            # 如果没有异步版本，用同步方式执行
            return await super()._arun(*args, **kwargs)


def _extract_docstring_description(func: Callable) -> str:
    """从函数的 docstring 中提取描述"""
    doc = inspect.getdoc(func) or ""
    if not doc:
        return ""
    
    # 获取第一行作为描述
    first_line = doc.split('\n')[0].strip()
    return first_line


def _infer_schema_from_signature(func: Callable) -> Type[SimpleSchema]:
    """从函数签名推断参数 schema"""
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    
    fields = {}
    for param_name, param in sig.parameters.items():
        # 跳过 *args 和 **kwargs
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        
        # 获取参数类型
        param_type = type_hints.get(param_name, Any)
        
        # 获取参数默认值
        default_value = ... if param.default == param.empty else param.default
        
        # 创建字段
        fields[param_name] = (param_type, FieldInfo(default=default_value))
    
    # 动态创建 Schema 类
    schema_class = type(
        f"{func.__name__}_Schema",
        (SimpleSchema,),
        {
            '__annotations__': {k: v[0] for k, v in fields.items()},
            **{k: v[1] for k, v in fields.items()}
        }
    )
    
    return schema_class


def ai_tool(
    name_or_callable: Optional[Union[str, Callable]] = None,
    args_schema: Optional[Type[SimpleSchema]] = None,
    return_direct: bool = False,
    infer_schema: bool = True,
):
    """
    AI 工具装饰器 - 将普通函数转换为 AI 可调用的工具
    这是 LangChain @tool 装饰器的完整实现，具备所有核心功能。
    完全使用 Python 标准库实现，不依赖 Pydantic 等三方库。
    参数:
        name_or_callable: 工具名称（字符串）或被装饰的函数
        args_schema: 自定义的 Schema 类用于参数验证
        return_direct: 是否直接返回结果（用于 Agent）
        infer_schema: 是否自动从函数签名推断 schema
    返回:
        包装后的工具对象（BaseTool 的子类实例）
    示例:
        # 最简单的用法
        # @ai_tool
        def add(a: int, b: int) -> int:
            '''两个数相加'''
            return a + b
        
        # 自定义工具名称
        # @ai_tool("custom_add")
        def add_numbers(a: int, b: int) -> int:
            '''加法运算'''
            return a + b
        
        # 使用自定义 schema
        class AddInput(SimpleSchema):
            a: int = Field(description="第一个加数")
            b: int = Field(description="第二个加数")
        
        # @ai_tool(args_schema=AddInput)
        def add(a: int, b: int) -> int:
            '''两个数相加'''
            return a + b
        
        # 异步工具
        # @ai_tool
        async def search(query: str) -> str:
            '''搜索信息'''
            return await async_search(query)
    """
    
    def decorator(func: Callable) -> BaseTool:
        """装饰器主逻辑"""
        
        # 确定工具名称
        if isinstance(name_or_callable, str):
            tool_name = name_or_callable
        elif callable(name_or_callable):
            tool_name = name_or_callable.__name__
        else:
            tool_name = func.__name__
        
        # 提取描述
        description = _extract_docstring_description(func)
        if not description:
            description = f"Execute {tool_name} function"
        
        # 确定 schema
        schema_to_use = args_schema
        if schema_to_use is None and infer_schema:
            try:
                schema_to_use = _infer_schema_from_signature(func)
            except Exception as e:
                print(f"Warning: Could not infer schema for {tool_name}: {e}")
        
        # 检查是否是异步函数
        # if asyncio.iscoroutinefunction(func): # 在 py3.14 以下可用
        if inspect.iscoroutinefunction(func):
            # 创建异步工具
            tool_instance = StructuredTool(
                name=tool_name,
                description=description,
                func=func,
                args_schema=schema_to_use,
                return_direct=return_direct,
                coroutine_func=func,
            )
        else:
            # 创建同步工具
            tool_instance = StructuredTool(
                name=tool_name,
                description=description,
                func=func,
                args_schema=schema_to_use,
                return_direct=return_direct,
            )
        
        # 保留原函数的元数据
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        
        # 将 wrapper 的一些属性复制到工具实例
        tool_instance.__wrapped__ = wrapper
        tool_instance.__doc__ = func.__doc__
        
        return tool_instance
    
    # 处理 # @ai_tool 和 # @ai_tool() 两种情况
    if callable(name_or_callable):
        # # @ai_tool 形式（没有括号）
        return decorator(name_or_callable)
    else:
        # # @ai_tool() 形式（有括号）
        return decorator


# 便捷函数：从已有函数创建工具
def create_tool_from_function(
    func: Callable,
    name: Optional[str] = None,
    description: Optional[str] = None,
    args_schema: Optional[Type[SimpleSchema]] = None,
    return_direct: bool = False,
) -> BaseTool:
    """
    从已有函数创建工具（不使用装饰器语法）
    参数:
        func: 要转换的函数
        name: 工具名称（默认使用函数名）
        description: 工具描述（默认从 docstring 提取）
        args_schema: 自定义参数 schema
        return_direct: 是否直接返回
    返回:
        工具实例
    """
    tool_name = name or func.__name__
    tool_desc = description or _extract_docstring_description(func)
    schema_to_use = args_schema
    if schema_to_use is None:
        try:
            schema_to_use = _infer_schema_from_signature(func)
        except Exception:
            pass
    return StructuredTool(
        name=tool_name,
        description=tool_desc,
        func=func,
        args_schema=schema_to_use,
        return_direct=return_direct,
        # coroutine_func=func if asyncio.iscoroutinefunction(func) else None, # 在 py3.14 以下可用
        coroutine_func=func if inspect.iscoroutinefunction(func) else None,
    )
