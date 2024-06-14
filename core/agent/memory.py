class Memory:
    def __init__(self):
        self.short_term_memory = []
        self.long_term_memory = []

    def update_short_term(self, data):
        self.short_term_memory.append(data)

    def update_long_term(self, data):
        self.long_term_memory.append(data)

    def get_recent_memory(self):
        return self.short_term_memory[-5:]

    def get_all_memory(self):
        return self.long_term_memory
