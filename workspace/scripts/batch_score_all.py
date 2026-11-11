#!/usr/bin/env python3
"""
workspace/scripts/batch_score_all.py
Pontua as vagas pendentes no jobs.db. Com --all, recalcula todas as vagas ainda em triagem.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "scripts"))

from tailor_resume import calculate_match_score, load_profile

DB_PATH = Path(os.environ["OPENCLAW_DB_PATH"]) if os.environ.get("OPENCLAW_DB_PATH") else (WORKSPACE_ROOT / "db" / "jobs.db")
TRIAGE_STATUSES = ("discovered", "scored", "rejected_score")


def score_jobs(conn: sqlite3.Connection, rescore_all: bool = False) -> int:
    """Aplica o scoring atual e o corte único de profile.json. Retorna quantas vagas mudaram."""
    profile = load_profile()
    min_score = profile.get("search_criteria", {}).get("min_match_score", 75)
    cur = conn.cursor()

    if rescore_all:
        cur.execute("""
            SELECT id, title, description, workplace_type, status, location FROM jobs
            WHERE status IN ('discovered', 'scored', 'rejected_score')
        """)
    else:
        cur.execute("""
            SELECT id, title, description, workplace_type, status, location FROM jobs
            WHERE match_score = 0 OR match_reasons IS NULL OR match_score IS NULL
        """)
    rows = cur.fetchall()

    for row_id, title, desc, wp, status, location in rows:
        score, reasons, gaps = calculate_match_score(
            profile=profile,
            job_title=title,
            job_description=desc or "",
            workplace_type=wp or "Remote",
            location=location or ""
        )
        new_status = status
        if status in TRIAGE_STATUSES:
            new_status = "scored" if score >= min_score else "rejected_score"

        cur.execute("""
            UPDATE jobs
            SET match_score = ?, match_reasons = ?, gaps = ?, status = ?, scored_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (score, json.dumps(reasons, ensure_ascii=False), json.dumps(gaps, ensure_ascii=False), new_status, row_id))

    conn.commit()
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Pontuação em lote das vagas")
    parser.add_argument("--all", action="store_true", help="Recalcula todas as vagas ainda em triagem")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    total = score_jobs(conn, rescore_all=args.all)
    conn.close()
    print(f"Concluído! {total} vagas pontuadas com sucesso.")


if __name__ == "__main__":
    main()
