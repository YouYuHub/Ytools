"""Public MCP connection hints and optional Playwright flag validation."""

def _option(args, name):
    for index, item in enumerate(args):
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
        if item == name and index + 1 < len(args) and not args[index + 1].startswith("--"):
            return args[index + 1]
    return None


def _browser_modes(config):
    if not isinstance(config, dict):
        return None
    args = [item for item in config.get("args", []) if isinstance(item, str)] if isinstance(config.get("args", []), list) else []
    command = str(config.get("command") or "")
    if not any("@playwright/mcp" in item.replace("\\", "/").lower() or "playwright-mcp" in item.lower() for item in [command, *args]):
        return None
    env = config.get("env") if isinstance(config.get("env"), dict) else {}
    extension = "--extension" in args or str(env.get("PLAYWRIGHT_MCP_EXTENSION", "")).lower() in {"true", "1"}
    isolated = "--isolated" in args or str(env.get("PLAYWRIGHT_MCP_ISOLATED", "")).lower() in {"true", "1"}
    cdp = bool(_option(args, "--cdp-endpoint") or env.get("PLAYWRIGHT_MCP_CDP_ENDPOINT"))
    remote = bool(_option(args, "--endpoint") or env.get("PLAYWRIGHT_MCP_ENDPOINT"))
    return args, env, extension, isolated, cdp, remote


def validate_browser_connection(config):
    modes = _browser_modes(config)
    if modes is None:
        return
    _, _, extension, isolated, cdp, remote = modes
    if extension and any((isolated, cdp, remote)):
        raise ValueError("Playwright 扩展连接方式不能同时启用 --isolated、--cdp-endpoint 或 --endpoint；连接已有浏览器请移除这些冲突参数。")
    if cdp and remote:
        raise ValueError("Playwright 的 --cdp-endpoint 与 --endpoint 是不同的连接方式，不能同时启用。")


def _describe_connection(config):
    """Only return fixed public text/booleans, not arbitrary config/env values."""
    result = {"persistent": isinstance(config, dict) and config.get("persistent") is True}
    modes = _browser_modes(config)
    if modes is None:
        return result
    args, env, extension, isolated, cdp, remote = modes
    channel = str(_option(args, "--browser") or env.get("PLAYWRIGHT_MCP_BROWSER") or "chrome")
    channel_label = {"chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "webkit": "WebKit"}.get(channel, "浏览器")
    mode = "extension" if extension else "cdp" if cdp else "remote" if remote else "isolated" if isolated else "managed"
    labels = {"extension": "现有浏览器 · 扩展连接", "cdp": "现有浏览器 · CDP 连接", "remote": "远程浏览器", "isolated": "独立隔离浏览器", "managed": "独立浏览器"}
    hints = {
        "extension": "首次实际调用时，请在运行后端的电脑上打开 " + channel_label + "，在 Playwright Extension 中授权并选择目标标签页。先列出标签页，按标题和 URL 确认目标，再读取快照与操作。手动改变页面后重新获取快照。",
        "cdp": "连接已开启远程调试的浏览器；先列出标签页，按标题和 URL 确认目标，再读取快照。",
        "remote": "连接配置的远程浏览器端点，页面范围由该端点决定。",
        "isolated": "在独立的隔离浏览器中打开页面，登录和页面状态属于此连接。",
        "managed": "由 Playwright 管理独立浏览器，页面状态属于该浏览器配置文件。",
    }
    hint = hints[mode]
    if mode == "extension" and not result["persistent"]:
        hint += " 此服务未启用 persistent，每次聊天工具调用都会重新连接；建议设为 true。"
    result.update(browser_mode=mode, browser_channel=channel_label, connection_label=labels[mode], connection_hint=hint)
    return result


def describe_mcp_server(config):
    """Public connection metadata; routing itself is generic and brand independent."""
    from util.mcp_profiles import profiles, select_profile, connection_config, public_profiles
    if not profiles(config):
        return _describe_connection(config)
    selected = select_profile(config)
    try:
        result = _describe_connection(connection_config(config, selected))
    except ValueError:
        result = {"configuration_error": True}
    entries = public_profiles(config)
    for entry in entries:
        # Locked-out profiles remain visible for configuration inspection, but
        # cannot be selected or launched by discovery, diagnostics or chat.
        effective = dict(config)
        effective.pop("locked_profile", None)
        try:
            entry.update(_describe_connection(connection_config(effective, entry["id"])))
        except ValueError:
            entry.update(configuration_error=True, connection_hint="此连接的启动配置无效，请检查 args/env/command。")
        entry["available"] = not entry.get("configuration_error") and (not config.get("locked_profile") or entry["id"] == config["locked_profile"])
    result.update(mcp_profiles=entries, default_profile=selected, locked_profile=config.get("locked_profile"))
    result["connection_label"] = "连接配置 · " + str(len(entries)) + " 项"
    result["connection_hint"] = "使用 mcp_profile 选择连接，原始工具参数放入 arguments。默认连接：" + selected + "。连续操作同一状态请保持连接一致。"
    if config.get("locked_profile"):
        result["connection_hint"] += " 此服务已固定连接，不能切换。"
    return result
