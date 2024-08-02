from fastapi import APIRouter
from core.llm.chat_model.chat_factory import ChatFactory
from pydantic import BaseModel
from typing import List, Dict, Any
from starlette.responses import StreamingResponse

router = APIRouter()

class ChatRequest(BaseModel):
    key: str
    model_name: str
    system: str
    history: List[Dict[str, Any]]
    gen_conf: Dict[str, Any]

@router.post("/chat")
async def chat(request: ChatRequest):
    factory = ChatFactory(key=request.key, model_name=request.model_name)
    chat_instance = factory.get_chat_instance()
    response, tokens = chat_instance.chat(request.system, request.history, request.gen_conf)
    print(response)
    print(tokens)
    return {"response": response, "tokens": tokens}

@router.post("/achat_stream")
async def achat_stream(request: ChatRequest):
    factory = ChatFactory(key=request.key, model_name=request.model_name)
    chat_instance = factory.get_chat_instance()

    async def event_generator():
        async for response in chat_instance.achat_streamly(request.system, request.history, request.gen_conf):
            yield f"data: {response}\n\n"

    return StreamingResponse(event_generator())

@router.post("/chat_stream")
async def chat_stream(request: ChatRequest):
    factory = ChatFactory(key=request.key, model_name=request.model_name)
    chat_instance = factory.get_chat_instance()

    def event_generator():
        for response in chat_instance.chat_streamly(request.system, request.history, request.gen_conf):
            yield f"data: {response}\n\n"

    return StreamingResponse(event_generator())