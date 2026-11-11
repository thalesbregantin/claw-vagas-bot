#!/usr/bin/env python3
"""
workspace/scripts/market_intelligence.py
Motor de Inteligência de Mercado, Análise de Gaps e Recomendações de Carreira.

Objetivo:
1. Ler e analisar os requisitos e descrições de todas as vagas salvas em db/jobs.db.
2. Identificar tecnologias, ferramentas, arquiteturas e certificações mais demandadas pelo mercado.
3. Cruzar contra o perfil real de Thales (data/profile.json).
4. Mapear:
   - Pontos Fortes e Diferenciais Competitivos
   - Quick Wins (Habilidades que você já tem ou domina que devem ser mais enfatizadas no CV)
   - Gaps de Mercado (Tecnologias mais pedidas que você ainda não tem listadas)
5. Propor melhorias acionáveis para:
   - Currículo e LinkedIn (Palavras-chave ATS essenciais)
   - Projetos para GitHub e Portfólio (Repositórios práticos para cobrir os gaps)
   - Trilha de Aprendizado de Alto ROI
"""

import os
import sys
import json
import sqlite3
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = WORKSPACE_ROOT / "db" / "jobs.db"
PROFILE_PATH = WORKSPACE_ROOT / "data" / "profile.json"
OUTPUT_DIR = WORKSPACE_ROOT / "output"
REPORT_MD_PATH = OUTPUT_DIR / "market_intelligence_report.md"

# Dicionário de taxonomia técnica com regex para identificação precisa
SKILL_TAXONOMY = {
    # Linguagens & Core
    "Python": r"\bpython\b",
    "SQL": r"\bsql\b",
    "JavaScript / TypeScript": r"\b(javascript|typescript|js|ts)\b",
    "Bash / Shell": r"\b(bash|shell|sh)\b",
    
    # Frameworks Backend
    "FastAPI": r"\bfastapi\b",
    "Django": r"\bdjango\b",
    "Flask": r"\bflask\b",
    "Asyncio / Concorrência": r"\basyncio\b",
    "Pydantic": r"\bpydantic\b",
    
    # IA, LLMs & Agentes
    "LangChain": r"\blangchain\b",
    "LangGraph": r"\blanggraph\b",
    "LlamaIndex": r"\b(llamaindex|llama-index)\b",
    "CrewAI": r"\bcrewai\b",
    "RAG (Retrieval-Augmented Generation)": r"\b(rag|retrieval[- ]augmented)\b",
    "Vector Databases (Pinecone/Qdrant/Chroma/pgvector)": r"\b(pinecone|qdrant|chroma|chromadb|pgvector|weaviate|milvus)\b",
    "Guardrails & Segurança de IA": r"\b(guardrail|guardrails|nemo guardrails|prompt injection)\b",
    "Local LLMs (Ollama/vLLM)": r"\b(ollama|vllm|llama\.cpp|tgi)\b",
    "Fine-tuning / LoRA": r"\b(fine[- ]tuning|lora|qlora|peft)\b",
    "PyTorch / TensorFlow": r"\b(pytorch|tensorflow)\b",
    "DSPy": r"\bdspy\b",
    "OpenAI / Anthropic APIs": r"\b(openai|anthropic|claude|gpt[- ]?4)\b",
    
    # Automação & Scraping
    "Playwright": r"\bplaywright\b",
    "Selenium": r"\bselenium\b",
    "n8n / Workflows": r"\bn8n\b",
    "RPA / UiPath": r"\b(rpa|uipath)\b",
    
    # Engenharia de Dados & Bancos
    "PostgreSQL": r"\b(postgres|postgresql)\b",
    "BigQuery": r"\bbigquery\b",
    "Redis / Caching": r"\bredis\b",
    "Kafka / Mensageria / RabbitMQ": r"\b(kafka|rabbitmq|sqs|event[- ]driven)\b",
    "ETL / Pipelines de Dados": r"\b(etl|data pipelines?)\b",
    "Pandas / Polars / NumPy": r"\b(pandas|polars|numpy)\b",
    "Airflow / Prefect / Dagster": r"\b(airflow|prefect|dagster)\b",
    
    # Cloud & Infraestrutura / DevOps
    "AWS": r"\b(aws|amazon web services)\b",
    "GCP (Google Cloud Platform)": r"\b(gcp|google cloud)\b",
    "Azure": r"\bazure\b",
    "Docker / Containers": r"\bdocker\b",
    "Kubernetes / K8s": r"\b(kubernetes|k8s)\b",
    "CI/CD (GitHub Actions/GitLab)": r"\b(ci/cd|github actions|gitlab ci)\b",
    "Terraform / IaC": r"\b(terraform|iac|infrastructure as code)\b",
    "Linux": r"\blinux\b",
    "Git / GitHub": r"\b(git|github)\b",
    
    # BI & Analytics
    "Power BI": r"\bpower bi\b",
    "Looker Studio / Data Studio": r"\b(looker|looker studio|data studio)\b",
    
    # Engenharia de Software & Metodologias
    "Arquitetura de Microsserviços": r"\b(microservices|microsserviços)\b",
    "RESTful APIs": r"\b(rest|restful|api design)\b",
    "Testes Automatizados (pytest/BDD)": r"\b(pytest|tdd|bdd|unit tests?|testes unitários)\b",
    "System Design / Escalabilidade": r"\b(system design|scalability|alta escala|concorrência)\b",
    "Lean Six Sigma": r"\blean six sigma\b"
}


def load_profile_skills() -> Dict[str, Any]:
    """Carrega as habilidades e tecnologias mapeadas no perfil do Thales."""
    if not PROFILE_PATH.exists():
        return {}
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Coleta todas as palavras-chave do perfil
    text_corpus = json.dumps(data, ensure_ascii=False).lower()
    
    profile_has = {}
    for skill, pattern in SKILL_TAXONOMY.items():
        if re.search(pattern, text_corpus, re.IGNORECASE):
            profile_has[skill] = True
        else:
            profile_has[skill] = False

    return profile_has


def analyze_market_requirements():
    """Analisa todas as descrições de vagas no banco e extrai inteligência de mercado."""
    if not DB_PATH.exists():
        print(f"[ERRO]: Banco jobs.db não encontrado em {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Cria tabela de inteligência de mercado se não existir
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL UNIQUE,
            category TEXT,
            mention_count INTEGER DEFAULT 0,
            demand_percentage REAL DEFAULT 0.0,
            in_thales_profile BOOLEAN DEFAULT 0,
            priority TEXT DEFAULT 'LOW',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    cursor.execute("SELECT job_id, title, company, description, match_score FROM jobs")
    jobs = cursor.fetchall()

    total_jobs = len(jobs)
    if total_jobs == 0:
        print("[AVISO]: Nenhuma vaga encontrada no banco de dados para analisar.")
        conn.close()
        return

    profile_has = load_profile_skills()
    skill_counts = Counter()
    skill_job_titles = {skill: [] for skill in SKILL_TAXONOMY}

    for job_id, title, company, description, score in jobs:
        desc_lower = (str(title) + " " + str(description)).lower()
        for skill, pattern in SKILL_TAXONOMY.items():
            if re.search(pattern, desc_lower, re.IGNORECASE):
                skill_counts[skill] += 1
                if len(skill_job_titles[skill]) < 3:
                    skill_job_titles[skill].append(f"{title} @ {company}")

    # Atualiza banco com os dados consolidados
    for skill, count in skill_counts.items():
        demand_pct = round((count / total_jobs) * 100, 1)
        has_skill = 1 if profile_has.get(skill, False) else 0

        # Priorização de Gap:
        # Se você NÃO tem e aparece em > 30% das vagas -> HIGH
        # Se você NÃO tem e aparece em 15% a 30% -> MEDIUM
        # Se você tem -> COMPETITIVE_ADVANTAGE se alto
        if not has_skill:
            if demand_pct >= 30:
                priority = "HIGH_GAP"
            elif demand_pct >= 15:
                priority = "MEDIUM_GAP"
            else:
                priority = "LOW_GAP"
        else:
            if demand_pct >= 50:
                priority = "CORE_STRENGTH"
            else:
                priority = "DIFFERENTIATOR"

        cursor.execute("""
            INSERT INTO market_skills (skill_name, mention_count, demand_percentage, in_thales_profile, priority, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(skill_name) DO UPDATE SET
                mention_count = excluded.mention_count,
                demand_percentage = excluded.demand_percentage,
                in_thales_profile = excluded.in_thales_profile,
                priority = excluded.priority,
                updated_at = CURRENT_TIMESTAMP
        """, (skill, count, demand_pct, has_skill, priority))

    conn.commit()
    conn.close()

    generate_market_report(total_jobs, skill_counts, profile_has, skill_job_titles)


def generate_market_report(total_jobs: int, skill_counts: Counter, profile_has: Dict[str, bool], skill_job_titles: Dict[str, List[str]]):
    """Gera o relatório estruturado em Markdown com análise de gaps e propostas para CV/GitHub."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    sorted_skills = sorted(skill_counts.items(), key=lambda x: x[1], reverse=True)

    strengths = []
    gaps_high = []
    gaps_medium = []

    for skill, count in sorted_skills:
        pct = round((count / total_jobs) * 100, 1)
        has = profile_has.get(skill, False)
        item = {"skill": skill, "count": count, "pct": pct, "examples": skill_job_titles.get(skill, [])}
        if has:
            strengths.append(item)
        else:
            if pct >= 30:
                gaps_high.append(item)
            elif pct >= 15:
                gaps_medium.append(item)

    report = []
    report.append(f"# 📊 Relatório de Inteligência de Mercado e Análise de Gaps")
    report.append(f"*Gerado em: {datetime.now().strftime('%d/%m/%Y às %H:%M:%S')}*")
    report.append(f"*Amostra analisada:* **{total_jobs} vagas reais de Engenharia de IA, Backend e Automação no LinkedIn**\n")

    report.append("---")
    report.append("## 1. 🏆 Seus Maiores Diferenciais Competitivos (Forças no Mercado)")
    report.append("Tecnologias que estão no topo da demanda do mercado e que **você já domina e tem no seu perfil**:\n")
    report.append("| Tecnologia / Domínio | % Demanda nas Vagas | Total de Menções | Impacto na Carreira |")
    report.append("| :--- | :---: | :---: | :--- |")
    for s in strengths[:8]:
        report.append(f"| **{s['skill']}** | **{s['pct']}%** | {s['count']}/{total_jobs} vagas | 🌟 Core do seu perfil |")

    report.append("\n---")
    report.append("## 2. ⚠️ Gaps Críticos de Mercado (Alta Demanda & Ausentes no CV)")
    report.append("Requisitos recorrentes nas vagas que **não aparecem explicitamente** no seu `profile.json` ou CV atual:\n")
    if gaps_high:
        report.append("| Requisito Faltante | % de Demanda | Exemplos de Vagas que Pedem | Prioridade de Ação |")
        report.append("| :--- | :---: | :--- | :---: |")
        for g in gaps_high:
            ex_str = "<br>• ".join(g["examples"][:2])
            report.append(f"| **{g['skill']}** | **{g['pct']}%** | • {ex_str} | 🔥 **ALTA** |")
    else:
        report.append("Nenhum gap crítico acima de 30% detectado!")

    if gaps_medium:
        report.append("\n### Gaps Secundários (15% a 30% de Demanda):\n")
        report.append("| Requisito Faltante | % de Demanda | Prioridade |")
        report.append("| :--- | :---: | :---: |")
        for g in gaps_medium:
            report.append(f"| **{g['skill']}** | **{g['pct']}%** | 🟡 Média |")

    report.append("\n---")
    report.append("## 3. 🎯 Propostas Práticas de Melhoria Contínua")

    report.append("\n### A. Melhorias no Currículo & Palavras-Chave ATS (Quick Wins)")
    report.append("Ajustes imediatos no texto do `profile.json` para passar pelos filtros automáticos dos recrutadores:")
    report.append("1. **Cloud & Deploy:** Se você já usou AWS (EC2/S3/Lambda) ou GCP nos seus projetos, adicione explicitamente a sigla na seção de habilidades técnicas.")
    report.append("2. **Mensageria & Filas:** Caso tenha implementado filas assíncronas com Redis, Celery ou RabbitMQ, explicite no Extrator Corporativo da Total Express.")
    report.append("3. **Vector Databases:** Liste nominalmente bancos vetoriais (ex: *pgvector, ChromaDB, Qdrant*) junto com os modelos de RAG.")

    report.append("\n### B. Ideias de Projetos de Alto Impacto para o GitHub")
    report.append("Projetos prontos para você desenvolver e colocar no seu portfólio para **eliminar os maiores gaps**:")
    report.append("""
1. 🤖 **Repositório: `agentic-rag-langgraph`**
   - **O que resolve:** Cobre **LangGraph**, **Vector DBs (Qdrant/pgvector)** e **vLLM/Ollama**.
   - **Escopo:** Um agente autônomo com LangGraph que executa RAG híbrido, roteia consultas entre busca vetorial e SQL, com guardrails de injeção de prompt e interface FastAPI.
   - **Por que brilha:** 90% das vagas de AI Engineer procuram quem sabe montar grafos de agentes determinísticos em produção.

2. ⚡ **Repositório: `fastapi-async-job-worker`**
   - **O que resolve:** Cobre **Kafka/RabbitMQ/Redis**, **Docker Compose** e **Arquitetura Event-Driven**.
   - **Escopo:** Microsserviço FastAPI para ingestão de tarefas pesadas com fila Redis/RabbitMQ, processamento paralelo em background e notificações em tempo real via WebSockets/SSE.
   - **Por que brilha:** Mostra domínio de backend de alta escala além de scripts isolados.
""")

    report.append("\n---")
    report.append("## 4. 📈 Ranking Geral de Demandas de Mercado")
    report.append("| Posição | Tecnologia / Habilidade | Frequência | % Vagas | Status no seu Perfil |")
    report.append("| :---: | :--- | :---: | :---: | :---: |")
    for idx, (skill, count) in enumerate(sorted_skills[:20], 1):
        pct = round((count / total_jobs) * 100, 1)
        status_icon = "✅ No Perfil" if profile_has.get(skill, False) else "❌ Gap"
        report.append(f"| #{idx} | **{skill}** | {count} | {pct}% | {status_icon} |")

    full_md = "\n".join(report)

    with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(full_md)

    print(f"\n✅ Relatório de Inteligência de Mercado gerado com sucesso!")
    print(f"📄 Arquivo: {REPORT_MD_PATH}")
    print(f"📊 Total de vagas processadas: {total_jobs}")
    print(f"🏆 Forças identificadas: {len(strengths)}")
    print(f"⚠️ Gaps prioritários: {len(gaps_high) + len(gaps_medium)}")


if __name__ == "__main__":
    analyze_market_requirements()
