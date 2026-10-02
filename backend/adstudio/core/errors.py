class AppError(Exception):
    """RFC 7807-style error; `code` is stable for clients."""

    status = 400
    code = "bad_request"

    def __init__(self, detail: str, *, code: str | None = None, status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code
        if status:
            self.status = status


class NotFound(AppError):
    status = 404
    code = "not_found"


class NonRetryable(Exception):
    """Raised by a job handler to fail immediately without retries."""
