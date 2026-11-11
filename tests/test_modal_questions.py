"""
tests/test_modal_questions.py
Perguntas de lista e de opção no modal Easy Apply, com estrutura copiada de um snapshot real do LinkedIn.
"""

import json
import pytest
import apply_job
import queue_guard
import telegram_bot

STEP = "\n".join([
    "  - dialog [ref=f200e956]:",
    '    - button "Fechar" [ref=f200e957] [cursor=pointer]',
    "          - paragraph [ref=f200e973]: 3 de 5 páginas",
    "              - generic [ref=f200e1208]: Qual seu nível de proficiência em Inglês?*",
    '              - combobox "Qual seu nível de proficiência em Inglês?*" [ref=f200e1211] [cursor=pointer]:',
    '                - option "Selecionar opção" [disabled]',
    '                - option "Nenhuma" [selected]',
    '                - option "Conversação"',
    '                - option "Proficiente"',
    '                - option "Nativo ou bilíngue"',
    "              - paragraph [ref=f200e1213]: Você concorda em se locomover até a localidade da vaga?*",
    "              - radiogroup [ref=f200e1214]:",
    '                      - radio "Você concorda em se locomover até a localidade da vaga?" [active]',
    "                    - paragraph [ref=f200e1221]: Sim",
    '                      - radio "Você concorda em se locomover até a localidade da vaga?"',
    "                    - paragraph [ref=f200e1227]: Não",
    "        - contentinfo [ref=f200e1006]:",
    '          - button "Voltar" [ref=f200e1152] [cursor=pointer]',
    '          - button "Avançar" [ref=f200e1007] [cursor=pointer]',
])

ENGLISH = "Qual seu nível de proficiência em Inglês?"
RELOCATE = "Você concorda em se locomover até a localidade da vaga?"


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, args, timeout=45):
        self.calls.append(args)
        return True, "ok"


@pytest.fixture
def browser(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(apply_job, "run_openclaw_browser", rec)
    monkeypatch.setattr(apply_job.time, "sleep", lambda s: None)
    return rec


def test_parses_select_and_radio_from_real_structure():
    selects = apply_job.parse_select_questions(STEP)
    radios = apply_job.parse_radio_questions(STEP)

    assert selects == [{"kind": "select", "label": ENGLISH, "ref": "f200e1211",
                        "options": ["Nenhuma", "Conversação", "Proficiente", "Nativo ou bilíngue"], "selected": "Nenhuma"}]
    assert radios[0]["label"] == RELOCATE
    assert radios[0]["options"] == ["Sim", "Não"]
    assert radios[0]["refs"] == {"Sim": "f200e1221", "Não": "f200e1227"}
    assert radios[0]["selected"] is None


def test_choose_option_never_guesses():
    assert apply_job.choose_option("Fluente", ["Nenhuma", "Conversação", "Proficiente"]) is None
    assert apply_job.choose_option("Yes", ["Sim", "Não"]) == "Sim"
    assert apply_job.choose_option("proficiente", ["Nenhuma", "Proficiente"]) == "Proficiente"
    assert apply_job.choose_option(None, ["Sim"]) is None


def test_unknown_select_asks_with_real_options_and_ignores_prefill(isolated_workspace, insert_job, job_field, browser):
    job_id = insert_job(status="in_progress")

    asked = apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)

    assert asked == ENGLISH
    assert job_field(job_id) == "needs_input"
    assert queue_guard.get_pending_question(job_id)["options"] == ["Nenhuma", "Conversação", "Proficiente", "Nativo ou bilíngue"]
    english_msg = [p for m, p in isolated_workspace["telegram"] if m == "sendMessage" and ENGLISH in p.get("text", "")][-1]
    markup = english_msg["reply_markup"]
    markup = json.loads(markup) if isinstance(markup, str) else markup
    assert markup["inline_keyboard"][2][0] == {"text": "Proficiente", "callback_data": f"ansi:{job_id}:0:2"}
    assert not [c for c in browser.calls if c[0] in ("select", "click")]


def test_all_unknown_questions_of_a_step_are_asked_at_once(isolated_workspace, insert_job, browser):
    job_id = insert_job(status="in_progress")

    apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)

    assert [q["question"] for q in queue_guard.get_pending_questions(job_id)] == [ENGLISH, RELOCATE]
    texts = [p.get("text", "") for m, p in isolated_workspace["telegram"] if m == "sendMessage"]
    assert any("2 perguntas" in t for t in texts)
    assert any("Pergunta 2/2" in t and RELOCATE in t for t in texts)


def test_known_answers_are_not_typed_while_questions_are_pending(isolated_workspace, insert_job, browser):
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer(RELOCATE, "Sim")

    apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)

    assert [q["question"] for q in queue_guard.get_pending_questions(job_id)] == [ENGLISH]
    assert not [c for c in browser.calls if c[0] in ("select", "click", "type")]


def test_learned_answers_fill_select_and_radio(isolated_workspace, insert_job, browser):
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer(ENGLISH, "Proficiente")
    queue_guard.save_learned_answer(RELOCATE, "Não")

    assert apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP) is None
    assert ["select", "f200e1211", "Proficiente"] in browser.calls
    assert ["click", "f200e1227"] in browser.calls


def test_option_button_saves_answer_and_requeues(isolated_workspace, insert_job, job_field, browser):
    job_id = insert_job(status="in_progress")
    apply_job.fill_screening_fields(job_id, "AI Engineer", "Acme", STEP)

    def click(data):
        telegram_bot.handle_update({"update_id": 1, "callback_query": {
            "id": "cb", "data": data, "message": {"message_id": 9, "chat": {"id": 111}}}})

    click(f"ansi:{job_id}:0:2")
    assert job_field(job_id) == "needs_input"  # ainda falta a pergunta de locomoção
    assert queue_guard.resolve_screening_question(ENGLISH) == (True, "Proficiente")

    click(f"ansi:{job_id}:1:1")
    assert job_field(job_id) == "approved"
    assert queue_guard.resolve_screening_question(RELOCATE) == (True, "Não")


def test_legacy_option_button_still_works(isolated_workspace, insert_job, job_field):
    job_id = insert_job(status="in_progress")
    queue_guard.mark_needs_input(job_id, ENGLISH, ["Nenhuma", "Conversação"])
    telegram_bot.handle_update({"update_id": 1, "callback_query": {
        "id": "cb", "data": f"ansi:{job_id}:1", "message": {"message_id": 9, "chat": {"id": 111}}}})
    assert job_field(job_id) == "approved"
    assert queue_guard.resolve_screening_question(ENGLISH) == (True, "Conversação")


def test_validation_error_is_detected():
    assert apply_job.validation_error('- paragraph [ref=e1]: Este campo é obrigatório') == "este campo é obrigatório"
    assert apply_job.validation_error('- button "Avançar" [ref=e2]') is None


def test_resume_list_is_not_a_question():
    step = "\n".join([
        "  - dialog [ref=f183e956]:",
        "            - paragraph [ref=f183e1017]: Selecione ou carregue um currículo no formato DOC, DOCX ou PDF com menos de 2 MB",
        "            - radiogroup [ref=f183e1019]:",
        "                        - paragraph [ref=f183e1028]: PDF",
        '                    - radio "CV_Antigo.pdf" [checked]',
        "                        - paragraph [ref=f183e1046]: PDF",
        '                    - radio "Curriculo_Novo.pdf"',
        "                        - paragraph [ref=f183e1064]: PDF",
    ])
    assert apply_job.parse_radio_questions(step) == []


def test_follow_company_checkbox_from_real_review_screen():
    review = "\n".join([
        "          - generic [ref=f268e1344]:",
        "            - generic [ref=f268e1346]:",
        '              - checkbox "Siga a empresa Locus.software para ficar por dentro das novidades" [checked]',
        "            - paragraph [ref=f268e1349]: Siga a empresa Locus.software para ficar por dentro das novidades",
    ])
    assert apply_job.find_follow_checkbox_ref(review) == "f268e1349"
    assert apply_job.find_follow_checkbox_ref(review.replace(" [checked]", "")) is None


def test_perceiver_describes_a_question_step():
    obs = apply_job.A11yPerceiver().observe(STEP)

    assert [(f.label, f.kind) for f in obs.fields] == [(ENGLISH, "select"), (RELOCATE, "radio")]
    english, relocate = obs.fields
    assert english.ref == "f200e1211" and english.selected == "Nenhuma"
    assert english.options == ["Nenhuma", "Conversação", "Proficiente", "Nativo ou bilíngue"]
    assert relocate.option_refs == {"Sim": "f200e1221", "Não": "f200e1227"}
    assert obs.buttons == {"next": "f200e1007"}
    assert obs.follow_ref is None
    assert obs.blockers == []
    assert apply_job.is_resume_step(STEP) is False
    assert all(f.source == "a11y" and f.confidence == 1.0 for f in obs.fields)


def test_perceiver_finds_upload_submit_and_follow():
    resume = apply_job.A11yPerceiver().observe(
        'dialog "Candidatar-se"\nbutton "Carregar currículo" [ref=e2]\nbutton "Avançar" [ref=e3]')
    review = apply_job.A11yPerceiver().observe(
        'dialog "Candidatar-se"\ncheckbox "Seguir empresa" [checked] [ref=e7]\nbutton "Enviar candidatura" [ref=e8]')

    assert resume.buttons == {"upload": "e2", "next": "e3"}
    assert apply_job.is_resume_step('button "Carregar currículo" [ref=e2]')
    assert review.buttons == {"submit": "e8"} and review.follow_ref == "e7"


def test_perceiver_skips_contact_fields_and_empty_lists():
    step = "\n".join([
        '- dialog [ref=d1]:',
        '  - textbox "E-mail" [ref=t1]',
        '  - textbox "Quantos anos de experiência você tem com Python?" [ref=t2]',
        '  - combobox "Código do país" [ref=c1]:',
        '    - option "Brasil (+55)" [selected]',
        '  - combobox "Cidade" [ref=c2]:',
    ])
    fields = apply_job.A11yPerceiver().fields(step)
    assert [(f.label, f.kind, f.ref) for f in fields] == [("Quantos anos de experiência você tem com Python?", "text", "t2")]


def test_unsupported_field_kind_is_asked_even_with_a_saved_answer(isolated_workspace, insert_job, job_field, browser):
    """Campo de tipo que o bot não sabe preencher vira pergunta: nunca é respondido sozinho."""
    job_id = insert_job(status="in_progress")
    queue_guard.save_learned_answer("Aceita os termos?", "Sim")
    field = apply_job.FieldObs(label="Aceita os termos?", kind="checkbox", options=["Sim"], ref="x1")

    assert apply_job.answer_fields(job_id, "AI Engineer", "Acme", [field]) == "Aceita os termos?"
    assert job_field(job_id) == "needs_input"
    assert browser.calls == []
