from typing import List, Dict
from langchain.schema import Document
from langchain.vectorstores.milvus import Milvus
import os

from configs import kbs_config
# from server.db.repository import list_file_num_docs_id_by_kb_name_and_file_name
# from server.kb.utils import KnowledgeFile
from server.kb.kb_service.base import KBService, SupportedVSType, EmbeddingsFunAdapter, score_threshold_process

class MilvusKBService(KBService):
    """
    Milvus知识库服务类，继承自KBService，实现了基于Milvus的知识库操作。
    Milvus是一个向量数据库，用于存储和检索向量数据。
    """

    milvus: Milvus

    @staticmethod
    def get_collection(milvus_name):
        """
        获取Milvus中的集合（Collection）对象。

        :param milvus_name: 集合名称。
        :return: Collection对象。
        """
        from pymilvus import Collection
        return Collection(milvus_name)

    def get_doc_by_ids(self, ids: List[str]) -> List[Document]:
        """
        根据ID列表获取文档。

        :param ids: 文档ID列表。
        :return: 文档列表。
        """
        result = []
        if self.milvus.col:
            # 根据ID查询文档数据
            data_list = self.milvus.col.query(expr=f'pk in {[int(_id) for _id in ids]}', output_fields=["*"])
            for data in data_list:
                # 将文本内容提取出来，作为文档的内容
                text = data.pop("text")
                result.append(Document(page_content=text, metadata=data))
        return result

    def del_doc_by_ids(self, ids: List[str]) -> bool:
        """
        根据ID列表删除文档。

        :param ids: 文档ID列表。
        """
        self.milvus.col.delete(expr=f'pk in {ids}')

    @staticmethod
    def search(milvus_name, content, limit=3):
        """
        在Milvus中进行搜索。

        :param milvus_name: 集合名称。
        :param content: 搜索内容。
        :param limit: 返回结果的数量限制。
        :return: 搜索结果。
        """
        search_params = {
            "metric_type": "L2",
            "params": {"nprobe": 10},
        }
        c = MilvusKBService.get_collection(milvus_name)
        return c.search(content, "embeddings", search_params, limit=limit, output_fields=["content"])

    def do_create_kb(self):
        """
        创建知识库的实现。此处留空，子类可能实现具体的创建逻辑。
        """
        pass

    def vs_type(self) -> str:
        """
        返回向量数据库的类型。

        :return: 向量数据库类型字符串。
        """
        return SupportedVSType.MILVUS

    def _load_milvus(self):
        """
        加载Milvus实例。

        :param embedding_function: 嵌入模型的函数适配器。
        :param collection_name: 集合名称。
        :param connection_args: 连接参数。
        :param index_params: 索引参数。
        :param search_params: 搜索参数。
        """
        self.milvus = Milvus(embedding_function=EmbeddingsFunAdapter(self.embed_model),
                             collection_name=self.kb_name,
                             connection_args=kbs_config.get("milvus"),
                             index_params=kbs_config.get("milvus_kwargs")["index_params"],
                             search_params=kbs_config.get("milvus_kwargs")["search_params"]
                             )

    def do_init(self):
        """
        初始化知识库服务的实现，包括加载Milvus实例。
        """
        self._load_milvus()

    def do_drop_kb(self):
        """
        删除知识库的实现，包括释放和删除集合。
        """
        if self.milvus.col:
            self.milvus.col.release()
            self.milvus.col.drop()

    def do_search(self, query: str, top_k: int, score_threshold: float):
        """
        执行搜索操作。

        :param query: 搜索查询字符串。
        :param top_k: 返回结果的数量。
        :param score_threshold: 分数阈值。
        :return: 搜索结果。
        """
        self._load_milvus()
        embed_func = EmbeddingsFunAdapter(self.embed_model)
        embeddings = embed_func.embed_query(query)
        docs = self.milvus.similarity_search_with_score_by_vector(embeddings, top_k)
        return score_threshold_process(score_threshold, top_k, docs)

    def do_add_doc(self, docs: List[Document], **kwargs) -> List[Dict]:
        """
        添加文档到知识库。

        :param docs: 文档列表。
        :return: 添加文档的信息列表。
        """
        for doc in docs:
            # 处理文档的元数据，转换为适合存储的格式
            for k, v in doc.metadata.items():
                doc.metadata[k] = str(v)
            for field in self.milvus.fields:
                doc.metadata.setdefault(field, "")
            doc.metadata.pop(self.milvus._text_field, None)
            doc.metadata.pop(self.milvus._vector_field, None)

        ids = self.milvus.add_documents(docs)
        doc_infos = [{"id": id, "metadata": doc.metadata} for id, doc in zip(ids, docs)]
        return doc_infos

    # def do_delete_doc(self, kb_file: KnowledgeFile, **kwargs):
    #     """
    #     根据文件信息删除文档。
    #
    #     :param kb_file: 知识文件对象，包含知识库名称和文件名。
    #     """
    #     id_list = list_file_num_docs_id_by_kb_name_and_file_name(kb_file.kb_name, kb_file.filename)
    #     if self.milvus.col:
    #         self.milvus.col.delete(expr=f'pk in {id_list}')

    def do_clear_vs(self):
        """
        清空向量数据库的实现，包括删除集合并重新初始化。
        """
        if self.milvus.col:
            self.do_drop_kb()
            self.do_init()


# if __name__ == '__main__':
#     from server.db.base import Base, engine
#
#     Base.metadata.create_all(bind=engine)
#     milvusService = MilvusKBService("test")
#
#     print(milvusService.get_doc_by_ids(["444022434274215486"]))
