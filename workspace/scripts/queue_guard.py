#!/usr/bin/env python3
"""
workspace/scripts/queue_guard.py
Controlador de segurança, rate-limiting e travas anti-bloqueio do OpenClaw.
Regras inegociáveis:
1. Teto de 15 aplicações Easy Apply por dia registradas na tabela daily_quotas.
2. Jitter randômico obrigatório de 180 a 300 segundos entre aplicações seguidas.
3. Bloqueio fora do horário comercial (08:00 às 20:00).
4. Verificação pré-submit da existência e integridade de output/resumes/<job_id>.pdf.
5. Suporte a transição para status 'needs_input' caso ocorra pergunta não mapeada.
"""

import os
import sys
import sqlite3
import argparse
import json
import random
import re
from datetime import datetime, time, date, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Fuso horário operacional: America/Sao_Paulo (UTC-3)
try:
    from zoneinfo import ZoneInfo
    BRT_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:
    BRT_TZ = timezone(timedelta(hours=-3), name="America/Sao_Paulo")

def get_now_brt() -> datetime:
    """Retorna datetime atual no fuso horário de Brasília (America/Sao_Paulo / UTC-3)."""
    return datetime.now(BRT_TZ)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ["OPENCLAW_DB_PATH"]) if os.environ.get("OPENCLAW_DB_PATH") else (WORKSPACE_ROOT / "db" / "jobs.db")
RESUMES_DIR = WORKSPACE_ROOT / "output" / "resumes"
PREFERENCES_FILE = "user_negative_preferences.json"

DEFAULT_MAX_DAILY = 15
MIN_DELAY_SECONDS = 180
MAX_DELAY_SECONDS = 300
OPERATING_HOUR_START = time(8, 0)
OPERATING_HOUR_END = time(20, 0)
OUTCOMES = ("response", "interview", "rejected", "no_response")


def get_db():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Banco jobs.db não encontrado em: {DB_PATH}. Execute init_db.py primeiro.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_today_quota(conn: sqlite3.Connection) -> Tuple[int, int]:
    """Retorna (count_aplicadas_hoje, limite_maximo)."""
    today_str = get_now_brt().date().isoformat()
    cursor = conn.cursor()
    cursor.execute("SELECT count, max_limit FROM daily_quotas WHERE date = ?", (today_str,))
    row = cursor.fetchone()
    if row:
        return row["count"], row["max_limit"]
    
    # Se não existir linha para a data, cria com valor padrão
    cursor.execute("""
        INSERT OR IGNORE INTO daily_quotas (date, count, max_limit)
        VALUES (?, 0, ?)
    """, (today_str, DEFAULT_MAX_DAILY))
    conn.commit()
    return 0, DEFAULT_MAX_DAILY


def check_can_apply() -> Tuple[bool, str, Dict[str, Any]]:
    """Verifica todas as guardrails antes de autorizar uma candidatura."""
    conn = get_db()
    cursor = conn.cursor()

    # 1. Checagem de Horário Comercial (08:00 às 20:00) em America/Sao_Paulo (UTC-3)
    now_brt = get_now_brt()
    current_time = now_brt.time()
    if not (OPERATING_HOUR_START <= current_time <= OPERATING_HOUR_END):
        conn.close()
        return False, f"Fora do horário comercial operacional (08:00 às 20:00 BRT). Horário atual: {current_time.strftime('%H:%M')}.", {
            "code": "OUTSIDE_HOURS",
            "current_time": current_time.strftime("%H:%M")
        }

    # 2. Checagem do Teto Diário (daily_quotas)
    today_count, max_limit = get_today_quota(conn)
    if today_count >= max_limit:
        conn.close()
        return False, f"Cota diária de {max_limit} candidaturas atingida ({today_count} aplicadas hoje).", {
            "code": "QUOTA_EXCEEDED",
            "today_count": today_count,
            "max_limit": max_limit
        }

    # 3. Checagem de Jitter e Intervalo entre Candidaturas (180s - 300s)
    cursor.execute("""
        SELECT applied_at
        FROM jobs
        WHERE status = 'applied'
        ORDER BY applied_at DESC
        LIMIT 1
    """)
    last_row = cursor.fetchone()

    if last_row and last_row["applied_at"]:
        try:
            last_dt = datetime.strptime(last_row["applied_at"], "%Y-%m-%d %H:%M:%S")
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            diff_seconds = (now_utc - last_dt).total_seconds()
            # Semente fixa por candidatura: o intervalo sorteado não muda a cada checagem da fila
            required_delay = random.Random(last_row["applied_at"]).randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            if diff_seconds < required_delay:
                wait_sec = int(required_delay - diff_seconds)
                conn.close()
                return False, f"Em intervalo de proteção anti-ban (jitter ativo). Aguarde {wait_sec}s.", {
                    "code": "COOLDOWN_ACTIVE",
                    "remaining_seconds": wait_sec,
                    "min_delay": MIN_DELAY_SECONDS
                }
        except ValueError:
            pass

    conn.close()
    return True, "Fila operacional e segura para submissão.", {
        "code": "OK",
        "today_count": today_count,
        "max_limit": max_limit,
        "remaining": max_limit - today_count
    }


def verify_resume(job_id: str) -> bool:
    """Verificação pré-submit: confere se o PDF existe e não está corrompido."""
    pdf_path = RESUMES_DIR / f"{job_id}.pdf"
    if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
        return False
    with open(pdf_path, "rb") as f:
        return f.read(4) == b"%PDF"


def get_next_approved_job() -> Optional[Dict[str, Any]]:
    """Busca a próxima vaga aprovada que passa pelo navegador (Easy Apply ou tipo ainda não confirmado)."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT job_id, title, company, location, workplace_type, job_url, apply_type, match_score, tailored_resume_path
        FROM jobs
        WHERE status = 'approved' AND apply_type != 'EXTERNAL_ATS'
        ORDER BY match_score DESC, updated_at ASC
        LIMIT 1
    """)
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_next_external_job() -> Optional[Dict[str, Any]]:
    """Busca vaga externa aprovada: não usa o navegador, só precisa do kit de candidatura no chat."""
    conn = get_db()
    row = conn.execute("""
        SELECT job_id, title, company FROM jobs
        WHERE status = 'approved' AND apply_type = 'EXTERNAL_ATS'
        ORDER BY updated_at ASC
        LIMIT 1
    """).fetchone()
    conn.close()
    return dict(row) if row else None


def claim_job(job_id: str) -> bool:
    """Reserva a vaga de forma atômica: só um processo sai de 'approved' para 'in_progress'."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = 'in_progress' WHERE job_id = ? AND status = 'approved'", (job_id,))
    claimed = cursor.rowcount == 1
    if claimed:
        cursor.execute("""
            INSERT INTO application_logs (job_id, step, status, message)
            VALUES (?, 'easy_apply_step', 'info', 'Iniciando submissão via Browser Relay')
        """, (job_id,))
    conn.commit()
    conn.close()
    return claimed


def release_job(job_id: str) -> None:
    """Devolve a vaga à fila sem registrar falha (usado no --dry-run)."""
    conn = get_db()
    conn.execute("UPDATE jobs SET status = 'approved' WHERE job_id = ? AND status = 'in_progress'", (job_id,))
    conn.commit()
    conn.close()


def recover_interrupted_jobs() -> int:
    """
    Vagas em 'in_progress' no boot foram interrompidas. Não voltam para a fila
    automaticamente porque o envio pode ter acontecido antes da interrupção.
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE jobs
        SET status = 'failed', error_log = 'Execução interrompida. Confira no LinkedIn se o envio aconteceu.'
        WHERE status = 'in_progress'
    """)
    recovered = cursor.rowcount
    conn.commit()
    conn.close()
    return recovered


def mark_in_progress(job_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = 'in_progress' WHERE job_id = ?", (job_id,))
    cursor.execute("""
        INSERT INTO application_logs (job_id, step, status, message)
        VALUES (?, 'easy_apply_step', 'info', 'Iniciando submissão via Browser Relay')
    """, (job_id,))
    conn.commit()
    conn.close()


def mark_applied(job_id: str, count_quota: bool = True) -> bool:
    """Conclui a aplicação: incrementa daily_quotas, altera status para applied e gera log."""
    conn = get_db()
    cursor = conn.cursor()
    today_str = get_now_brt().date().isoformat()

    # Incrementa contador diário
    if count_quota:
        cursor.execute("""
            INSERT INTO daily_quotas (date, count, max_limit)
            VALUES (?, 1, ?)
            ON CONFLICT(date) DO UPDATE SET count = count + 1, updated_at = CURRENT_TIMESTAMP
        """, (today_str, DEFAULT_MAX_DAILY))

    cursor.execute("""
        UPDATE jobs
        SET status = 'applied', applied_at = CURRENT_TIMESTAMP, error_log = NULL
        WHERE job_id = ?
    """, (job_id,))

    cursor.execute("""
        INSERT INTO application_logs (job_id, step, status, message)
        VALUES (?, 'applied', 'success', 'Candidatura Easy Apply submetida e confirmada com sucesso.')
    """, (job_id,))

    conn.commit()
    conn.close()
    return True


def mark_external_applied(job_id: str) -> bool:
    """Usuário confirmou no chat que enviou a vaga externa. Não consome a cota do LinkedIn."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE jobs SET status = 'applied', applied_at = CURRENT_TIMESTAMP, error_log = NULL
        WHERE job_id = ? AND status = 'notified_external'
    """, (job_id,))
    updated = cursor.rowcount == 1
    if updated:
        cursor.execute("""
            INSERT INTO application_logs (job_id, step, status, message)
            VALUES (?, 'applied', 'success', 'Candidatura externa confirmada pelo usuário no chat.')
        """, (job_id,))
    conn.commit()
    conn.close()
    return updated


TECH_YEARS = {
    "desenvolvimento de software": "5",
    "software development": "5",
    "engenharia de software": "5",
    "software engineering": "5",
    "python": "5",
    "fastapi": "3",
    "sql": "5",
    "postgresql": "5",
    "postgres": "5",
    "banco de dados": "5",
    "bigquery": "5",
    "agentes de ia": "2",
    "ai agents": "2",
    "llm": "2",
    "llms": "2",
    "langchain": "2",
    "rag": "2",
    "playwright": "3",
    "automação web": "3",
    "web scraping": "3",
    "n8n": "4",
    "rpa": "4",
    "power bi": "5",
    "looker studio": "5",
    "docker": "5",
    "linux": "5",
    "shell script": "5",
    "bash": "5",
    "git": "5",
    "github": "5",
    "pytest": "2",
    "testes automatizados": "2",
    "bdd": "2",
    "gherkin": "2",
    "lean six sigma": "2",
    "six sigma": "2",
    "green belt": "2",
    "google apps script": "3",
    "javascript": "3"
}

# Palavras que podem sobrar numa pergunta de anos. Qualquer outra indica uma tecnologia
# não mapeada ("Java e Python") e exige resposta humana.
YEARS_FILLER_WORDS = {
    "quantos", "anos", "ano", "de", "da", "do", "experiência", "experiencia", "você", "voce", "tem",
    "possui", "com", "em", "na", "no", "e", "profissional", "trabalho", "how", "many", "years", "year",
    "of", "work", "do", "you", "have", "with", "in", "and", "using", "experience", "professional", "the"
}

PORTUGUESE_HINTS = (
    "você", "voce", "quantos", "qual", "está", "possui", "trabalhar", "reside", "mora", "aceita",
    "pretensão", "aviso", "maior", "nível", "graduação", "ensino", "disposto", "cidadão", "precisa"
)

ON_SITE_TERMS = ("presencial", "on-site", "onsite", "in office", "in-office", "escritório", "relocat", "mudança", "híbrido", "hybrid")


def get_data_dir() -> Path:
    return Path(os.environ["OPENCLAW_DATA_DIR"]) if os.environ.get("OPENCLAW_DATA_DIR") else (WORKSPACE_ROOT / "data")


def normalize_question(question: str) -> str:
    return " ".join(question.lower().split()).strip(" ?.:!*")


def load_learned_answers(data_dir: Optional[Path] = None) -> Dict[str, str]:
    learned_file = (data_dir or get_data_dir()) / "learned_answers.json"
    if not learned_file.exists():
        return {}
    try:
        with open(learned_file, "r", encoding="utf-8") as f:
            return {normalize_question(q): str(a) for q, a in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def read_learned_answers_raw() -> Dict[str, str]:
    """Respostas aprendidas com o texto original das perguntas, na ordem em que foram salvas."""
    learned_file = get_data_dir() / "learned_answers.json"
    if not learned_file.exists():
        return {}
    try:
        with open(learned_file, "r", encoding="utf-8") as f:
            return {str(q): str(a) for q, a in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def _write_learned_answers(learned: Dict[str, str]) -> None:
    with open(get_data_dir() / "learned_answers.json", "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=2)


def _md_question(question: str) -> str:
    return " ".join(question.split()).replace("|", "/")


def _remove_md_rows(question: str) -> None:
    """Tira de EXPERIENCE_ANSWERS.md as linhas aprendidas dessa pergunta, para não ficar resposta antiga."""
    md_file = get_data_dir() / "EXPERIENCE_ANSWERS.md"
    if not md_file.exists():
        return
    marker = f"| **{_md_question(question)}** |"
    lines = md_file.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [l for l in lines if not (l.startswith(marker) and "Aprendido via Telegram" in l)]
    if len(kept) != len(lines):
        md_file.write_text("".join(kept), encoding="utf-8")


def _find_learned_key(learned: Dict[str, str], question: str) -> Optional[str]:
    target = normalize_question(question)
    return next((q for q in learned if normalize_question(q) == target), None)


def save_learned_answer(question: str, answer: str) -> None:
    """Grava a resposta do usuário em learned_answers.json e em EXPERIENCE_ANSWERS.md (substitui a anterior)."""
    learned = read_learned_answers_raw()
    existing = _find_learned_key(learned, question)
    if existing is not None:
        learned.pop(existing)
        _remove_md_rows(existing)
    learned[question] = answer
    _write_learned_answers(learned)

    table_answer = " ".join(answer.split()).replace("|", "/")
    with open(get_data_dir() / "EXPERIENCE_ANSWERS.md", "a", encoding="utf-8") as f:
        f.write(f"\n| **{_md_question(question)}** | **{table_answer}** | Aprendido via Telegram em {get_now_brt().strftime('%d/%m/%Y %H:%M')} |\n")


def delete_learned_answer(question: str) -> bool:
    """Apaga uma resposta aprendida. A pergunta volta a ser feita no Telegram na próxima vaga."""
    learned = read_learned_answers_raw()
    existing = _find_learned_key(learned, question)
    if existing is None:
        return False
    learned.pop(existing)
    _write_learned_answers(learned)
    _remove_md_rows(existing)
    return True


def resolve_years_question(q_norm: str, learned: Dict[str, str]) -> Tuple[bool, Optional[str]]:
    """Só responde se TODAS as tecnologias citadas forem conhecidas e tiverem o mesmo tempo."""
    known = dict(TECH_YEARS)
    known.update({k: v for k, v in learned.items() if " " not in k})

    remaining = q_norm
    values = set()
    for tech in sorted(known, key=len, reverse=True):
        pattern = r"(?<![\w+#.])" + re.escape(tech) + r"(?![\w+#])"
        if re.search(pattern, remaining):
            values.add(known[tech])
            remaining = re.sub(pattern, " ", remaining)

    leftovers = set(re.findall(r"[^\W\d_][\w+#.-]*", remaining)) - YEARS_FILLER_WORDS
    if len(values) == 1 and not leftovers:
        return True, values.pop()
    return False, None


def resolve_salary_question(q_norm: str) -> Tuple[bool, Optional[str]]:
    """Responde apenas quando regime ou moeda estão explícitos em EXPERIENCE_ANSWERS.md."""
    if re.search(r"\b(pj|b2b|contractor)\b", q_norm):
        return True, "18000"
    if re.search(r"\bclt\b", q_norm):
        return True, "14000"
    if re.search(r"\b(usd|dollars?|dólar|dólares)\b|us\$", q_norm):
        if re.search(r"\b(hour|hourly|hora)\b", q_norm):
            return True, "35"
        if re.search(r"\b(month|monthly|mês|mensal)\b", q_norm):
            return True, "5000"
        if re.search(r"\b(year|yearly|annual|ano|anual)\b", q_norm):
            return True, "65000"
    return False, None


def resolve_screening_question(question: str, answers_file: Optional[Path] = None) -> Tuple[bool, Optional[str]]:
    """
    Motor estrito de triagem determinística (Zero Alucinação).
    Consulta as respostas de EXPERIENCE_ANSWERS.md e as aprendidas no chat. Retorna
    (True, valor) se mapeada, ou (False, None) caso exija intervenção humana (needs_input).
    Na dúvida, não responde: uma pergunta a mais é melhor que uma resposta falsa.
    """
    q_norm = normalize_question(question)
    learned = load_learned_answers(answers_file.parent if answers_file else None)

    if q_norm in learned:
        return True, learned[q_norm]

    is_portuguese = any(hint in q_norm for hint in PORTUGUESE_HINTS)
    yes = "Sim" if is_portuguese else "Yes"
    no = "Não" if is_portuguese else "No"

    if any(term in q_norm for term in ["quantos anos", "anos de experiência", "how many years", "years of experience", "years of work experience"]):
        return resolve_years_question(q_norm, learned)

    if any(term in q_norm for term in ["salary expectations flexible", "pretensão salarial é negociável"]):
        return True, yes
    if any(term in q_norm for term in ["pretensão", "remuneração", "salary", "expected compensation"]):
        return resolve_salary_question(q_norm)

    if any(k in q_norm for k in ["notice period", "aviso prévio"]):
        return True, "15 dias" if is_portuguese else "2 weeks"

    if any(k in q_norm for k in ["nível de inglês", "proficiência em inglês", "english proficiency", "level of english", "english level"]):
        return True, "Intermediário" if is_portuguese else "Intermediate"
    if any(k in q_norm for k in ["nível de espanhol", "proficiência em espanhol", "spanish proficiency", "level of spanish", "spanish level"]):
        return True, "Básico" if is_portuguese else "Basic"

    if any(k in q_norm for k in ["authorized to work in brazil", "autorizado a trabalhar no brasil", "cidadão brasileiro"]):
        return True, yes
    if any(k in q_norm for k in ["require visa", "precisa de visto", "visa sponsorship"]):
        return True, no
    if any(k in q_norm for k in ["located in brazil", "reside no brasil", "mora no brasil"]):
        return True, yes
    if any(k in q_norm for k in ["willing to relocate", "disposto a se mudar", "disposta a se mudar"]):
        return True, no
    if any(k in q_norm for k in ["100% remote", "100% remoto", "totalmente remoto", "trabalho remoto"]):
        if not any(term in q_norm for term in ON_SITE_TERMS):
            return True, yes
    if any(k in q_norm for k in ["hybrid model in são paulo", "híbrido em são paulo"]):
        return True, yes
    if any(k in q_norm for k in ["independent contractor", "trabalhar como pj", "contratação pj", "work as a contractor"]):
        return True, yes
    if any(k in q_norm for k in ["completed a bachelor", "ensino superior completo", "possui ensino superior"]):
        return True, yes
    if any(k in q_norm for k in ["at least 18", "maior de 18"]):
        return True, yes
    if any(k in q_norm for k in ["background check", "verificação de antecedentes"]):
        return True, yes

    return False, None


def mark_needs_input(job_id: str, questions, options: Optional[List[str]] = None) -> bool:
    """Pausa a vaga em 'needs_input' por perguntas de triagem desconhecidas.
    `questions` é uma pergunta (str) ou a lista de {"question", "options", "suggestion"} de uma etapa inteira.
    As opções reais do campo (lista ou radio) ficam no log para virar botões no chat, assim como a
    sugestão {"from", "answer"} vinda de uma pergunta parecida já respondida."""
    if isinstance(questions, str):
        questions = [{"question": questions, "options": options or []}]
    questions = [{"question": q["question"], "options": list(q.get("options") or []),
                  **({"suggestion": q["suggestion"]} if q.get("suggestion") else {})} for q in questions]
    labels = " | ".join(q["question"] for q in questions)
    error_log = (f"Pergunta não mapeada: {labels}" if len(questions) == 1
                 else f"Perguntas não mapeadas ({len(questions)}): {labels}")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = 'needs_input', error_log = ? WHERE job_id = ?", (error_log, job_id))
    cursor.execute("""
        INSERT INTO application_logs (job_id, step, status, message, payload)
        VALUES (?, 'needs_input', 'warning', ?, ?)
    """, (
        job_id,
        "Processo pausado: pergunta de triagem não mapeada em EXPERIENCE_ANSWERS.md.",
        json.dumps({"questions": questions, "question": questions[0]["question"],
                    "options": questions[0]["options"]}, ensure_ascii=False)
    ))
    conn.commit()
    conn.close()
    return True


def get_pending_questions(job_id: str) -> List[Dict[str, Any]]:
    """Perguntas pendentes da última pausa da vaga, com as opções do campo quando houver."""
    conn = get_db()
    row = conn.execute("""
        SELECT payload FROM application_logs
        WHERE job_id = ? AND step = 'needs_input'
        ORDER BY id DESC LIMIT 1
    """, (job_id,)).fetchone()
    job = conn.execute("SELECT error_log FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    try:
        data = json.loads(row["payload"]) if row and row["payload"] else {}
    except ValueError:
        data = {}
    if data.get("questions"):
        return data["questions"]
    if data.get("question"):
        return [{"question": data["question"], "options": data.get("options", [])}]
    legacy = (job["error_log"] or "") if job else ""
    if legacy.startswith("Pergunta não mapeada:"):
        return [{"question": legacy.replace("Pergunta não mapeada:", "", 1).strip(), "options": []}]
    return []


def get_pending_question(job_id: str) -> Dict[str, Any]:
    """Primeira pergunta pendente da vaga (compatibilidade com pausas de uma pergunta só)."""
    questions = get_pending_questions(job_id)
    return questions[0] if questions else {"question": "", "options": []}


def unanswered_questions(job_id: str) -> List[Dict[str, Any]]:
    """Perguntas pendentes que ainda não têm resposta aprendida."""
    learned = load_learned_answers()
    return [q for q in get_pending_questions(job_id) if normalize_question(q["question"]) not in learned]


def mark_failed(job_id: str, error_msg: str) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = 'failed', error_log = ? WHERE job_id = ?", (error_msg, job_id))
    cursor.execute("""
        INSERT INTO application_logs (job_id, step, status, message)
        VALUES (?, 'failed', 'error', ?)
    """, (job_id, f"Falha na candidatura: {error_msg}"))
    conn.commit()
    conn.close()
    return True


def load_excluded_companies() -> set:
    """Empresas descartadas pelo usuário, usadas para filtrar as próximas recomendações."""
    prefs_file = get_data_dir() / PREFERENCES_FILE
    if not prefs_file.exists():
        return set()
    try:
        with open(prefs_file, "r", encoding="utf-8") as f:
            return {c.strip().lower() for c in json.load(f).get("excluded_companies", [])}
    except (OSError, ValueError):
        return set()


def record_outcome(job_id: str, outcome: str) -> bool:
    """Registra o que aconteceu depois do envio."""
    if outcome not in OUTCOMES:
        return False
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE jobs SET outcome = ?, outcome_at = CURRENT_TIMESTAMP
        WHERE job_id = ? AND status = 'applied'
    """, (outcome, job_id))
    updated = cursor.rowcount == 1
    if updated:
        cursor.execute("""
            INSERT INTO application_logs (job_id, step, status, message)
            VALUES (?, 'outcome', 'info', ?)
        """, (job_id, f"Resultado registrado: {outcome}"))
    conn.commit()
    conn.close()
    return updated


def get_outcome_stats() -> Dict[str, Any]:
    """Resume o funil pós-envio e o retorno por faixa de match_score."""
    conn = get_db()
    totals = conn.execute("""
        SELECT COUNT(*) AS applied,
               SUM(outcome IS NULL) AS pending,
               SUM(outcome IN ('response', 'interview')) AS positive,
               SUM(outcome = 'interview') AS interviews,
               SUM(outcome IN ('rejected', 'no_response')) AS negative
        FROM jobs WHERE status = 'applied'
    """).fetchone()
    bands = conn.execute("""
        SELECT (match_score / 10) * 10 AS band,
               SUM(outcome IS NOT NULL) AS decided,
               SUM(outcome IN ('response', 'interview')) AS positive
        FROM jobs WHERE status = 'applied'
        GROUP BY band ORDER BY band DESC
    """).fetchall()
    conn.close()
    return {
        "applied": totals["applied"] or 0,
        "pending": totals["pending"] or 0,
        "positive": totals["positive"] or 0,
        "interviews": totals["interviews"] or 0,
        "negative": totals["negative"] or 0,
        "bands": [dict(b) for b in bands if b["decided"]]
    }


def print_status():
    conn = get_db()
    cursor = conn.cursor()
    can_apply, reason, meta = check_can_apply()
    today_count, max_limit = get_today_quota(conn)

    cursor.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'approved' AND apply_type != 'EXTERNAL_ATS'")
    approved_count = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'needs_input'")
    needs_input_count = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'applied'")
    total_applied = cursor.fetchone()["c"]

    print("\n=======================================================")
    print("      OPENCLAW LINKEDIN QUEUE GUARD — STATUS          ")
    print("=======================================================")
    print(f" • Permissão de Disparo    : {'[LIBERADO]' if can_apply else '[BLOQUEADO]'}")
    print(f" • Motivo / Diagnóstico    : {reason}")
    print(f" • Cota Diária             : {today_count} / {max_limit} (Hoje)")
    print(f" • Vagas Aprovadas na Fila : {approved_count}")
    print(f" • Vagas em 'needs_input'  : {needs_input_count}")
    print(f" • Total Aplicado Geral    : {total_applied}")
    print("=======================================================\n")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Queue Guard - Controlador Anti-Ban do OpenClaw")
    parser.add_argument("--can-apply", action="store_true", help="Verifica permissão para aplicar agora")
    parser.add_argument("--get-next", action="store_true", help="Retorna próxima vaga aprovada elegível")
    parser.add_argument("--verify-resume", metavar="JOB_ID", help="Verifica se o PDF existe e é válido")
    parser.add_argument("--mark-in-progress", metavar="JOB_ID", help="Marca vaga como in_progress")
    parser.add_argument("--mark-applied", metavar="JOB_ID", help="Registra candidatura com sucesso")
    parser.add_argument("--mark-needs-input", nargs=2, metavar=("JOB_ID", "QUESTION"), help="Marca vaga como needs_input")
    parser.add_argument("--mark-failed", nargs=2, metavar=("JOB_ID", "REASON"), help="Marca vaga como failed")
    parser.add_argument("--status", action="store_true", help="Exibe resumo do estado operacional")

    args = parser.parse_args()

    if args.can_apply:
        allowed, reason, meta = check_can_apply()
        print(json.dumps({"allowed": allowed, "reason": reason, "meta": meta}, indent=2, ensure_ascii=False))
        sys.exit(0 if allowed else 1)

    if args.get_next:
        next_job = get_next_approved_job()
        print(json.dumps(next_job or {}, indent=2, ensure_ascii=False))
        return

    if args.verify_resume:
        valid = verify_resume(args.verify_resume)
        print(json.dumps({"job_id": args.verify_resume, "valid_pdf": valid}))
        sys.exit(0 if valid else 1)

    if args.mark_in_progress:
        mark_in_progress(args.mark_in_progress)
        print(f"[OK] Vaga {args.mark_in_progress} -> in_progress")

    if args.mark_applied:
        mark_applied(args.mark_applied)
        print(f"[OK] Vaga {args.mark_applied} -> applied (cota atualizada)")

    if args.mark_needs_input:
        job_id, q = args.mark_needs_input
        mark_needs_input(job_id, q)
        print(f"[OK] Vaga {job_id} -> needs_input ('{q}')")

    if args.mark_failed:
        job_id, r = args.mark_failed
        mark_failed(job_id, r)
        print(f"[OK] Vaga {job_id} -> failed ('{r}')")

    if args.status or not any(vars(args).values()):
        print_status()


if __name__ == "__main__":
    main()
