from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Callable
import uuid

from pydantic import BaseModel, Field, ValidationError

from . import db, retrieval

MAX_TOOL_ATTACHMENT_BYTES = 10 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    mode: str = Field(default="hybrid", pattern=r"^(lexical|semantic|hybrid)$")
    limit: int = Field(default=5, ge=1, le=20)


class GetItemArgs(BaseModel):
    entity_id: str = Field(min_length=1, max_length=200)


class AddNoteArgs(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(max_length=1_000_000)
    tags: list[str] = Field(default_factory=list, max_length=100)


class AttachFileArgs(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    content_base64: str = Field(min_length=1, max_length=14_000_000)
    mime_type: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=100)
    sha256: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def search_knowledge(args: SearchArgs) -> dict[str, Any]:
    return retrieval.search(args.query, mode=args.mode, limit=args.limit)


def get_item(args: GetItemArgs) -> dict[str, Any]:
    entity = db.current_entity(args.entity_id)
    if entity is None:
        raise ValueError("entity not found or deleted")
    history = db.entity_history(args.entity_id)
    return {
        "entity": entity, "conflict": history["conflict"], "branch_count": len(history["heads"]),
        "citation": {"entity_id": entity["entity_id"], "source_event_id": entity["_version"],
                     "title": entity.get("title") or entity.get("filename"),
                     "filename": entity.get("filename"), "locator": {"type": "entity"}},
    }


def add_note(args: AddNoteArgs) -> dict[str, Any]:
    entity_id = str(uuid.uuid4())
    event = db.add_event("note", entity_id, _now(), {"title": args.title, "text": args.text, "tags": args.tags})
    index_result = retrieval.rebuild_index()
    return {"entity_id": entity_id, "event_id": event["event_id"], "created": True,
            "indexed": index_result["failed"] == 0}


def attach_file(args: AttachFileArgs) -> dict[str, Any]:
    try:
        data = base64.b64decode(args.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("content_base64 is not valid base64") from exc
    if len(data) > MAX_TOOL_ATTACHMENT_BYTES:
        raise ValueError("tool attachments are limited to 10 MiB; use POST /files for larger files")
    digest = hashlib.sha256(data).hexdigest()
    if args.sha256 is not None:
        if not SHA256_PATTERN.fullmatch(args.sha256) or args.sha256 != digest:
            raise ValueError("sha256 does not match the decoded content")
    meta = db.store_blob(data, args.filename, args.mime_type)
    entity_id = str(uuid.uuid4())
    event = db.add_event("document", entity_id, _now(), {
        "title": args.title or args.filename, "filename": args.filename,
        "mime_type": args.mime_type, "blob": meta, "tags": args.tags,
    })
    index_result = retrieval.rebuild_index()
    return {"entity_id": entity_id, "event_id": event["event_id"], "blob": meta, "created": True,
            "indexed": index_result["failed"] == 0,
            "index_failures": index_result["failures"]}


ToolHandler = tuple[type[BaseModel], Callable[[Any], dict[str, Any]]]
HANDLERS: dict[str, ToolHandler] = {
    "search_knowledge": (SearchArgs, search_knowledge),
    "get_item": (GetItemArgs, get_item),
    "add_note": (AddNoteArgs, add_note),
    "attach_file": (AttachFileArgs, attach_file),
}


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search the user's local knowledge and return evidence with source citations.",
            "parameters": SearchArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item",
            "description": "Get the current authoritative version of a local knowledge entity.",
            "parameters": GetItemArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": "Save a new note to local knowledge. Use only when the user explicitly asks to save it.",
            "parameters": AddNoteArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_file",
            "description": "Save a small base64-encoded file supplied by the user. Never invent file content.",
            "parameters": AttachFileArgs.model_json_schema(),
        },
    },
]


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    entry = HANDLERS.get(name)
    if entry is None:
        raise ValueError(f"unknown tool: {name}")
    model, handler = entry
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        raise ValueError(f"invalid arguments for {name}: {exc}") from exc
    return handler(validated)
