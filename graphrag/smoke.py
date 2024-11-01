import argparse
import json

from graphrag import leiden
from graphrag.community_reports_extractor import CommunityReportsExtractor
from graphrag.entity_resolution import EntityResolution
from graphrag.graph_extractor import GraphExtractor
from graphrag.leiden import add_community_info2graph

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-t', '--tenant_id', default=False, help="Tenant ID", action='store', required=True)
    parser.add_argument('-d', '--doc_id', default=False, help="Document ID", action='store', required=True)
    args = parser.parse_args()

    from api.db import LLMType
    from api.db.services.llm_service import LLMBundle
    from api.settings import retrievaler
    from api.db.database import SessionLocal

    db = SessionLocal()
    ex = GraphExtractor(LLMBundle(db, args.tenant_id, LLMType.CHAT))
    docs = [d["text"] for d in
            retrievaler.chunk_list(args.doc_id, args.tenant_id, max_count=6, fields=["content_with_weight"])]
    graph = ex(docs)

    er = EntityResolution(LLMBundle(db, args.tenant_id, LLMType.CHAT))
    graph = er(graph.output)

    comm = leiden.run(graph.output, {})
    add_community_info2graph(graph.output, comm)

    # print(json.dumps(nx.node_link_data(graph.output), ensure_ascii=False,indent=2))
    print(json.dumps(comm, ensure_ascii=False, indent=2))

    cr = CommunityReportsExtractor(LLMBundle(db, args.tenant_id, LLMType.CHAT))
    cr = cr(graph.output)
    print("------------------ COMMUNITY REPORT ----------------------\n", cr.output)
    print(json.dumps(cr.structured_output, ensure_ascii=False, indent=2))