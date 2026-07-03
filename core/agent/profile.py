class Profile:
    def __init__(self):
        self.name = "SQL助手"
        self.role = "SQLExpert"
        self.goal = "SQL 生成、翻译、优化"
        self.constraints = ["只能处理 SQL 相关问题"]

    def get_profile(self):
        return {"name": self.name, "role": self.role, "goal": self.goal, "constraints": self.constraints}
