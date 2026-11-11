"""
tests/test_tailor_resume.py
Testes para o motor de tailoring e geração de PDF ATS:
1. Integridade do PDF (%PDF e tamanho > 1KB)
2. Sem vazamento de Header/Footer de robô (sem file:/// e sem C:\\)
3. Higiene do canal stdout para MCP (saída estritamente JSON)
4. Suporte a UTF-8 e emojis no Windows
"""

import os
import sys
import json
import sqlite3
import subprocess
from pathlib import Path
import pytest
import tailor_resume


def test_pdf_integrity_and_ats_generation(isolated_workspace):
    """
    Verifica geração real do PDF:
    - Inicia com cabeçalho de assinatura mágica %PDF
    - Tamanho superior a 1KB
    """
    profile = tailor_resume.load_profile()
    job_id = "test-job-ats-1"
    
    template_data = tailor_resume.tailor_resume_content(
        profile=profile,
        job_title="Senior AI Engineer",
        job_company="Cognitive Labs",
        job_id=job_id,
        job_description="Python, FastAPI, RAG, LangChain, Agentes de IA"
    )
    
    html = tailor_resume.render_html(template_data)
    assert "<title>" in html
    assert "Thales" in html
    
    out_pdf = isolated_workspace["resumes_dir"] / f"{job_id}.pdf"
    rendered = tailor_resume.render_pdf(html, out_pdf)
    
    assert rendered is True
    assert out_pdf.exists()
    assert out_pdf.stat().st_size > 1024, "PDF deve ser maior que 1KB"
    
    pdf_bytes = out_pdf.read_bytes()
    assert pdf_bytes.startswith(b"%PDF"), "Arquivo gerado deve ter assinatura de PDF válida (%PDF)"


def test_no_robot_header_footer_leakage(isolated_workspace):
    """
    Garante que o PDF gerado não contenha cabeçalho/rodapé com
    caminho de arquivo local (file:///) ou disco local (C:\\).
    """
    profile = tailor_resume.load_profile()
    job_id = "test-job-clean-header"
    
    template_data = tailor_resume.tailor_resume_content(
        profile=profile,
        job_title="Engenheiro de Automação",
        job_company="Tech Solutions",
        job_id=job_id,
        job_description="Playwright, Python, n8n, SQL"
    )
    
    html = tailor_resume.render_html(template_data)
    out_pdf = isolated_workspace["resumes_dir"] / f"{job_id}.pdf"
    tailor_resume.render_pdf(html, out_pdf)
    
    pdf_bytes = out_pdf.read_bytes()
    # Verifica tanto em bytes diretos quanto string decodificada tolerante
    pdf_text = pdf_bytes.decode("latin1", errors="ignore")
    
    assert "file:///" not in pdf_text, "Vazamento detectado: URL file:/// encontrada no PDF"
    assert "C:\\Users\\" not in pdf_text, "Vazamento detectado: caminho absoluto local encontrado no PDF"


def test_mcp_channel_stdout_hygiene(isolated_workspace):
    """
    Executa tailor_resume.py como subprocesso CLI e valida
    que o stdout seja ESTRITAMENTE um JSON válido, sem warnings ou logs soltos.
    """
    script_path = Path(__file__).resolve().parent.parent / "workspace" / "scripts" / "tailor_resume.py"
    test_db = isolated_workspace["db_path"]
    
    # 1. Garante que exista uma vaga no banco de teste para a CLI
    conn = sqlite3.connect(test_db)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO jobs (job_id, title, company, job_url, apply_type, description, status)
        VALUES ('test-cli-hygiene', 'Engenheiro de IA', 'Empresa Teste', 'https://job', 'EASY_APPLY', 'Python FastAPI', 'discovered')
    """)
    conn.commit()
    conn.close()
    
    env = os.environ.copy()
    env["OPENCLAW_DB_PATH"] = str(test_db)
    env["OPENCLAW_OUTPUT_DIR"] = str(isolated_workspace["workspace"] / "output")
    
    res = subprocess.run(
        [sys.executable, str(script_path), "--job-id", "test-cli-hygiene"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env
    )
    
    stdout = res.stdout.strip()
    assert res.returncode == 0, f"Script falhou com erro: {res.stderr}"
    
    # Valida higiene estrita: o primeiro caractere deve ser { e o último }
    assert stdout.startswith("{"), f"stdout poluído antes do JSON:\n{stdout[:150]}"
    assert stdout.endswith("}"), f"stdout poluído após o JSON:\n{stdout[-150:]}"
    
    # json.loads direto sem nenhum strip/corte
    parsed = json.loads(stdout)
    assert parsed["job_id"] == "test-cli-hygiene"
    assert "match_score" in parsed
    assert "tailored_resume_path" in parsed


def test_utf8_windows_heavy_accents_and_emojis(isolated_workspace):
    """
    Garante que textos com acentuação pesada, caracteres especiais e emojis
    sejam processados no scoring e no HTML/PDF sem gerar UnicodeEncodeError.
    """
    profile = tailor_resume.load_profile()
    job_id = "test-job-utf8-emojis"
    
    complex_title = "Engenheiro de IA Sênior (LLMs & Automação) 🚀🎯"
    complex_description = (
        "Vaga em São Paulo, SP (Remoto). Exige proficiência em IA Generativa, "
        "orquestração de fluxos com n8n, microsserviços em FastAPI e visão computacional com OpenCV. "
        "Atenção à legislação LGPD e conformidade tributária!"
    )
    
    # 1. Teste de scoring
    score, reasons, gaps = tailor_resume.calculate_match_score(
        profile=profile,
        job_title=complex_title,
        job_description=complex_description,
        workplace_type="Remote"
    )
    assert score >= 75
    assert any("São Paulo" in r or "Sênior" in r or "IA" in r for r in reasons)
    
    # 2. Teste de montagem de dados e renderização HTML
    template_data = tailor_resume.tailor_resume_content(
        profile=profile,
        job_title=complex_title,
        job_company="Cognitive Enterprise Brasil Ltda.",
        job_id=job_id,
        job_description=complex_description
    )
    html = tailor_resume.render_html(template_data)
    assert "São Paulo" in html
    
    # 3. Teste de compilação em PDF
    out_pdf = isolated_workspace["resumes_dir"] / f"{job_id}.pdf"
    rendered = tailor_resume.render_pdf(html, out_pdf)
    assert rendered is True
    assert out_pdf.exists()
