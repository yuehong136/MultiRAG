"""Moodle REST boundary; source payloads follow the raw API, not SDK coercion."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any


class MoodleSource:
    def __init__(self, mode: str = "complete") -> None:
        self.mode = mode
        self.token = "synthetic-secret"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.core = SimpleNamespace(webservice=SimpleNamespace(get_site_info=lambda: SimpleNamespace(sitename="scratch")))
        self.modules = [
            {"id": 11, "modname": "assign", "name": "Assignment", "description": "First<b> </b>Last", "timemodified": 100},
            {"id": 12, "modname": "quiz", "name": "Quiz", "description": "Questions", "timemodified": 100},
        ]

    def post(self, function: str, **params: Any) -> Any:
        self.calls.append((function, params))
        if function == "core_course_get_courses":
            if self.mode == "listing-error":
                raise RuntimeError(self.token)
            if self.mode == "empty":
                return []
            if self.mode == "null":
                return None
            return [{"id": 1, "fullname": "One"}, {"id": 2, "fullname": "Two"}]
        if function == "core_course_get_contents":
            if params["courseid"] == 2:
                if self.mode == "course-error":
                    raise RuntimeError(self.token)
                if self.mode == "malformed":
                    return [{"id": 2}]
                return [{"id": 2, "name": "second", "modules": deepcopy(self.modules[1:])}]
            return [{"id": 1, "name": "first", "modules": deepcopy(self.modules[:1])}]
        if function == "mod_forum_get_forum_discussions":
            if self.mode == "forum-warning":
                return {"discussions": [], "warnings": [{"warningcode": "noview"}]}
            return {"discussions": [{"id": 90, "name": "Topic", "message": "Body", "timemodified": 150}], "warnings": []}
        raise AssertionError(function)
