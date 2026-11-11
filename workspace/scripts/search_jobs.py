#!/usr/bin/env python3
"""
workspace/scripts/search_jobs.py
Pipeline autônomo de busca de vagas no LinkedIn via JobSpy:
1. Busca vagas recentes (off-account) para os cargos-alvo
2. Deduplica e salva no SQLite (db/jobs.db)
3. Executa o tailor_resume.py para cálculo de match_score e geração do PDF ATS
4. Despacha cartões das vagas qualificadas para o Telegram autorizado
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import queue_guard
from tailor_resume import load_profile

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ["OPENCLAW_DB_PATH"]) if os.environ.get("OPENCLAW_DB_PATH") else (WORKSPACE_ROOT / "db" / "jobs.db")
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"

TARGET_ROLES = [
    "Senior AI Engineer",
    "Engenheiro de IA",
    "Senior Agentic AI Engineer",
    "Python Backend Engineer"
]


def send_telegram_message(text: str, reply_markup: Dict = None):
    return common.send_message(common.authorized_chat_id(), text, reply_markup=reply_markup)


def build_job_card(job: Dict[str, str], score: int, reasons: List[str], gaps: List[str]) -> str:
    type_labels = {"EXTERNAL_ATS": "Portal da empresa", "EASY_APPLY": "Easy Apply"}
    top_reasons = "\n".join(f"• {r}" for r in reasons[:3]) or "• Nenhum motivo forte identificado"
    top_gaps = "\n".join(f"• {g}" for g in gaps[:2]) or "• Nenhum gap identificado"
    return (
        f"🎯 *Nova vaga — Fit {score}%*\n"
        f"💼 *Cargo:* {job['title']}\n"
        f"🏢 *Empresa:* {job['company']}\n"
        f"📍 *Local:* {job['location']} ({job['workplace_type']})\n"
        f"🏷️ *Tipo:* {type_labels.get(job['apply_type'], 'Confirmado ao aprovar')}\n\n"
        f"🌟 *Motivos:*\n{top_reasons}\n\n"
        f"⚠️ *Gaps:*\n{top_gaps}"
    )


def run_discovery():
    print("=" * 60)
    print("🔍 INICIANDO BUSCA DE VAGAS NO LINKEDIN (JobSpy)")
    print("=" * 60)

    from jobspy import scrape_jobs

    min_score = load_profile().get("search_criteria", {}).get("min_match_score", 75)
    excluded = queue_guard.load_excluded_companies()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    new_jobs: List[Dict[str, str]] = []

    for role in TARGET_ROLES:
        print(f"\n🔎 Buscando vagas para: '{role}'...")
        try:
            jobs_df = scrape_jobs(
                **common.scrape_options(),
                search_term=role,
                location="Brazil",
                results_wanted=6,
                hours_old=72,
                fetch_description=True
            )
        except Exception as e:
            print(f"  ❌ Erro ao buscar '{role}': {e}")
            continue

        if jobs_df is None or len(jobs_df) == 0:
            print(f"  Nenhuma vaga recente encontrada para '{role}'.")
            continue

        for _, row in jobs_df.iterrows():
            job = common.parse_jobspy_row(row)
            if not job or job["company"].strip().lower() in excluded:
                continue
            job_id, is_new = common.save_job(cursor, job)
            conn.commit()
            if is_new:
                new_jobs.append(job)
                print(f"  ✨ Nova vaga salva: [{job_id}] {job['title']} @ {job['company']}")

    conn.close()
    print(f"\n✅ Total de novas vagas descobertas: {len(new_jobs)}")

    if not new_jobs:
        send_telegram_message("🔍 *Busca concluída*\nNenhuma vaga inédita nas últimas 72h para seus cargos-alvo.")
        return

    print("\n" + "=" * 60)
    print("📊 CALCULANDO MATCH SCORE E GERANDO CURRÍCULOS ATS")
    print("=" * 60)

    tailor_script = SCRIPTS_DIR / "tailor_resume.py"
    qualified_count = 0

    for job in new_jobs:
        jid = job["job_id"]
        res = subprocess.run([sys.executable, str(tailor_script), "--job-id", jid], capture_output=True, text=True, encoding="utf-8")
        if res.returncode != 0:
            print(f"  ❌ Erro no tailoring da vaga {jid}: {res.stderr}")
            continue

        result = json.loads(res.stdout)
        score = result.get("match_score", 0)
        print(f"  🎯 {jid}: Match Score {score}%")
        if score < min_score:
            continue

        qualified_count += 1
        send_telegram_message(
            build_job_card(job, score, result.get("match_reasons", []), result.get("gaps", [])),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🚀 Aprovar", "callback_data": f"approve:{jid}"},
                        {"text": "⏭️ Pular", "callback_data": f"skip:{jid}"}
                    ],
                    [{"text": "🌐 Ver Anúncio", "url": job["job_url"]}]
                ]
            }
        )

    summary_msg = (
        f"✅ *Busca concluída*\n"
        f"• Vagas analisadas: {len(new_jobs)}\n"
        f"• Qualificadas (Fit ≥ {min_score}%): {qualified_count}"
    )
    send_telegram_message(summary_msg)
    print("\n" + summary_msg)


if __name__ == "__main__":
    run_discovery()
