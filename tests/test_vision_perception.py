"""
tests/test_vision_perception.py
Leitor por visão (plano B) com o Claude simulado: nenhum teste chama a API.
Regra coberta: a visão só pode parar a etapa; nunca cria ação, nunca muda refs e nunca derruba a candidatura.
"""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import apply_job
import queue_guard
import vision_perception
from vision_perception import VisionAssistedPerceiver, VisionObs, VisionPerceiver, merge
from test_apply_job import FULL_FLOW, FakeBrowser, apply_env, quota_today  # noqa: F401  (apply_env é fixture)
from test_modal_questions import ENGLISH, RELOCATE, STEP

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def vfield(label, kind="select", category="screening", options=(), required=True, filled=False, confidence="high"):
    return {"label": label, "kind": kind, "category": category, "options": list(options),
            "required": required, "filled": filled, "confidence": confidence}


def seen(fields=(), submit=False, next_=True, errors=()):
    return {"fields": list(fields), "submit_visible": submit, "next_visible": next_, "errors": list(errors)}


AGREES = seen([vfield(ENGLISH), vfield(RELOCATE, kind="radio", options=["Sim", "Não"])])


class FakeClaude:
    """Imita client.beta.messages.create: devolve o JSON combinado ou levanta o erro combinado."""

    def __init__(self, payload=None, stop_reason="end_turn", error=None, text=None):
        self.payload, self.stop_reason, self.error, self.text = payload, stop_reason, error, text
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        text = self.text if self.text is not None else json.dumps(self.payload)
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)])


def a11y(snapshot=STEP):
    return apply_job.A11yPerceiver().observe(snapshot)


def api_error(status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx2.Response(status, request=request)


# ---------- VisionPerceiver: resposta do modelo -> observação ou None ----------

def test_valid_response_becomes_observation():
    client = FakeClaude(AGREES)
    obs = VisionPerceiver(client=client).observe(PNG, STEP)

    assert isinstance(obs, VisionObs)
    assert [f.label for f in obs.fields] == [ENGLISH, RELOCATE]
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["messages"][0]["content"][0]["type"] == "image"
    assert STEP in request["messages"][0]["content"][1]["text"]
    assert "tools" not in request, "A visão só observa: o modelo não recebe nenhuma ferramenta de ação"


@pytest.mark.parametrize("client", [
    FakeClaude(text="isto não é json"),
    FakeClaude({"fields": [{"label": "sem os outros campos"}], "submit_visible": False, "next_visible": True, "errors": []}),
    FakeClaude(seen([vfield("X", kind="slider")])),
    FakeClaude(AGREES, stop_reason="refusal"),
    FakeClaude(AGREES, stop_reason="max_tokens"),
])
def test_invalid_or_incomplete_response_is_discarded(client):
    assert VisionPerceiver(client=client).observe(PNG, STEP) is None


def test_api_failures_are_swallowed():
    errors = [
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
        anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")),
        anthropic.AuthenticationError("chave inválida", response=api_error(401), body=None),
        anthropic.RateLimitError("limite", response=api_error(429), body=None),
        anthropic.InternalServerError("erro", response=api_error(500), body=None),
    ]
    for error in errors:
        assert VisionPerceiver(client=FakeClaude(error=error)).observe(PNG, STEP) is None


def test_missing_or_huge_image_never_calls_the_api():
    client = FakeClaude(AGREES)
    perceiver = VisionPerceiver(client=client)
    assert perceiver.observe(b"", STEP) is None
    assert perceiver.observe(b"0" * (vision_perception.MAX_IMAGE_BYTES + 1), STEP) is None
    assert client.requests == []


# ---------- merge: a árvore vence, a visão só acrescenta motivos para parar ----------

def test_agreement_keeps_a11y_untouched():
    base = a11y()
    merged = merge(base, VisionObs.model_validate(AGREES))

    assert merged.blockers == []
    assert merged.fields == base.fields and merged.buttons == base.buttons
    assert merged.source == "merged"


def test_no_vision_returns_a11y_as_is():
    base = a11y()
    assert merge(base, None) is base


def test_required_field_only_vision_sees_blocks_the_step():
    merged = merge(a11y(), VisionObs.model_validate(seen(AGREES["fields"] + [vfield("Aceita os termos de uso?", kind="checkbox")])))

    assert len(merged.blockers) == 1 and "Aceita os termos de uso?" in merged.blockers[0]
    assert [f.label for f in merged.fields] == [ENGLISH, RELOCATE], "Campo visto só na imagem nunca vira campo preenchível"


def test_required_field_classified_as_other_also_blocks():
    """Visto em chamada real: o modelo classificou um aceite de termos obrigatório como 'other'."""
    terms = vfield("Declaro que li e aceito os termos do processo seletivo", kind="checkbox", category="other")
    merged = merge(a11y(), VisionObs.model_validate(seen(AGREES["fields"] + [terms])))
    assert len(merged.blockers) == 1 and "aceito os termos" in merged.blockers[0]


@pytest.mark.parametrize("extra", [
    vfield("Comentários adicionais", kind="text", required=False),
    vfield("Cidade", kind="typeahead", filled=True),
    vfield("Algo borrado no canto", kind="unknown", confidence="low"),
    vfield("Telefone celular", kind="text", category="contact"),
    vfield("Seguir a empresa", kind="checkbox", category="follow_company", filled=True),
])
def test_optional_filled_low_confidence_or_non_screening_fields_do_not_block(extra):
    merged = merge(a11y(), VisionObs.model_validate(seen(AGREES["fields"] + [extra])))
    assert merged.blockers == []


def test_kind_disagreement_blocks():
    merged = merge(a11y(), VisionObs.model_validate(seen([vfield(ENGLISH, kind="checkbox")])))
    assert len(merged.blockers) == 1 and "divergente" in merged.blockers[0]


def test_radio_option_disagreement_blocks():
    merged = merge(a11y(), VisionObs.model_validate(seen([vfield(RELOCATE, kind="radio", options=["Sim", "Não", "Talvez"])])))
    assert len(merged.blockers) == 1 and "Opções divergentes" in merged.blockers[0]


def test_closed_select_without_visible_options_is_not_a_disagreement():
    merged = merge(a11y(), VisionObs.model_validate(seen([vfield(ENGLISH, options=[])])))
    assert merged.blockers == []


def test_submit_seen_only_in_image_blocks_and_adds_no_ref():
    base = a11y()
    merged = merge(base, VisionObs.model_validate(seen(AGREES["fields"], submit=True)))

    assert any("botão de enviar" in b for b in merged.blockers)
    assert "submit" not in merged.buttons


def test_label_matching_tolerates_asterisk_accents_and_extra_hint():
    fields = [vfield("Qual seu nivel de proficiencia em ingles? *"),
              vfield(RELOCATE + " (obrigatório)", kind="radio", options=["Sim", "Não"])]
    assert merge(a11y(), VisionObs.model_validate(seen(fields))).blockers == []


# ---------- VisionAssistedPerceiver e fluxo completo ----------

def test_assisted_perceiver_survives_screenshot_and_vision_failures():
    base = a11y()

    def broken_screenshot():
        raise OSError("sem navegador")

    no_image = VisionAssistedPerceiver(apply_job.A11yPerceiver(), VisionPerceiver(client=FakeClaude(AGREES)), lambda: None)
    crashed = VisionAssistedPerceiver(apply_job.A11yPerceiver(), VisionPerceiver(client=FakeClaude(AGREES)), broken_screenshot)

    for perceiver in (no_image, crashed):
        obs = perceiver.observe(STEP)
        assert obs.fields == base.fields and obs.blockers == []


def use_vision(monkeypatch, client):
    monkeypatch.setenv("VISION_FALLBACK", "1")
    monkeypatch.setattr(vision_perception, "VisionPerceiver", lambda: VisionPerceiver(client=client))
    monkeypatch.setattr(apply_job, "take_screenshot", lambda: PNG)


def test_flow_with_agreeing_vision_applies_normally(apply_env, insert_job, job_field, monkeypatch):
    client = FakeClaude(seen([]))
    use_vision(monkeypatch, client)
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "success"
    assert browser.clicked("e8")
    assert len(client.requests) == 3, "Uma leitura por etapa do formulário (currículo, pergunta, revisão)"


def test_flow_stops_before_any_action_when_vision_blocks(apply_env, insert_job, job_field, monkeypatch, isolated_workspace):
    client = FakeClaude(seen([vfield("Você aceita os termos da vaga?", kind="checkbox")]))
    use_vision(monkeypatch, client)
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    result = apply_job.apply_to_job(job_id)

    assert result["status"] == "failed" and "Você aceita os termos da vaga?" in result["message"]
    assert job_field(job_id) == "failed"
    assert not browser.clicked("e8") and not browser.commands("type")
    assert quota_today() == 0


def test_flow_with_broken_api_behaves_like_no_vision(apply_env, insert_job, job_field, monkeypatch):
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    use_vision(monkeypatch, FakeClaude(error=error))
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id)["status"] == "success"
    assert job_field(job_id) == "applied"


def test_dry_run_with_vision_never_submits(apply_env, insert_job, job_field, monkeypatch):
    use_vision(monkeypatch, FakeClaude(seen([], submit=False)))
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    assert apply_job.apply_to_job(job_id, dry_run=True)["status"] == "dry_run"
    assert not browser.clicked("e8")
    assert job_field(job_id) == "approved"


def test_vision_is_off_by_default(apply_env, insert_job, monkeypatch):
    def explode():
        raise AssertionError("A visão não pode ser construída com VISION_FALLBACK desligado")

    monkeypatch.setattr(vision_perception, "VisionPerceiver", explode)
    apply_env(*FULL_FLOW)
    assert apply_job.apply_to_job(insert_job())["status"] == "success"
    assert isinstance(apply_job.build_perceiver(), apply_job.A11yPerceiver)


def test_missing_sdk_or_credentials_falls_back_to_a11y(monkeypatch):
    def no_credentials():
        raise RuntimeError("sem ANTHROPIC_API_KEY")

    monkeypatch.setenv("VISION_FALLBACK", "1")
    monkeypatch.setattr(vision_perception, "VisionPerceiver", no_credentials)
    assert isinstance(apply_job.build_perceiver(), apply_job.A11yPerceiver)


def test_screenshot_path_is_read_from_cli_output(tmp_path, monkeypatch):
    shot = tmp_path / "shot one.png"
    shot.write_bytes(PNG)
    monkeypatch.setattr(apply_job, "run_openclaw_browser", lambda args, timeout=45: (True, f"MEDIA:{shot}\n"))
    assert apply_job.take_screenshot() == PNG

    monkeypatch.setattr(apply_job, "run_openclaw_browser", lambda args, timeout=45: (False, "browser not running"))
    assert apply_job.take_screenshot() is None


def test_dry_run_reports_blockers_without_failing_the_job(apply_env, insert_job, job_field, monkeypatch, isolated_workspace):
    use_vision(monkeypatch, FakeClaude(seen([vfield("Você aceita os termos da vaga?", kind="checkbox")])))
    browser = apply_env(*FULL_FLOW)
    job_id = insert_job()

    result = apply_job.apply_to_job(job_id, dry_run=True)

    assert result["status"] == "dry_run" and "Você aceita os termos da vaga?" in result["blockers"][0]
    assert job_field(job_id) == "approved", "Dry-run não tira a vaga da fila"
    assert not browser.clicked("e8")


def test_screenshot_path_relative_to_home_is_expanded(tmp_path, monkeypatch):
    """Formato real do OpenClaw 2026.10.5: caminho que começa em ~ (pasta do usuário)."""
    media = tmp_path / ".openclaw" / "media" / "browser"
    media.mkdir(parents=True)
    (media / "0c4657ce.png").write_bytes(PNG)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    output = r"~\.openclaw\media\browser\0c4657ce.png"
    monkeypatch.setattr(apply_job, "run_openclaw_browser", lambda args, timeout=45: (True, output))
    assert apply_job.take_screenshot() == PNG
