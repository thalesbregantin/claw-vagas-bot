"""
tests/test_question_similarity.py
Perguntas parecidas: a resposta de uma pergunta equivalente vira sugestão no Telegram.
Regra coberta: sugestão nunca é preenchida sozinha; só vale depois que o usuário toca em "Usar".
"""

import json

import anthropic
import httpx2
import pytest

import apply_job
import queue_guard
import question_similarity
import telegram_bot
from test_modal_questions import ENGLISH, RELOCATE, STEP, Recorder
from test_vision_perception import FakeClaude

ENGLISH_OLD = "Você possui algum nível de proficiência em inglês?"
SPANISH_OLD = "Você possui algum nível de proficiência em espanhol?"
TRAVEL_OLD = "Você tem disponibilidade para viagens a trabalho?"
TRAVEL_STEP = '- dialog [ref=d1]:\n  - textbox "Tem disponibilidade para viagens a trabalho?*" [ref=t1]\n  - button "Avançar" [ref=b1]'
TRAVEL_NEW = "Tem disponibilidade para viagens a trabalho?"


@pytest.fixture
def browser(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(apply_job, "run_openclaw_browser", rec)
    monkeypatch.setattr(apply_job.time, "sleep", lambda s: None)
    return rec


def click(data):
    telegram_bot.handle_update({"update_id": 1, "callback_query": {
        "id": "cb", "data": data, "message": {"message_id": 9, "chat": {"id": 111}}}})


def question_messages(workspace):
    out = []
    for method, params in workspace["telegram"]:
        markup = params.get("reply_markup")
        markup = json.loads(markup) if isinstance(markup, str) else markup
        if method == "sendMessage" and markup and "inline_keyboard" in markup:
            out.append((params.get("text", ""), [b for row in markup["inline_keyboard"] for b in row]))
    return out


# ---------- comparação local ----------

def test_local_match_finds_same_question_in_other_words():
    learned = [ENGLISH_OLD, SPANISH_OLD, "langgraph", "Você trabalharia em um modelo híbrido?"]
    assert question_similarity.local_matches(ENGLISH, learned) == [ENGLISH_OLD]


def test_local_match_ignores_keywords_and_the_question_itself():
    assert question_similarity.local_matches(ENGLISH, ["inglês", "nível inglês", ENGLISH + " *"]) == []
    assert question_similarity.local_matches(ENGLISH, ["nível de inglês"]) == ["nível de inglês"]


def test_local_match_does_not_mix_different_subjects():
    assert question_similarity.local_matches("Qual seu nível de proficiência em francês?", [ENGLISH_OLD, SPANISH_OLD]) == []


# ---------- comparação pelo Claude (simulado) ----------

def test_ai_match_maps_numbers_back_to_questions_and_sends_no_answers():
    learned = {ENGLISH_OLD: "Intermediário-RESPOSTA-SECRETA", "crewai": "2", SPANISH_OLD: "Básico-RESPOSTA-SECRETA"}
    client = FakeClaude({"matches": [1, 1, 7, 0, True]})

    assert question_similarity.ai_matches(ENGLISH, list(learned), client) == [ENGLISH_OLD]
    sent = json.dumps(client.requests[0], ensure_ascii=False)
    assert ENGLISH_OLD in sent and "RESPOSTA-SECRETA" not in sent and "crewai" not in sent


def test_ai_says_no_match_is_respected():
    """O Claude respondeu que nada equivale: não cai para a comparação local, que sugeriria."""
    assert question_similarity.find_similar(ENGLISH, [ENGLISH_OLD], client=FakeClaude({"matches": []})) == []


@pytest.mark.parametrize("client", [
    FakeClaude(error=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))),
    FakeClaude({"matches": [1]}, stop_reason="refusal"),
    FakeClaude(text="não é json"),
    FakeClaude({"outra_chave": [1]}),
])
def test_ai_failure_falls_back_to_local_comparison(client):
    assert question_similarity.find_similar(ENGLISH, [ENGLISH_OLD], client=client) == [ENGLISH_OLD]


def test_ai_is_off_without_key_or_when_disabled(monkeypatch):
    monkeypatch.setenv("SIMILAR_QUESTIONS_AI", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert question_similarity.ai_enabled() is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-teste")
    assert question_similarity.ai_enabled() is True
    monkeypatch.setenv("SIMILAR_QUESTIONS_AI", "0")
    assert question_similarity.ai_enabled() is False


def test_no_candidates_never_calls_the_api():
    client = FakeClaude({"matches": [1]})
    assert question_similarity.find_similar(ENGLISH, ["langgraph", "crewai"], client=client) == []
    assert client.requests == []


# ---------- fluxo: sugestão no Telegram, nunca preenchimento automático ----------

def test_similar_answer_is_suggested_but_never_filled(isolated_workspace, insert_job, job_field, browser):
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer(ENGLISH_OLD, "Conversação")
    queue_guard.save_learned_answer(RELOCATE, "Sim")

    asked = apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)

    assert asked == ENGLISH and job_field(job_id) == "needs_input"
    assert browser.calls == [], "Sugestão não autoriza preencher nada"
    pending = queue_guard.get_pending_questions(job_id)
    assert pending[0]["suggestion"] == {"from": ENGLISH_OLD, "answer": "Conversação"}
    text, buttons = question_messages(isolated_workspace)[-1]
    assert "💡" in text and ENGLISH_OLD in text
    assert buttons[0] == {"text": "✅ Usar: Conversação", "callback_data": f"anss:{job_id}:0"}
    assert [b["text"] for b in buttons[1:5]] == ["Nenhuma", "Conversação", "Proficiente", "Nativo ou bilíngue"]
    assert all(len(b["callback_data"].encode("utf-8")) <= 64 for b in buttons)


def test_accepting_the_suggestion_creates_the_alias(isolated_workspace, insert_job, job_field, browser):
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer(ENGLISH_OLD, "Conversação")
    queue_guard.save_learned_answer(RELOCATE, "Sim")
    apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)

    click(f"anss:{job_id}:0")

    assert job_field(job_id) == "approved"
    assert queue_guard.resolve_screening_question(ENGLISH) == (True, "Conversação")
    assert queue_guard.resolve_screening_question(ENGLISH_OLD) == (True, "Conversação")

    # Na próxima execução a pergunta casa por texto exato: preenche sem perguntar e sem comparar nada
    browser.calls.clear()
    assert apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP) is None
    assert ["select", "f200e1211", "Conversação"] in browser.calls


def test_suggestion_must_match_a_real_option(isolated_workspace, insert_job, browser):
    """'Intermediário' não existe na lista da vaga: não vira sugestão. A outra resposta parecida, que existe, vira."""
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer(ENGLISH_OLD, "Intermediário")
    apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)
    assert "suggestion" not in queue_guard.get_pending_questions(job_id)[0]
    assert not any(b["callback_data"].startswith("anss:") for _, bs in question_messages(isolated_workspace) for b in bs)

    queue_guard.save_learned_answer("Qual é o seu nível de proficiência no idioma inglês?", "Proficiente")
    apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)
    assert queue_guard.get_pending_questions(job_id)[0]["suggestion"]["answer"] == "Proficiente"


def test_text_question_gets_suggestion_and_other_answers_still_work(isolated_workspace, insert_job, job_field, browser):
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer(TRAVEL_OLD, "Sim, até 20% do tempo")

    assert apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", TRAVEL_STEP) == TRAVEL_NEW
    _, buttons = question_messages(isolated_workspace)[-1]
    assert buttons[0]["text"] == "✅ Usar: Sim, até 20% do tempo"

    click(f"ans:{job_id}:0:Não")  # o usuário preferiu outra resposta
    assert queue_guard.resolve_screening_question(TRAVEL_NEW) == (True, "Não")
    assert queue_guard.resolve_screening_question(TRAVEL_OLD) == (True, "Sim, até 20% do tempo")


def test_use_button_without_suggestion_changes_nothing(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="in_progress")
    queue_guard.mark_needs_input(job_id, [{"question": "Você tem CNH?", "options": []}])

    click(f"anss:{job_id}:0")
    click(f"anss:{job_id}:5")

    assert job_field(job_id) == "needs_input"
    assert queue_guard.load_learned_answers() == {}


def test_suggestion_failure_does_not_block_the_question(isolated_workspace, insert_job, job_field, browser, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("comparação fora do ar")

    monkeypatch.setattr(question_similarity, "find_similar", boom)
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer(ENGLISH_OLD, "Conversação")

    assert apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP) == ENGLISH
    assert job_field(job_id) == "needs_input"
