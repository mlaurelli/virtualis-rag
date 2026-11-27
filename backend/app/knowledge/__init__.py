"""Knowledge graph documentale (GraphRAG).

Estrazione di entità (persone, organizzazioni, contratti, progetti, date,
importi, prodotti) e relazioni dai documenti indicizzati, a integrazione del
recupero vettoriale per i quesiti che attraversano molti documenti.

Il grafo vive in SQLite (tabelle ``entities``, ``entity_mentions``,
``relations``), è disabilitato di default (``kg_enabled``) perché l'estrazione
usa il LLM (una chiamata per documento) ed è ricostruibile in qualunque
momento con ``scripts/kg_build.py``. Tutto il modulo è best-effort: un
fallimento dell'estrazione non deve mai bloccare ingest o ricerca.
"""
