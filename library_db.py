
import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/lucy_vintage.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_library_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS library_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_code TEXT UNIQUE,
                document_type TEXT NOT NULL DEFAULT 'book',
                title TEXT NOT NULL,
                storage_key TEXT,
                checksum TEXT,
                language TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS library_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                pdf_page INTEGER NOT NULL,
                printed_page TEXT,
                original_text TEXT,
                russian_text TEXT,
                extraction_status TEXT DEFAULT 'pending',
                FOREIGN KEY (document_id)
                    REFERENCES library_documents(id),
                UNIQUE(document_id, pdf_page)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS library_import_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                last_completed_page INTEGER DEFAULT 0,
                error_message TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id)
                    REFERENCES library_documents(id)
            )
        """)
