# test_youdao_embed.py
from core.llm.embedding_model.youdao_embedding import YoudaoEmbed


def main():
    # 设置模型名称
    model_name = "bce-embedding-base_v1"

    # 初始化YoudaoEmbed实例
    embedder = YoudaoEmbed(model_name=model_name)

    # 测试文本
    texts = [
        "这是一个测试文本。",
        "我们正在测试YoudaoEmbed的编码功能。",
        "希望这个测试能够顺利通过。"
    ]

    # 对文本进行编码
    embeddings = embedder.encode(texts)

    # 打印结果
    print("Embeddings:", embeddings)

    # 测试单个查询文本
    query_text = "这是一个查询文本。"
    query_embedding = embedder.encode_queries(query_text)

    # 打印结果
    print("Query Embedding:", query_embedding)


if __name__ == '__main__':
    main()
