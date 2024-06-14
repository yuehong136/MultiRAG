class Planner:
    def plan(self, input):
        # 简单的计划示例
        if "SQL" in input:
            return f"{input} \n\n 生成 SQL 语句"
        else:
            return "无法处理非 SQL 相关问题"
