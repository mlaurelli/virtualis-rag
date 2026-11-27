#!/usr/bin/env python3
"""Recupero automatico dei documenti falliti in formato legacy (.doc/.xls).

Gira DENTRO il container app (copiato con docker cp). Per ogni documento in
stato 'failed' dovuto a formati Office pre-2007 che Docling non converte:

1. ritrova il file originale in /data/uploads tramite content_hash;
2. lo converte in testo (.doc → catdoc, .xls → xls2csv);
3. lo ricarica via API (/api/ingest/upload) come .txt;
4. elimina la riga fallita originale.

I file vuoti (0 byte) non sono convertibili: la riga viene solo eliminata.
Idempotente: a ogni esecuzione processa solo i falliti del momento.

Uso:
    docker cp m365_recover_failed.py virtualis-app-1:/tmp/ &&
    docker exec virtualis-app-1 python3 /tmp/m365_recover_failed.py
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request

DB = "/data/virtualis.db"
UPLOADS = "/data/uploads"
API = "http://localhost:8000"
ADMIN_USER = os.environ.get("VIRTUALIS_ADMIN", "admin")
ADMIN_PASS = os.environ.get("VIRTUALIS_ADMIN_PASSWORD", "")


def api(method: str, path: str, token: str, body: bytes | None = None,
        headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(
        API + path, data=body, method=method,
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def login() -> str:
    req = urllib.request.Request(
        API + "/api/auth/login",
        data=json.dumps({"username": ADMIN_USER, "password": ADMIN_PASS}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["access_token"]


def main() -> int:
    db = sqlite3.connect(DB)
    rows = db.execute(
        "select id, title, doc_type, content_hash from documents where status='failed'"
    ).fetchall()
    if not rows:
        print("nessun documento fallito: niente da fare")
        return 0
    print(f"documenti falliti: {len(rows)}")

    by_hash = {}
    for name in os.listdir(UPLOADS):
        path = os.path.join(UPLOADS, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                by_hash[hashlib.sha256(fh.read()).hexdigest()] = path

    token = login()
    converted = deleted = skipped = 0
    for doc_id, title, doc_type, content_hash in rows:
        src = by_hash.get(content_hash)
        text = b""
        if src and doc_type == "doc":
            result = subprocess.run(["catdoc", "-w", src], capture_output=True)
            text = result.stdout
        elif src and doc_type == "xls":
            result = subprocess.run(["xls2csv", src], capture_output=True)
            text = result.stdout
        if not text.strip():
            # File assente, vuoto o non legacy: nessuna conversione possibile
            skipped += 1
        else:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", title)[:120] + ".txt"
            boundary = "----recover"
            payload = (
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="file"; filename="{safe}"\r\n'
                f"Content-Type: text/plain\r\n\r\n"
            ).encode() + text + f"\r\n--{boundary}--\r\n".encode()
            code, resp_body = api(
                "POST", "/api/ingest/upload", token, payload,
                {"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            if code == 202:
                converted += 1
                print(f"  OK {safe} ({len(text)} bytes)")
            else:
                print(f"  ERRORE upload {safe}: HTTP {code} {resp_body[:100]}")
                continue  # non eliminare la riga se l'upload fallisce
        code, _ = api("DELETE", f"/api/documents/{doc_id}", token)
        if code == 204:
            deleted += 1
    print(f"convertiti+caricati: {converted} | righe eliminate: {deleted} "
          f"| non convertibili (vuoti): {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
