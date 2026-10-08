
import re

from library_db import get_connection
from knowledge_bridge import save_library_fact


FACT_PATTERNS = [
    r"\bwas used\b",
    r"\bwere used\b",
    r"\bwas made\b",
    r"\bwere made\b",
    r"\bwas produced\b",
    r"\bwere produced\b",
    r"\bwas discovered\b",
    r"\bwas manufactured\b",
    r"\bwere manufactured\b",
    r"\bwas exported\b",
    r"\bwere exported\b",
    r"\bcentury\b",
    r"\bcenturies\b",
    r"\broman\b",
    r"\bmedieval\b",
    r"\bvictorian\b",
    r"\bwhitby\b",
]


def split_sentences(text):
    text = re.sub(r"\s+", " ", text or "").strip()

    if not text:
        return []

    return re.split(r"(?<=[.!?])\s+", text)


def is_fact_candidate(sentence):
    sentence = sentence.strip()

    if len(sentence) < 45 or len(sentence) > 500:
        return False

    if not re.search(r"[.!?]$", sentence):
        return False

    if not re.search(r"[a-zA-Z]", sentence):
        return False

    return any(
        re.search(pattern, sentence, re.IGNORECASE)
        for pattern in FACT_PATTERNS
    )


def extract_page_facts(document_code, pdf_page, limit=10):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.cleaned_text, p.original_text
            FROM library_pages p
            JOIN library_documents d
                ON d.id = p.document_id
            WHERE d.document_code = ?
              AND p.pdf_page = ?
            """,
            (document_code, pdf_page),
        ).fetchone()

    if row is None:
        raise ValueError("Страница документа не найдена")

    text = row[0] or row[1] or ""

    if not text.strip():
        return []

    candidates = []

    for sentence in split_sentences(text):
        if is_fact_candidate(sentence):
            candidates.append(sentence.strip())

        if len(candidates) >= limit:
            break

    return candidates


def save_page_facts(document_code, pdf_page, limit=10):
    candidates = extract_page_facts(
        document_code,
        pdf_page,
        limit=limit,
    )

    saved = []

    for sentence in candidates:
        fact_id = save_library_fact(
            document_code=document_code,
            pdf_page=pdf_page,
            topic="Jet — кандидат на проверку",
            fact=sentence,
            notes=(
                "Автоматически выделенный фрагмент OCR. "
                "Не проверен по изображению страницы."
            ),
        )

        saved.append({
            "id": fact_id,
            "text": sentence,
            "pdf_page": pdf_page,
            "status": "extracted",
        })

    return saved
