from .profile import Profile
from .memory import Memory
from .planning import Planner
from .action import Action

class Agent:
    def __init__(self, api_key):
        self.profile = Profile()
        self.memory = Memory()
        self.planner = Planner()
        self.action = Action(api_key)

    def handle(self, user_input):
        self.memory.update_short_term(user_input)
        plan = self.planner.plan(user_input)
        action_result = self.action.execute(plan)
        self.memory.update_long_term(action_result)
        return action_result

    def get_profile(self):
        return self.profile.get_profile()

    def get_memory(self):
        return {
            "short_term": self.memory.get_recent_memory(),
            "long_term": self.memory.get_all_memory()
        }
