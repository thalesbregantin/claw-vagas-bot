#!/usr/bin/env python3
"""
workspace/scripts/apply_job.py
Motor de Execução de Candidatura Assistida Easy Apply no LinkedIn via OpenClaw Browser Relay.

Regras e Guardrails Inegociáveis (SOUL.md & AGENTS.md):
1. Só executa vagas com status 'approved' (reserva atômica via queue_guard.claim_job).
2. Valida travas anti-ban via queue_guard.py (cota diária 15/dia, horário 08h-20h, jitter).
3. Gera e valida o currículo PDF da vaga em output/resumes/<job_id>.pdf.
4. Anexa o PDF no modal; sem anexo confirmado, não envia.
5. Responde perguntas eliminatórias via queue_guard.resolve_screening_question.
6. Trava needs_input caso encontre qualquer pergunta não prevista (Zero Alucinação).
7. Desmarca opção 'Seguir empresa', submete e só registra 'applied' com a confirmação na tela.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import queue_guard
import question_similarity
import tailor_resume
from step_perception import (  # noqa: F401  (reexportados: os testes e o bot usam via apply_job)
    A11yPerceiver, FieldObs, StepObs, clean_label, dialog_section, find_button_ref, find_follow_checkbox_ref,
    has_dialog, is_contact_field, is_resume_step, parse_radio_questions, parse_select_questions, validation_error,
    RESUME_STEP_KEYWORDS, UPLOAD_BUTTON_LABELS,
)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ["OPENCLAW_DB_PATH"]) if os.environ.get("OPENCLAW_DB_PATH") else (WORKSPACE_ROOT / "db" / "jobs.db")
RESUMES_DIR = WORKSPACE_ROOT / "output" / "resumes"
OPENCLAW_UPLOADS_DIR = Path(tempfile.gettempdir()) / "openclaw" / "uploads"

MAX_MODAL_STEPS = 8
CONFIRMATION_CHECKS = 4
CONFIRMATION_KEYWORDS = ["candidatura enviada", "application submitted", "sua candidatura foi enviada", "application was sent"]
ALREADY_APPLIED_PATTERN = r"\bcandidatou-se\b|\bapplied\b[^\n]{0,30}\bago\b|\bsee application\b|\bver candidatura\b"



def send_telegram(text: str, reply_markup: Optional[Dict] = None) -> Optional[Dict]:
    return common.send_message(common.authorized_chat_id(), text, reply_markup=reply_markup)


def send_telegram_document(file_path: Path, caption: str = "", filename: Optional[str] = None) -> bool:
    return common.send_document(common.authorized_chat_id(), file_path, caption, filename)


def openclaw_command() -> List[str]:
    """Resolve o CLI sem cmd.exe: no Windows o shim .cmd reinterpretaria '&' e aspas."""
    exe = shutil.which("openclaw")
    if not exe:
        raise RuntimeError("CLI 'openclaw' não encontrado no PATH.")
    if os.name != "nt":
        return [exe]

    base_dir = Path(exe).parent
    entrypoint = base_dir / "node_modules" / "openclaw" / "openclaw.mjs"
    if not entrypoint.exists():
        raise RuntimeError(f"Entrypoint do OpenClaw não encontrado em {entrypoint}.")
    node = base_dir / "node.exe"
    return [str(node) if node.exists() else (shutil.which("node") or "node"), str(entrypoint)]


TRANSIENT_BROWSER_ERRORS = ("frame changed", "; retry", "navigation in progress", "execution context was destroyed")
SNAPSHOT_RETRIES = 4


def run_openclaw_browser(cmd_args: List[str], timeout: int = 45) -> Tuple[bool, str]:
    """Executa comando no openclaw browser. Snapshot durante carregamento da página é repetido."""
    attempts = SNAPSHOT_RETRIES if cmd_args[:1] == ["snapshot"] else 1
    for attempt in range(attempts):
        ok, out = _run_openclaw_once(cmd_args, timeout)
        if ok or not any(e in out.lower() for e in TRANSIENT_BROWSER_ERRORS) or attempt == attempts - 1:
            return ok, out
        print(f"  ⏳ Página ainda carregando; nova leitura ({attempt + 2}/{attempts})...", flush=True)
        time.sleep(2)
    return ok, out


def _run_openclaw_once(cmd_args: List[str], timeout: int) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(
            openclaw_command() + ["browser"] + cmd_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, out.strip()
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        return False, str(e)


def is_linkedin_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "linkedin.com" or host.endswith(".linkedin.com"))


def is_already_applied(snapshot: str) -> bool:
    """Procura o selo do LinkedIn. A palavra 'Applied' sozinha aparece em cargos como 'Applied AI Engineer'."""
    return re.search(ALREADY_APPLIED_PATTERN, snapshot, re.IGNORECASE) is not None


CLOSED_JOB_KEYWORDS = ("não aceita candidaturas", "no longer accepting applications", "vaga não está mais disponível",
                       "this job is no longer available", "candidaturas encerradas")
EASY_APPLY_LABELS = ["candidatura simplificada", "easy apply"]
EXTERNAL_APPLY_LABELS = ("candidatar-se no site da empresa", "apply on company website", "candidatar-se", "candidate-se", "apply")


def is_closed_job(snapshot: str) -> bool:
    lowered = snapshot.lower()
    return any(k in lowered for k in CLOSED_JOB_KEYWORDS)


def is_external_apply(snapshot: str) -> bool:
    """Só botões e links contam: a descrição da vaga costuma ter 'apply now'."""
    for line in snapshot.splitlines():
        m = re.match(r'\s*(?:-\s*)?(button|link)\s+"([^"]*)"', line, re.IGNORECASE)
        if not m:
            continue
        label = m.group(2).strip().lower()
        if any(label == l or label.startswith(l + " ") for l in EXTERNAL_APPLY_LABELS):
            return True
    return False


PAGE_READY_ATTEMPTS = 6
PAGE_READY_DELAY = 2


def wait_for_snapshot(ready: Callable[[str], bool], attempts: int = PAGE_READY_ATTEMPTS,
                      delay: float = PAGE_READY_DELAY) -> Tuple[bool, str]:
    """Lê a página até `ready(snapshot)` valer. Leitura com erro ou página pela metade repete.
    Esgotadas as tentativas, devolve a última leitura e quem chamou decide o que fazer."""
    ok, out = False, ""
    for attempt in range(attempts):
        ok, out = run_openclaw_browser(["snapshot"])
        if ok and ready(out):
            return True, out
        if attempt < attempts - 1:
            print(f"  ⏳ Página ainda não está pronta; nova leitura ({attempt + 2}/{attempts})...", flush=True)
            time.sleep(delay)
    return ok, out


def job_page_state(snapshot: str) -> Optional[str]:
    """Estado da candidatura visível na página da vaga, ou None se ela ainda não mostrou nenhum."""
    if is_already_applied(snapshot):
        return "applied"
    if find_button_ref(snapshot, EASY_APPLY_LABELS):
        return "easy_apply"
    if is_closed_job(snapshot):
        return "closed"
    if is_external_apply(snapshot):
        return "external"
    return None


def read_job_page() -> Tuple[bool, str]:
    """Espera a página da vaga mostrar o estado da candidatura antes de decidir.
    'Externa' só vale vista em duas leituras seguidas: o botão Easy Apply pode aparecer depois do resto."""
    seen_external = [False]

    def ready(snapshot: str) -> bool:
        state = job_page_state(snapshot)
        confirmed = state in ("applied", "easy_apply", "closed") or (state == "external" and seen_external[0])
        seen_external[0] = state == "external"
        return confirmed

    return wait_for_snapshot(ready)


def read_modal() -> Tuple[bool, str]:
    """Espera o formulário (dialog) aparecer. Sem ele, a leitura pegaria o fundo da página."""
    return wait_for_snapshot(has_dialog)


def close_job(job: sqlite3.Row) -> Dict[str, Any]:
    """Vaga encerrada pelo anunciante: sai da fila sem consumir a cota."""
    job_id = job["job_id"]
    reason = "Vaga encerrada: o LinkedIn não aceita mais candidaturas."
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE jobs SET status = 'skipped', error_log = ? WHERE job_id = ?", (reason, job_id))
    conn.execute("""
        INSERT INTO application_logs (job_id, step, status, message)
        VALUES (?, 'easy_apply_step', 'info', ?)
    """, (job_id, reason))
    conn.commit()
    conn.close()
    send_telegram(f"🔒 *{job['title']}* — *{job['company']}* foi encerrada pelo anunciante. Tirei da fila.")
    return {"status": "closed", "job_id": job_id, "message": reason}


def is_confirmed(snapshot: str) -> bool:
    lowered = snapshot.lower()
    return any(k in lowered for k in CONFIRMATION_KEYWORDS)


def resume_filename(profile: Dict[str, Any]) -> str:
    """Nome que o recrutador vê: sem cargo e sem ID interno."""
    name = profile.get("personal", {}).get("display_name", "Candidato")
    return "Curriculo_" + re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_") + ".pdf"


def stage_resume_for_upload(pdf_path: Path, filename: str) -> Path:
    """O CLI só aceita arquivos dentro da pasta de uploads do OpenClaw."""
    dest_dir = OPENCLAW_UPLOADS_DIR / uuid.uuid4().hex
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    shutil.copyfile(pdf_path, dest)
    return dest


def attach_resume(pdf_path: Path, filename: str, modal_snapshot: str = "") -> bool:
    """Anexa o PDF e confirma que o nome do arquivo apareceu no modal.
    O modal do LinkedIn fica num iframe: o seletor CSS não o alcança, então usa o ref do botão de carregar."""
    staged = stage_resume_for_upload(pdf_path, filename)
    upload_ref = find_button_ref(modal_snapshot, UPLOAD_BUTTON_LABELS)
    target = ["--ref", upload_ref] if upload_ref else ["--element", "input[type='file']"]
    ok, out = run_openclaw_browser(["upload", str(staged), *target, "--timeout-ms", "20000"], timeout=60)
    if not ok:
        print(f"⚠️ Falha no upload do currículo: {out}", flush=True)
        return False
    time.sleep(2)
    ok, snapshot = wait_for_snapshot(lambda s: filename.lower() in s.lower())
    return ok and filename.lower() in snapshot.lower()


def prepare_resume(job_id: str) -> Optional[Path]:
    """Gera o PDF com o template atual e valida o arquivo antes de abrir o navegador."""
    try:
        tailor_resume.process_job_tailoring(job_id, force_pdf=True)
    except Exception as e:
        print(f"⚠️ Falha ao gerar currículo de {job_id}: {e}", flush=True)
        return None
    return RESUMES_DIR / f"{job_id}.pdf" if queue_guard.verify_resume(job_id) else None


def build_external_kit(profile: Dict[str, Any], title: str, description: str) -> Dict[str, str]:
    """Monta textos para portais externos usando somente fatos de profile.json."""
    keywords = tailor_resume.matched_job_keywords(profile, title, description)
    stack = ", ".join(k.upper() for k in keywords[:5]) or "Python, IA e automação"

    highlights = []
    for experience in profile.get("experience", [])[:2]:
        highlights.extend(experience.get("achievements", [])[:1])

    return {
        "pitch": profile.get("personal", {}).get("headline", ""),
        "motivation": f"Tenho interesse na posição de {title} porque ela usa tecnologias com as quais trabalho: {stack}.",
        "highlights": "\n".join(f"• {h}" for h in highlights)
    }


def external_apply_link(job: sqlite3.Row) -> Optional[str]:
    """Telegram recusa botões com URL inválida; usa só links http(s)."""
    for url in (job["apply_url"], job["job_url"]):
        if url and str(url).strip().lower().startswith(("http://", "https://")):
            return str(url).strip()
    return None


def skip_external(job: sqlite3.Row) -> Dict[str, Any]:
    """Foco em Easy Apply: vaga externa sai da fila sem kit e sem consumir a cota."""
    job_id = job["job_id"]
    reason = "Candidatura no site da empresa; fora do foco Easy Apply."
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE jobs SET apply_type = 'EXTERNAL_ATS', status = 'skipped', error_log = ?
        WHERE job_id = ?
    """, (reason, job_id))
    conn.execute("""
        INSERT INTO application_logs (job_id, step, status, message)
        VALUES (?, 'easy_apply_step', 'info', ?)
    """, (job_id, reason))
    conn.commit()
    conn.close()
    send_telegram(f"⏭️ *{job['title']}* — *{job['company']}* não tem Easy Apply. Pulei e sigo com a fila.")
    return {"status": "skipped_external", "job_id": job_id, "message": reason}


def deliver_external_kit(job: sqlite3.Row, pdf_path: Optional[Path]) -> Dict[str, Any]:
    """Vaga fora do Easy Apply: entrega PDF e textos no chat; o usuário confirma o envio pelo botão."""
    job_id, title, company = job["job_id"], job["title"], job["company"]
    profile = tailor_resume.load_profile()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE jobs SET apply_type = 'EXTERNAL_ATS', status = 'notified_external' WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()

    if pdf_path:
        send_telegram_document(pdf_path, f"📄 Currículo para *{title}* — *{company}*", resume_filename(profile))
    resume_note = "" if pdf_path else "⚠️ Não consegui gerar o PDF desta vaga. Use seu currículo padrão.\n\n"

    buttons = []
    link = external_apply_link(job)
    if link:
        buttons.append([{"text": "👉 Abrir Vaga", "url": link}])
    buttons.append([{"text": "✅ Já me candidatei", "callback_data": f"ext_applied:{job_id}"}])

    kit = build_external_kit(profile, title, job["description"] or "")
    send_telegram(
        f"🚧 *Candidatura no portal da empresa*\n"
        f"*{title}* — *{company}*\n\n"
        f"Esta vaga precisa ser enviada por você no site da empresa.\n\n"
        f"{resume_note}"
        f"🎯 *Sobre você:*\n`{kit['pitch']}`\n\n"
        f"💡 *Interesse na vaga:*\n`{kit['motivation']}`\n\n"
        f"🛠️ *Destaques do seu histórico:*\n`{kit['highlights']}`\n\n"
        f"Revise os textos antes de colar. Depois de enviar, toque em *Já me candidatei* para acompanhar o resultado.",
        reply_markup={"inline_keyboard": buttons}
    )
    return {"status": "external_ats", "message": "Vaga externa com PDF e textos entregues"}


YES_NO = {"sim": "yes", "yes": "yes", "não": "no", "nao": "no", "no": "no"}


def choose_option(value: Optional[str], options: List[str]) -> Optional[str]:
    """Casa a resposta conhecida com uma opção real do campo. Sem casamento claro, não escolhe."""
    if not value:
        return None
    v = " ".join(str(value).lower().split())
    lowered = {o.lower(): o for o in options}
    if v in lowered:
        return lowered[v]
    if v in YES_NO:
        for o in options:
            if YES_NO.get(o.lower()) == YES_NO[v]:
                return o
    starts = [o for o in options if o.lower().startswith(v) or v.startswith(o.lower())]
    return starts[0] if len(starts) == 1 else None


def suggest_answer(label: str, options: List[str]) -> Optional[Dict[str, str]]:
    """Resposta que o usuário já deu a uma pergunta equivalente, para ele confirmar com um toque.
    É só sugestão: nada é preenchido com ela. Em campo com opções, só vale se casar com uma opção real."""
    learned = queue_guard.read_learned_answers_raw()
    try:
        similar = question_similarity.find_similar(label, list(learned))
    except Exception as e:  # sugestão é conveniência: sem ela, a pergunta segue normalmente
        print(f"⚠️ Sugestão de resposta indisponível: {e}", flush=True)
        return None
    for question in similar:
        answer = choose_option(learned[question], options) if options else learned[question]
        if answer:
            return {"from": question, "answer": answer}
    return None


def ask_user(job_id: str, title: str, company: str, questions: List[Dict[str, Any]]) -> str:
    """Pausa a vaga e manda todas as perguntas sem resposta da etapa de uma vez, uma mensagem por pergunta."""
    for q in questions:
        print(f"⚠️ [NEEDS_INPUT]: {q['question']} {q.get('options') or ''}")
    queue_guard.mark_needs_input(job_id, questions)
    total = len(questions)
    if total > 1:
        send_telegram(
            f"A vaga *{title}* — *{company}* fez *{total} perguntas* sem resposta cadastrada.\n"
            f"Responda cada uma abaixo. Quando todas tiverem resposta, refaço a candidatura desde o início."
        )
    for i, q in enumerate(questions):
        options = q.get("options") or []
        options_text = ("\n\nOpções da vaga: " + " · ".join(options)) if options else ""
        suggestion = q.get("suggestion")
        if suggestion:
            options_text += (f"\n\n💡 Você já respondeu algo parecido:\n_\"{suggestion['from']}\"_ → *{suggestion['answer']}*\n"
                             f"Se for a mesma coisa, toque em *Usar*.")
        if total > 1:
            text = f"❓ *Pergunta {i + 1}/{total}:* *\"{q['question']}\"*{options_text}"
        else:
            text = (f"A vaga *{title}* — *{company}* fez uma pergunta sem resposta cadastrada:\n\n"
                    f"❓ *\"{q['question']}\"*{options_text}\n\n"
                    f"Responda abaixo. Vou guardar a resposta e refazer a candidatura desde o início.")
        send_telegram(text, reply_markup=question_buttons(job_id, q["question"], options, i, suggestion))
    return questions[0]["question"]


def question_buttons(job_id: str, label: str, options: Optional[List[str]] = None, question_index: int = 0,
                     suggestion: Optional[Dict[str, str]] = None) -> Dict:
    """Gera botões de resposta. Com opções reais do campo, o botão leva os índices (limite de 64 bytes do Telegram).
    Havendo sugestão de pergunta parecida, o primeiro botão a aceita; o texto dela fica guardado na pausa."""
    q = question_index
    rows = []
    if suggestion:
        rows.append([{"text": f"✅ Usar: {suggestion['answer'][:45]}", "callback_data": f"anss:{job_id}:{q}"}])
    if options:
        rows += [[{"text": o[:60], "callback_data": f"ansi:{job_id}:{q}:{i}"}] for i, o in enumerate(options[:8])]
    else:
        if any(w in label.lower() for w in ["anos", "years", "experiência", "experience", "tempo"]):
            quick = [["0", "1"], ["2", "3"], ["4", "5"]]
        else:
            quick = [["Sim", "Não"]]
        rows += [[{"text": o, "callback_data": f"ans:{job_id}:{q}:{o}"} for o in row] for row in quick]
    rows.append([{"text": "✍️ Digitar resposta no chat", "callback_data": f"ans_prompt:{job_id}:{q}"}])
    return {"inline_keyboard": rows}


def answer_fields(job_id: str, title: str, company: str, fields: List[FieldObs]) -> Optional[str]:
    """Responde os campos observados da etapa. Retorna a primeira pergunta que exige resposta humana.
    Avalia a etapa inteira antes de mexer na tela: havendo dúvidas, pergunta todas juntas e não preenche nada.
    O pré-preenchimento do LinkedIn vem de candidaturas antigas e pode estar errado: só vale a resposta cadastrada."""
    actions: List[Tuple[List[str], str]] = []
    unknown: List[Dict[str, Any]] = []
    seen = set()

    def add_unknown(label: str, options: List[str]):
        key = queue_guard.normalize_question(label)
        if key not in seen:
            seen.add(key)
            unknown.append({"question": label, "options": options, "suggestion": suggest_answer(label, options)})

    for f in fields:
        has_answer, value = queue_guard.resolve_screening_question(f.label)
        if f.kind == "text":
            if not has_answer:
                add_unknown(f.label, [])
                continue
            # "--" impede que uma resposta iniciada por "-" seja lida como opção do CLI
            actions.append((["type", "--", f.ref, str(value)], f"✍️ Preenchendo campo '{f.label}' (ref={f.ref})"))
            continue
        # Tipo de campo que o bot não sabe preencher nunca é respondido sozinho
        choice = choose_option(value, f.options) if has_answer and f.kind in ("select", "radio") else None
        if not choice:
            add_unknown(f.label, f.options)
            continue
        if f.selected == choice:
            continue
        cmd = ["select", f.ref, choice] if f.kind == "select" else ["click", f.option_refs[choice]]
        actions.append((cmd, f"🔘 '{f.label}' -> '{choice}'"))

    if unknown:
        return ask_user(job_id, title, company, unknown)

    for cmd, log in actions:
        print(log)
        run_openclaw_browser(cmd)
        time.sleep(0.5)
    return None


def fill_screening_fields(job_id: str, title: str, company: str, snapshot: str) -> Optional[str]:
    """Lê os campos da etapa pela árvore de acessibilidade e responde (ver answer_fields)."""
    return answer_fields(job_id, title, company, A11yPerceiver().fields(snapshot))


def fail(job_id: str, message: str) -> Dict[str, Any]:
    queue_guard.mark_failed(job_id, message)
    send_telegram(f"❌ *Candidatura não concluída*\nVaga: `{job_id}`\nMotivo: {message}")
    return {"status": "failed", "message": message}


def vision_enabled() -> bool:
    return os.environ.get("VISION_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")


def take_screenshot() -> Optional[bytes]:
    """Imagem da aba atual para o leitor por visão. Qualquer falha devolve None e a etapa segue só com a árvore."""
    ok, out = run_openclaw_browser(["screenshot", "--type", "png"])
    # O CLI imprime o caminho salvo, em geral relativo à pasta do usuário: ~\.openclaw\media\browser\<id>.png
    match = re.search(r"((?:~|[A-Za-z]:)?[\\/][^\r\n\"'<>|]*?\.png)", out) if ok else None
    if not match:
        print(f"⚠️ [VISÃO] Screenshot falhou; etapa segue sem visão. ({out[:160]})", flush=True)
        return None
    try:
        return Path(os.path.expanduser(match.group(1).strip())).read_bytes()
    except OSError:
        return None


def build_perceiver():
    """Leitor de etapas. Com VISION_FALLBACK=1, a visão confere a leitura da árvore; ela só pode parar o bot."""
    base = A11yPerceiver()
    if not vision_enabled():
        return base
    try:
        import vision_perception
        return vision_perception.VisionAssistedPerceiver(base, vision_perception.VisionPerceiver(), take_screenshot)
    except Exception as e:  # SDK ausente ou sem credencial: a candidatura segue pelo caminho normal
        print(f"⚠️ Visão indisponível ({e}); seguindo só com a árvore de acessibilidade.", flush=True)
        return base


def run_modal(job: sqlite3.Row, pdf_path: Path, upload_name: str, dry_run: bool,
              perceiver: Optional[A11yPerceiver] = None) -> Dict[str, Any]:
    """Percorre as etapas do modal. Não envia sem currículo anexado.
    `perceiver` lê cada etapa; as ações saem sempre dos refs que ele devolve."""
    job_id, title, company = job["job_id"], job["title"], job["company"]
    perceiver = perceiver or build_perceiver()
    resume_attached = False

    for step_num in range(1, MAX_MODAL_STEPS + 1):
        print(f"\n📋 [ETAPA {step_num}] Processando tela do modal...")
        ok, modal_snap = read_modal()
        if not ok:
            return fail(job_id, f"Falha ao ler o modal: {modal_snap[:200]}")
        modal_snap = dialog_section(modal_snap)

        # A. Anexa o currículo na etapa correspondente
        if not resume_attached and is_resume_step(modal_snap):
            resume_attached = attach_resume(pdf_path, upload_name, modal_snap)
            if not resume_attached:
                return fail(job_id, "Não foi possível confirmar o anexo do currículo. Nada foi enviado.")
            ok, modal_snap = run_openclaw_browser(["snapshot"])
            if not ok:
                return fail(job_id, f"Falha ao ler o modal: {modal_snap[:200]}")
            modal_snap = dialog_section(modal_snap)

        # Uma leitura por etapa, já com o currículo anexado. Divergência entre leitores para tudo antes de agir.
        obs = perceiver.observe(modal_snap)
        if obs.blockers and dry_run:
            queue_guard.release_job(job_id)
            print(f"🧪 [DRY-RUN] Etapa {step_num} seria parada por segurança. A vaga voltou para a fila.")
            return {"status": "dry_run", "job_id": job_id, "step": step_num, "blockers": obs.blockers}
        if obs.blockers:
            return fail(job_id, f"Etapa {step_num} parada por segurança; nada foi enviado. " + " | ".join(obs.blockers))

        # B. Desmarca 'Siga a empresa'
        if obs.follow_ref:
            print("☑️ Desmarcando 'seguir empresa'")
            run_openclaw_browser(["click", obs.follow_ref])
            time.sleep(1)

        # C. Responde as perguntas visíveis
        unanswered = answer_fields(job_id, title, company, obs.fields)
        if unanswered:
            return {"status": "needs_input", "question": unanswered}

        # D. Etapa final: envia e exige a confirmação do LinkedIn
        submit_ref = obs.buttons.get("submit")
        if submit_ref:
            if not resume_attached:
                return fail(job_id, "O modal chegou ao envio sem etapa de currículo. Nada foi enviado.")
            if dry_run:
                queue_guard.release_job(job_id)
                print("🧪 [DRY-RUN] Parado antes de enviar. A vaga voltou para a fila.")
                return {"status": "dry_run", "job_id": job_id, "resume_attached": True}

            run_openclaw_browser(["click", submit_ref])
            for _ in range(CONFIRMATION_CHECKS):
                time.sleep(3)
                ok, confirmation = run_openclaw_browser(["snapshot"])
                if ok and is_confirmed(confirmation):
                    return record_success(job_id, title, company)
            return fail(job_id, "Enviar foi clicado, mas sem confirmação na tela. Confira no LinkedIn antes de tentar de novo.")

        # E. Avança para a próxima etapa
        next_ref = obs.buttons.get("next")
        if not next_ref:
            return fail(job_id, "Nenhum botão de avanço encontrado no modal.")
        run_openclaw_browser(["click", next_ref])
        time.sleep(2)
        ok, after = run_openclaw_browser(["snapshot"])
        error = validation_error(dialog_section(after)) if ok else None
        if error:
            return fail(job_id, f"O LinkedIn recusou a etapa {step_num} ('{error}'). Há um campo sem suporte; nada foi enviado.")

    return fail(job_id, f"O modal não terminou em {MAX_MODAL_STEPS} etapas. Pode haver campo sem suporte (lista ou opção).")


def record_success(job_id: str, title: str, company: str) -> Dict[str, Any]:
    queue_guard.mark_applied(job_id)
    send_telegram(
        f"🎉 *Candidatura confirmada pelo LinkedIn*\n\n"
        f"💼 *Vaga:* {title}\n"
        f"🏢 *Empresa:* {company}\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        reply_markup={"inline_keyboard": [[{"text": "🎙️ Treinar Entrevista", "callback_data": f"interview_start:{job_id}"}]]}
    )
    print(f"✅ [SUCESSO] Candidatura registrada para {job_id}!")
    return {"status": "success", "job_id": job_id}


def apply_to_job(job_id: str, dry_run: bool = False) -> Dict[str, Any]:
    """Executa fluxo completo de candidatura para uma vaga aprovada."""
    print(f"\n🎯 [APPLY] Iniciando processo para vaga: {job_id}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    job = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()

    if not job:
        return {"status": "error", "message": f"Vaga {job_id} não encontrada no banco jobs.db."}
    if job["status"] != "approved":
        return {"status": "error", "message": f"Vaga {job_id} está em '{job['status']}'; só vagas aprovadas são executadas."}

    title, company, job_url = job["title"], job["company"], job["job_url"]

    # Vagas de outros sites não passam pelo navegador nem consomem a cota
    if not is_linkedin_url(job_url):
        if dry_run:
            print("🧪 [DRY-RUN] Vaga externa: nada é alterado nem enviado ao chat.")
            return {"status": "dry_run", "job_id": job_id, "external": True}
        if not queue_guard.claim_job(job_id):
            return {"status": "error", "message": "Vaga já reservada por outro processo."}
        if common.easy_apply_only():
            return skip_external(job)
        return deliver_external_kit(job, prepare_resume(job_id))

    # Dry-run nunca envia: horário, cota e intervalo valem só para envios reais
    can_apply, reason, _ = (True, "", {}) if dry_run else queue_guard.check_can_apply()
    if not can_apply:
        print(f"🛡️ Candidatura bloqueada por guardrails de segurança: {reason}")
        return {"status": "blocked", "message": reason}

    if not queue_guard.claim_job(job_id):
        return {"status": "error", "message": "Vaga já reservada por outro processo."}

    pdf_path = prepare_resume(job_id)
    if not pdf_path:
        return fail(job_id, "Currículo PDF ausente ou inválido.")
    upload_name = resume_filename(tailor_resume.load_profile())

    send_telegram(f"🚀 *Iniciando candidatura*\n💼 {title}\n🏢 {company}")

    # Sempre navega: um modal aberto pode pertencer a outra vaga
    ok, out = run_openclaw_browser(["navigate", job_url])
    if not ok:
        return fail(job_id, f"Falha ao abrir a vaga: {out[:200]}")
    time.sleep(2)
    ok, snapshot = read_job_page()
    if not ok:
        return fail(job_id, f"Falha ao ler a página: {snapshot[:200]}")

    if is_already_applied(snapshot):
        queue_guard.mark_applied(job_id, count_quota=False)
        send_telegram(f"✅ *Já candidatado:* {title} — {company}.")
        return {"status": "already_applied", "job_id": job_id}

    easy_apply_ref = find_button_ref(snapshot, EASY_APPLY_LABELS)
    if not easy_apply_ref and is_closed_job(snapshot):
        if dry_run:
            queue_guard.release_job(job_id)
            print("🧪 [DRY-RUN] Vaga encerrada pelo anunciante. A vaga voltou para a fila.")
            return {"status": "dry_run", "job_id": job_id, "closed": True}
        return close_job(job)
    if not easy_apply_ref:
        if is_external_apply(snapshot):
            if dry_run:
                queue_guard.release_job(job_id)
                print("🧪 [DRY-RUN] Vaga externa detectada. A vaga voltou para a fila.")
                return {"status": "dry_run", "job_id": job_id, "external": True}
            if common.easy_apply_only():
                return skip_external(job)
            return deliver_external_kit(job, pdf_path)
        return fail(job_id, "Botão de candidatura não encontrado na página.")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE jobs SET apply_type = 'EASY_APPLY' WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()

    run_openclaw_browser(["click", easy_apply_ref])
    time.sleep(2)
    return run_modal(job, pdf_path, upload_name, dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aplica para uma vaga via LinkedIn Easy Apply.")
    parser.add_argument("--job-id", required=True, help="ID da vaga (ex: li-4485359827)")
    parser.add_argument("--dry-run", action="store_true", help="Percorre o modal e anexa o PDF, mas para antes de enviar")
    args = parser.parse_args()

    result = apply_to_job(args.job_id, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
