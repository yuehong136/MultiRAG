#!/usr/bin/env bash
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
if [[ "${MULTIRAG_ALLOW_WRITE:-0}" != 1 ]]; then
    request GET '/chats?page=1&page_size=10'
    exit
fi
chat_id='' session_id=''
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$session_id" ]]; then
        request DELETE "/chats/$chat_id/sessions" "$(jq -nc --arg id "$session_id" '{ids:[$id]}')" || status=1
    fi
    if [[ -n "$chat_id" ]]; then
        request DELETE "/chats/$chat_id" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
name="example-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
chat=$(request POST /chats "$(jq -nc --arg name "$name" '{name:$name,dataset_ids:[],prompt_config:{system:"Answer briefly",parameters:[]}}')")
chat_id=$(jq -er '.id | select(type == "string" and length > 0)' <<<"$chat")
session=$(request POST "/chats/$chat_id/sessions" '{"name":"Example session"}')
session_id=$(jq -er '.id | select(type == "string" and length > 0)' <<<"$session")
payload=$(jq -nc --arg chat "$chat_id" --arg session "$session_id" '{chat_id:$chat,session_id:$session,messages:[{role:"user",content:"What is retrieval augmented generation?"}],stream:false}')
request POST /chat/completions "$payload"
# Capture bounded SSE, require business success and its final data:true frame.
sse=$(curl --silent --show-error --fail-with-body --no-buffer --connect-timeout 5 --max-time "$timeout" \
    --header "Authorization: Bearer ${MULTIRAG_API_KEY}" --header 'Content-Type: application/json' \
    --data "$(jq '.stream=true' <<<"$payload")" "$base/chat/completions")
printf '%s\n' "$sse"
printf '%s\n' "$sse" | sed -n 's/^data: *//p' | jq -se 'length > 0 and all(.code == 0) and .[-1].data == true' >/dev/null
request GET "/chats/$chat_id/sessions"
