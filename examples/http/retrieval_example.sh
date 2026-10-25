#!/usr/bin/env bash
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
: "${MULTIRAG_DATASET_ID:?Set MULTIRAG_DATASET_ID}"
for keyword in false true; do
    request POST /retrieval "$(jq -nc --arg id "$MULTIRAG_DATASET_ID" --argjson keyword "$keyword" \
        '{dataset_ids:[$id],question:"retrieval workflow",page:1,page_size:5,similarity_threshold:0,top_k:10,keyword:$keyword}')"
done
