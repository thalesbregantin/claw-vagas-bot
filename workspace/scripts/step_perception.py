#!/usr/bin/env python3
"""
workspace/scripts/step_perception.py
Percepção de uma etapa do formulário Easy Apply: transforma o snapshot de acessibilidade do
OpenClaw em uma observação estruturada (campos, botões, estado).

Este módulo só lê. Não clica, não digita e não decide respostas: quem age é o apply_job.py,
sempre pelo `ref` do elemento e com as travas do queue_guard.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RESUME_STEP_KEYWORDS = ["carregar currículo", "upload resume", "enviar currículo", "currículo", "resume"]
UPLOAD_BUTTON_LABELS = ["carregar currículo", "upload resume", "enviar currículo"]
CONTACT_FIELD_KEYWORDS = ["e-mail", "email", "telefone", "phone", "celular", "mobile", "nome", "name",
                          "pesquisar", "search", "código do país", "country code", "endereço", "address"]
VALIDATION_ERROR_KEYWORDS = ["este campo é obrigatório", "campo obrigatório", "this field is required",
                             "insira uma resposta", "please enter", "enter a valid", "insira um número",
                             "selecione uma opção", "please make a selection"]
PLACEHOLDER_OPTIONS = {"selecionar opção", "select an option", "selecione uma opção"}


def find_button_ref(snapshot: str, keywords: List[str]) -> Optional[str]:
    """Extrai com máxima precisão o ref de um botão no snapshot do OpenClaw."""
    for line in snapshot.splitlines():
        line_clean = line.strip()
        if "button" not in line_clean.lower():
            continue
        line_lower = line_clean.lower()
        for kw in keywords:
            if kw.lower() in line_lower:
                m = re.search(r'ref=([a-zA-Z0-9_]+)', line_clean)
                if m:
                    return m.group(1)
    return None


def dialog_section(snapshot: str) -> str:
    """Recorta o modal ativo. O snapshot traz a página inteira, e o fundo tem 'currículo', 'Pesquisar' etc."""
    lines = snapshot.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*-\s*dialog\b", line):
            indent = len(line) - len(line.lstrip())
            block = [line]
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                block.append(nxt)
            return "\n".join(block)
    return snapshot


FOLLOW_KEYWORDS = ("siga a empresa", "seguir", "follow")


def find_follow_checkbox_ref(modal: str) -> Optional[str]:
    """Ref para desmarcar 'Siga a empresa'. O checkbox do LinkedIn não tem ref: clica no rótulo logo abaixo."""
    lines = modal.splitlines()
    for i, line in enumerate(lines):
        lowered = line.lower()
        if "checkbox" not in lowered or "[checked]" not in lowered or not any(k in lowered for k in FOLLOW_KEYWORDS):
            continue
        own = re.search(r"ref=([a-zA-Z0-9_]+)", line)
        if own:
            return own.group(1)
        for nxt in lines[i + 1:i + 3]:
            m = re.search(r"ref=([a-zA-Z0-9_]+)", nxt)
            if m and any(k in nxt.lower() for k in FOLLOW_KEYWORDS):
                return m.group(1)
    return None


def has_dialog(snapshot: str) -> bool:
    return re.search(r"^\s*(?:-\s*)?dialog\b", snapshot, re.MULTILINE) is not None


def clean_label(label: str) -> str:
    return " ".join(label.replace("*", " ").split())


def is_contact_field(label: str) -> bool:
    return any(k in label.lower() for k in CONTACT_FIELD_KEYWORDS)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_select_questions(modal: str) -> List[Dict[str, Any]]:
    """Listas (combobox/select): rótulo, ref, opções reais e a opção já marcada."""
    lines = modal.splitlines()
    questions = []
    for i, line in enumerate(lines):
        m = re.match(r'\s*-\s*(?:combobox|listbox)\s+"([^"]+)"[^\n]*?ref=([a-zA-Z0-9_]+)', line)
        if not m:
            continue
        options, selected = [], None
        for nxt in lines[i + 1:]:
            if _indent(nxt) <= _indent(line):
                break
            om = re.match(r'\s*-\s*option\s+"([^"]*)"(.*)', nxt)
            if not om or om.group(1).strip().lower() in PLACEHOLDER_OPTIONS:
                continue
            options.append(om.group(1).strip())
            if "[selected]" in om.group(2):
                selected = om.group(1).strip()
        questions.append({"kind": "select", "label": clean_label(m.group(1)), "ref": m.group(2),
                          "options": options, "selected": selected})
    return questions


def parse_radio_questions(modal: str) -> List[Dict[str, Any]]:
    """Grupos de opção (radio). O LinkedIn põe o texto da opção num parágrafo logo após cada radio."""
    lines = modal.splitlines()
    questions = []
    for i, line in enumerate(lines):
        if not re.match(r'\s*-\s*radiogroup\b', line):
            continue
        label = ""
        for prev in reversed(lines[max(0, i - 4):i]):
            pm = re.match(r'\s*-\s*(?:paragraph|generic|legend|text)[^:]*:\s*(.+)', prev)
            if pm and pm.group(1).strip():
                label = pm.group(1).strip()
                break
        options, refs, selected, pending_checked, names = [], {}, None, None, []
        for nxt in lines[i + 1:]:
            if _indent(nxt) <= _indent(line):
                break
            rm = re.match(r'\s*-\s*radio\s+"([^"]*)"(.*)', nxt)
            if rm:
                names.append(rm.group(1))
                label = label or rm.group(1)
                pending_checked = "[checked]" in rm.group(2)
                continue
            pm = re.match(r'\s*-\s*(?:paragraph|generic|text)\s*\[ref=([a-zA-Z0-9_]+)\][^:]*:\s*(.+)', nxt)
            if pm and pending_checked is not None:
                text = pm.group(2).replace("[new]", "").strip()
                options.append(text)
                refs[text] = pm.group(1)
                if pending_checked:
                    selected = text
                pending_checked = None
        # A lista de currículos já enviados também é um radiogroup: não é pergunta de triagem
        is_resume_list = any(re.search(r"\.(pdf|docx?)$", n.strip(), re.IGNORECASE) for n in names)
        if options and not is_resume_list:
            questions.append({"kind": "radio", "label": clean_label(label), "refs": refs,
                              "options": options, "selected": selected})
    return questions


def validation_error(modal: str) -> Optional[str]:
    lowered = modal.lower()
    for k in VALIDATION_ERROR_KEYWORDS:
        if k in lowered:
            return k
    return None


SUBMIT_LABELS = ["enviar candidatura", "submit application"]
NEXT_LABELS = ["avaliar", "revisar", "review", "avançar", "next", "continuar"]


@dataclass
class FieldObs:
    """Um campo de triagem da etapa. `kind`: "text", "select" ou "radio"."""
    label: str
    kind: str
    options: List[str] = field(default_factory=list)
    ref: Optional[str] = None                                   # caixa de texto ou lista
    option_refs: Dict[str, str] = field(default_factory=dict)   # radio: opção -> ref
    selected: Optional[str] = None
    confidence: float = 1.0
    source: str = "a11y"


@dataclass
class StepObs:
    """O que existe em uma etapa do formulário."""
    fields: List[FieldObs]
    buttons: Dict[str, str]          # "upload" | "next" | "submit" -> ref
    follow_ref: Optional[str]
    blockers: List[str] = field(default_factory=list)   # motivos para parar a etapa sem agir
    source: str = "a11y"


def is_resume_step(snapshot: str) -> bool:
    lowered = snapshot.lower()
    return any(k in lowered for k in RESUME_STEP_KEYWORDS)


class A11yPerceiver:
    """Lê a etapa pela árvore de acessibilidade. É a fonte de verdade: tudo o que vira ação sai daqui."""

    def observe(self, snapshot: str) -> StepObs:
        buttons = {}
        for name, labels in (("upload", UPLOAD_BUTTON_LABELS), ("next", NEXT_LABELS), ("submit", SUBMIT_LABELS)):
            ref = find_button_ref(snapshot, labels)
            if ref:
                buttons[name] = ref
        return StepObs(fields=self.fields(snapshot), buttons=buttons, follow_ref=find_follow_checkbox_ref(snapshot))

    def fields(self, snapshot: str) -> List[FieldObs]:
        """Perguntas de triagem na ordem da tela: caixas de texto, listas e grupos de opção.
        Campos de contato e listas sem opções ficam de fora."""
        found: List[FieldObs] = []
        for raw_label, ref in re.findall(r'textbox[^"\n]*"([^"\n]+)"[^\n]*ref=([a-zA-Z0-9_]+)', snapshot):
            label = clean_label(raw_label)
            if not is_contact_field(label):
                found.append(FieldObs(label=label, kind="text", ref=ref))
        for q in parse_select_questions(snapshot) + parse_radio_questions(snapshot):
            if not q["label"] or is_contact_field(q["label"]) or not q["options"]:
                continue
            found.append(FieldObs(label=q["label"], kind=q["kind"], options=q["options"], ref=q.get("ref"),
                                  option_refs=q.get("refs", {}), selected=q["selected"]))
        return found
