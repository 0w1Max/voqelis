"""Isolated Day Facts state+export smoke, never opens production databases."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from voqelis.day_facts.export import export_docx, export_pdf
from voqelis.day_facts.store import DayFactsStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    work = args.work_dir.resolve(strict=True)
    if not work.is_dir():
        parser.error("Not a directory")
    with TemporaryDirectory(prefix="day-facts-smoke-", dir=work) as scratch:
        root = Path(scratch)
        store = DayFactsStore(root / "test.sqlite3")
        day = date(2026, 10, 26)
        try:
            draft = store.begin(1, day)
            draft = store.accept(1, draft.token, "Гуляли с другом.")
            draft = store.accept(
                1, draft.token,
                "Друг разговаривал раздражённо. Мы разошлись по домам.",
            )
            draft = store.next(1, draft.token)
            draft = store.accept(
                1, draft.token,
                "Я не так общаюсь. Я плохой друг.",
            )
            draft = store.next(1, draft.token)
            entry = store.save(1, draft.token)
            assert entry is not None
            assert len(entry.facts) == len(entry.judgments) == 2
            assert store.for_day(2, day) == []
            entries = store.for_day(1, day)
            export_docx(entries, day, root / "result.docx")
            export_pdf(entries, day, root / "result.pdf")
            export_pdf(entries, day, root / "phone.pdf", mobile=True)
            assert all((root / file).stat().st_size > 2000 for file in (
                "result.docx", "result.pdf", "phone.pdf"
            ))
        finally:
            store.close()
        store = DayFactsStore(root / "test.sqlite3")
        try:
            assert store.get(1, entry.id) == entry
            assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            store.close()
        print("DAY FACTS V1 SMOKE PASS: SQLite, DOCX, PDF A4, PDF mobile")


if __name__ == "__main__":
    main()
