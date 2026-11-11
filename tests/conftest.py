"""
tests/conftest.py
Fixtures compartilhadas para testes isolados sem afetar db/jobs.db de produção.
Usa os dados de exemplo versionados, sem dados pessoais, e bloqueia chamadas ao Telegram.
"""

import os
import sys
import sqlite3
import shutil
from pathlib import Path
import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent / "workspace"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Adiciona workspace/scripts ao sys.path para import direto
SCRIPTS_DIR = WORKSPACE_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "111")


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    """
    Cria uma cópia isolada de db, data, templates e output em tmp_path
    e redireciona as constantes dos scripts para ela.
    """
    test_ws = tmp_path / "workspace"
    test_ws.mkdir()

    # Copia schema, templates e data de exemplo
    shutil.copytree(WORKSPACE_ROOT / "templates", test_ws / "templates")
    data_dir = test_ws / "data"
    data_dir.mkdir()
    source_data = WORKSPACE_ROOT / "data"
    shutil.copyfile(source_data / "profile.example.json", data_dir / "profile.json")
    shutil.copyfile(source_data / "EXPERIENCE_ANSWERS.example.md", data_dir / "EXPERIENCE_ANSWERS.md")
    shutil.copyfile(source_data / "learned_answers.example.json", data_dir / "learned_answers.json")

    # Cria pastas db e output
    db_dir = test_ws / "db"
    db_dir.mkdir()
    resumes_dir = test_ws / "output" / "resumes"
    resumes_dir.mkdir(parents=True)
    logs_dir = test_ws / "output" / "logs"
    logs_dir.mkdir(parents=True)

    # Inicializa banco de teste aplicando schema.sql
    test_db = db_dir / "jobs.db"
    schema_sql = (WORKSPACE_ROOT / "db" / "schema.sql").read_text(encoding="utf-8")

    conn = sqlite3.connect(test_db)
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()

    # Monkeypatch nos módulos de scripts
    import queue_guard
    import tailor_resume
    import init_db
    import apply_job
    import telegram_bot
    import common

    monkeypatch.setenv("OPENCLAW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("EASY_APPLY_ONLY", "1")
    monkeypatch.setenv("VISION_FALLBACK", "0")  # nenhum teste chama a API de visão de verdade
    monkeypatch.setenv("SIMILAR_QUESTIONS_AI", "0")  # sugestões nos testes usam só a comparação local

    monkeypatch.setattr(queue_guard, "WORKSPACE_ROOT", test_ws)
    monkeypatch.setattr(queue_guard, "DB_PATH", test_db)
    monkeypatch.setattr(queue_guard, "RESUMES_DIR", resumes_dir)

    monkeypatch.setattr(tailor_resume, "WORKSPACE_ROOT", test_ws)
    monkeypatch.setattr(tailor_resume, "DATA_DIR", data_dir)
    monkeypatch.setattr(tailor_resume, "PROFILE_PATH", data_dir / "profile.json")
    monkeypatch.setattr(tailor_resume, "TEMPLATES_DIR", test_ws / "templates")
    monkeypatch.setattr(tailor_resume, "OUTPUT_DIR", test_ws / "output")
    monkeypatch.setattr(tailor_resume, "RESUMES_DIR", resumes_dir)
    monkeypatch.setattr(tailor_resume, "DB_PATH", test_db)

    monkeypatch.setattr(init_db, "WORKSPACE_ROOT", test_ws)
    monkeypatch.setattr(init_db, "DB_PATH", test_db)
    monkeypatch.setattr(init_db, "DB_DIR", db_dir)
    monkeypatch.setattr(init_db, "SCHEMA_PATH", WORKSPACE_ROOT / "db" / "schema.sql")
    monkeypatch.setattr(init_db, "OUTPUT_DIR", test_ws / "output")
    monkeypatch.setattr(init_db, "RESUMES_DIR", resumes_dir)

    monkeypatch.setattr(apply_job, "DB_PATH", test_db)
    monkeypatch.setattr(apply_job, "RESUMES_DIR", resumes_dir)
    monkeypatch.setattr(apply_job, "OPENCLAW_UPLOADS_DIR", tmp_path / "uploads")

    monkeypatch.setattr(telegram_bot, "DB_PATH", test_db)
    monkeypatch.setattr(telegram_bot, "WORKSPACE_ROOT", test_ws)

    # Nenhum teste fala com o Telegram: as chamadas ficam registradas em "telegram"
    telegram_calls = []

    def fake_telegram_request(method, params, timeout=25):
        telegram_calls.append((method, params))
        return {"ok": True, "result": []}

    monkeypatch.setattr(common, "telegram_request", fake_telegram_request)
    monkeypatch.setattr(telegram_bot, "telegram_api_call", fake_telegram_request)
    monkeypatch.setattr(common, "send_document", lambda *args, **kwargs: True)

    return {
        "workspace": test_ws,
        "db_path": test_db,
        "resumes_dir": resumes_dir,
        "data_dir": data_dir,
        "templates_dir": test_ws / "templates",
        "telegram": telegram_calls
    }


@pytest.fixture
def insert_job(isolated_workspace):
    """Insere uma vaga no banco de teste."""
    def _insert(job_id="li-100", status="approved", title="Senior AI Engineer", company="Acme",
                job_url="https://www.linkedin.com/jobs/view/100", apply_type="UNKNOWN", **extra):
        columns = {
            "job_id": job_id, "title": title, "company": company, "job_url": job_url,
            "apply_type": apply_type, "status": status,
            "description": "Python, FastAPI, RAG, LangChain e SQL", **extra
        }
        conn = sqlite3.connect(isolated_workspace["db_path"])
        conn.execute(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            tuple(columns.values())
        )
        conn.commit()
        conn.close()
        return job_id
    return _insert


@pytest.fixture
def job_field(isolated_workspace):
    """Lê um campo de uma vaga no banco de teste."""
    def _read(job_id, field="status"):
        conn = sqlite3.connect(isolated_workspace["db_path"])
        row = conn.execute(f"SELECT {field} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.close()
        return row[0] if row else None
    return _read
