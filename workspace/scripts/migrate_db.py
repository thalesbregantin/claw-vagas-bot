#!/usr/bin/env python3
"""
workspace/scripts/migrate_db.py
Migração idempotente do jobs.db para o formato atual. Sempre cria backup antes de gravar.
1. Adiciona as colunas e tabelas novas do schema.
2. Padroniza job_id (linkedin-li-1 -> li-1, indeed-in-1 -> in-1, 1 -> li-1), unindo repetidas.
3. Corrige apply_type: LinkedIn nunca confirmado vira UNKNOWN; Indeed fica EXTERNAL_ATS.
4. Marca como 'skipped' as cópias de cargo+empresa+local ainda em triagem.
5. Recalcula o match_score das vagas em triagem.
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_score_all
import common
import init_db

# Maior valor = vaga mais avançada no funil; é a que sobrevive quando há duplicatas
STATUS_RANK = {
    "applied": 10, "in_progress": 9, "needs_input": 8, "approved": 7, "notified_external": 6,
    "failed": 5, "pending_approval": 4, "scored": 3, "rejected_score": 2, "discovered": 1, "skipped": 0
}
TRIAGE_STATUSES = ("discovered", "scored", "rejected_score")


def rank(job: sqlite3.Row) -> tuple:
    return (STATUS_RANK.get(job["status"], 0), job["match_score"] or 0, -job["id"])


def normalize_ids(conn: sqlite3.Connection) -> Dict[str, int]:
    groups: Dict[str, List[sqlite3.Row]] = {}
    for job in conn.execute("SELECT id, job_id, status, match_score FROM jobs").fetchall():
        groups.setdefault(common.canonical_job_id(job["job_id"]), []).append(job)

    renamed = merged = 0
    for canonical, jobs in groups.items():
        keeper = max(jobs, key=rank)
        for job in jobs:
            if job["id"] == keeper["id"]:
                continue
            conn.execute("UPDATE application_logs SET job_id = ? WHERE job_id = ?", (keeper["job_id"], job["job_id"]))
            conn.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))
            merged += 1
        if keeper["job_id"] != canonical:
            conn.execute("UPDATE application_logs SET job_id = ? WHERE job_id = ?", (canonical, keeper["job_id"]))
            conn.execute("UPDATE jobs SET job_id = ? WHERE id = ?", (canonical, keeper["id"]))
            renamed += 1
    return {"renamed": renamed, "merged": merged}


def fix_apply_types(conn: sqlite3.Connection) -> int:
    cursor = conn.execute("""
        UPDATE jobs SET apply_type = CASE WHEN job_url LIKE '%linkedin.com%' THEN 'UNKNOWN' ELSE 'EXTERNAL_ATS' END
        WHERE status NOT IN ('applied', 'notified_external') AND apply_type != 'EASY_APPLY'
    """)
    return cursor.rowcount


def skip_duplicates(conn: sqlite3.Connection) -> int:
    groups: Dict[tuple, List[sqlite3.Row]] = {}
    rows = conn.execute("SELECT id, job_id, title, company, location, status, match_score FROM jobs").fetchall()
    for job in rows:
        key = (job["title"].strip().lower(), job["company"].strip().lower(), (job["location"] or "").strip().lower())
        groups.setdefault(key, []).append(job)

    skipped = 0
    for jobs in groups.values():
        keeper = max(jobs, key=rank)
        for job in jobs:
            if job["id"] != keeper["id"] and job["status"] in TRIAGE_STATUSES:
                conn.execute(
                    "UPDATE jobs SET status = 'skipped', error_log = ? WHERE id = ?",
                    (f"Duplicada de {keeper['job_id']}", job["id"])
                )
                skipped += 1
    return skipped


def migrate(db_path: Path, backup: bool = True) -> Dict[str, int]:
    if backup:
        target = db_path.with_name(f"{db_path.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
        shutil.copyfile(db_path, target)
        print(f"[OK] Backup criado em {target}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(init_db.SCHEMA_PATH.read_text(encoding="utf-8"))
        init_db.ensure_columns(conn)
        # O schema liga as FKs; job_id é renomeado em jobs e application_logs na mesma transação
        conn.execute("PRAGMA foreign_keys = OFF")

        report = normalize_ids(conn)
        report["apply_types_fixed"] = fix_apply_types(conn)
        report["duplicates_skipped"] = skip_duplicates(conn)
        conn.commit()

        report["rescored"] = batch_score_all.score_jobs(conn, rescore_all=True)
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migra o jobs.db para o formato atual")
    parser.add_argument("--db", default=str(batch_score_all.DB_PATH), help="Caminho do banco")
    args = parser.parse_args()

    print(migrate(Path(args.db)))


if __name__ == "__main__":
    main()
