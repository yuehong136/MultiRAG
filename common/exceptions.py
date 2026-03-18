class TaskCanceledException(Exception):
    def __init__(self, msg):
        self.msg = msg


class ArgumentException(Exception):
    def __init__(self, msg):
        self.msg = msg


class NotFoundException(Exception):
    def __init__(self, msg):
        self.msg = msg
