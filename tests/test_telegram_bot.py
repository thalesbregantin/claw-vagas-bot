"""
tests/test_telegram_bot.py
Comportamento do bot:
1. Chat não autorizado é ignorado
2. Aprovação idempotente e fila persistente no banco
3. Texto livre não vira resposta de triagem por acidente
4. Empresas descartadas saem das recomendações
5. Registro de resultados pós-envio
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path
import pytest
import queue_guard
import telegram_bot


def text_update(text, chat_id=111):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def callback_update(data, chat_id=111):
    return {"update_id": 1, "callback_query": {
        "id": "cb", "data": data, "message": {"message_id": 9, "chat": {"id": chat_id}}
    }}


def sent_texts(workspace):
    return [params.get("text", "") for method, params in workspace["telegram"] if method in ("sendMessage", "editMessageText")]


@pytest.fixture(autouse=True)
def clean_chat_state():
    telegram_bot.AWAITING_ANSWER.clear()
    telegram_bot.ACTIVE_INTERVIEWS.clear()


def test_unauthorized_chat_is_ignored(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="scored")

    telegram_bot.handle_update(text_update(f"aprovar {job_id}", chat_id=999))
    telegram_bot.handle_update(text_update("⚙️ Meu Perfil & Memória", chat_id=999))
    telegram_bot.handle_update(callback_update(f"approve:{job_id}", chat_id=999))

    assert job_field(job_id) == "scored"
    assert isolated_workspace["telegram"] == []


def test_authorized_chat_is_served(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="scored")
    telegram_bot.handle_update(text_update(f"aprovar {job_id}"))
    assert job_field(job_id) == "approved"


def test_double_approval_keeps_one_entry(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="scored")
    telegram_bot.handle_approve("111", job_id)
    telegram_bot.handle_approve("111", job_id)
    assert job_field(job_id) == "approved"
    assert "já está na fila" in sent_texts(isolated_workspace)[-1]


@pytest.mark.parametrize("status", ["applied", "in_progress"])
def test_cannot_reapprove_finished_job(isolated_workspace, insert_job, job_field, status):
    job_id = insert_job(status=status)
    telegram_bot.handle_approve("111", job_id)
    assert job_field(job_id) == status


def test_cannot_skip_applied_job(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="applied")
    telegram_bot.handle_skip("111", job_id)
    assert job_field(job_id) == "applied"


def test_worker_never_forces_guardrails(isolated_workspace, insert_job, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(telegram_bot.subprocess, "run", fake_run)
    telegram_bot.run_apply_job(insert_job())

    assert "--force" not in captured["cmd"]


def test_worker_failure_leaves_queue(isolated_workspace, insert_job, job_field, monkeypatch):
    """Sem isso, um crash antes da reserva repetiria a mesma vaga para sempre."""
    monkeypatch.setattr(
        telegram_bot.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "Traceback")
    )
    job_id = insert_job()
    telegram_bot.run_apply_job(job_id)

    assert job_field(job_id) == "failed"
    assert any("não concluída" in t for t in sent_texts(isolated_workspace))


def test_worker_timeout_marks_failed(isolated_workspace, insert_job, job_field, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(telegram_bot.subprocess, "run", fake_run)
    job_id = insert_job(status="in_progress")
    telegram_bot.run_apply_job(job_id)
    assert job_field(job_id) == "failed"


def test_approved_jobs_survive_restart(isolated_workspace, insert_job):
    """A fila é o banco: não há estado em memória para perder."""
    job_id = insert_job(status="scored")
    telegram_bot.handle_approve("111", job_id)
    assert queue_guard.get_next_approved_job()["job_id"] == job_id


def test_interrupted_job_is_not_requeued(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="in_progress")
    assert queue_guard.recover_interrupted_jobs() == 1
    assert job_field(job_id) == "failed"
    assert "Confira no LinkedIn" in job_field(job_id, "error_log")


def test_free_text_does_not_answer_pending_question(isolated_workspace, insert_job, monkeypatch, job_field):
    job_id = insert_job(status="needs_input", error_log="Pergunta não mapeada: Você aceita viagens?")
    searches = []
    monkeypatch.setattr(telegram_bot, "handle_search_custom", lambda *a, **k: searches.append(a))

    telegram_bot.handle_update(text_update("ache vagas de golang"))

    assert job_field(job_id) == "needs_input"
    assert queue_guard.load_learned_answers() == {}
    assert searches


def test_typed_answer_after_prompt_is_saved(isolated_workspace, insert_job, job_field):
    question = "Você aceita viagens?"
    job_id = insert_job(status="needs_input", error_log=f"Pergunta não mapeada: {question}")

    telegram_bot.handle_update(callback_update(f"ans_prompt:{job_id}"))
    telegram_bot.handle_update(text_update("Sim, até 20% do tempo"))

    assert job_field(job_id) == "approved"
    assert queue_guard.resolve_screening_question(question) == (True, "Sim, até 20% do tempo")
    assert "Você aceita viagens" in (isolated_workspace["data_dir"] / "EXPERIENCE_ANSWERS.md").read_text(encoding="utf-8")


def test_button_answer_is_saved(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="needs_input", error_log="Pergunta não mapeada: Você tem CNH?")
    telegram_bot.handle_update(callback_update(f"ans:{job_id}:Sim"))
    assert job_field(job_id) == "approved"
    assert queue_guard.resolve_screening_question("você tem cnh") == (True, "Sim")


def test_stale_answer_does_not_requeue(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="applied", error_log="Pergunta não mapeada: Você tem CNH?")
    telegram_bot.handle_update(callback_update(f"ans:{job_id}:Sim"))
    assert job_field(job_id) == "applied"
    assert queue_guard.load_learned_answers() == {}


def test_excluded_company_leaves_recommendations(isolated_workspace, insert_job):
    bad = insert_job(job_id="li-1", status="scored", company="Empresa Ruim", job_url="https://www.linkedin.com/jobs/view/1", match_score=95)
    good = insert_job(job_id="li-2", status="scored", company="Empresa Boa", job_url="https://www.linkedin.com/jobs/view/2", match_score=80)

    telegram_bot.record_negative_feedback(bad, "company")
    telegram_bot.handle_show_ready_jobs("111", limit=3)

    cards = [t for t in sent_texts(isolated_workspace) if "Vaga para avaliar" in t]
    assert len(cards) == 1
    assert "Empresa Boa" in cards[0]
    assert good


def test_card_does_not_claim_salary_alignment(isolated_workspace, insert_job):
    insert_job(status="scored", match_score=90, match_reasons=json.dumps(["Motivo"]), gaps=json.dumps([]))
    telegram_bot.handle_show_ready_jobs("111")
    assert not any("Pretensão Alinhada" in t for t in sent_texts(isolated_workspace))


@pytest.mark.parametrize("text,expected", [
    ("ache vagas de golang", "golang"),
    ("Procura vagas para desenvolvedor Rust remoto", "desenvolvedor Rust remoto"),
    ("vagas de python na Nubank", "python"),
    ("buscar algo", "Senior Python AI Engineer"),
])
def test_search_term_follows_the_request(text, expected):
    assert telegram_bot.extract_search_term(text) == expected


def test_daily_report_lists_only_today(isolated_workspace, insert_job, monkeypatch):
    now = datetime(2026, 10, 29, 14, 0, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: now)
    insert_job(job_id="li-1", status="applied", company="Hoje", job_url="https://www.linkedin.com/jobs/view/1", applied_at="2026-10-29 15:00:00")
    insert_job(job_id="li-2", status="applied", company="Ontem", job_url="https://www.linkedin.com/jobs/view/2", applied_at="2026-10-28 15:00:00")

    telegram_bot.handle_daily_report("111")

    report = sent_texts(isolated_workspace)[-1]
    assert "Hoje" in report
    assert "Ontem" not in report


def test_status_panel_shows_zero_without_quota_row(isolated_workspace, insert_job):
    insert_job(status="applied", applied_at="2026-01-01 10:00:00")
    telegram_bot.handle_status_fup("111")
    assert "0/15" in sent_texts(isolated_workspace)[-1]


def test_outcome_is_recorded_and_reported(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="applied", match_score=85)
    telegram_bot.handle_outcomes("111")
    telegram_bot.handle_update(callback_update(f"outcome:{job_id}:interview"))

    assert job_field(job_id, "outcome") == "interview"
    stats = queue_guard.get_outcome_stats()
    assert stats["interviews"] == 1
    assert stats["pending"] == 0
    assert stats["bands"] == [{"band": 80, "decided": 1, "positive": 1}]


def test_outcome_rejects_unknown_code_and_unsent_job(isolated_workspace, insert_job):
    applied = insert_job(job_id="li-1", status="applied", job_url="https://www.linkedin.com/jobs/view/1")
    scored = insert_job(job_id="li-2", status="scored", job_url="https://www.linkedin.com/jobs/view/2")
    assert queue_guard.record_outcome(applied, "invented") is False
    assert queue_guard.record_outcome(scored, "interview") is False


def test_interview_feedback_is_objective(isolated_workspace):
    interview = {"title": "AI Engineer", "company": "Acme", "description": "Python e Kubernetes"}

    feedback = telegram_bot.build_interview_feedback(interview, "Usei Python e reduzi o tempo em 90%.")

    assert "/ 10" not in feedback
    assert "90%" in feedback
    assert "KUBERNETES" in feedback


def test_short_empty_answers_get_different_feedback(isolated_workspace):
    interview = {"title": "AI Engineer", "company": "Acme", "description": "Python"}
    short = telegram_bot.build_interview_feedback(interview, "não sei")
    long = telegram_bot.build_interview_feedback(interview, "Python " * 100 + "reduzi 30%")
    assert short != long
    assert "Resposta curta" in short
    assert "Resposta curta" not in long


def test_external_job_can_be_confirmed_and_tracked(isolated_workspace, insert_job, job_field):
    job_id = insert_job(job_id="in-1", status="notified_external", apply_type="EXTERNAL_ATS",
                        job_url="https://br.indeed.com/viewjob?jk=1", match_score=81)

    telegram_bot.handle_update(callback_update(f"ext_applied:{job_id}"))
    assert job_field(job_id) == "applied"
    assert job_field(job_id, "applied_at") is not None
    assert queue_guard.get_today_quota(queue_guard.get_db())[0] == 0

    telegram_bot.handle_update(callback_update(f"outcome:{job_id}:response"))
    assert job_field(job_id, "outcome") == "response"


def test_external_confirm_only_from_notified_external(isolated_workspace, insert_job, job_field):
    job_id = insert_job(job_id="in-2", status="scored", apply_type="EXTERNAL_ATS",
                        job_url="https://br.indeed.com/viewjob?jk=2")
    assert queue_guard.mark_external_applied(job_id) is False
    assert job_field(job_id) == "scored"


def test_focus_mode_hides_and_refuses_external_jobs(isolated_workspace, insert_job, job_field):
    ext = insert_job(job_id="in-9", status="scored", apply_type="EXTERNAL_ATS",
                     job_url="https://br.indeed.com/viewjob?jk=9", match_score=95, title="Vaga Externa")
    insert_job(job_id="li-9", status="scored", match_score=80, title="Vaga Interna",
               job_url="https://www.linkedin.com/jobs/view/9")

    telegram_bot.handle_show_ready_jobs("111")
    texts = " ".join(sent_texts(isolated_workspace))
    assert "Vaga Interna" in texts and "Vaga Externa" not in texts

    telegram_bot.handle_update(callback_update(f"approve:{ext}"))
    assert job_field(ext) == "scored"


def test_scrape_options_follow_focus(monkeypatch):
    import common
    monkeypatch.setenv("EASY_APPLY_ONLY", "1")
    assert common.scrape_options() == {"site_name": ["linkedin"], "easy_apply": True}
    monkeypatch.setenv("EASY_APPLY_ONLY", "0")
    assert "indeed" in common.scrape_options()["site_name"]


def test_ready_counter_matches_the_cards(isolated_workspace, insert_job):
    insert_job(job_id="li-1", status="scored", company="Boa", job_url="https://www.linkedin.com/jobs/view/1", match_score=90)
    insert_job(job_id="li-2", status="scored", company="Externa", apply_type="EXTERNAL_ATS",
               job_url="https://www.linkedin.com/jobs/view/2", match_score=85)
    bad = insert_job(job_id="li-3", status="scored", company="Ruim", job_url="https://www.linkedin.com/jobs/view/3", match_score=80)
    telegram_bot.record_negative_feedback(bad, "company")

    label = telegram_bot.get_main_keyboard()["keyboard"][0][1]["text"]
    telegram_bot.handle_show_ready_jobs("111", limit=10)
    cards = [t for t in sent_texts(isolated_workspace) if "Vaga para avaliar" in t]

    assert label == f"📥 Vagas Prontas ({len(cards)})"
    assert len(cards) == 1


def memory_buttons(workspace):
    for method, params in reversed(workspace["telegram"]):
        markup = params.get("reply_markup")
        markup = json.loads(markup) if isinstance(markup, str) else markup
        if markup and "inline_keyboard" in markup:
            return [b for row in markup["inline_keyboard"] for b in row]
    return []


def test_my_answers_edit_and_delete(isolated_workspace):
    queue_guard.save_learned_answer("Você tem CNH?", "Sim")
    queue_guard.save_learned_answer("Você aceita viagens?", "Não")

    telegram_bot.handle_update(text_update("/respostas"))
    view = next(b for b in memory_buttons(isolated_workspace) if b["text"].startswith("Você tem CNH"))

    telegram_bot.handle_update(callback_update(view["callback_data"]))
    edit = next(b for b in memory_buttons(isolated_workspace) if "Editar" in b["text"])
    telegram_bot.handle_update(callback_update(edit["callback_data"]))
    telegram_bot.handle_update(text_update("Sim, categoria B"))
    assert queue_guard.resolve_screening_question("Você tem CNH?") == (True, "Sim, categoria B")

    md = (isolated_workspace["data_dir"] / "EXPERIENCE_ANSWERS.md").read_text(encoding="utf-8")
    assert md.count("| **Você tem CNH?** |") == 1

    telegram_bot.handle_update(callback_update(view["callback_data"]))
    delete = next(b for b in memory_buttons(isolated_workspace) if "Apagar" in b["text"])
    telegram_bot.handle_update(callback_update(delete["callback_data"]))
    assert queue_guard.resolve_screening_question("Você tem CNH?")[0] is True  # só pediu confirmação
    confirm = next(b for b in memory_buttons(isolated_workspace) if "Sim, apagar" in b["text"])
    telegram_bot.handle_update(callback_update(confirm["callback_data"]))

    assert "você tem cnh" not in queue_guard.load_learned_answers()
    assert "Você tem CNH" not in (isolated_workspace["data_dir"] / "EXPERIENCE_ANSWERS.md").read_text(encoding="utf-8")
    assert queue_guard.resolve_screening_question("Você aceita viagens?") == (True, "Não")
    for b in memory_buttons(isolated_workspace):
        assert len(b["callback_data"].encode("utf-8")) <= 64


def test_typed_answer_targets_the_right_question(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="in_progress")
    queue_guard.mark_needs_input(job_id, [{"question": "Você tem CNH?", "options": []},
                                          {"question": "Você aceita viagens?", "options": []}])

    telegram_bot.handle_update(callback_update(f"ans_prompt:{job_id}:1"))
    telegram_bot.handle_update(text_update("Até 20% do tempo"))
    assert job_field(job_id) == "needs_input"
    assert queue_guard.resolve_screening_question("Você aceita viagens?") == (True, "Até 20% do tempo")

    telegram_bot.handle_update(callback_update(f"ans:{job_id}:0:Sim"))
    assert job_field(job_id) == "approved"
