"""
tests/test_quality_rules.py
Regras de honestidade e qualidade dos dados:
1. Triagem só responde o que está mapeado
2. Currículo cita apenas o que está no perfil e não expõe a automação
3. Pontuação penaliza vagas fora do foco
4. IDs, deduplicação e migração do banco
5. Nenhum segredo no código
"""

import re
import sqlite3
import ssl
from pathlib import Path
import pytest
import common
import migrate_db
import queue_guard
import tailor_resume

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "workspace" / "scripts"


@pytest.mark.parametrize("question,expected", [
    ("Quantos anos de experiência você tem com Python?", "5"),
    ("How many years of work experience do you have with Docker?", "5"),
    ("Quantos anos de experiência com FastAPI?", "3"),
    ("Pretensão salarial PJ", "18000"),
    ("Pretensão salarial CLT", "14000"),
    ("What is your hourly salary expectation in USD?", "35"),
    ("Are you legally authorized to work in Brazil?", "Yes"),
    ("Você reside no Brasil?", "Sim"),
    ("Do you require visa sponsorship?", "No"),
    ("Are you comfortable working 100% remote?", "Yes"),
    ("Are you willing to relocate?", "No"),
    ("What is your notice period?", "2 weeks"),
    ("Qual seu nível de inglês?", "Intermediário"),
    ("What is your level of Spanish?", "Basic"),
    ("What is your English level?", "Intermediate"),
])
def test_mapped_questions(isolated_workspace, question, expected):
    assert queue_guard.resolve_screening_question(question) == (True, expected)


@pytest.mark.parametrize("question", [
    "Você tem experiência com Java e Python?",
    "Quantos anos de experiência com Java e Python?",
    "Quantos anos de experiência com Kubernetes?",
    "Quantos anos de experiência com Python e FastAPI?",
    "Qual sua pretensão salarial?",
    "Pretensão salarial em Python",
    "Você se sente confortável com trabalho presencial e não remoto?",
    "Você possui inglês avançado?",
    "Você tem graduação em Ciência da Computação?",
    "Is this role in Git-based CI, are you willing to work weekends?",
])
def test_ambiguous_questions_require_a_human(isolated_workspace, question):
    assert queue_guard.resolve_screening_question(question) == (False, None)


def test_learned_answer_with_question_mark_is_found(isolated_workspace):
    queue_guard.save_learned_answer("Quantos anos com LangGraph?", "2")
    assert queue_guard.resolve_screening_question("quantos anos com langgraph?") == (True, "2")


def test_learned_single_word_tech_feeds_years_question(isolated_workspace):
    queue_guard.save_learned_answer("langgraph", "2")
    assert queue_guard.resolve_screening_question("Quantos anos de experiência com LangGraph?") == (True, "2")


def test_learned_answer_keeps_markdown_table_valid(isolated_workspace):
    queue_guard.save_learned_answer("Qual A | B?\nresponda", "x|y")
    lines = (isolated_workspace["data_dir"] / "EXPERIENCE_ANSWERS.md").read_text(encoding="utf-8").splitlines()
    assert lines[-1].count("|") == 4


def test_summary_only_cites_profile_skills(isolated_workspace):
    profile = tailor_resume.load_profile()
    data = tailor_resume.tailor_resume_content(
        profile, "Backend Engineer", "Acme", "li-1",
        "Requisitos: Python, FastAPI, Django, Flask, Kubernetes, AWS, Redis e Celery."
    )
    summary = data["tailored_summary"]
    assert "PYTHON" in summary
    for invented in ["DJANGO", "FLASK", "KUBERNETES", "AWS", "REDIS", "CELERY"]:
        assert invented not in summary


def test_summary_without_matches_is_the_base_summary(isolated_workspace):
    profile = tailor_resume.load_profile()
    data = tailor_resume.tailor_resume_content(profile, "Cargo", "Acme", "li-1", "Requisitos: Kubernetes.")
    assert data["tailored_summary"] == profile["summary"]


def test_resume_html_has_no_automation_marks(isolated_workspace):
    profile = tailor_resume.load_profile()
    html = tailor_resume.render_html(tailor_resume.tailor_resume_content(
        profile, "Cargo Secreto da Vaga", "Empresa Secreta", "li-999", "Python"
    ))
    for leaked in ["OpenClaw", "li-999", "Otimizado ATS", "Empresa Secreta", "Cargo Secreto", "Especialista em IA, Automação"]:
        assert leaked not in html


def test_same_title_at_two_companies_gets_two_pdfs(isolated_workspace, insert_job, monkeypatch):
    monkeypatch.setattr(tailor_resume, "render_pdf", lambda html, path: path.write_text(html, encoding="utf-8") or True)
    description = "Python, FastAPI, RAG, LangChain, SQL e Docker. 100% remoto."
    first = insert_job(job_id="li-1", status="discovered", company="Alfa", title="Senior AI Engineer", job_url="https://www.linkedin.com/jobs/view/1", description=description)
    second = insert_job(job_id="li-2", status="discovered", company="Beta", title="Senior AI Engineer", job_url="https://www.linkedin.com/jobs/view/2", description=description)

    paths = {tailor_resume.process_job_tailoring(j)["tailored_resume_path"] for j in (first, second)}

    assert paths == {"output/resumes/li-1.pdf", "output/resumes/li-2.pdf"}


def test_failed_render_records_no_pdf(isolated_workspace, insert_job, monkeypatch):
    monkeypatch.setattr(tailor_resume, "render_pdf", lambda html, path: False)
    job_id = insert_job(status="discovered", description="Python, FastAPI, RAG, LangChain, SQL e Docker. 100% remoto.")
    assert tailor_resume.process_job_tailoring(job_id)["tailored_resume_path"] is None


def test_force_pdf_keeps_approved_status_below_cutoff(isolated_workspace, insert_job, job_field, monkeypatch):
    """Aprovada com nota baixa não pode voltar para rejected_score no meio da candidatura."""
    monkeypatch.setattr(tailor_resume, "render_pdf", lambda html, path: True)
    job_id = insert_job(status="in_progress", title="Assistente", description="")

    result = tailor_resume.process_job_tailoring(job_id, force_pdf=True)

    assert result["match_score"] < 75
    assert result["tailored_resume_path"]
    assert job_field(job_id) == "in_progress"


def test_verify_resume_rejects_non_pdf(isolated_workspace):
    (isolated_workspace["resumes_dir"] / "li-1.pdf").write_bytes(b"<html>" + b"0" * 2000)
    (isolated_workspace["resumes_dir"] / "li-2.pdf").write_bytes(b"%PDF-1.7" + b"0" * 2000)
    assert queue_guard.verify_resume("li-1") is False
    assert queue_guard.verify_resume("li-2") is True


GOOD_DESCRIPTION = "Python, FastAPI, LangChain, RAG, SQL, Docker, agentes de IA. Trabalho remoto."


@pytest.mark.parametrize("title,description", [
    ("Sales Engineer", GOOD_DESCRIPTION),
    ("Marketing Automation Engineer", GOOD_DESCRIPTION),
    ("Post-Sales Solution Architect", GOOD_DESCRIPTION),
    ("Senior Java Engineer", GOOD_DESCRIPTION),
    ("Retail Trainer", "Treinamento em lojas."),
    ("Estágio em Engenharia", GOOD_DESCRIPTION),
    ("Logic Design Engineer", GOOD_DESCRIPTION),
])
def test_off_target_jobs_stay_below_cutoff(isolated_workspace, title, description):
    profile = tailor_resume.load_profile()
    score, _, gaps = tailor_resume.calculate_match_score(profile, title, description, "Remote")
    assert score < 75, title
    assert gaps


@pytest.mark.parametrize("title", ["Senior AI Engineer", "Engenheiro de IA Sênior", "Senior Python Developer"])
def test_on_target_jobs_pass_cutoff(isolated_workspace, title):
    profile = tailor_resume.load_profile()
    score, _, _ = tailor_resume.calculate_match_score(profile, title, GOOD_DESCRIPTION, "Remote")
    assert score >= 75, title


def test_substrings_are_not_keywords():
    assert tailor_resume.extract_keywords_from_job("Retail email trainer", "html mainframe") == []


def test_description_with_foreign_stack_counts_as_gap(isolated_workspace):
    profile = tailor_resume.load_profile()
    _, _, gaps = tailor_resume.calculate_match_score(profile, "Senior Engineer", "Python, Java, Spring e Kubernetes", "Remote")
    assert any("JAVA" in g for g in gaps)


def test_hybrid_uses_job_location(isolated_workspace):
    profile = tailor_resume.load_profile()
    sp = tailor_resume.calculate_match_score(profile, "AI Engineer", "Python", "Hybrid", "São Paulo, SP")[0]
    other = tailor_resume.calculate_match_score(profile, "AI Engineer", "Python", "Hybrid", "Recife, PE")[0]
    assert sp > other


@pytest.mark.parametrize("raw,expected", [
    ("linkedin-li-123", "li-123"),
    ("li-123", "li-123"),
    ("urn:li:jobPosting:123", "li-123"),
    ("123", "li-123"),
    ("indeed-in-abc", "in-abc"),
    ("in-abc", "in-abc"),
])
def test_canonical_job_id(raw, expected):
    assert common.canonical_job_id(raw) == expected


def row(**overrides):
    base = {"id": "li-1", "site": "linkedin", "title": "AI Engineer", "company": "Acme", "location": "São Paulo",
            "job_url": "https://www.linkedin.com/jobs/view/1", "description": "Python " * 20, "is_remote": False}
    return {**base, **overrides}


def test_linkedin_type_is_unknown_and_indeed_is_external():
    assert common.parse_jobspy_row(row())["apply_type"] == "UNKNOWN"
    assert common.parse_jobspy_row(row(site="indeed", id="in-1"))["apply_type"] == "EXTERNAL_ATS"


def test_nan_is_treated_as_empty():
    assert common.parse_jobspy_row(row(company=float("nan"))) is None
    assert common.parse_jobspy_row(row(location=float("nan")), default_location="Remoto")["location"] == "Remoto"


def test_save_job_deduplicates_by_id_url_and_content(isolated_workspace):
    conn = sqlite3.connect(isolated_workspace["db_path"])
    cursor = conn.cursor()

    first = common.parse_jobspy_row(row())
    assert common.save_job(cursor, first) == ("li-1", True)
    assert common.save_job(cursor, first) == ("li-1", False)
    same_url = common.parse_jobspy_row(row(id="li-2", title="Outro"))
    assert common.save_job(cursor, same_url) == ("li-1", False)
    same_content = common.parse_jobspy_row(row(id="li-3", job_url="https://www.linkedin.com/jobs/view/3"))
    assert common.save_job(cursor, same_content) == ("li-1", False)

    assert cursor.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    conn.close()


def test_migration_merges_ids_and_is_idempotent(isolated_workspace, insert_job, job_field):
    insert_job(job_id="linkedin-li-1", status="applied", job_url="https://www.linkedin.com/jobs/view/1", company="A")
    insert_job(job_id="li-1", status="scored", job_url="https://www.linkedin.com/jobs/view/1/", company="A")
    insert_job(job_id="linkedin-li-2", status="scored", job_url="https://www.linkedin.com/jobs/view/2", company="B", apply_type="EXTERNAL_ATS", location="SP", match_score=50)
    insert_job(job_id="li-3", status="scored", job_url="https://www.linkedin.com/jobs/view/3", company="B", location="SP", match_score=10)
    insert_job(job_id="indeed-in-4", status="scored", job_url="https://br.indeed.com/viewjob?jk=4", company="C", apply_type="EXTERNAL_ATS")

    conn = sqlite3.connect(isolated_workspace["db_path"])
    conn.execute("INSERT INTO application_logs (job_id, step, status, message) VALUES ('linkedin-li-1', 'applied', 'success', 'ok')")
    conn.commit()
    conn.close()

    report = migrate_db.migrate(isolated_workspace["db_path"], backup=False)

    conn = sqlite3.connect(isolated_workspace["db_path"])
    assert conn.execute("SELECT job_id FROM application_logs").fetchone()[0] == "li-1"
    conn.close()
    assert report["merged"] == 1
    assert job_field("li-1") == "applied"
    assert job_field("linkedin-li-1") is None
    assert job_field("li-2", "apply_type") == "UNKNOWN"
    assert job_field("in-4", "apply_type") == "EXTERNAL_ATS"
    assert report["duplicates_skipped"] == 1
    assert job_field("li-3") == "skipped"

    again = migrate_db.migrate(isolated_workspace["db_path"], backup=False)
    assert again["renamed"] == again["merged"] == again["duplicates_skipped"] == 0


def test_migration_adds_columns_to_old_database(isolated_workspace):
    conn = sqlite3.connect(isolated_workspace["db_path"])
    conn.execute("ALTER TABLE jobs DROP COLUMN outcome")
    conn.execute("ALTER TABLE jobs DROP COLUMN outcome_at")
    conn.commit()
    conn.close()

    migrate_db.migrate(isolated_workspace["db_path"], backup=False)

    conn = sqlite3.connect(isolated_workspace["db_path"])
    assert "outcome" in {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()


def test_schema_has_market_skills(isolated_workspace):
    conn = sqlite3.connect(isolated_workspace["db_path"])
    assert conn.execute("SELECT count(*) FROM market_skills").fetchone()[0] == 0
    conn.close()


def test_jitter_is_stable_between_checks(isolated_workspace, insert_job, monkeypatch):
    from datetime import datetime, timedelta, timezone

    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    insert_job(status="applied", applied_at=(now_utc - timedelta(seconds=200)).strftime("%Y-%m-%d %H:%M:%S"))
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: datetime(2026, 10, 29, 14, 0, tzinfo=queue_guard.BRT_TZ))

    results = {queue_guard.check_can_apply()[2]["code"] for _ in range(30)}

    assert len(results) == 1


def test_tls_verification_is_on():
    assert common.SSL_CTX.verify_mode == ssl.CERT_REQUIRED
    assert common.SSL_CTX.check_hostname is True


def test_no_secrets_or_shell_in_scripts():
    for script in SCRIPTS_DIR.glob("*.py"):
        source = script.read_text(encoding="utf-8")
        assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{30,}", source), f"Token em {script.name}"
        assert "CERT_NONE" not in source, f"TLS desligado em {script.name}"
        assert "shell=True" not in source, f"shell=True em {script.name}"


def test_load_env_does_not_override_process_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comentário\nFOO_TEST=do_arquivo\nBAR_TEST=\"com aspas\"\n", encoding="utf-8")
    monkeypatch.setenv("FOO_TEST", "do_processo")
    monkeypatch.delenv("BAR_TEST", raising=False)

    common.load_env(env_file)

    assert common.require_env("FOO_TEST") == "do_processo"
    assert common.require_env("BAR_TEST") == "com aspas"
    monkeypatch.delenv("BAR_TEST")


def test_require_env_explains_missing_value(monkeypatch):
    monkeypatch.delenv("MISSING_TEST", raising=False)
    with pytest.raises(RuntimeError, match=".env"):
        common.require_env("MISSING_TEST")
