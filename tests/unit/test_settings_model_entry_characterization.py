"""行为钉板：user_default_llm.default_models 的模型条目解析规则。

这套 `name@factory` 拼接与兜底逻辑将在配置重构 Phase 1 收编进
common/app_config.py 的 UserDefaultLLMConfig，语义必须逐条保持。
"""

from common.settings import _parse_model_entry, _resolve_per_model_config


class TestParseModelEntry:
    def test_string_entry(self):
        assert _parse_model_entry("glm-4") == {"name": "glm-4", "factory": None, "api_key": None, "base_url": None}

    def test_dict_entry_with_name(self):
        entry = {"name": "glm-4", "factory": "ZHIPU-AI", "api_key": "k", "base_url": "http://x"}
        assert _parse_model_entry(entry) == {"name": "glm-4", "factory": "ZHIPU-AI", "api_key": "k", "base_url": "http://x"}

    def test_dict_entry_model_key_alias(self):
        # `model` 是 `name` 的别名
        assert _parse_model_entry({"model": "glm-4"})["name"] == "glm-4"

    def test_dict_entry_name_wins_over_model(self):
        assert _parse_model_entry({"name": "a", "model": "b"})["name"] == "a"

    def test_garbage_entry_yields_empty(self):
        assert _parse_model_entry(42) == {"name": "", "factory": None, "api_key": None, "base_url": None}
        assert _parse_model_entry(None) == {"name": "", "factory": None, "api_key": None, "base_url": None}


class TestResolvePerModelConfig:
    def test_appends_factory_when_name_has_no_at(self):
        entry = {"name": "glm-4", "factory": "ZHIPU-AI", "api_key": None, "base_url": None}
        resolved = _resolve_per_model_config(entry, "BACKUP", "bk", "http://bk")
        assert resolved["model"] == "glm-4@ZHIPU-AI"
        assert resolved["factory"] == "ZHIPU-AI"

    def test_name_with_at_kept_verbatim(self):
        entry = {"name": "glm-4@ZHIPU-AI", "factory": "OTHER", "api_key": None, "base_url": None}
        assert _resolve_per_model_config(entry, None, None, None)["model"] == "glm-4@ZHIPU-AI"

    def test_backup_values_fill_missing_fields(self):
        entry = {"name": "glm-4", "factory": None, "api_key": None, "base_url": None}
        resolved = _resolve_per_model_config(entry, "BACKUP", "backup-key", "http://backup")
        assert resolved == {"model": "glm-4@BACKUP", "factory": "BACKUP", "api_key": "backup-key", "base_url": "http://backup"}

    def test_entry_values_win_over_backups(self):
        entry = {"name": "glm-4", "factory": "MINE", "api_key": "mine-key", "base_url": "http://mine"}
        resolved = _resolve_per_model_config(entry, "BACKUP", "backup-key", "http://backup")
        assert resolved == {"model": "glm-4@MINE", "factory": "MINE", "api_key": "mine-key", "base_url": "http://mine"}

    def test_empty_name_never_gets_factory_suffix(self):
        entry = {"name": "", "factory": None, "api_key": None, "base_url": None}
        resolved = _resolve_per_model_config(entry, "BACKUP", None, None)
        assert resolved["model"] == ""
        assert resolved["factory"] == "BACKUP"

    def test_whitespace_name_is_stripped(self):
        entry = {"name": "  glm-4  ", "factory": "F", "api_key": None, "base_url": None}
        assert _resolve_per_model_config(entry, None, None, None)["model"] == "glm-4@F"
