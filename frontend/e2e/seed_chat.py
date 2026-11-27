"""Seed E2E: inserisce una sessione chat (utente + assistente) nel DB SQLite.

Usato dalla fixture Playwright ``seed``: la sessione serve a testare il
rendering della chat (markdown, citazioni, feedback) senza chiamare il LLM.
Eseguito con l'interprete del venv backend; il server E2E è già attivo ma
sqlite tollera una scrittura breve da un secondo processo.

Uso: python seed_chat.py <db_path> '<payload JSON>'
Payload: {"username", "title", "question", "answer", "citations": [...]}
Stampa su stdout l'id della sessione creata.
"""

import json
import sqlite3
import sys


def main() -> None:
    db_path = sys.argv[1]
    payload = json.loads(sys.argv[2])
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT id FROM users WHERE username = ?", (payload["username"],)
        ).fetchone()
        if row is None:
            raise SystemExit(f"utente {payload['username']!r} non trovato")
        user_id = row[0]
        cur.execute(
            "INSERT INTO chat_sessions (user_id, title) VALUES (?, ?)",
            (user_id, payload["title"]),
        )
        session_id = cur.lastrowid
        cur.execute(
            "INSERT INTO chat_messages (session_id, role, content, citations_json)"
            " VALUES (?, 'user', ?, '[]')",
            (session_id, payload["question"]),
        )
        cur.execute(
            "INSERT INTO chat_messages (session_id, role, content, citations_json)"
            " VALUES (?, 'assistant', ?, ?)",
            (session_id, payload["answer"], json.dumps(payload["citations"])),
        )
        conn.commit()
    finally:
        conn.close()
    print(session_id)


if __name__ == "__main__":
    main()
