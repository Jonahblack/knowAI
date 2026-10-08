import importlib

import pytest


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_HOME", str(tmp_path))
    from knowledge_node import db

    importlib.reload(db)
    db.init_db()
    return db
