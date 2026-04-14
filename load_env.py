"""
.env文件加载模块
其中的变量设置应该为字符串类型类似于：

```
API_KEY=your_api_key
SECRET_KEY   =       your_secret_key
```
或者：
```
API_KEY = " your_api_key "
SECRET_KEY =   "     your_secret_key      "
```
"""
from pathlib import Path

env_path = None
env_vars = {}


def _resolve_variable_references(value: str, variables: dict) -> str:
    """
    解析字符串中的变量引用，如${VAR_NAME}
    :param value: 要解析的字符串
    :param variables: 变量字典
    :return: 解析后的字符串
    """
    # 支持使用 # 在变量名中注释后面所有内容
    result = value.split("#")[0].strip().strip("'").strip("\"").strip()
    while True:
        start = result.find("${")
        if start == -1:
            break
        end = result.find("}", start)
        if end == -1:
            break
        var_name = result[start+2:end].strip()
        if var_name in variables:
            # 递归解析，防止嵌套引用
            replacement = _resolve_variable_references(variables[var_name], variables)
            result = result[:start] + replacement + result[end+1:]
        else:
            # 变量未定义，保留原样
            break
    return result


def init_path(
    path: str = None, /,
    filename: str = ".env",
    coding: str = "utf-8"
) -> None:
    """
    初始化.env文件路径
    :param path: 文件路径
    :return: None
    """
    if path is None:
        path = Path.cwd() / filename
    global env_path
    env_path = path
    if not env_path.exists():
        raise FileNotFoundError(f"{env_path} not found")
    
    # 第一阶段：读取所有变量到临时字典
    raw_vars = {}
    with open(env_path, "r", encoding=coding) as f:
        for line in f:
            if "=" in line:
                var_name, var_value = line.strip().split("=")
                if not var_name.strip().startswith("#"):
                    raw_vars[var_name.strip().strip("\"").strip("\'").strip()] = var_value.strip().strip("\"").strip("'").strip()
    
    # 第二阶段：解析所有变量引用
    global env_vars
    env_vars = {}
    for var_name, raw_value in raw_vars.items():
        env_vars[var_name] = _resolve_variable_references(raw_value, raw_vars)


def load_var(var_name: str, default: str = None,
    #coding: str = "utf-8"
) -> str | None:
    """
    加载.env文件中的变量值
    :param var_name: 变量名
    :param default: 默认值
    :return: 变量值
    """
    if env_path is None:
        return default
        # raise ValueError("env_path is not set, please call init_path() first")
    #with open(env_path, "r", encoding=coding) as f:
    #    for line in f:
    #        if line.startswith(var_name):
    #            return line.split("=")[1].strip().strip("\"").strip("'").strip()
    if var_name in env_vars:
        return env_vars[var_name]
    return default


if __name__ == "__main__":
    init_path()
    print(env_path)
    print(env_vars)
    # print(load_var("API_KEY"))
