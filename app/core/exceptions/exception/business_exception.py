
class BusinessException(Exception):
    '''自定义业务异常'''
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")