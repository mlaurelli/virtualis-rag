"""Test del worker di conversione in sottoprocesso (convert_worker).

Il figlio è un VERO processo spawn: le monkeypatch non attraversano il
confine di processo, quindi gli scenari di morte/lentezza usano gli hook
d'ambiente ``_VIRTUALIS_TEST_CONVERT_*`` letti dal figlio a ogni richiesta
(l'ambiente è ereditato allo spawn). L'uccisione del figlio simula
esattamente l'OOM killer del cgroup (SIGKILL).
"""

import pytest

from app.config import get_settings
from app.db import get_session_factory, init_db
from app.ingestion import convert_worker, pipeline
from app.ingestion.converter import ConversionError, NormalizedDoc
from app.models import Document, DocumentStatus, Source


@pytest.fixture
def worker(tmp_settings):
    """Nessun figlio residuo prima/dopo il test (avvio lazy nel test stesso)."""
    convert_worker.shutdown()
    yield
    convert_worker.shutdown()


def _write_md(tmp_path, name: str = "doc.md") -> object:
    md = tmp_path / name
    md.write_text(
        "# Titolo di prova\n\nParagrafo con contenuto qualsiasi.\n",
        encoding="utf-8",
    )
    return md


# --- worker: percorso diretto -----------------------------------------------------


def test_conversione_normale_e_worker_persistente(worker, tmp_path) -> None:
    """Conversione .md via worker: risultato corretto e figlio riusato."""
    md = _write_md(tmp_path)
    norm = convert_worker.convert(md, timeout=120)
    assert isinstance(norm, NormalizedDoc)
    assert norm.title == "Titolo di prova"
    assert any(block.kind == "paragraph" for block in norm.blocks)

    pid = convert_worker._worker.process.pid
    norm2 = convert_worker.convert(md, timeout=120)
    assert norm2.blocks
    # Stesso processo: il worker è persistente, non uno spawn per documento
    assert convert_worker._worker.process.pid == pid


def test_riciclo_worker_dopo_n_conversioni(worker, tmp_path, monkeypatch) -> None:
    """Ogni CONVERT_WORKER_MAX_TASKS conversioni il figlio viene ricreato,
    così la RSS accumulata da torch/Docling torna al sistema."""
    monkeypatch.setenv("CONVERT_WORKER_MAX_TASKS", "2")
    get_settings.cache_clear()
    md = _write_md(tmp_path)
    assert convert_worker.convert(md, timeout=120).blocks
    pid = convert_worker._worker.process.pid
    assert convert_worker.convert(md, timeout=120).blocks
    assert convert_worker._worker is None  # riciclato dopo 2 conversioni
    assert convert_worker.convert(md, timeout=120).blocks
    assert convert_worker._worker.process.pid != pid
    assert convert_worker._worker.process.is_alive()


def test_errore_di_conversione_non_uccide_il_worker(worker, tmp_path) -> None:
    """Un ConversionError nel figlio arriva pulito e il worker resta vivo."""
    with pytest.raises(ConversionError, match="File non trovato"):
        convert_worker.convert(tmp_path / "manca.md", timeout=120)
    assert convert_worker._worker.process.is_alive()
    assert convert_worker.convert(_write_md(tmp_path), timeout=120).blocks


def test_figlio_morto_a_meta_errore_pulito_e_ricreazione(
    worker, tmp_path, monkeypatch
) -> None:
    """Figlio che muore durante la richiesta (come l'OOM killer): il padre
    riceve un errore pulito e il worker è ricreato al prossimo uso."""
    md = _write_md(tmp_path)
    monkeypatch.setenv("_VIRTUALIS_TEST_CONVERT_EXIT", "1")
    with pytest.raises(ConversionError, match="limite di memoria"):
        convert_worker.convert(md, timeout=120)
    assert convert_worker._worker is None  # worker scartato dopo la morte

    monkeypatch.delenv("_VIRTUALIS_TEST_CONVERT_EXIT")
    # La richiesta successiva funziona su un figlio nuovo
    assert convert_worker.convert(md, timeout=120).blocks
    assert convert_worker._worker.process.is_alive()


def test_timeout_richiesta_errore_pulito_e_ricreazione(
    worker, tmp_path, monkeypatch
) -> None:
    """Richiesta oltre il timeout: errore pulito, figlio terminato, ricreato."""
    md = _write_md(tmp_path)
    monkeypatch.setenv("_VIRTUALIS_TEST_CONVERT_SLEEP_S", "60")
    with pytest.raises(ConversionError, match="tempo massimo"):
        convert_worker.convert(md, timeout=8)
    assert convert_worker._worker is None  # worker scartato dopo il timeout

    monkeypatch.delenv("_VIRTUALIS_TEST_CONVERT_SLEEP_S")
    assert convert_worker.convert(md, timeout=120).blocks


# --- integrazione con la pipeline -------------------------------------------------


@pytest.fixture
async def session_factory_worker(tmp_settings, monkeypatch):
    """Pipeline con CONVERT_IN_SUBPROCESS attivo (override del default test)."""
    monkeypatch.setenv("CONVERT_IN_SUBPROCESS", "true")
    get_settings.cache_clear()
    convert_worker.shutdown()
    await init_db()
    sf = get_session_factory()
    await pipeline.start_worker(sf)
    yield sf
    await pipeline.stop_worker()
    convert_worker.shutdown()


async def _make_source(session_factory) -> int:
    async with session_factory() as db:
        source = Source(name="test", type="local_fs", config_json={})
        db.add(source)
        await db.commit()
        await db.refresh(source)
        return source.id


async def test_pipeline_ingest_via_worker(session_factory_worker, tmp_path) -> None:
    """Ingest end-to-end con conversione nel sottoprocesso: documento indexed."""
    sf = session_factory_worker
    source_id = await _make_source(sf)
    md = _write_md(tmp_path)

    document_id = await pipeline.ingest_file(md, source_id, str(md), sf)
    await pipeline.wait_idle()

    async with sf() as db:
        document = await db.get(Document, document_id)
        assert document.status == DocumentStatus.indexed.value, document.error


async def test_pipeline_documento_patologico_failed_e_pipeline_viva(
    session_factory_worker, tmp_path, monkeypatch
) -> None:
    """Il figlio muore sul documento patologico: solo QUEL documento è failed
    (con messaggio esplicito), l'app resta viva e il successivo è indexed."""
    sf = session_factory_worker
    source_id = await _make_source(sf)

    monkeypatch.setenv("_VIRTUALIS_TEST_CONVERT_EXIT", "1")
    bad = tmp_path / "patologico.md"
    bad.write_text("# Patologico\n\nSimula la planimetria raster.\n", encoding="utf-8")
    bad_id = await pipeline.ingest_file(bad, source_id, str(bad), sf)
    await pipeline.wait_idle()
    monkeypatch.delenv("_VIRTUALIS_TEST_CONVERT_EXIT")

    ok = tmp_path / "sano.md"
    ok.write_text("# Sano\n\nContenuto valido e indicizzabile.\n", encoding="utf-8")
    ok_id = await pipeline.ingest_file(ok, source_id, str(ok), sf)
    await pipeline.wait_idle()

    async with sf() as db:
        bad_doc = await db.get(Document, bad_id)
        ok_doc = await db.get(Document, ok_id)
    assert bad_doc.status == DocumentStatus.failed.value
    assert "limite di memoria" in bad_doc.error
    assert ok_doc.status == DocumentStatus.indexed.value, ok_doc.error


async def test_flag_off_percorso_in_process_invariato(
    tmp_settings, tmp_path, monkeypatch
) -> None:
    """Con CONVERT_IN_SUBPROCESS=false (default nei test) la pipeline usa la
    conversione in-process e non tocca mai il worker."""

    def boom(*args, **kwargs):
        raise AssertionError("il worker non deve essere usato con il flag off")

    monkeypatch.setattr(convert_worker, "convert", boom)
    await init_db()
    sf = get_session_factory()
    await pipeline.start_worker(sf)
    try:
        source_id = await _make_source(sf)
        md = _write_md(tmp_path)
        document_id = await pipeline.ingest_file(md, source_id, str(md), sf)
        await pipeline.wait_idle()
        async with sf() as db:
            document = await db.get(Document, document_id)
            assert document.status == DocumentStatus.indexed.value, document.error
    finally:
        await pipeline.stop_worker()
