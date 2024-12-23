from core.llm.chat_model.base import Base


class SparkChat(Base):
    def __init__(
            self, key, model_name, base_url="https://spark-api-open.xf-yun.com/v1"
    ):
        if not base_url:
            base_url = "https://spark-api-open.xf-yun.com/v1"
        model2version = {
            "Spark-Max": "generalv3.5",
            "Spark-Lite": "general",
            "Spark-Pro": "generalv3",
            "Spark-Pro-128K": "pro-128k",
            "Spark-4.0-Ultra": "4.0Ultra",
        }
        version2model = {v: k for k, v in model2version.items()}
        assert model_name in model2version or model_name in version2model, f"The given model name is not supported yet. Support: {list(model2version.keys())}"
        if model_name in model2version:
            model_version = model2version[model_name]
        else:
            model_version = model_name
        super().__init__(key, model_version, base_url)