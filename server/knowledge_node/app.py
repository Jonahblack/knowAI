from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal
import uuid

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from . import db, llm, retrieval, tools
from .sync_api import router as sync_router

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
INDEX_REBUILD_LOCK = Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Local Knowledge Node", version="0.5.0", lifespan=lifespan)
app.include_router(sync_router)


class NoteIn(BaseModel):
    title: str
    text: str
    tags: list[str] = Field(default_factory=list)
    entity_id: str | None = None


class NoteUpdate(BaseModel):
    base_event_id: str
    title: str
    text: str
    tags: list[str] = Field(default_factory=list)


class NoteResolution(BaseModel):
    base_event_ids: list[str] = Field(min_length=2)
    title: str
    text: str
    tags: list[str] = Field(default_factory=list)


class IndexRequest(BaseModel):
    embed: bool = False
    caption_images: bool = False
    force: bool = False
    embedding_model: str = retrieval.DEFAULT_EMBEDDING_MODEL
    caption_model: str = retrieval.DEFAULT_CAPTION_MODEL


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    model: str = Field(default=llm.DEFAULT_LLM_MODEL, min_length=1, max_length=500)
    retrieval_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    max_sources: int = Field(default=6, ge=1, le=20)
    allow_writes: bool = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_note(entity_id: str) -> dict[str, Any]:
    entity = db.current_entity(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="note not found")
    if entity["kind"] not in {"note", "upsert"}:
        raise HTTPException(status_code=409, detail="entity is not a note")
    return entity


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "device_id": db.device_id(), "clock": db.vector_clock()}


@app.post("/notes")
def create_note(note: NoteIn) -> dict[str, Any]:
    entity_id = note.entity_id or str(uuid.uuid4())
    if db.entity_history(entity_id) is not None:
        raise HTTPException(status_code=409, detail="entity_id already exists")
    return db.add_event("note", entity_id, now_iso(), {"title": note.title, "text": note.text, "tags": note.tags})


@app.put("/notes/{entity_id}")
def update_note(entity_id: str, note: NoteUpdate) -> dict[str, Any]:
    require_note(entity_id)
    try:
        return db.add_event("note", entity_id, now_iso(),
                            {"title": note.title, "text": note.text, "tags": note.tags},
                            note.base_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/entities/{entity_id}")
def delete_entity(entity_id: str, base_event_id: str = Query(...)) -> dict[str, Any]:
    if db.entity_history(entity_id) is None:
        raise HTTPException(status_code=404, detail="entity not found")
    try:
        return db.add_event("delete", entity_id, now_iso(), {}, base_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/notes/{entity_id}/resolve")
def resolve_note(entity_id: str, resolution: NoteResolution) -> dict[str, Any]:
    require_note(entity_id)
    history = db.entity_history(entity_id)
    current_heads = {head["event_id"] for head in history["heads"]}
    supplied_heads = set(resolution.base_event_ids)
    if len(supplied_heads) != len(resolution.base_event_ids) or supplied_heads != current_heads:
        raise HTTPException(status_code=409, detail="base_event_ids must contain every current branch head exactly once")
    return db.add_event(
        "note", entity_id, now_iso(),
        {"title": resolution.title, "text": resolution.text, "tags": resolution.tags,
         "_merged_event_ids": resolution.base_event_ids},
        resolution.base_event_ids[0],
    )


@app.post("/files")
async def add_file(file: UploadFile = File(...), title: str | None = Form(None), tags: str | None = Form(None)) -> dict[str, Any]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file exceeds 100 MiB upload limit")
    meta = db.store_blob(data, file.filename, file.content_type)
    entity_id = str(uuid.uuid4())
    event = db.add_event("document", entity_id, now_iso(), {
        "title": title or file.filename, "filename": file.filename,
        "mime_type": file.content_type, "blob": meta,
        "tags": [value.strip() for value in (tags or "").split(",") if value.strip()],
    })
    return {"event": event, "blob": meta}


@app.get("/entities")
def entities() -> list[dict[str, Any]]:
    return db.current_entities()


@app.get("/entities/{entity_id}")
def entity(entity_id: str) -> dict[str, Any]:
    item = db.current_entity(entity_id)
    if item is None:
        raise HTTPException(status_code=404, detail="entity not found or deleted")
    return item


@app.get("/entities/{entity_id}/history")
def entity_history(entity_id: str) -> dict[str, Any]:
    history = db.entity_history(entity_id)
    if history is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return history


@app.get("/conflicts")
def conflicts() -> list[dict[str, Any]]:
    return db.conflict_histories()


@app.post("/index/rebuild")
def rebuild_index(request: IndexRequest) -> dict[str, Any]:
    if not INDEX_REBUILD_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="an index rebuild is already running")
    try:
        return retrieval.rebuild_index(
            embed=request.embed, caption_images=request.caption_images, force=request.force,
            embedding_model=request.embedding_model, caption_model=request.caption_model,
        )
    except retrieval.OptionalDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    finally:
        INDEX_REBUILD_LOCK.release()


@app.get("/index/status")
def index_status() -> dict[str, Any]:
    return db.index_status()


@app.get("/search")
def search(
    q: str = Query(min_length=1),
    mode: Literal["lexical", "semantic", "hybrid"] = "hybrid",
    limit: int = Query(default=10, ge=1, le=50),
    model: str = retrieval.DEFAULT_EMBEDDING_MODEL,
) -> dict[str, Any]:
    try:
        return retrieval.search(q, mode=mode, limit=limit, model_id=model)
    except retrieval.OptionalDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/tools")
def tool_definitions() -> list[dict[str, Any]]:
    return tools.TOOL_DEFINITIONS


@app.post("/tools/search_knowledge")
def tool_search_knowledge(arguments: tools.SearchArgs) -> dict[str, Any]:
    return tools.search_knowledge(arguments)


@app.post("/tools/get_item")
def tool_get_item(arguments: tools.GetItemArgs) -> dict[str, Any]:
    try:
        return tools.get_item(arguments)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/tools/add_note")
def tool_add_note(arguments: tools.AddNoteArgs) -> dict[str, Any]:
    return tools.add_note(arguments)


@app.post("/tools/attach_file")
def tool_attach_file(arguments: tools.AttachFileArgs) -> dict[str, Any]:
    try:
        return tools.attach_file(arguments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/assistant/chat")
async def assistant_chat(request: ChatRequest) -> dict[str, Any]:
    try:
        return await llm.chat(
            request.message, model=request.model, retrieval_mode=request.retrieval_mode,
            max_sources=request.max_sources, allow_writes=request.allow_writes,
        )
    except retrieval.OptionalDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (llm.ModelProtocolError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=f"local model server error: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
