"""Protocol errors are deterministic; transport retries must not repeat them."""


class MessagesProtocolError(ValueError):
    pass


class ResponsesProtocolError(ValueError):
    pass


class ResponsesUpstreamError(RuntimeError):
    def __init__(self, detail, response=None):
        super().__init__(detail)
        self.response = response or {}


class MessagesUpstreamError(RuntimeError):
    def __init__(self, detail, error_type="api_error"):
        super().__init__(detail)
        self.error_type = error_type
