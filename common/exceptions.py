class TaskCanceledException(Exception):
    def __init__(self, msg):
        self.msg = msg


class ArgumentException(Exception):
    def __init__(self, msg):
        self.msg = msg


class NotFoundException(Exception):
    def __init__(self, msg):
        self.msg = msg


class ModelException(Exception):
    """Safe model failure; provider bodies and credentials never form the message."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
