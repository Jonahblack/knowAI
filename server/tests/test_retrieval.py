from array import array

from knowledge_node import retrieval


def test_chunking_is_stable_and_preserves_locator():
    segments = [retrieval.Segment("First sentence. " * 30, {"page": 3})]
    first = retrieval.chunk_segments(segments, "version-1", max_chars=120, overlap_chars=20)
    second = retrieval.chunk_segments(segments, "version-1", max_chars=120, overlap_chars=20)

    assert len(first) > 1
    assert [chunk["chunk_id"] for chunk in first] == [chunk["chunk_id"] for chunk in second]
    assert all(chunk["locator"] == {"page": 3} for chunk in first)


def test_note_index_and_lexical_search_include_citation(database):
    event = database.add_event(
        "note", "note-1", "2026-01-01T00:00:00+00:00",
        {"title": "Boiler manual", "text": "The reset valve is behind the lower panel.", "tags": ["home"]},
    )

    result = retrieval.rebuild_index()
    response = retrieval.search("reset valve", mode="lexical")

    assert result["indexed"] == 1
    assert response["results"][0]["citation"]["entity_id"] == "note-1"
    assert response["results"][0]["citation"]["source_event_id"] == event["event_id"]
    assert "lower panel" in response["results"][0]["text"]


def test_new_entity_version_invalidates_old_chunks(database):
    root = database.add_event(
        "note", "note-1", "2026-01-01T00:00:00+00:00",
        {"title": "Old title", "text": "obsolete phrase", "tags": []},
    )
    retrieval.rebuild_index()
    database.add_event(
        "note", "note-1", "2026-01-02T00:00:00+00:00",
        {"title": "New title", "text": "replacement phrase", "tags": []}, root["event_id"],
    )

    retrieval.rebuild_index()

    assert retrieval.search("obsolete", mode="lexical")["results"] == []
    assert retrieval.search("replacement", mode="lexical")["results"]


def test_hybrid_rank_fusion_works_without_embeddings(database):
    database.add_event(
        "note", "note-1", "2026-01-01T00:00:00+00:00",
        {"title": "Recipe", "text": "Add saffron to the rice.", "tags": []},
    )
    retrieval.rebuild_index()

    response = retrieval.search("saffron", mode="hybrid")

    assert response["results"]
    assert response["model_id"] is None


def test_stored_vectors_are_used_for_semantic_search(database, monkeypatch):
    database.add_event(
        "note", "note-1", "2026-01-01T00:00:00+00:00",
        {"title": "Vehicle", "text": "The automobile is in the garage.", "tags": []},
    )
    retrieval.rebuild_index()
    chunk = database.indexed_chunks()[0]
    vector = array("f", [1.0, 0.0])
    database.replace_embeddings("fake-model", [(chunk["chunk_id"], 2, vector.tobytes())])

    class FakeModel:
        def encode_query(self, query, normalize_embeddings=True):
            return [1.0, 0.0]

    monkeypatch.setattr(retrieval, "_load_embedding_model", lambda _: FakeModel())
    response = retrieval.search("car", mode="semantic", model_id="fake-model")

    assert response["results"][0]["citation"]["entity_id"] == "note-1"
    assert response["results"][0]["score"] == 1.0
