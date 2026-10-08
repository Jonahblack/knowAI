import asyncio
import base64

from knowledge_node import llm, retrieval, tools


class FakeClient:
    def __init__(self, messages):
        self.responses = iter(messages)
        self.requests = []

    async def complete(self, **request):
        self.requests.append(request)
        return next(self.responses)


def test_tool_registry_exposes_expected_allowlist():
    assert set(tools.HANDLERS) == {"search_knowledge", "get_item", "add_note", "attach_file"}
    assert {item["function"]["name"] for item in tools.TOOL_DEFINITIONS} == set(tools.HANDLERS)


def test_add_note_tool_is_immediately_searchable(database):
    result = tools.execute_tool("add_note", {"title": "Gate code", "text": "The code is 2468", "tags": ["home"]})

    assert result["created"] is True
    response = tools.execute_tool("search_knowledge", {"query": "2468", "mode": "lexical", "limit": 5})
    assert response["results"][0]["citation"]["entity_id"] == result["entity_id"]


def test_attach_file_validates_hash_and_size(database):
    data = b"local attachment"
    encoded = base64.b64encode(data).decode("ascii")
    result = tools.execute_tool("attach_file", {
        "filename": "sample.txt", "content_base64": encoded, "mime_type": "text/plain",
    })

    assert result["created"] is True
    assert database.blob_path(result["blob"]["sha256"]).read_bytes() == data


def test_agent_executes_allowlisted_tool_and_returns_auditable_citations(database):
    database.add_event(
        "note", "manual", "2026-01-01T00:00:00+00:00",
        {"title": "Boiler", "text": "Press the red reset switch.", "tags": ["home"]},
    )
    retrieval.rebuild_index()
    client = FakeClient([
        {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "search_knowledge", "arguments": '{"query":"boiler reset","mode":"lexical"}'},
            }],
        },
        {"role": "assistant", "content": "Press the red reset switch [S1]."},
    ])

    response = asyncio.run(llm.chat("How do I reset the boiler?", client=client))

    assert response["answer"].endswith("[S1].")
    assert response["cited_source_ids"] == ["S1"]
    assert response["invalid_source_ids"] == []
    assert response["tool_trace"][0]["ok"] is True
    assert response["sources"][0]["entity_id"] == "manual"


def test_agent_rejects_tools_not_enabled_for_the_request(database):
    client = FakeClient([
        {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "bad", "type": "function",
                            "function": {"name": "add_note", "arguments": '{"title":"x","text":"y"}'}}],
        },
        {"role": "assistant", "content": "I cannot run that tool."},
    ])

    response = asyncio.run(llm.chat("Run a command", client=client))

    assert response["tool_trace"][0]["ok"] is False
    assert "not allowed" in response["tool_trace"][0]["error"]
    assert response["writes_enabled"] is False
