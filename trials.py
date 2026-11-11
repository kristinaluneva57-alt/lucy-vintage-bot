"""Сравнительные испытания: старая Люся (атрибуция по книгам) и новая
(исследование и глубокая атрибуция) на одних и тех же карточках.

Бот ничего не запускает сам: исследования запускает администратор, а
/trial только собирает из базы то, что уже сделано, и измеримые
показатели. Правильность атрибуции и качество аналогов оценивает
Кристина по протоколу docs/RESEARCH_TRIALS.md — колонки для оценок
в файле оставлены пустыми.
"""

import json
import sqlite3

import research_engine

DB_TIMEOUT = 30
VERDICTS = {"exact": "точное совпадение", "probable": "вероятное", "stylistic": "только стиль", "none": "нет"}


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT)
    conn.execute(f"PRAGMA busy_timeout = {DB_TIMEOUT * 1000}")
    return conn


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def old_attribution(db_path, item_code):
    """Последняя завершённая атрибуция старой Люси по карточке."""
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "visual_attributions"):
            return None
        row = conn.execute(
            "SELECT id, mode, result_json FROM visual_attributions "
            "WHERE item_code = ? AND status = 'done' ORDER BY id DESC LIMIT 1",
            (item_code,),
        ).fetchone()
        if row is None:
            return None
        attribution_id, mode, raw = row
        cost = 0.0
        if _table_exists(conn, "visual_ai_calls"):
            cost = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status IN ('done', 'unknown') THEN cost_usd ELSE 0 END), 0) "
                "FROM visual_ai_calls WHERE attribution_id = ?",
                (attribution_id,),
            ).fetchone()[0]
    finally:
        conn.close()
    result = json.loads(raw or "{}")
    evidence = result.get("evidence") or []
    best = next((e for e in evidence if e.get("verdict") in ("exact", "probable")), None)
    return {
        "id": attribution_id,
        "mode": mode,
        "best": (f"{best.get('document_code')} стр. {best.get('pdf_page')} — "
                 f"{VERDICTS.get(best.get('verdict'), best.get('verdict'))}") if best else "совпадения нет",
        "pages_compared": len([e for e in evidence if e.get("verdict")]),
        "cost": cost,
    }


def new_research(db_path, item_code, mode):
    """Последнее завершённое исследование новой Люси в этом режиме."""
    conn = _connect(db_path)
    try:
        if not _table_exists(conn, "item_research"):
            return None
        row = conn.execute(
            "SELECT id FROM item_research WHERE item_code = ? AND mode = ? AND status = 'done' "
            "ORDER BY id DESC LIMIT 1",
            (item_code, mode),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    record = research_engine.get(db_path, row[0])
    result = record["result"]
    cited = research_engine._cited(result)
    metrics = result.get("metrics") or {}
    return {
        "id": record["id"],
        "summary": result["identification"].get("summary") or "—",
        "confidence": research_engine.CONFIDENCE_LABELS[result["identification"]["confidence"]],
        "books": len([r for r in cited if r.startswith("B")]),
        "web": len([r for r in cited if r.startswith("W")]),
        "analogs": len(result.get("analogs") or []),
        "sold": len([p for p in result.get("prices") or [] if p["kind"] == "sold"]),
        "removed": metrics.get("removed", 0),
        "downgraded": metrics.get("downgraded", 0),
        "cost": record.get("cost_usd") or 0.0,
    }


def collect(db_path, item_codes):
    return [
        {
            "item": code,
            "old": old_attribution(db_path, code),
            "research": new_research(db_path, code, "research"),
            "deep": new_research(db_path, code, "deep"),
        }
        for code in item_codes
    ]


def summary_lines(rows):
    lines = ["🧪 ИСПЫТАНИЯ: старая Люся и новая", ""]
    totals = {"old": [], "research": [], "deep": []}
    for row in rows:
        lines.append(f"💎 {row['item']}")
        old = row["old"]
        if old:
            lines.append(f"• старая: {old['best']} · сравнено страниц {old['pages_compared']} · {old['cost']:.4f} $")
            totals["old"].append(old["cost"])
        else:
            lines.append("• старая: не запускалась (карточка → «🔬 Исследовать по библиотеке» → «🔬 Начать исследование»)")
        for mode, label in (("research", "исследование"), ("deep", "глубокая")):
            item = row[mode]
            if item:
                lines.append(
                    f"• {label}: {item['summary'][:90]} · уверенность {item['confidence']} · "
                    f"книг {item['books']}, сайтов {item['web']}, аналогов {item['analogs']}, "
                    f"продаж {item['sold']} · удалено без источника {item['removed']}, "
                    f"понижено {item['downgraded']} · {item['cost']:.4f} $"
                )
                totals[mode].append(item["cost"])
            else:
                lines.append(f"• {label}: не запускалась (/research {row['item']}"
                             + (" deep" if mode == "deep" else "") + ")")
        lines.append("")
    lines.append("Средняя стоимость одного исследования:")
    for mode, label in (("old", "старая"), ("research", "исследование"), ("deep", "глубокая")):
        values = totals[mode]
        lines.append(f"• {label}: " + (f"{sum(values) / len(values):.4f} $ (по {len(values)})" if values else "нет данных"))
    lines.append("")
    lines.append("Правильность и качество аналогов оценивает Кристина — колонки в файле ниже.")
    return lines


def score_sheet(rows):
    """Таблица для оценок Кристины (TSV): измеренное заполнено, оценки — пустые."""
    header = [
        "карточка", "версия", "вывод", "уверенность", "источники книг", "источники сайтов",
        "аналогов", "удалено без источника", "стоимость $",
        "правильность 0-2", "аналоги 0-2", "источники проверены 0-2", "ложных утверждений", "комментарий",
    ]
    lines = ["\t".join(header)]
    for row in rows:
        old = row["old"]
        lines.append("\t".join([
            row["item"], "старая", old["best"] if old else "не запускалась", "", "", "", "", "",
            f"{old['cost']:.4f}" if old else "", "", "", "", "", "",
        ]))
        for mode, label in (("research", "исследование"), ("deep", "глубокая")):
            item = row[mode]
            lines.append("\t".join([
                row["item"], label,
                item["summary"].replace("\t", " ") if item else "не запускалась",
                item["confidence"] if item else "", str(item["books"]) if item else "",
                str(item["web"]) if item else "", str(item["analogs"]) if item else "",
                str(item["removed"]) if item else "", f"{item['cost']:.4f}" if item else "",
                "", "", "", "", "",
            ]))
    return "\n".join(lines) + "\n"
