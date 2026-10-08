
from library_db import get_connection


def init_knowledge_bridge():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS library_knowledge_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                library_page_id INTEGER NOT NULL,
                source_note TEXT,
                FOREIGN KEY (knowledge_id)
                    REFERENCES knowledge(id),
                FOREIGN KEY (library_page_id)
                    REFERENCES library_pages(id),
                UNIQUE(knowledge_id, library_page_id)
            )
        """)


def save_library_fact(
    document_code,
    pdf_page,
    topic,
    fact,
    notes=None,
):
    if not topic.strip() or not fact.strip():
        raise ValueError("Topic and fact are required")

    with get_connection() as conn:
        page = conn.execute("""
            SELECT p.id
            FROM library_pages p
            JOIN library_documents d
                ON d.id = p.document_id
            WHERE d.document_code = ?
              AND p.pdf_page = ?
        """, (document_code, pdf_page)).fetchone()

        if page is None:
            raise ValueError("Source page not found")

        page_id = page[0]

        existing = conn.execute("""
            SELECT k.id
            FROM knowledge k
            JOIN library_knowledge_sources s
                ON s.knowledge_id = k.id
            WHERE s.library_page_id = ?
              AND k.topic = ?
              AND k.fact = ?
        """, (page_id, topic, fact)).fetchone()

        if existing:
            return existing[0]

        cursor = conn.execute("""
            INSERT INTO knowledge (
                topic, fact, status, notes
            )
            VALUES (?, ?, 'extracted', ?)
        """, (topic, fact, notes))

        knowledge_id = cursor.lastrowid

        conn.execute("""
            INSERT INTO library_knowledge_sources (
                knowledge_id, library_page_id
            )
            VALUES (?, ?)
        """, (knowledge_id, page_id))

        return knowledge_id


def get_library_fact(knowledge_id):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                k.topic,
                k.fact,
                k.status,
                d.title,
                d.document_code,
                p.pdf_page,
                p.printed_page
            FROM knowledge k
            JOIN library_knowledge_sources s
                ON s.knowledge_id = k.id
            JOIN library_pages p
                ON p.id = s.library_page_id
            JOIN library_documents d
                ON d.id = p.document_id
            WHERE k.id = ?
        """, (knowledge_id,)).fetchone()

        return row
