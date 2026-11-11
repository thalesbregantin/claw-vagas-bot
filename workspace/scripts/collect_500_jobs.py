#!/usr/bin/env python3
"""
workspace/scripts/collect_500_jobs.py
Coletor em Massa de Vagas (LinkedIn + Indeed + Glassdoor) para Inteligência de Mercado.

Meta: Coletar e enriquecer pelo menos 500 vagas reais e detalhadas de:
- Engenharia de IA / LLMs / Agentes
- Python Backend / FastAPI / Arquitetura
- Engenharia de Dados / Analytics / Automação
Com deduplicação no SQLite (db/jobs.db) e extração completa de requisitos.
"""

import os
import sys
import time
import json
import sqlite3
import random
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = WORKSPACE_ROOT / "db" / "jobs.db"
OUTPUT_DIR = WORKSPACE_ROOT / "output"

# Matriz rica de busca para cobrir o ecossistema completo de IA e Backend
SEARCH_TERMS = [
    # Inteligência Artificial, Agentes & LLMs
    "Senior AI Engineer",
    "Engenheiro de IA",
    "AI Engineer",
    "Engenheiro de Inteligência Artificial",
    "Senior Agentic AI Engineer",
    "LLM Engineer",
    "Generative AI Engineer",
    "Machine Learning Engineer",
    "AI Solutions Architect",
    "LangChain",
    
    # Python & Backend Moderno
    "Senior Python Developer",
    "Python Backend Engineer",
    "Desenvolvedor Python Sênior",
    "FastAPI",
    "Python Software Engineer",
    "Desenvolvedor Backend Python",
    "Python Specialist",
    
    # Dados, Analytics & Automação
    "Engenheiro de Dados Python",
    "Analytics Engineer Sênior",
    "Automation & AI Engineer",
    "Data Engineer Sênior",
    "Engenheiro de Software Backend"
]

LOCATIONS = [
    "Brazil",
    "São Paulo, Brazil",
    "Remoto"
]

TARGET_TOTAL = 500


def get_current_job_count(conn: sqlite3.Connection) -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) FROM jobs")
    return cursor.fetchone()[0]


def collect_mass_jobs(target_count: int = TARGET_TOTAL):
    print("=" * 65)
    print(f"🚀 INICIANDO MINERAÇÃO EM MASSA DE VAGAS (META: {target_count}+ VAGAS)")
    print("Fontes: LinkedIn + Indeed (Brasil/Remoto)")
    print("=" * 65)

    from jobspy import scrape_jobs

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    current_count = get_current_job_count(conn)
    print(f"📦 Total de vagas já existentes no banco: {current_count}")

    if current_count >= target_count:
        print(f"✅ O banco já possui {current_count} vagas (meta atingida).")
    
    total_added = 0
    iteration = 0

    # Embaralha buscas para maximizar diversidade
    queries = []
    for loc in LOCATIONS:
        for term in SEARCH_TERMS:
            queries.append((term, loc))
    random.shuffle(queries)

    for term, location in queries:
        current_count = get_current_job_count(conn)
        if current_count >= target_count:
            print(f"\n🎉 META ATINGIDA COM SUCESSO! Total no banco: {current_count} vagas.")
            break

        iteration += 1
        print(f"\n[{iteration}/{len(queries)}] 🔎 Buscando: '{term}' em '{location}' (Progresso: {current_count}/{target_count})...")

        try:
            # Alterna sites para cobrir o mercado de forma balanceada
            jobs_df = scrape_jobs(
                **common.scrape_options(),
                search_term=term,
                location=location,
                country_indeed="brazil",
                results_wanted=15,
                fetch_description=True,
                hours_old=168 # Últimos 7 dias
            )
        except Exception as e:
            print(f"  ⚠️ Aviso na coleta de '{term}': {e}")
            time.sleep(2)
            continue

        if jobs_df is None or len(jobs_df) == 0:
            print(f"  Nenhum resultado nesta combinação. Próxima...")
            continue

        batch_new = 0
        for _, row in jobs_df.iterrows():
            job = common.parse_jobspy_row(row, default_location=location)
            if not job:
                continue

            _, is_new = common.save_job(cursor, job)
            conn.commit()
            if is_new:
                batch_new += 1
                total_added += 1

        print(f"  ✨ {batch_new} novas vagas adicionadas neste lote (Total banco: {get_current_job_count(conn)})")
        
        # Jitter de cortesia entre requisições (1.5s a 3.0s)
        time.sleep(random.uniform(1.5, 3.0))

    final_count = get_current_job_count(conn)
    conn.close()

    print("\n" + "=" * 65)
    print(f"🏁 MINERAÇÃO CONCLUÍDA!")
    print(f"📊 Total de vagas no banco jobs.db: {final_count}")
    print(f"📥 Novas vagas adicionadas nesta execução: {total_added}")
    print("=" * 65)

    # Executa automaticamente a análise de inteligência sobre o dataset expandido
    print("\n🧠 Disparando análise de Inteligência de Mercado sobre o novo dataset...")
    intel_script = WORKSPACE_ROOT / "scripts" / "market_intelligence.py"
    if intel_script.exists():
        import subprocess
        subprocess.run([sys.executable, str(intel_script)], check=False)


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else TARGET_TOTAL
    collect_mass_jobs(target)
