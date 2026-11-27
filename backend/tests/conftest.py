"""Fixture condivise: settings isolate su directory temporanee."""

import pytest

from app.config import get_settings
from app.db import reset_engine
from app.ingestion import pipeline


@pytest.fixture(autouse=True)
def _no_warmup(monkeypatch):
    """Niente preriscaldamento modelli nei test: il caricamento ONNX costa
    secondi/minuti. I test del warmup lo riattivano esplicitamente."""
    monkeypatch.setenv("WARMUP_ON_START", "false")


@pytest.fixture(autouse=True)
def _convert_in_process(monkeypatch):
    """Conversione in-process nei test: niente sottoprocesso spawn (avvio
    lento e superfluo per la maggior parte dei casi). I test del worker
    (tests/test_convert_worker.py) riattivano il flag esplicitamente."""
    monkeypatch.setenv("CONVERT_IN_SUBPROCESS", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _agent_v2_off(monkeypatch):
    """Architettura agentica v2 SPENTA di default nei test: i test storici
    del grafo (LLM mockato con risposte per marcatore di prompt) verificano
    il percorso classico, che deve restare immutato. I test della v2
    (tests/test_react.py) riattivano i flag esplicitamente."""
    monkeypatch.setenv("AGENT_MODE", "graph")
    monkeypatch.setenv("HYDE_ENABLED", "false")
    monkeypatch.setenv("CRAG_ENABLED", "false")
    monkeypatch.setenv("CRITIC_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Settings con data_dir/upload_dir in tmp_path e stato pipeline pulito.

    L'Embedder singleton NON viene azzerato: i modelli restano caldi tra un
    test e l'altro (il caricamento ONNX costa secondi).
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    get_settings.cache_clear()
    reset_engine()
    pipeline.reset_state()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()
    reset_engine()
    pipeline.reset_state()
