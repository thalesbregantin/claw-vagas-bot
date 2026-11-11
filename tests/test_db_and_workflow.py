"""
tests/test_db_and_workflow.py
Testes de banco de dados SQLite, máquina de estados e trava anti-alucinação:
1. Deduplicação de vagas (não sobrescrever 'applied' nem 'rejected_score')
2. Separação de fluxo entre EXTERNAL_ATS e EASY_APPLY
3. Trava needs_input estrita para perguntas eliminatórias (Zero Alucinação)
"""

import sqlite3
import json
from pathlib import Path
import pytest
import queue_guard


def test_job_deduplication_preserves_terminal_status(isolated_workspace):
    """
    Inserir a mesma vaga duas vezes não duplica registro nem
    sobrescreve status 'applied' ou 'rejected_score'.
    """
    conn = queue_guard.get_db()
    cursor = conn.cursor()
    
    # 1. Insere vaga e avança para applied
    cursor.execute("""
        INSERT INTO jobs (job_id, title, company, job_url, apply_type, status, applied_at)
        VALUES ('dup-101', 'AI Lead', 'Empresa X', 'https://job/101', 'EASY_APPLY', 'applied', CURRENT_TIMESTAMP)
    """)
    conn.commit()
    
    # 2. Simula novo batimento de descoberta tentando reinserir a mesma vaga
    # A política de descoberta deve checar existência prévia antes de inserir
    cursor.execute("SELECT id, status FROM jobs WHERE job_id = 'dup-101'")
    existing = cursor.fetchone()
    assert existing is not None
    assert existing["status"] == "applied"
    
    # Tenta um INSERT com ON CONFLICT que não deve alterar status de vagas finalizadas
    cursor.execute("""
        INSERT INTO jobs (job_id, title, company, job_url, apply_type, status)
        VALUES ('dup-101', 'AI Lead Novo Título', 'Empresa X', 'https://job/101', 'EASY_APPLY', 'discovered')
        ON CONFLICT(job_id) DO UPDATE SET
            title = excluded.title
        WHERE status NOT IN ('applied', 'rejected_score')
    """)
    conn.commit()
    
    # Verifica que o status continua 'applied' e não foi resetado para 'discovered'
    cursor.execute("SELECT COUNT(*) as c, status FROM jobs WHERE job_id = 'dup-101' GROUP BY status")
    row = cursor.fetchone()
    assert row["c"] == 1, "Não deve haver duplicação de linhas"
    assert row["status"] == "applied", "Status final 'applied' deve ser preservado"
    conn.close()


def test_flow_separation_external_ats_vs_easy_apply(isolated_workspace):
    """
    Separação estrita de fluxos:
    - EXTERNAL_ATS: notificado via chat com link direto e PDF, termina em 'notified_external'
                    e NUNCA entra na fila do Chrome Relay.
    - EASY_APPLY: transição 'discovered' -> 'pending_approval' -> 'approved' -> 'applied'.
    """
    conn = queue_guard.get_db()
    cursor = conn.cursor()
    
    # 1. Vaga EXTERNAL_ATS
    cursor.execute("""
        INSERT INTO jobs (job_id, title, company, job_url, apply_url, apply_type, status)
        VALUES ('job-ext-1', 'Python Arch', 'Gupy Corp', 'https://job/ext1', 'https://gupy.io/apply/1', 'EXTERNAL_ATS', 'notified_external')
    """)
    
    # 2. Vaga EASY_APPLY aprovada
    cursor.execute("""
        INSERT INTO jobs (job_id, title, company, job_url, apply_type, match_score, status)
        VALUES ('job-easy-1', 'AI Engineer', 'Linkedin Corp', 'https://job/easy1', 'EASY_APPLY', 95, 'approved')
    """)
    conn.commit()
    conn.close()
    
    # Valida que get_next_approved_job() retorna APENAS EASY_APPLY
    next_job = queue_guard.get_next_approved_job()
    assert next_job is not None
    assert next_job["job_id"] == "job-easy-1"
    assert next_job["apply_type"] == "EASY_APPLY"
    
    # Mesmo se uma EXTERNAL_ATS estiver acidentalmente com status 'approved',
    # a consulta do queue_guard deve ignorá-la categoricamente.
    conn = queue_guard.get_db()
    conn.execute("UPDATE jobs SET status = 'approved' WHERE job_id = 'job-ext-1'")
    conn.commit()
    conn.close()
    
    next_after = queue_guard.get_next_approved_job()
    assert next_after["job_id"] == "job-easy-1", "Vaga EXTERNAL_ATS não pode entrar no Chrome Relay"


def test_screening_zero_hallucination_needs_input(isolated_workspace):
    """
    Trava contra Alucinação:
    - Se a pergunta constar em EXPERIENCE_ANSWERS.md: retorna valor mapeado com sucesso.
    - Se a pergunta NÃO constar em EXPERIENCE_ANSWERS.md: DEVE pausar o fluxo,
      chamar mark_needs_input, alterar status para 'needs_input' e NÃO permitir a aplicação.
    """
    # Importa o módulo de triagem estrita
    try:
        import screening_guard
    except ImportError:
        # Se ainda não existir módulo separado, valida via queue_guard
        import queue_guard as screening_guard
    
    job_id = "job-screening-test"
    conn = queue_guard.get_db()
    conn.execute("""
        INSERT INTO jobs (job_id, title, company, job_url, apply_type, status)
        VALUES (?, 'AI Automation Eng', 'Enterprise AI', 'https://job/scr', 'EASY_APPLY', 'in_progress')
    """, (job_id,))
    conn.commit()
    conn.close()
    
    answers_md_path = isolated_workspace["data_dir"] / "EXPERIENCE_ANSWERS.md"
    assert answers_md_path.exists()
    
    # 1. Pergunta MAPEADA: "Quantos anos de experiência com Python?"
    has_answer, ans_val = screening_guard.resolve_screening_question(
        question="Quantos anos de experiência você tem com Python?",
        answers_file=answers_md_path
    )
    assert has_answer is True
    assert ans_val == "5"
    
    # 2. Pergunta NÃO MAPEADA: "Quantos anos de experiência com Rust e Solana?"
    unknown_question = "Quantos anos de experiência você tem com desenvolvimento de contratos em Rust e Solana?"
    has_unknown, val_unknown = screening_guard.resolve_screening_question(
        question=unknown_question,
        answers_file=answers_md_path
    )
    assert has_unknown is False
    assert val_unknown is None
    
    # Ao falhar em encontrar resposta, dispara mark_needs_input
    queue_guard.mark_needs_input(job_id, unknown_question)
    
    # Verifica que o status no banco virou 'needs_input' e registrou log
    conn = queue_guard.get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT status, error_log FROM jobs WHERE job_id = ?", (job_id,))
    row = cursor.fetchone()
    assert row["status"] == "needs_input"
    assert "Rust e Solana" in row["error_log"]
    
    cursor.execute("SELECT step, status, message FROM application_logs WHERE job_id = ? AND step = 'needs_input'", (job_id,))
    log_row = cursor.fetchone()
    assert log_row is not None
    assert log_row["status"] == "warning"
    conn.close()
