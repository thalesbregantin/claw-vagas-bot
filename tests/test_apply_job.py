"""
tests/test_apply_job.py
Fluxo de candidatura de ponta a ponta com um navegador simulado:
1. Só envia com vaga aprovada, travas liberadas, PDF anexado e confirmação do LinkedIn
2. Pergunta desconhecida pausa em needs_input sem enviar
3. Chamadas ao CLI não passam por shell
"""

from datetime import datetime
from pathlib import Path
import json
import subprocess
import pytest
import apply_job
import queue_guard
import tailor_resume

JOB_PAGE = 'heading "Senior Applied AI Engineer"\nbutton "Candidatura simplificada" [ref=e1]'
RESUME_STEP = 'dialog "Candidatar-se"\nbutton "Carregar currículo" [ref=e2]\nbutton "Avançar" [ref=e3]'
QUESTION_STEP = 'dialog "Candidatar-se"\ntextbox "Quantos anos de experiência você tem com Python?" [ref=e4]\nbutton "Revisar" [ref=e5]'
UNKNOWN_STEP = 'dialog "Candidatar-se"\ntextbox "Qual sua experiência com contratos em Solana?" [ref=e6]\nbutton "Revisar" [ref=e5]'
REVIEW_STEP = 'dialog "Candidatar-se"\ncheckbox "Seguir empresa" [checked] [ref=e7]\nbutton "Enviar candidatura" [ref=e8]'
CONFIRMED = 'dialog\nheading "Sua candidatura foi enviada"'


class FakeBrowser:
    """Simula o CLI: cada clique avança a lista de telas e o upload mostra o arquivo no modal."""

    def __init__(self, screens, upload_ok=True):
        self.screens = list(screens)
        self.position = 0
        self.upload_ok = upload_ok
        self.uploaded = None
        self.calls = []

    def current(self):
        screen = self.screens[min(self.position, len(self.screens) - 1)]
        return f"{screen}\ntext \"{self.uploaded}\"" if self.uploaded else screen

    def __call__(self, args, timeout=45):
        self.calls.append(args)
        if args[0] == "snapshot":
            return True, self.current()
        if args[0] == "click" and args[1] != "e7":
            self.position += 1
        if args[0] == "upload" and self.upload_ok:
            self.uploaded = Path(args[1]).name
        return True, "ok"

    def commands(self, name):
        return [c for c in self.calls if c[0] == name]

    def clicked(self, ref):
        return ["click", ref] in self.calls


@pytest.fixture
def apply_env(isolated_workspace, insert_job, monkeypatch):
    """Horário comercial fixo, sem esperas e com PDF simulado."""
    now = datetime(2026, 10, 29, 14, 0, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: now)
    monkeypatch.setattr(apply_job.time, "sleep", lambda seconds: None)

    def fake_render_pdf(html, output_path):
        output_path.write_bytes(b"%PDF-1.7" + b"0" * 2000)
        return True

    monkeypatch.setattr(tailor_resume, "render_pdf", fake_render_pdf)

    def use_browser(*screens, **kwargs):
        browser = FakeBrowser(screens, **kwargs)
        monkeypatch.setattr(apply_job, "run_openclaw_browser", browser)
        return browser

    return use_browser


FULL_FLOW = (JOB_PAGE, RESUME_STEP, QUESTION_STEP, REVIEW_STEP, CONFIRMED)


def quota_today():
    conn = queue_guard.get_db()
    count = queue_guard.get_today_quota(conn)[0]
    conn.close()
    return count


def test_full_flow_uploads_answers_and_confirms(apply_env, insert_job, job_field):
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    result = apply_job.apply_to_job(job_id)

    assert result["status"] == "success"
    assert job_field(job_id) == "applied"
    assert job_field(job_id, "apply_type") == "EASY_APPLY"
    assert quota_today() == 1
    assert browser.commands("navigate")[0][1].startswith("https://www.linkedin.com")
    assert browser.commands("upload"), "O PDF precisa ser anexado"
    assert ["type", "--", "e4", "5"] in browser.calls
    assert browser.clicked("e7"), "'Seguir empresa' deve ser desmarcado"
    assert browser.clicked("e8")


def test_always_navigates_before_reading_page(apply_env, insert_job):
    """Um modal aberto pode ser de outra vaga: o primeiro comando é sempre navegar."""
    browser = apply_env(*FULL_FLOW)
    apply_job.apply_to_job(insert_job())
    assert browser.calls[0][0] == "navigate"


def test_uploaded_filename_has_no_internal_id(apply_env, insert_job):
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()
    apply_job.apply_to_job(job_id)
    filename = Path(browser.commands("upload")[0][1]).name
    assert job_id not in filename
    assert filename.startswith("Curriculo_")


def test_submit_without_confirmation_is_not_applied(apply_env, insert_job, job_field):
    browser = apply_env(JOB_PAGE, RESUME_STEP, REVIEW_STEP, 'dialog "Algo inesperado"')
    job_id = insert_job()

    result = apply_job.apply_to_job(job_id)

    assert browser.clicked("e8")
    assert result["status"] == "failed"
    assert job_field(job_id) == "failed"
    assert quota_today() == 0


def test_failed_upload_never_submits(apply_env, insert_job, job_field):
    browser = apply_env(*FULL_FLOW, upload_ok=False)
    job_id = insert_job()

    result = apply_job.apply_to_job(job_id)

    assert result["status"] == "failed"
    assert not browser.clicked("e8")
    assert job_field(job_id) == "failed"


def test_review_without_resume_step_never_submits(apply_env, insert_job):
    browser = apply_env(JOB_PAGE, REVIEW_STEP, CONFIRMED)
    assert apply_job.apply_to_job(insert_job())["status"] == "failed"
    assert not browser.clicked("e8")


def test_dry_run_stops_before_submit(apply_env, insert_job, job_field):
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    result = apply_job.apply_to_job(job_id, dry_run=True)

    assert result["status"] == "dry_run"
    assert browser.commands("upload")
    assert not browser.clicked("e8")
    assert job_field(job_id) == "approved"
    assert quota_today() == 0


def test_unknown_question_pauses_without_submit(apply_env, insert_job, job_field, isolated_workspace):
    browser = apply_env(JOB_PAGE, RESUME_STEP, UNKNOWN_STEP, REVIEW_STEP, CONFIRMED)
    job_id = insert_job()

    result = apply_job.apply_to_job(job_id)

    assert result["status"] == "needs_input"
    assert job_field(job_id) == "needs_input"
    assert not browser.clicked("e8")
    assert not browser.commands("type")
    assert any("Solana" in params.get("text", "") for _, params in isolated_workspace["telegram"])


def test_answer_button_callback_fits_telegram_limit(isolated_workspace):
    markup = apply_job.question_buttons("in-b79ddb23c252e539", "Quantos anos de experiência?")
    for row in markup["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode("utf-8")) <= 64


@pytest.mark.parametrize("status", ["scored", "applied", "skipped", "in_progress"])
def test_only_approved_jobs_run(apply_env, insert_job, job_field, status):
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job(status=status)

    assert apply_job.apply_to_job(job_id)["status"] == "error"
    assert browser.calls == []
    assert job_field(job_id) == status


def test_guardrails_block_outside_hours(apply_env, insert_job, job_field, monkeypatch):
    browser = apply_env(*FULL_FLOW)
    night = datetime(2026, 10, 29, 23, 0, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: night)
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "blocked"
    assert browser.calls == []
    assert job_field(job_id) == "approved"


def test_already_applied_badge_does_not_consume_quota(apply_env, insert_job, job_field):
    apply_env('heading "Senior AI Engineer"\ntext "Applied 3 days ago"')
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "already_applied"
    assert job_field(job_id) == "applied"
    assert quota_today() == 0


def test_applied_in_job_title_is_not_already_applied():
    assert not apply_job.is_already_applied('heading "Senior Applied AI Engineer"')
    assert not apply_job.is_already_applied('text "Applied Intuition is hiring"')
    assert apply_job.is_already_applied('text "Candidatou-se há 2 dias"')


def test_external_linkedin_job_delivers_kit(apply_env, insert_job, job_field, kit_mode):
    browser = apply_env('heading "AI Engineer"\nbutton "Candidatar-se" [ref=e9]')
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "external_ats"
    assert job_field(job_id) == "notified_external"
    assert job_field(job_id, "apply_type") == "EXTERNAL_ATS"
    assert not browser.clicked("e9")


def test_non_linkedin_job_skips_browser_and_guardrails(apply_env, insert_job, job_field, monkeypatch, kit_mode):
    browser = apply_env(*FULL_FLOW)
    night = datetime(2026, 10, 29, 23, 0, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: night)
    job_id = insert_job(job_id="in-abc", job_url="https://br.indeed.com/viewjob?jk=abc", apply_type="EXTERNAL_ATS")

    assert apply_job.apply_to_job(job_id)["status"] == "external_ats"
    assert browser.calls == []
    assert job_field(job_id) == "notified_external"


def test_external_kit_uses_only_profile_facts(isolated_workspace):
    profile = tailor_resume.load_profile()
    kit = apply_job.build_external_kit(profile, "Backend Engineer", "Python, Kubernetes e AWS")
    text = " ".join(kit.values())
    assert "PYTHON" in text
    assert "KUBERNETES" not in text
    assert "Acompanho" not in text


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/jobs/view/1", True),
    ("https://br.linkedin.com/jobs/view/1", True),
    ("http://www.linkedin.com/jobs/view/1", False),
    ("https://linkedin.com.evil.example/jobs", False),
    ("https://evil.example/?x=linkedin.com", False),
    ("", False),
])
def test_linkedin_url_validation(url, expected):
    assert apply_job.is_linkedin_url(url) is expected


def test_cli_runs_without_shell(monkeypatch):
    """Texto com '&' deve chegar como argumento único, sem passar por cmd.exe."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured.update(cmd=cmd, kwargs=kwargs)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(apply_job, "openclaw_command", lambda: ["node", "openclaw.mjs"])
    monkeypatch.setattr(apply_job.subprocess, "run", fake_run)

    assert apply_job.run_openclaw_browser(["type", "--", "e1", "x&calc"]) == (True, "ok")
    assert not captured["kwargs"].get("shell")
    assert captured["cmd"][-1] == "x&calc"


def test_cli_failure_is_reported(monkeypatch):
    def fake_command():
        raise RuntimeError("CLI 'openclaw' não encontrado no PATH.")

    monkeypatch.setattr(apply_job, "openclaw_command", fake_command)
    ok, out = apply_job.run_openclaw_browser(["snapshot"])
    assert ok is False
    assert "openclaw" in out


def test_external_kit_offers_confirm_button(apply_env, insert_job, isolated_workspace, kit_mode):
    apply_env()
    job_id = insert_job(job_id="in-btn", job_url="https://br.indeed.com/viewjob?jk=btn", apply_type="EXTERNAL_ATS")

    apply_job.apply_to_job(job_id)

    markups = [json.dumps(p.get("reply_markup", "")) for m, p in isolated_workspace["telegram"] if m == "sendMessage"]
    assert any(f"ext_applied:{job_id}" in m for m in markups)
    assert quota_today() == 0


def test_dry_run_does_not_touch_external_job(apply_env, insert_job, job_field, isolated_workspace):
    browser = apply_env()
    job_id = insert_job(job_id="in-dry", job_url="https://br.indeed.com/viewjob?jk=dry", apply_type="EXTERNAL_ATS")

    assert apply_job.apply_to_job(job_id, dry_run=True)["status"] == "dry_run"
    assert job_field(job_id) == "approved"
    assert isolated_workspace["telegram"] == []
    assert browser.calls == []


def test_dry_run_releases_external_linkedin_job(apply_env, insert_job, job_field):
    apply_env('heading "AI Engineer"\nbutton "Candidatar-se" [ref=e9]')
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id, dry_run=True)["status"] == "dry_run"
    assert job_field(job_id) == "approved"


@pytest.fixture
def kit_mode(monkeypatch):
    """Modo com vagas externas: entrega o kit no chat."""
    monkeypatch.setenv("EASY_APPLY_ONLY", "0")


def test_focus_mode_skips_external_linkedin_job(apply_env, insert_job, job_field, isolated_workspace):
    browser = apply_env('heading "AI Engineer"\nbutton "Candidatar-se" [ref=e9]')
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "skipped_external"
    assert job_field(job_id) == "skipped"
    assert job_field(job_id, "apply_type") == "EXTERNAL_ATS"
    assert not browser.clicked("e9")
    assert quota_today() == 0
    assert not any(m == "sendDocument" for m, _ in isolated_workspace["telegram"])


def test_focus_mode_skips_non_linkedin_job(apply_env, insert_job, job_field):
    browser = apply_env()
    job_id = insert_job(job_id="in-foc", job_url="https://br.indeed.com/viewjob?jk=foc", apply_type="EXTERNAL_ATS")

    assert apply_job.apply_to_job(job_id)["status"] == "skipped_external"
    assert job_field(job_id) == "skipped"
    assert browser.calls == []


def test_dry_run_ignores_hours_but_real_run_is_blocked(apply_env, insert_job, job_field, monkeypatch):
    apply_env(*FULL_FLOW)
    night = datetime(2026, 10, 29, 23, 0, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: night)
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "blocked"
    assert apply_job.apply_to_job(job_id, dry_run=True)["status"] == "dry_run"
    assert job_field(job_id) == "approved"
    assert quota_today() == 0


def test_snapshot_retries_while_page_loads(monkeypatch):
    results = [
        (1, "GatewayClientRequestError: Error: Frame changed while its browser snapshot was being captured; retry."),
        (0, 'button "Candidatura simplificada" [ref=e5]'),
    ]
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        code, out = results.pop(0)
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    monkeypatch.setattr(apply_job, "openclaw_command", lambda: ["openclaw"])
    monkeypatch.setattr(apply_job.subprocess, "run", fake_run)
    monkeypatch.setattr(apply_job.time, "sleep", lambda s: None)

    ok, out = apply_job.run_openclaw_browser(["snapshot"])
    assert ok and "ref=e5" in out
    assert len(calls) == 2


def test_non_transient_error_is_not_retried(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="Browser not running", stderr="")

    monkeypatch.setattr(apply_job, "openclaw_command", lambda: ["openclaw"])
    monkeypatch.setattr(apply_job.subprocess, "run", fake_run)
    assert apply_job.run_openclaw_browser(["snapshot"])[0] is False
    assert len(calls) == 1


def test_closed_job_is_not_treated_as_external(apply_env, insert_job, job_field, isolated_workspace):
    apply_env('heading "AI Engineer"\nparagraph [ref=e3]: Não aceita candidaturas agora\ntext: Join us! Apply now!')
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "closed"
    assert job_field(job_id) == "skipped"
    assert "encerrada" in job_field(job_id, "error_log")
    assert quota_today() == 0


def test_apply_word_in_description_is_not_external():
    page = 'heading "AI Engineer"\n- text: When you apply for this position... Apply now!\n- button "Salvar vaga" [ref=e2]'
    assert not apply_job.is_external_apply(page)
    assert apply_job.is_external_apply('- link "Candidatar-se no site da empresa" [ref=f32e183] [cursor=pointer]:')
    assert apply_job.is_external_apply('  - button "Apply" [ref=e9]')


def test_dialog_section_ignores_page_background():
    page = "\n".join([
        "- generic [ref=e1]:",
        "  - dialog [active] [ref=e2]:",
        '    - heading "Candidate-se à empresa Acme" [ref=e3]',
        '    - button "Avançar" [ref=e4]',
        "  - generic [ref=e5]:",
        '    - link "Avalie seu currículo" [ref=e6]',
        '    - textbox "Pesquisar" [ref=e7]',
    ])
    modal = apply_job.dialog_section(page)
    assert "Avançar" in modal
    assert "currículo" not in modal and "Pesquisar" not in modal
    assert apply_job.dialog_section("sem modal") == "sem modal"


def test_upload_uses_button_ref_inside_iframe(isolated_workspace, monkeypatch, tmp_path):
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.7" + b"0" * 2000)
    calls = []

    def fake_browser(args, timeout=45):
        calls.append(args)
        if args[0] == "snapshot":
            return True, 'radio "Curriculo_Teste.pdf" [checked]'
        return True, "upload armed for 1 file(s)"

    monkeypatch.setattr(apply_job, "run_openclaw_browser", fake_browser)
    monkeypatch.setattr(apply_job, "OPENCLAW_UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(apply_job.time, "sleep", lambda s: None)
    modal = '- paragraph: Currículo*\n- button "Carregar currículo" [ref=f183e1149] [cursor=pointer]'

    assert apply_job.attach_resume(pdf, "Curriculo_Teste.pdf", modal)
    upload = [c for c in calls if c[0] == "upload"][0]
    assert upload[2:4] == ["--ref", "f183e1149"]


class SlowBrowser(FakeBrowser):
    """Antes de mostrar a tela certa, cada posição devolve as leituras 'atrasadas' de `loading`."""

    def __init__(self, screens, loading, **kwargs):
        super().__init__(screens, **kwargs)
        self.loading = {pos: list(snaps) for pos, snaps in loading.items()}

    def __call__(self, args, timeout=45):
        if args[0] == "snapshot" and self.loading.get(self.position):
            self.calls.append(args)
            return True, self.loading[self.position].pop(0)
        return super().__call__(args, timeout)


@pytest.fixture
def slow_env(apply_env, monkeypatch):
    def use(screens, loading, **kwargs):
        apply_env(*screens)
        browser = SlowBrowser(screens, loading, **kwargs)
        monkeypatch.setattr(apply_job, "run_openclaw_browser", browser)
        return browser
    return use


HALF_LOADED = 'heading "Senior Applied AI Engineer"\nbutton "Salvar" [ref=e90]'
EXTERNAL_FIRST = 'heading "Senior Applied AI Engineer"\nbutton "Candidatar-se" [ref=e91]'


def test_waits_for_job_page_before_deciding(slow_env, insert_job, job_field):
    browser = slow_env(FULL_FLOW, {0: [HALF_LOADED, HALF_LOADED]})
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "success"
    assert job_field(job_id) == "applied"
    assert browser.clicked("e1")


def test_external_button_seen_once_is_not_trusted(slow_env, insert_job, job_field):
    """O botão genérico apareceu antes do Easy Apply: a vaga não pode ser pulada como externa."""
    browser = slow_env(FULL_FLOW, {0: [EXTERNAL_FIRST]})
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "success"
    assert job_field(job_id, "apply_type") == "EASY_APPLY"
    assert not browser.clicked("e91")


def test_page_that_never_loads_fails_without_clicking(slow_env, insert_job, job_field):
    browser = slow_env((HALF_LOADED,), {})
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "failed"
    assert len(browser.commands("snapshot")) == apply_job.PAGE_READY_ATTEMPTS
    assert not browser.commands("click")


def test_waits_for_modal_to_open(slow_env, insert_job, job_field):
    """Depois do clique em Easy Apply a página ainda aparece sem o formulário por um instante."""
    browser = slow_env(FULL_FLOW, {1: [JOB_PAGE, JOB_PAGE]})
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "success"
    assert browser.calls.count(["click", "e1"]) == 1, "Não pode clicar de novo em Easy Apply com o formulário abrindo"


def test_waits_for_uploaded_file_to_show(apply_env, insert_job, job_field, monkeypatch):
    browser = apply_env(*FULL_FLOW)
    real_current, delay = browser.current, {"left": 2}

    def late_current():
        if browser.uploaded and delay["left"] > 0:
            delay["left"] -= 1
            return browser.screens[browser.position]
        return real_current()

    monkeypatch.setattr(browser, "current", late_current)
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "success"


def test_modal_only_acts_on_what_the_perceiver_reports(apply_env, insert_job, job_field, monkeypatch):
    """O botão Enviar existe na tela, mas o leitor não o reportou: não pode ser clicado."""
    class NoSubmit(apply_job.A11yPerceiver):
        def observe(self, snapshot):
            obs = super().observe(snapshot)
            obs.buttons.pop("submit", None)
            return obs

    monkeypatch.setattr(apply_job, "A11yPerceiver", NoSubmit)
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "failed"
    assert not browser.clicked("e8")
    assert job_field(job_id) == "failed"
