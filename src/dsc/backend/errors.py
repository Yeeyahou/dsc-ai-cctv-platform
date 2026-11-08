class Problem(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


def missing(code: str = "NOT_FOUND") -> Problem:
    return Problem(404, code, "요청한 리소스를 찾을 수 없습니다")
