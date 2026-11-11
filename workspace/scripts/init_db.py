#!/usr/bin/env python3
"""
workspace/scripts/init_db.py
Inicializador e mantenedor do banco SQLite para o LinkedIn OpenClaw Agent.
Aplica workspace/db/schema.sql, cria diretórios essenciais e garante registro
de cotas em daily_quotas.
"""

import sys
import sqlite3
import argparse
import json
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def get_now_brt() -> datetime:
    """Retorna datetime atual no fuso horário de Brasília (America/Sao_Paulo / UTC-3)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-3), name="America/Sao_Paulo"))

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = WORKSPACE_ROOT / "db"
DB_PATH = DB_DIR / "jobs.db"
SCHEMA_PATH = DB_DIR / "schema.sql"
OUTPUT_DIR = WORKSPACE_ROOT / "output"
RESUMES_DIR = OUTPUT_DIR / "resumes"
LOGS_DIR = OUTPUT_DIR / "logs"
DATA_DIR = WORKSPACE_ROOT / "data"


def ensure_directories():
    """Garante que as pastas de dados e saídas estejam criadas."""
    for d in [DB_DIR, DATA_DIR, OUTPUT_DIR, RESUMES_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def ensure_columns(conn: sqlite3.Connection):
    """Adiciona colunas novas em bancos criados por versões anteriores do schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, column_type in [("outcome", "TEXT"), ("outcome_at", "TIMESTAMP")]:
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {column_type}")


def init_db(reset: bool = False):
    """Executa o schema.sql no banco jobs.db."""
    ensure_directories()

    if reset and DB_PATH.exists():
        try:
            DB_PATH.unlink()
            print(f"[WARN] Banco de dados existente removido: {DB_PATH}")
        except Exception as e:
            print(f"[ERRO] Falha ao remover banco: {e}")
            sys.exit(1)

    if not SCHEMA_PATH.exists():
        print(f"[ERRO] Arquivo de schema não encontrado: {SCHEMA_PATH}")
        sys.exit(1)

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(schema_sql)
        ensure_columns(conn)
        # Garante inicialização da cota diária para o dia atual em America/Sao_Paulo (UTC-3)
        today_str = get_now_brt().date().isoformat()
        conn.execute("""
            INSERT OR IGNORE INTO daily_quotas (date, count, max_limit)
            VALUES (?, 0, 15)
        """, (today_str,))
        conn.commit()
        print(f"[OK] Banco de dados inicializado em {DB_PATH}")
        print(f"[OK] Cota diária registrada para {today_str} (limite: 15)")
    except Exception as e:
        print(f"[ERRO] Erro ao aplicar schema: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


def seed_data():
    """Popula com dados realistas para testes de fluxo."""
    init_db(reset=False)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    sample_jobs = [
        {
            "job_id": "4123984710",
            "title": "Senior AI & Automation Engineer",
            "company": "Cognitive Scale AI",
            "location": "São Paulo, SP (Remoto)",
            "workplace_type": "Remote",
            "job_url": "https://www.linkedin.com/jobs/view/4123984710/",
            "apply_url": None,
            "apply_type": "EASY_APPLY",
            "description": "Buscamos Senior AI & Automation Engineer para liderar arquitetura de agentes autônomos em Python, pipelines RAG com FastAPI e servidores MCP.",
            "match_score": 92,
            "match_reasons": json.dumps([
                "Experiência com Python, FastAPI e Docker",
                "Domínio de arquiteturas de Agentes LLM, RAG e MCP",
                "Fit perfeito de modelo 100% Remoto no fuso de São Paulo"
            ], ensure_ascii=False),
            "gaps": json.dumps([], ensure_ascii=False),
            "tailored_resume_path": "output/resumes/4123984710.pdf",
            "status": "pending_approval"
        },
        {
            "job_id": "4125892102",
            "title": "Lead Python & GenAI Solutions Architect",
            "company": "Fintech Apex",
            "location": "Global Remote",
            "workplace_type": "Remote",
            "job_url": "https://www.linkedin.com/jobs/view/4125892102/",
            "apply_url": "https://careers.fintechapex.io/apply/genai-lead-991",
            "apply_type": "EXTERNAL_ATS",
            "description": "Fintech internacional busca GenAI Solutions Architect para projetar copilot com microsserviços Python, AWS ECS e bancos vetoriais.",
            "match_score": 88,
            "match_reasons": json.dumps([
                "Arquitetura de microsserviços Python de alta escala",
                "Segurança de LLMs e bancos vetoriais corporativos",
                "Inglês fluente para atuação com squads internacionais"
            ], ensure_ascii=False),
            "gaps": json.dumps(["Domínio prévio de regulamentação financeira SOX"], ensure_ascii=False),
            "tailored_resume_path": "output/resumes/4125892102.pdf",
            "status": "notified_external"
        }
    ]

    for j in sample_jobs:
        cursor.execute("""
            INSERT OR REPLACE INTO jobs (
                job_id, title, company, location, workplace_type, job_url, apply_url,
                apply_type, description, match_score, match_reasons, gaps,
                tailored_resume_path, status, scored_at, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            j["job_id"], j["title"], j["company"], j["location"], j["workplace_type"],
            j["job_url"], j["apply_url"], j["apply_type"], j["description"],
            j["match_score"], j["match_reasons"], j["gaps"], j["tailored_resume_path"],
            j["status"]
        ))
        cursor.execute("""
            INSERT INTO application_logs (job_id, step, status, message)
            VALUES (?, 'seed', 'info', 'Vaga de teste inserida para validação de fluxo')
        """, (j["job_id"],))

    conn.commit()
    conn.close()
    print("[OK] Vagas de teste populadas com sucesso.")


def main():
    parser = argparse.ArgumentParser(description="Inicializador do Banco SQLite para OpenClaw")
    parser.add_argument("--reset", action="store_true", help="Recria o banco do zero")
    parser.add_argument("--seed", action="store_true", help="Insere dados de exemplo")

    args = parser.parse_args()
    init_db(reset=args.reset)
    if args.seed:
        seed_data()


if __name__ == "__main__":
    main()
