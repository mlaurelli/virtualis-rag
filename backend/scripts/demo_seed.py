#!/usr/bin/env python3
"""Generatore del corpus dimostrativo per la sessione di consegna (cap. 14).

Crea in ``demo_corpus/`` (default, relativo a ``backend/``) un piccolo
archivio aziendale italiano e coerente — contratti, verbali, listini,
procedure — pensato per domande agentiche di confronto tra documenti.

Uso (dalla cartella ``backend/``)::

    uv run python scripts/demo_seed.py                 # genera solo i file
    uv run python scripts/demo_seed.py --ingest        # genera e indicizza
    uv run python scripts/demo_seed.py --ingest --data-dir data-demo

L'ingest usa la pipeline reale dell'applicazione su un ``DATA_DIR`` dedicato
(default ``data-demo/``, così non si toccano i dati di lavoro in ``data/``).
Al primo ingest vengono scaricati i modelli di retrieval (~2 GB, una tantum).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
# Permette l'import di `app` anche senza installazione del package
sys.path.insert(0, str(BACKEND_ROOT))

DEFAULT_CORPUS_DIR = BACKEND_ROOT / "demo_corpus"
DEFAULT_DATA_DIR = BACKEND_ROOT / "data-demo"

# Domande pronte da copiare in chat alla fine della generazione
EXAMPLE_QUESTIONS = {
    "fast": [
        "Qual è il canone annuo del contratto di manutenzione del 2025?",
        "Quanto costa il sensore di pressione SP-220 nel listino 2025?",
    ],
    "agentiche": [
        "Confronta le clausole di garanzia e le penali del contratto di "
        "manutenzione 2023 con quelle del 2025: che cosa è cambiato?",
        "Quale delibera del CDA ha approvato il budget di manutenzione 2025 "
        "e come si collega al contratto di manutenzione attualmente vigente?",
        "Per sostituire i filtri della linea B: quali articoli del listino "
        "servono, quanto costano in totale e quale procedura devo seguire?",
    ],
}


# ---------------------------------------------------------------------------
# Costruttori di formato
# ---------------------------------------------------------------------------

def _write_docx(path: Path, title: str, sections: list[tuple[str, list[str]]]) -> None:
    """DOCX con titolo e sezioni (stili Heading reali, per il chunking)."""
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    for heading, paragraphs in sections:
        doc.add_heading(heading, level=1)
        for para in paragraphs:
            doc.add_paragraph(para)
    doc.save(path)


def _write_xlsx(
    path: Path, sheet: str, headers: list[str], rows: list[list[object]]
) -> None:
    """XLSX con una tabella intestata."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for row in rows:
        ws.append(row)
    for col in ws.columns:
        width = max(len(str(cell.value or "")) for cell in col) + 2
        ws.column_dimensions[col[0].column_letter].width = min(width, 60)
    wb.save(path)


def _escape_pdf(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _write_pdf(path: Path, lines: list[str]) -> None:
    """PDF testuale minimo (Helvetica/WinAnsi), estraibile dai converter.

    Writer volutamente essenziale: nessuna dipendenza extra, xref calcolato.
    """
    per_page = 44
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]

    kids: list[str] = []
    extra: list[tuple[int, bytes]] = []
    for idx, page_lines in enumerate(pages):
        page_id = 4 + idx * 2
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        stream_lines = ["BT", "/F1 11 Tf", "14 TL", "50 800 Td"]
        if page_lines:
            stream_lines.append(f"({_escape_pdf(page_lines[0])}) Tj")
            for line in page_lines[1:]:
                stream_lines.append("T*")
                stream_lines.append(f"({_escape_pdf(line)}) Tj")
        stream_lines.append("ET")
        stream = "\n".join(stream_lines).encode("latin-1", errors="replace")
        page_obj = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        content_obj = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
        extra.extend([(page_id, page_obj), (content_id, content_obj)])

    objects = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()),
        (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>"),
        *extra,
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num, body in objects:
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    size = max(offsets) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for n in range(1, size):
        out += f"{offsets[n]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def _write_md(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Contenuti del corpus
# ---------------------------------------------------------------------------

def generate_corpus(dest: Path) -> list[Path]:
    """Genera i 10 documenti dimostrativi in ``dest``. Ritorna i path creati."""
    dest.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    def _add(name: str) -> Path:
        p = dest / name
        created.append(p)
        return p

    _write_docx(
        _add("Contratto_Manutenzione_2023.docx"),
        "Contratto di Manutenzione Full-Service 2023",
        [
            ("Parti e oggetto", [
                "Il presente contratto è stipulato tra Officine Riunite S.p.A. "
                "(di seguito \"Cliente\") e TecnoService S.r.l. (di seguito "
                "\"Fornitore\") e ha per oggetto la manutenzione full-service "
                "delle linee di produzione A e B dello stabilimento di Lodi.",
                "Decorrenza: 1° gennaio 2023. Durata: 24 mesi, con rinnovo "
                "tacito annuale salvo disdetta con preavviso di 90 giorni.",
            ]),
            ("Canone e pagamenti", [
                "Il canone annuo è pari a € 18.000,00 (diciottomila/00) oltre "
                "IVA, fatturato in quattro rate trimestrali anticipate.",
                "I ricambi sono esclusi dal canone e addebitati a consuntivo "
                "secondo il listino vigente al momento dell'intervento.",
            ]),
            ("Garanzia e livelli di servizio", [
                "La garanzia sugli interventi di riparazione è di 12 mesi "
                "dalla data di collaudo del singolo intervento.",
                "Il Fornitore garantisce un tempo massimo di intervento (SLA) "
                "di 48 ore lavorative dalla segnalazione di guasto bloccante.",
                "In caso di superamento dello SLA si applica una penale pari "
                "al 2% del canone trimestrale per ogni giorno di ritardo, "
                "fino a un massimo del 10% del canone annuo.",
            ]),
            ("Clausole finali", [
                "Il contratto è regolato dalla legge italiana. Foro "
                "competente: Milano. Le parti si danno atto che eventuali "
                "modifiche devono risultare da scrittura privata sottoscritta.",
            ]),
        ],
    )

    _write_docx(
        _add("Contratto_Manutenzione_2025.docx"),
        "Contratto di Manutenzione Full-Service 2025",
        [
            ("Parti e oggetto", [
                "Il presente contratto è stipulato tra Officine Riunite S.p.A. "
                "(di seguito \"Cliente\") e TecnoService S.r.l. (di seguito "
                "\"Fornitore\") e ha per oggetto la manutenzione full-service "
                "delle linee di produzione A, B e C dello stabilimento di Lodi, "
                "incluse le attività di manutenzione predittiva basate sui "
                "dati dei sensori di linea.",
                "Decorrenza: 1° gennaio 2025. Durata: 36 mesi, con rinnovo "
                "tacito annuale salvo disdetta con preavviso di 120 giorni. "
                "Il contratto sostituisce integralmente quello del 2023.",
            ]),
            ("Canone e pagamenti", [
                "Il canone annuo è pari a € 21.500,00 (ventunmilacinquecento/00) "
                "oltre IVA, fatturato in dodici rate mensili posticipate.",
                "I ricambi del kit filtri standard sono inclusi nel canone; "
                "gli altri ricambi sono addebitati a consuntivo con sconto "
                "del 10% sul listino vigente.",
            ]),
            ("Garanzia e livelli di servizio", [
                "La garanzia sugli interventi di riparazione è estesa a "
                "24 mesi dalla data di collaudo del singolo intervento.",
                "Il tempo massimo di intervento (SLA) per guasto bloccante "
                "è ridotto a 24 ore, di cui al massimo 4 ore per la presa in "
                "carico remota tramite teleassistenza.",
                "In caso di superamento dello SLA si applica una penale pari "
                "al 5% del canone mensile per ogni giorno di ritardo, fino a "
                "un massimo del 15% del canone annuo.",
                "Il Fornitore si impegna inoltre al rispetto del protocollo "
                "di cybersecurity IEC 62443 per tutti gli accessi remoti ai "
                "PLC di linea, con audit annuale a proprio carico.",
            ]),
            ("Clausole finali", [
                "Il contratto è regolato dalla legge italiana. Foro "
                "competente: Lodi. È previsto un riesame congiunto dei KPI "
                "di servizio ogni sei mesi.",
            ]),
        ],
    )

    _write_docx(
        _add("Verbale_CDA_2024.docx"),
        "Verbale del Consiglio di Amministrazione del 14 novembre 2024",
        [
            ("Partecipanti e ordine del giorno", [
                "Riunione del Consiglio di Amministrazione di Officine Riunite "
                "S.p.A. tenutasi il 14 novembre 2024 presso la sede di Lodi. "
                "Presenti: la Presidente Avv. Carla Nembrini, l'Amministratore "
                "Delegato Ing. Paolo Roveda, i consiglieri Dott.ssa Elisa "
                "Fontana e Dott. Marco Bellandi. Assente giustificato il "
                "consigliere Dott. Andrea Piva.",
                "Ordine del giorno: 1) budget manutenzione 2025; 2) rinnovo "
                "del contratto di manutenzione full-service; 3) varie ed "
                "eventuali.",
            ]),
            ("Delibera n. 2024-017 — Budget manutenzione 2025", [
                "Il Consiglio, esaminata la relazione dell'Amministratore "
                "Delegato, approva con la delibera n. 2024-017 un budget di "
                "manutenzione per l'esercizio 2025 pari a € 85.000,00, così "
                "ripartito: € 43.000 per i canoni dei contratti di "
                "manutenzione, € 28.000 per ricambi e materiali di consumo, "
                "€ 14.000 per interventi straordinari.",
                "Il Consiglio nomina l'Ing. Paolo Roveda responsabile "
                "dell'esecuzione del piano, con delega di spesa fino a "
                "€ 10.000 per singolo intervento straordinario.",
            ]),
            ("Rinnovo del contratto di manutenzione", [
                "Il Consiglio prende atto della proposta di TecnoService "
                "S.r.l. e autorizza l'Amministratore Delegato alla "
                "sottoscrizione del nuovo contratto di manutenzione "
                "full-service con decorrenza 1° gennaio 2025, che estende la "
                "copertura alla linea C e introduce la manutenzione "
                "predittiva, nelle forme risultanti dalla bozza allegata.",
                "La delibera n. 2024-017 costituisce la copertura finanziaria "
                "del nuovo contratto: il canone annuo previsto rientra nella "
                "voce \"canoni\" del budget approvato.",
            ]),
        ],
    )

    _write_xlsx(
        _add("Listino_2025.xlsx"),
        "Listino ricambi",
        ["Codice", "Descrizione", "Unità", "Prezzo €", "Note"],
        [
            ["FLT-100", "Filtro olio idraulico linea A/B", "pz", 42.50, "Kit standard"],
            ["FLT-110", "Filtro aria compressa linea B", "pz", 38.90, "Kit standard"],
            ["FLT-205", "Filtro gasolio gruppo elettrogeno", "pz", 55.00, ""],
            ["PMP-310", "Pompa di circolazione 3 kW", "pz", 890.00, "Tempo consegna 3 settimane"],
            ["PMP-315", "Guarnizioni pompa PMP-310 (kit)", "kit", 65.00, ""],
            ["SP-220", "Sensore di pressione 0-250 bar", "pz", 210.00,
             "Certificato ATEX incluso"],
            ["SP-225", "Sensore di temperatura PT100", "pz", 96.00, ""],
            ["VLV-040", "Valvola di sicurezza tarata 12 bar", "pz", 145.00,
             "Taratura annuale obbligatoria"],
            ["BLT-510", "Cinghia di trasmissione linea B", "pz", 78.40, ""],
            ["OIL-900", "Olio idraulico ISO VG 46 (tanica 20 l)", "tanica", 88.00, ""],
        ],
    )

    _write_xlsx(
        _add("Piano_Manutenzione_2025.xlsx"),
        "Piano 2025",
        ["Trimestre", "Linea", "Intervento", "Responsabile", "Stato"],
        [
            ["Q1", "A", "Sostituzione cinghie e tensionamento", "TecnoService",
             "Pianificato"],
            ["Q1", "B", "Sostituzione kit filtri (FLT-100 + FLT-110)", "TecnoService",
             "Pianificato"],
            ["Q2", "C", "Taratura valvole di sicurezza VLV-040", "TecnoService",
             "Pianificato"],
            ["Q2", "B", "Analisi vibrazionale cuscinetti", "TecnoService",
             "Pianificato"],
            ["Q3", "A", "Sostituzione olio idraulico e filtri", "TecnoService",
             "Pianificato"],
            ["Q3", "B", "Sostituzione kit filtri (FLT-100 + FLT-110)", "TecnoService",
             "Pianificato"],
            ["Q4", "C", "Verifica sensori di pressione SP-220", "TecnoService",
             "Pianificato"],
            ["Q4", "A/B/C", "Audit cybersecurity IEC 62443 accessi remoti",
             "TecnoService", "Pianificato"],
        ],
    )

    _write_pdf(
        _add("Nota_Operativa.pdf"),
        [
            "NOTA OPERATIVA NO-2025/03",
            "Sostituzione dei filtri della linea B",
            "",
            "1. SCOPO",
            "La presente nota descrive la procedura operativa per la sostituzione",
            "del kit filtri della linea B, prevista dal Piano di Manutenzione 2025",
            "nei trimestri Q1 e Q3.",
            "",
            "2. MATERIALI RICHIESTI",
            "- Filtro olio idraulico FLT-100 (1 pz)",
            "- Filtro aria compressa FLT-110 (1 pz)",
            "- Olio idraulico ISO VG 46 OIL-900 (rabbocco, se necessario)",
            "I codici e i prezzi unitari sono quelli del Listino 2025. Per il",
            "contratto di manutenzione 2025 il kit filtri standard è incluso nel",
            "canone; eventuali ricambi aggiuntivi seguono lo sconto del 10%.",
            "",
            "3. SICUREZZA",
            "Prima dell'intervento applicare la procedura LOTO (Lockout-Tagout)",
            "descritta nel Manuale di Sicurezza dell'Impianto, sezione 3.",
            "DPI obbligatori: guanti nitrilici, occhiali, scarpe antinfortunistiche.",
            "",
            "4. PROCEDURA",
            "a) Fermare la linea B e verificare l'assenza di pressione residua",
            "   sul circuito idraulico (manometro di linea a zero).",
            "b) Sostituire il filtro FLT-100 chiudendo prima la valvola di",
            "   intercettazione a monte del gruppo pompe.",
            "c) Sostituire il filtro FLT-110 sul trattamento aria.",
            "d) Riavviare in modalità test per 15 minuti e verificare l'assenza",
            "   di trafilamenti; registrare l'esito sul rapportino di intervento.",
            "",
            "5. REGISTRAZIONE",
            "L'intervento va registrato a sistema con riferimento alla presente",
            "nota e al contratto di manutenzione vigente.",
            "",
            "Emessa il 10 gennaio 2025 - Ufficio Manutenzione, Ing. P. Roveda",
        ],
    )

    _write_docx(
        _add("Manuale_Sicurezza_Impianto.docx"),
        "Manuale di Sicurezza dell'Impianto — Stabilimento di Lodi",
        [
            ("1. Scopo e campo di applicazione", [
                "Il presente manuale definisce le procedure di sicurezza per "
                "gli interventi sulle linee di produzione A, B e C e si "
                "applica a tutto il personale interno e alle imprese esterne.",
            ]),
            ("2. Dispositivi di protezione individuale", [
                "Per tutti gli interventi di manutenzione sono obbligatori: "
                "scarpe antinfortunistiche S3, occhiali di protezione e guanti "
                "idonei al rischio specifico (nitrilici per oli e grassi).",
            ]),
            ("3. Procedura LOTO (Lockout-Tagout)", [
                "Ogni intervento su organi in movimento o circuiti in "
                "pressione richiede l'applicazione della procedura LOTO: "
                "sezionamento dell'alimentazione, applicazione del lucchetto "
                "personale, apposizione del cartello e verifica dell'assenza "
                "di energia residua prima dell'accesso.",
                "La rimozione del lucchetto è consentita esclusivamente al "
                "manutentore che lo ha applicato, a intervento concluso.",
            ]),
            ("4. Gestione delle emergenze", [
                "In caso di sversamento di olio idraulico intervenire con il "
                "kit assorbente posizionato presso il gruppo pompe della "
                "linea B e avvisare il responsabile di reparto.",
            ]),
        ],
    )

    _write_docx(
        _add("Policy_Rimborsi_2025.docx"),
        "Policy Rimborsi e Trasferte 2025",
        [
            ("Ambito", [
                "La presente policy disciplina i rimborsi spese per le "
                "trasferte del personale di Officine Riunite S.p.A. con "
                "effetto dal 1° gennaio 2025.",
            ]),
            ("Vitto e alloggio", [
                "Il rimborso dei pasti è riconosciuto fino a € 35,00 al "
                "giorno per trasferte in Italia e € 50,00 per l'estero, "
                "dietro presentazione di giustificativi fiscali.",
                "Il pernottamento è rimborsato fino a € 120,00 a notte in "
                "Italia; per importi superiori serve autorizzazione preventiva "
                "del responsabile di funzione.",
            ]),
            ("Trasporti", [
                "L'uso dell'auto propria è rimborsato a € 0,45 per chilometro. "
                "I viaggi in treno sono prenotati dall'ufficio amministrativo "
                "in classe standard; la business class è ammessa oltre le 4 "
                "ore di viaggio.",
            ]),
        ],
    )

    _write_docx(
        _add("Bilancio_Sostenibilita_2024.docx"),
        "Bilancio di Sostenibilità 2024 — Sintesi energetica",
        [
            ("Consumi energetici", [
                "Nel 2024 lo stabilimento di Lodi ha consumato 1.240 MWh di "
                "energia elettrica, in riduzione del 6% rispetto al 2023 "
                "grazie alla sostituzione dei motori della linea A con unità "
                "ad alta efficienza.",
                "Le emissioni di ambito 1 e 2 ammontano a 412 tonnellate di "
                "CO2 equivalente, con un calo dell'8% sul 2023.",
            ]),
            ("Obiettivi 2025", [
                "Per il 2025 è prevista l'installazione di un impianto "
                "fotovoltaico da 320 kWp sulla copertura del capannone B, con "
                "obiettivo di coprire il 22% del fabbisogno elettrico.",
                "La manutenzione predittiva introdotta con il contratto 2025 "
                "è attesa ridurre i fermi non pianificati del 15%, con "
                "benefici indiretti sui consumi specifici.",
            ]),
        ],
    )

    _write_md(
        _add("Circolare_Fermo_Impianto.md"),
        "# Circolare di servizio n. 07/2025 — Fermo impianto estivo\n"
        "\n"
        "A tutto il personale di produzione.\n"
        "\n"
        "Si comunica che lo stabilimento di Lodi osserverà il fermo estivo "
        "dal **4 al 22 agosto 2025**. Durante il fermo TecnoService S.r.l. "
        "eseguirà gli interventi del Piano di Manutenzione 2025 previsti per "
        "il Q3, inclusa la sostituzione dell'olio idraulico e dei filtri "
        "sulla linea A e il kit filtri sulla linea B (Nota Operativa "
        "NO-2025/03).\n"
        "\n"
        "L'accesso allo stabilimento durante il fermo è consentito solo al "
        "personale autorizzato dall'Ing. Roveda.\n"
        "\n"
        "Lodi, 30 giugno 2025 — La Direzione di Stabilimento\n",
    )

    return created


# ---------------------------------------------------------------------------
# Ingest sulla pipeline reale
# ---------------------------------------------------------------------------

async def _ingest(corpus_dir: Path, data_dir: Path) -> None:
    """Indicizza il corpus con la pipeline reale su un DATA_DIR dedicato."""
    # Le variabili vanno impostate PRIMA del primo accesso a get_settings()
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["UPLOAD_DIR"] = str(data_dir / "uploads")

    from sqlalchemy import select

    from app.db import get_session_factory, init_db
    from app.ingestion import pipeline
    from app.models import Source, SourceType

    await init_db()
    session_factory = get_session_factory()

    async with session_factory() as db:
        result = await db.execute(
            select(Source).where(
                Source.type == SourceType.local_fs.value,
                Source.name == "demo-corpus",
            )
        )
        source = result.scalar_one_or_none()
        if source is None:
            source = Source(
                name="demo-corpus",
                type=SourceType.local_fs.value,
                config_json={"path": str(corpus_dir)},
            )
            db.add(source)
            await db.commit()
            await db.refresh(source)
        source_id = source.id

    await pipeline.start_worker(session_factory)
    try:
        for path in sorted(corpus_dir.iterdir()):
            if path.is_file():
                print(f"  → accodato {path.name}")
                await pipeline.ingest_file(
                    path,
                    source_id,
                    str(path),
                    session_factory,
                    doc_meta={
                        "title": path.stem,
                        "modified_at": datetime.fromtimestamp(
                            path.stat().st_mtime, tz=UTC
                        ).replace(tzinfo=None),
                    },
                )
        await pipeline.wait_idle()
    finally:
        await pipeline.stop_worker()

    status = pipeline.pipeline_status()
    recent = status.get("recent", [])
    failed = [r for r in recent if r.get("status") == "failed"]
    print(f"\nIngest completato su DATA_DIR={data_dir}: "
          f"{len(recent) - len(failed)} documenti, {len(failed)} errori.")
    for r in failed:
        print(f"  ERRORE su {r.get('title')}: {r.get('error')}")
    if failed:
        raise SystemExit(1)


def _print_questions() -> None:
    print("\nDomande di esempio da copiare in chat:")
    print("\n  Percorso rapido (fast):")
    for q in EXAMPLE_QUESTIONS["fast"]:
        print(f"    • {q}")
    print("\n  Ciclo agentico (confronto tra documenti):")
    for q in EXAMPLE_QUESTIONS["agentiche"]:
        print(f"    • {q}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
        help=f"destinazione dei file generati (default: {DEFAULT_CORPUS_DIR})",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"DATA_DIR dedicato per l'ingest (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="dopo la generazione, indicizza il corpus con la pipeline reale",
    )
    args = parser.parse_args()

    created = generate_corpus(args.corpus_dir)
    print(f"Corpus dimostrativo generato in {args.corpus_dir}:")
    for path in created:
        print(f"  • {path.name} ({path.stat().st_size} byte)")

    if args.ingest:
        print("\nAvvio ingest sulla pipeline reale…")
        asyncio.run(_ingest(args.corpus_dir, args.data_dir))

    _print_questions()


if __name__ == "__main__":
    main()
