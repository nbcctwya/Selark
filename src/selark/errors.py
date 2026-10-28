class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, upstream_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.upstream_status = upstream_status

    def body(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error" if self.status < 500 else "upstream_error",
                "code": self.code,
            }
        }
