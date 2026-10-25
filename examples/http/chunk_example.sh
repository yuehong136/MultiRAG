#!/usr/bin/env bash
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
: "${MULTIRAG_DATASET_ID:?Set MULTIRAG_DATASET_ID}"
: "${MULTIRAG_DOCUMENT_ID:?Set MULTIRAG_DOCUMENT_ID}"
path="/datasets/$MULTIRAG_DATASET_ID/documents/$MULTIRAG_DOCUMENT_ID/chunks"
request GET "$path?page=1&page_size=10"
if [[ "${MULTIRAG_ALLOW_WRITE:-0}" != 1 ]]; then exit; fi
chunk_id=''
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$chunk_id" ]]; then
        request DELETE "$path" "$(jq -nc --arg id "$chunk_id" '{chunk_ids:[$id]}')" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
marker=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
chunk=$(request POST "$path" "$(jq -nc --arg content "Example chunk $marker" '{content:$content}')")
chunk_id=$(jq -er '.chunk.id | select(type == "string" and length > 0)' <<<"$chunk")
request PATCH "$path/$chunk_id" "$(jq -nc --arg content "Updated example chunk $marker" '{content:$content}')"
request GET "$path/$chunk_id"
