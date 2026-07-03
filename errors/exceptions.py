# app/core/exceptions.py


# app/core/exceptions.py


class CustomException(Exception):
    def __init__(self, message: str):
        self.message = message


class ScriptRunningError(Exception):
    def __init__(self, message: str):
        self.message = message


class ItemNotFoundError(Exception):
    pass


class AITranslateException(Exception):
    def __init__(self, message: str):
        self.message = message
