"""
tests/test_dashboard.py
Garante que o dashboard escapa dados raspados e mostra o retorno pós-envio.
"""

import generate_dashboard


def _render(isolated_workspace, monkeypatch, tmp_path):
    monkeypatch.setattr(generate_dashboard, "DB_PATH", isolated_workspace["db_path"])
    monkeypatch.setattr(generate_dashboard, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(generate_dashboard, "DASHBOARD_HTML_PATH", tmp_path / "out" / "dashboard.html")
    path = generate_dashboard.generate_dashboard()
    return path.read_text(encoding="utf-8")


def test_dashboard_escapa_dados_da_vaga(isolated_workspace, insert_job, monkeypatch, tmp_path):
    insert_job(job_id="li-1", status="applied", title="<script>alert(1)</script>",
               company="A&B", job_url="javascript:alert(1)", match_score=82)

    page = _render(isolated_workspace, monkeypatch, tmp_path)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "A&amp;B" in page
    assert "javascript:" not in page


def test_dashboard_mostra_retorno_por_faixa(isolated_workspace, insert_job, monkeypatch, tmp_path):
    insert_job(job_id="li-1", status="applied", match_score=85, outcome="interview")
    insert_job(job_id="li-2", status="applied", match_score=88, outcome="no_response")
    insert_job(job_id="li-3", status="applied", match_score=76)

    page = _render(isolated_workspace, monkeypatch, tmp_path)

    assert "Retorno por Faixa de Fit" in page
    assert "80–89%" in page
    assert "50%" in page
    assert "Entrevista" in page
    assert "Pendente" in page


def test_dashboard_sem_banco_nao_quebra(monkeypatch, tmp_path):
    monkeypatch.setattr(generate_dashboard, "DB_PATH", tmp_path / "nao_existe.db")
    assert generate_dashboard.generate_dashboard() is None
