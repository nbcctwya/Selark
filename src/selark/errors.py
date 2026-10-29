class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        upstream_status: int | None = None,
        *,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.upstream_status = upstream_status
        self.retry_after = retry_after

    def headers(self):
        if self.status == 401:
            return {"WWW-Authenticate": "Bearer"}
        if self.status in {429, 503} or self.retry_after is not None:
            seconds = self.retry_after
            if seconds is None:
                seconds = 60 if self.code == "caller_rate_limit" else 1
            return {"Retry-After": str(seconds)}
        return {}

    def body(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error" if self.status < 500 else "upstream_error",
                "code": self.code,
            }
        }
