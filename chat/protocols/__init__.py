"""Model wire protocols; application messages remain protocol independent."""

SUPPORTED_API_TYPES = frozenset({"chat_completions", "messages", "responses"})


def protocol_client(config):
    """Factory for protocols with their own transport; Chat keeps its facade."""
    import importlib
    module = {"messages": "chat.messages_client", "responses": "chat.responses_client"}.get(
        normalize_api_type(config.get("apiType")))
    return importlib.import_module(module) if module else None


def normalize_api_type(value):
    return str(value or "chat_completions").strip().casefold().replace("-", "_")


def supports_model(config):
    return isinstance(config, dict) and normalize_api_type(config.get("apiType")) in SUPPORTED_API_TYPES
