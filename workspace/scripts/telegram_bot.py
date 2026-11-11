#!/usr/bin/env python3
"""
workspace/scripts/telegram_bot.py
Bot Interativo, Rápido, Limpo e de Alta Performance para o Telegram (@Claw_vagas_bot).

Otimizações e Correções Implementadas:
1. Respostas ultra-rápidas (< 250ms) com SQLite em modo WAL e PRAGMAs de alto desempenho.
2. Pré-aquecimento do Whisper local em background thread (zero lag na 1ª mensagem de voz).
3. Transcrição local CPU-bound com faster-whisper (int8, 4 threads, zero custo de API).
4. Processador de Linguagem Natural com extração inteligente e filtro estrito de empresa (ex: VR Benefícios).
5. Exibição estrita: buscas mostram APENAS as vagas encontradas para a empresa/termo solicitado.
6. Fila persistente no banco: aprovações sobrevivem a reinícios e respeitam as travas anti-ban.
7. Design executivo sóbrio, cartões escaneáveis e botões inline interativos.
8. Relatório de fim de dia (empresas aplicadas) e Painel de Follow-Up (FUP).
9. Atende somente o chat autorizado em workspace/.env.
"""

import os
import sys
import hashlib
import json
import sqlite3
import subprocess
import time
import threading
import urllib.request
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common
import queue_guard
from common import send_message

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ["OPENCLAW_DB_PATH"]) if os.environ.get("OPENCLAW_DB_PATH") else (WORKSPACE_ROOT / "db" / "jobs.db")
OUTPUT_DIR = WORKSPACE_ROOT / "output"
REPORT_MD_PATH = OUTPUT_DIR / "market_intelligence_report.md"
TMP_AUDIO_DIR = WORKSPACE_ROOT / "output" / "audio_tmp"
TMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

WORKER_POLL_SECONDS = 15
APPLY_TIMEOUT_SECONDS = 900
NOT_APPROVABLE_STATUSES = ("applied", "in_progress", "approved")
CLOSED_STATUSES = ("applied", "skipped", "in_progress", "approved", "notified_external")
JOB_CARD_COLUMNS = "job_id, title, company, location, workplace_type, match_score, match_reasons, gaps, job_url, apply_type, description"
OUTCOME_LABELS = {
    "response": "💬 Resposta",
    "interview": "🎤 Entrevista",
    "rejected": "❌ Recusada",
    "no_response": "🔇 Sem retorno"
}

# Correção de compatibilidade entre PyAV 14+ e faster-whisper (remove metadata_errors='ignore')
try:
    import av
    _orig_av_open = av.open
    def _safe_av_open(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _orig_av_open(*args, **kwargs)
    av.open = _safe_av_open
except Exception as e:
    print(f"⚠️ [AV PATCH ERROR]: {e}", flush=True)

# Cache do modelo de transcrição para inicialização rápida e singleton
WHISPER_MODEL = None
WHISPER_LOCK = threading.Lock()

def get_db_connection() -> sqlite3.Connection:
    """Retorna conexão com SQLite otimizada para concorrência e leitura rápida."""
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    conn.execute("PRAGMA cache_size = -64000;")  # 64MB de cache na RAM
    return conn


def get_whisper_model():
    """Retorna instância singleton do WhisperModel com threads controladas."""
    global WHISPER_MODEL
    with WHISPER_LOCK:
        if WHISPER_MODEL is None:
            from faster_whisper import WhisperModel
            WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=4)
    return WHISPER_MODEL


def warmup_whisper_in_background():
    """Carrega o modelo do Whisper em segundo plano no boot do bot."""
    def _warmup():
        try:
            print("⚡ [WHISPER] Pré-aquecendo modelo Whisper local em background...", flush=True)
            t0 = time.time()
            get_whisper_model()
            dt = time.time() - t0
            print(f"✅ [WHISPER] Modelo pronto para áudio em {dt:.1f}s!", flush=True)
        except Exception as e:
            print(f"⚠️ [WHISPER WARMUP]: {e}", flush=True)

    threading.Thread(target=_warmup, daemon=True).start()


def run_apply_job(job_id: str):
    """Executa apply_job.py em subprocesso. Falha técnica tira a vaga da fila em vez de repetir."""
    apply_script = WORKSPACE_ROOT / "scripts" / "apply_job.py"
    print(f"🎬 [APPLY WORKER] Processando vaga {job_id}...", flush=True)
    try:
        proc = subprocess.run(
            [sys.executable, str(apply_script), "--job-id", job_id],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=APPLY_TIMEOUT_SECONDS, check=False
        )
        print(f"🏁 [APPLY WORKER] Concluído para {job_id} (Código {proc.returncode})", flush=True)
        failure = f"Erro técnico (código {proc.returncode})." if proc.returncode != 0 else None
        if failure:
            print((proc.stderr or "")[-800:], flush=True)
    except subprocess.TimeoutExpired:
        failure = "Tempo limite excedido. Confira no LinkedIn se o envio aconteceu."

    if not failure:
        return
    conn = get_db_connection()
    row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    if row and row["status"] in ("approved", "in_progress"):
        queue_guard.mark_failed(job_id, failure)
        send_message(common.authorized_chat_id(), f"❌ *Candidatura não concluída*\nVaga: `{job_id}`\nMotivo: {failure}")


def apply_worker():
    """Consome as vagas aprovadas no banco, uma por vez e respeitando as travas do queue_guard."""
    print("🚗 [APPLY WORKER] Fila sequencial de candidaturas iniciada...", flush=True)
    last_block_code = None
    while True:
        try:
            external = queue_guard.get_next_external_job()
            if external:
                run_apply_job(external["job_id"])
                continue

            job = queue_guard.get_next_approved_job()
            if not job:
                time.sleep(WORKER_POLL_SECONDS)
                continue

            can_apply, reason, meta = queue_guard.check_can_apply()
            if not can_apply:
                code = meta.get("code")
                if code != "COOLDOWN_ACTIVE" and code != last_block_code:
                    send_message(common.authorized_chat_id(), f"⏸️ *Fila pausada:* {reason}\nAs vagas aprovadas continuam salvas.")
                last_block_code = code
                time.sleep(WORKER_POLL_SECONDS)
                continue

            last_block_code = None
            run_apply_job(job["job_id"])
        except Exception as e:
            print(f"❌ [APPLY WORKER EXCEPTION]: {e}", flush=True)
            time.sleep(WORKER_POLL_SECONDS)


telegram_api_call = common.telegram_request


def edit_message_text(chat_id: str, message_id: int, text: str, reply_markup: Optional[Dict] = None, parse_mode: str = "Markdown"):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup:
        params["reply_markup"] = reply_markup

    res = telegram_api_call("editMessageText", params)
    if not res or not res.get("ok"):
        if parse_mode:
            params.pop("parse_mode", None)
            return telegram_api_call("editMessageText", params)
    return res


def answer_callback_query(callback_query_id: str, text: str = ""):
    return telegram_api_call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def make_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Gera uma barra de progresso visual elegante em blocos Unicode."""
    if total <= 0:
        return "[ ░░░░░░░░░░ ]"
    filled = int(round(length * current / float(total)))
    filled = max(0, min(length, filled))
    return f"[ {'▉' * filled}{'░' * (length - filled)} ]"


def get_ready_jobs() -> List[sqlite3.Row]:
    """Vagas que o usuário pode aprovar agora, na ordem dos cartões. Base única do contador e da listagem."""
    conn = get_db_connection()
    rows = conn.execute(f"""
        SELECT {JOB_CARD_COLUMNS}
        FROM jobs
        WHERE status IN ('scored', 'pending_approval'){common.recommendable_sql()}
        ORDER BY match_score DESC, id DESC
    """).fetchall()
    conn.close()
    excluded = queue_guard.load_excluded_companies()
    return [r for r in rows if (r["company"] or "").strip().lower() not in excluded]


def get_main_keyboard() -> Dict:
    """Retorna o teclado inferior executivo e focado em ação."""
    ready_count = 0
    try:
        ready_count = len(get_ready_jobs())
    except Exception:
        pass

    ready_label = f"📥 Vagas Prontas ({ready_count})" if ready_count > 0 else "📥 Vagas Prontas"
    return {
        "keyboard": [
            [{"text": "🔍 Buscar Novas Vagas"}, {"text": ready_label}],
            [{"text": "📈 Meu Desempenho"}, {"text": "⚙️ Meu Perfil & Memória"}],
            [{"text": "📊 Registrar Resultados"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }


def download_telegram_file(file_id: str, dest_path: Path) -> bool:
    """Baixa um arquivo de áudio/voz dos servidores do Telegram."""
    res = telegram_api_call("getFile", {"file_id": file_id})
    if not res or not res.get("ok"):
        return False
    file_path = res["result"]["file_path"]
    download_url = f"{common.TELEGRAM_API_URL}/file/bot{common.telegram_token()}/{file_path}"
    try:
        req = urllib.request.Request(download_url)
        with urllib.request.urlopen(req, context=common.SSL_CTX, timeout=30) as resp, open(dest_path, "wb") as out_f:
            out_f.write(resp.read())
        return True
    except Exception as e:
        print(f"[Erro Download Áudio]: {type(e).__name__}", flush=True)
        return False


def transcribe_voice(file_path: Path) -> str:
    """Transcreve arquivo de áudio em português usando faster-whisper local."""
    try:
        model = get_whisper_model()
        segments, info = model.transcribe(str(file_path), language="pt")
        return "".join([s.text for s in segments]).strip()
    except Exception as e:
        print(f"[Erro Transcrição Whisper]: {e}", flush=True)
        return ""


def handle_start(chat_id: str):
    welcome = (
        "🚀 *Bem-vindo ao seu Copiloto de Carreira!*\n\n"
        "Eu busco vagas de IA e Engenharia, ajusto seu currículo para cada uma e "
        "cuido das candidaturas que você aprovar no LinkedIn.\n\n"
        "💡 *Como você pode me comandar:*\n"
        "• Toque nos botões do menu inferior para navegar.\n"
        "• Digite ou envie um áudio com o que quiser (ex: _\"Ache vagas de Python remoto na BairesDev\"_).\n\n"
        "Toque em *📥 Vagas Prontas* para começar."
    )
    send_message(chat_id, welcome, reply_markup=get_main_keyboard())


def handle_status_fup(chat_id: str):
    """Exibe o Painel de Carreira gamificado, executivo e limpo."""
    conn = get_db_connection()
    cur = conn.cursor()

    total_jobs = cur.execute("SELECT count(*) FROM jobs").fetchone()[0]
    scored = cur.execute("SELECT count(*) FROM jobs WHERE status IN ('scored', 'pending_approval')").fetchone()[0]
    applied = cur.execute("SELECT count(*) FROM jobs WHERE status = 'applied'").fetchone()[0]

    today_str = queue_guard.get_now_brt().date().isoformat()
    quota_row = cur.execute("SELECT count, max_limit FROM daily_quotas WHERE date = ?", (today_str,)).fetchone()
    today_applied = quota_row[0] if quota_row else 0
    max_limit = quota_row[1] if quota_row else queue_guard.DEFAULT_MAX_DAILY

    cur.execute("""
        SELECT title, company FROM jobs
        WHERE status = 'applied'
        ORDER BY applied_at DESC, id DESC
        LIMIT 1
    """)
    last_applied = cur.fetchone()
    conn.close()

    progress_bar = make_progress_bar(today_applied, max_limit)
    last_app_text = f"{last_applied['title']} @ {last_applied['company']}" if last_applied else "Nenhuma ainda"

    stats = queue_guard.get_outcome_stats()
    band_lines = [f"• Fit {b['band']}+: {b['positive']}/{b['decided']} com retorno" for b in stats["bands"]]
    bands_text = ("\n" + "\n".join(band_lines)) if band_lines else ""

    text = (
        f"🚀 *Seu Painel de Carreira* | 📅 {queue_guard.get_now_brt().strftime('%d/%m/%Y')}\n\n"
        f"🎯 *Hoje:* {progress_bar} *{today_applied}/{max_limit}* candidaturas\n"
        f"📦 *Banco de Vagas:* *{total_jobs}* coletadas\n"
        f"⏳ *Aguardando você:* *{scored}* vagas\n"
        f"📌 *Última candidatura:* {last_app_text}\n\n"
        f"📊 *Resultados:* {applied} enviadas · {stats['positive']} com retorno · "
        f"{stats['interviews']} entrevistas · {stats['pending']} sem registro{bands_text}"
    )
    send_message(chat_id, text, reply_markup=get_main_keyboard())


def handle_daily_report(chat_id: str):
    """Gera o relatório do dia com as vagas enviadas hoje."""
    today_brt = queue_guard.get_now_brt()
    conn = get_db_connection()
    cur = conn.cursor()

    # applied_at é gravado em UTC; o dia do relatório é o de Brasília
    cur.execute("""
        SELECT title, company, location, match_score
        FROM jobs
        WHERE status = 'applied' AND date(applied_at, '-3 hours') = ?
        ORDER BY applied_at DESC
    """, (today_brt.date().isoformat(),))
    applied_jobs = cur.fetchall()
    conn.close()

    if not applied_jobs:
        send_message(
            chat_id,
            "Nenhuma candidatura foi enviada hoje ainda.\nToque em *📥 Vagas Prontas* para aprovar as primeiras vagas da fila!",
            reply_markup=get_main_keyboard()
        )
        return

    items = []
    for row in applied_jobs:
        loc = row["location"] or "Não informado"
        items.append(f"• *{row['title']}* — {row['company']}\n  Fit: {row['match_score'] or 0}% · Local: {loc}")

    report_text = (
        f"*Relatório Diário de Candidaturas*\n"
        f"Data: {today_brt.strftime('%d/%m/%Y')}\n"
        f"Total enviado hoje: *{len(applied_jobs)}* (limite: {queue_guard.DEFAULT_MAX_DAILY}/dia)\n\n"
        + "\n\n".join(items)
    )
    send_message(chat_id, report_text, reply_markup=get_main_keyboard())


def handle_outcomes(chat_id: str, limit: int = 5):
    """Lista candidaturas enviadas sem resultado registrado, para fechar o funil."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT job_id, title, company, applied_at FROM jobs
        WHERE status = 'applied' AND outcome IS NULL
        ORDER BY applied_at ASC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    if not rows:
        send_message(chat_id, "Nenhuma candidatura aguardando registro de resultado.", reply_markup=get_main_keyboard())
        return

    for row in rows:
        codes = list(OUTCOME_LABELS)
        keyboard = [
            [{"text": OUTCOME_LABELS[c], "callback_data": f"outcome:{row['job_id']}:{c}"} for c in codes[i:i + 2]]
            for i in range(0, len(codes), 2)
        ]
        send_message(
            chat_id,
            f"📬 *{row['title']}* — {row['company']}\nEnviada em {(row['applied_at'] or '')[:10]}. O que aconteceu?",
            reply_markup={"inline_keyboard": keyboard}
        )


def render_job_card(row: sqlite3.Row) -> Tuple[str, Dict]:
    """Renderiza o Tinder de Vagas com estética executiva e botões acionáveis."""
    jid = row["job_id"]
    title = row["title"]
    comp = row["company"]
    loc = row["location"] or "Brasil"
    wp = row["workplace_type"] or "Remoto"
    score = row["match_score"] or 0
    reasons_json = row["match_reasons"]
    gaps_json = row["gaps"]
    url = row["job_url"]
    apply_type = row["apply_type"]

    reasons = json.loads(reasons_json) if reasons_json else []
    gaps = json.loads(gaps_json) if gaps_json else []

    # Blindagem on-the-fly: se a vaga não tiver pontuação calculada, calcula na hora
    if score == 0 or not reasons:
        try:
            sys.path.insert(0, str(WORKSPACE_ROOT / "scripts"))
            from tailor_resume import calculate_match_score, load_profile
            prof = load_profile()
            desc = ""
            try:
                desc = row["description"] or ""
            except Exception:
                pass
            score, reasons, gaps = calculate_match_score(prof, title, desc, wp, loc)
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE jobs
                SET match_score = ?, match_reasons = ?, gaps = ?
                WHERE job_id = ?
            """, (score, json.dumps(reasons, ensure_ascii=False), json.dumps(gaps, ensure_ascii=False), jid))
            conn.commit()
            conn.close()
        except Exception:
            pass

    wp_lower = wp.lower()
    if "remote" in wp_lower or "remoto" in wp_lower:
        modalidade_chip = "🏠 100% Remoto"
    elif "hybrid" in wp_lower or "híbrido" in wp_lower:
        modalidade_chip = f"🏢 Híbrido ({loc.split(',')[0].strip()})"
    else:
        modalidade_chip = f"📍 {loc.split(',')[0].strip()}"

    type_chips = {"EASY_APPLY": "⚡ Easy Apply", "EXTERNAL_ATS": "💼 Portal da empresa"}
    type_chip = type_chips.get(apply_type, "❔ Tipo confirmado ao aprovar")
    fit_gauge = make_progress_bar(score, 100)

    reasons_text = "\n".join([f"• {r}" for r in reasons[:2]]) if reasons else "• Nenhum motivo forte identificado"
    gap_item = f"• {gaps[0]}" if gaps else "• Nenhum gap impeditivo detectado"

    card_text = (
        f"💼 *Vaga para avaliar*\n"
        f"*{title}* — *{comp}*\n\n"
        f"🟢 *Afinidade com seu Perfil:* {fit_gauge} *{score}%*\n"
        f"{modalidade_chip} · {type_chip}\n"
        f"🎯 *Por que essa vaga combina com você:*\n"
        f"{reasons_text}\n\n"
        f"⚠️ *Atenção a este requisito:*\n"
        f"{gap_item}"
    )

    inline_keyboard = {
        "inline_keyboard": [
            [
                {"text": "🚀 Candidatar-se Agora", "callback_data": f"approve:{jid}"},
                {"text": "⏭️ Pular Vaga", "callback_data": f"skip:{jid}"}
            ],
            [
                {"text": "🌐 Ver Anúncio Original", "url": url}
            ]
        ]
    }
    return card_text, inline_keyboard


def handle_show_specific_job_cards(chat_id: str, job_ids: List[str]):
    """Envia cartões EXCLUSIVAMENTE para a lista de IDs de vagas fornecida."""
    if not job_ids:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    placeholders = ",".join(["?"] * len(job_ids))
    rows = cur.execute(f"""
        SELECT {JOB_CARD_COLUMNS}
        FROM jobs
        WHERE job_id IN ({placeholders})
        ORDER BY match_score DESC
    """, job_ids).fetchall()
    conn.close()

    for row in rows:
        card_text, keyboard = render_job_card(row)
        send_message(chat_id, card_text, reply_markup=keyboard)
        time.sleep(0.3)


def handle_show_ready_jobs(chat_id: str, limit: int = 3):
    """Envia cartões compactos e limpos das vagas mais bem pontuadas do banco geral."""
    rows = get_ready_jobs()[:limit]

    if not rows:
        send_message(chat_id, "Nenhuma vaga pendente de aprovação no momento.\nToque em *Buscar Vagas* para minerar novas oportunidades.", reply_markup=get_main_keyboard())
        return

    for row in rows:
        card_text, keyboard = render_job_card(row)
        send_message(chat_id, card_text, reply_markup=keyboard)
        time.sleep(0.3)


def is_matching_company(job_company: str, target_company: str) -> bool:
    """Verifica de forma estrita se a empresa do anúncio é a empresa procurada."""
    j_comp = (job_company or "").strip().lower()
    t_comp = (target_company or "").strip().lower()

    if not j_comp or not t_comp:
        return False

    # Filtro estrito para VR Benefícios
    if "vr" in t_comp:
        return bool(re.search(r"\bvr\b", j_comp, re.IGNORECASE) or "vr software" in j_comp or "vr benef" in j_comp)

    # Filtro para Itaú
    if "itau" in t_comp or "itaú" in t_comp:
        return "itau" in j_comp or "itaú" in j_comp

    # Filtro para Nubank
    if "nubank" in t_comp or "nu " in t_comp:
        return "nubank" in j_comp or "nu pagamentos" in j_comp

    # Match geral por substring
    return t_comp in j_comp or j_comp in t_comp


def handle_search_custom(chat_id: str, query_term: str, company: Optional[str] = None):
    """Busca vagas com filtro estrito de empresa e exibe APENAS os resultados pertinentes."""
    def _run():
        search_desc = f"na empresa '{company}'" if company else f"com o termo '{query_term}'"

        # 1. Checagem prévia no banco local
        conn = get_db_connection()
        cur = conn.cursor()
        closed = ",".join("?" * len(CLOSED_STATUSES))
        local_ids = []
        if company:
            cur.execute(f"""
                SELECT job_id, company FROM jobs
                WHERE status NOT IN ({closed}){common.recommendable_sql()}
                ORDER BY match_score DESC, id DESC
            """, CLOSED_STATUSES)
            all_local = cur.fetchall()
            for r in all_local:
                if is_matching_company(r["company"], company):
                    local_ids.append(r["job_id"])
                    if len(local_ids) >= 3:
                        break
        else:
            excluded = queue_guard.load_excluded_companies()
            cur.execute(f"""
                SELECT job_id, company FROM jobs
                WHERE (title LIKE ? OR description LIKE ?) AND status NOT IN ({closed}){common.recommendable_sql()}
                ORDER BY match_score DESC, id DESC
            """, (f"%{query_term}%", f"%{query_term}%") + CLOSED_STATUSES)
            local_ids = [r["job_id"] for r in cur.fetchall() if r["company"].strip().lower() not in excluded][:3]
        conn.close()

        if local_ids:
            send_message(chat_id, f"Encontrei vagas já catalogadas no seu banco {search_desc}:")
            handle_show_specific_job_cards(chat_id, local_ids)
            return

        # 2. Busca externa na web (JobSpy) de forma assíncrona
        sources = "com Easy Apply no LinkedIn" if common.easy_apply_only() else "no LinkedIn e Indeed"
        send_message(chat_id, f"Pesquisando vagas {search_desc} {sources}...")

        from jobspy import scrape_jobs
        search_str = f"\"{company}\"" if company else query_term
        try:
            df = scrape_jobs(
                **common.scrape_options(),
                search_term=search_str,
                location="Brazil",
                country_indeed="brazil",
                results_wanted=20,
                fetch_description=True
            )
        except Exception as e:
            print(f"[Erro na busca externa]: {e}", flush=True)
            send_message(chat_id, "Não foi possível completar a busca externa agora. Tente novamente em alguns minutos.")
            return

        if df is None or len(df) == 0:
            send_message(chat_id, f"Nenhuma vaga aberta encontrada {search_desc} no momento.")
            return

        conn = get_db_connection()
        cur = conn.cursor()
        saved_ids = []
        ignored_other_companies = set()

        excluded = queue_guard.load_excluded_companies()
        for _, r in df.iterrows():
            job = common.parse_jobspy_row(r)
            if not job:
                continue

            # ⚠️ FILTRO ESTRITO DE EMPRESA: Se o usuário pediu uma empresa, descarta outras!
            if company and not is_matching_company(job["company"], company):
                ignored_other_companies.add(job["company"])
                continue
            if job["company"].strip().lower() in excluded:
                continue

            job_id, _is_new = common.save_job(cur, job)
            conn.commit()
            saved_ids.append(job_id)

        conn.close()

        # Avalia e personaliza currículo para as novas vagas
        tailor_script = WORKSPACE_ROOT / "scripts" / "tailor_resume.py"
        for jid in saved_ids:
            subprocess.run([sys.executable, str(tailor_script), "--job-id", jid], capture_output=True, check=False)

        if saved_ids:
            send_message(chat_id, f"Busca concluída! {len(saved_ids)} vaga(s) da empresa *{company or query_term}* encontradas:")
            handle_show_specific_job_cards(chat_id, saved_ids[:3])
        else:
            if company:
                send_message(
                    chat_id,
                    f"Busca finalizada: nenhuma vaga aberta pela empresa *{company}* no momento.\n"
                    f"_({len(ignored_other_companies)} empresas diferentes apareceram nos resultados e foram ignoradas.)_"
                )
            else:
                send_message(chat_id, f"Nenhuma vaga compatível encontrada {search_desc}.")

    threading.Thread(target=_run, daemon=True).start()


def extract_search_term(text: str, default: str = "Senior Python AI Engineer") -> str:
    """Extrai o cargo ou a tecnologia pedida: 'ache vagas de golang remoto' -> 'golang remoto'."""
    m = re.search(r"\bvagas?\s+(?:de|para|como|em)\s+(.+)", text.strip(), re.IGNORECASE)
    term = m.group(1) if m else ""
    term = re.split(r"\s+(?:na|no|da|do)\s+(?:empresa\s+)?", term, maxsplit=1)[0]
    term = term.strip(" .!?\"'")
    return term or default


def parse_and_execute_natural_language(chat_id: str, text: str):
    """Interpreta texto livre ou áudio transcrito com extração inteligente de entidades."""
    raw = text.strip()
    lower = raw.lower()

    # 0. Resposta de triagem, somente depois de o usuário tocar em "Digitar resposta"
    if chat_id in AWAITING_ANSWER and not text.startswith("/"):
        job_id, question_index = AWAITING_ANSWER.pop(chat_id)
        submit_screening_answer(chat_id, job_id, raw, question_index=question_index)
        return
    if chat_id in AWAITING_MEMORY_EDIT and not text.startswith("/"):
        finish_memory_edit(chat_id, AWAITING_MEMORY_EDIT.pop(chat_id), raw)
        return

    # 0.1 Aprovação / Pulo direto por comando
    if lower.startswith("aprovar "):
        handle_approve(chat_id, raw.split(" ", 1)[1].strip())
        return
    if lower.startswith("pular ") or lower.startswith("descartar "):
        handle_skip(chat_id, raw.split(" ", 1)[1].strip())
        return

    # 1. Meu Desempenho e Painel de Carreira
    if any(k in lower for k in ["desempenho", "meu desempenho", "status", "fup", "painel", "cotas"]):
        handle_status_fup(chat_id)
        return

    # 2. Vagas Prontas para Aprovação (Tinder de Vagas)
    if any(k in lower for k in ["vagas prontas", "aprovar vagas", "fila de vagas", "prontas", "quais vagas"]):
        handle_show_ready_jobs(chat_id, limit=3)
        return

    # 3. Buscar Novas Vagas
    if any(k in lower for k in ["buscar novas vagas", "buscar vagas", "procurar vagas", "novas vagas"]):
        send_message(chat_id, "Iniciando mineração de novas oportunidades de tecnologia e IA...")
        handle_search_custom(chat_id, "Python AI Remoto")
        return

    # 4. Meu Perfil & Memória
    if any(k in lower for k in ["meu perfil", "perfil", "memória", "memoria", "respostas"]):
        handle_profile_memory(chat_id)
        return

    # 5. Relatório Diário de fim de dia
    if any(k in lower for k in ["relatório", "relatorio", "fim do dia", "fim de dia", "resumo do dia"]):
        handle_daily_report(chat_id)
        return

    # 5.1 Resultados das candidaturas enviadas
    if any(k in lower for k in ["resultados", "registrar resultado"]):
        handle_outcomes(chat_id)
        return

    # 5. Extração de Empresa Específica (ex: "vagas da empresa VR benefícios", "na VR", "no Itaú")
    company_candidate = None
    empresa_match = re.search(r"\b(?:empresa|companhia|da|do|na|no)\s+([a-zA-Z0-9\s&]+?)(?:\s+(?:de|para|com|em|remoto|remota|presencial)|$)", raw, re.IGNORECASE)

    if "vr beneficios" in lower or "vr benefícios" in lower or " vr " in f" {lower} ":
        company_candidate = "VR Benefícios"
    elif "itau" in lower or "itaú" in lower:
        company_candidate = "Itaú"
    elif "nubank" in lower:
        company_candidate = "Nubank"
    elif "bairesdev" in lower:
        company_candidate = "BairesDev"
    elif empresa_match:
        cand = empresa_match.group(1).strip()
        if len(cand) > 2 and cand.lower() not in ["todas", "qualquer", "mais", "uma", "novas"]:
            company_candidate = cand

    # Se detectou empresa específica
    if company_candidate:
        handle_search_custom(chat_id, extract_search_term(raw, default="Python AI"), company=company_candidate)
        return

    # 6. Busca Geral por Cargo / Tecnologia
    if any(k in lower for k in ["acha", "ache", "busca", "buscar", "procura", "encontra", "pesquisa", "vagas", "veja"]):
        handle_search_custom(chat_id, extract_search_term(raw))
        return

    # Fallback inteligente
    send_message(
        chat_id,
        f"Não entendi *\"{text}\"*.\n\nUse o menu abaixo ou peça uma busca (ex: _\"Busca vagas na empresa VR Beneficios\"_).",
        reply_markup=get_main_keyboard()
    )


def handle_approve(chat_id: str, job_id: str, message_id: Optional[int] = None):
    """Aprova a vaga. O worker lê a fila do banco, então a aprovação sobrevive a reinícios."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, company, status, apply_type FROM jobs WHERE job_id = ?", (job_id,))
    job = cur.fetchone()
    if not job:
        send_message(chat_id, f"Vaga `{job_id}` não encontrada.")
        conn.close()
        return

    if common.easy_apply_only() and job["apply_type"] == "EXTERNAL_ATS" and job["status"] not in NOT_APPROVABLE_STATUSES:
        conn.close()
        feedback = (f"*{job['title']}* — *{job['company']}* é candidatura no site da empresa.\n"
                    f"O foco atual é só Easy Apply do LinkedIn (EASY_APPLY_ONLY=1).")
    elif job["status"] in NOT_APPROVABLE_STATUSES:
        conn.close()
        labels = {"applied": "já foi enviada", "in_progress": "está em andamento", "approved": "já está na fila"}
        feedback = f"*{job['title']}* — *{job['company']}* {labels[job['status']]}."
    else:
        cur.execute("UPDATE jobs SET status = 'approved', error_log = NULL WHERE job_id = ?", (job_id,))
        conn.commit()
        position = cur.execute("SELECT count(*) FROM jobs WHERE status = 'approved'").fetchone()[0]
        conn.close()
        feedback = (
            f"Vaga aprovada: *{job['title']}* — *{job['company']}*\n"
            f"Posição na fila: {position}. O envio segue a cota, o horário e o intervalo de segurança."
        )

    if message_id:
        edit_message_text(chat_id, message_id, feedback)
    else:
        send_message(chat_id, feedback)


def submit_screening_answer(chat_id: str, job_id: str, answer: str, message_id: Optional[int] = None,
                            question_index: int = 0):
    """Guarda a resposta de uma pergunta. Quando todas as perguntas da pausa têm resposta, a vaga volta à fila."""
    conn = get_db_connection()
    row = conn.execute("SELECT title, company, status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()

    questions = queue_guard.get_pending_questions(job_id) if row else []
    if not row or row["status"] != "needs_input" or not 0 <= question_index < len(questions):
        send_message(chat_id, "Essa pergunta não está mais pendente.", reply_markup=get_main_keyboard())
        return

    question = questions[question_index]["question"]
    queue_guard.save_learned_answer(question, answer)
    remaining = queue_guard.unanswered_questions(job_id)
    if remaining:
        feedback = f"✅ *\"{question}\"* → *{answer}*\nFaltam {len(remaining)} pergunta(s) desta vaga."
        if message_id:
            edit_message_text(chat_id, message_id, feedback)
        else:
            send_message(chat_id, feedback)
        return

    handle_approve(chat_id, job_id, message_id=message_id)
    send_message(
        chat_id,
        f"Resposta guardada: *\"{answer}\"*.\nA candidatura de *{row['title']}* volta para a fila e recomeça do início.",
        reply_markup=get_main_keyboard()
    )


AWAITING_ANSWER: Dict[str, Tuple[str, int]] = {}
AWAITING_MEMORY_EDIT: Dict[str, str] = {}
ACTIVE_INTERVIEWS: Dict[str, Dict[str, Any]] = {}


def record_negative_feedback(job_id: str, reason_code: str):
    """Grava o motivo do descarte. Empresas descartadas saem das próximas recomendações."""
    prefs_file = queue_guard.get_data_dir() / queue_guard.PREFERENCES_FILE
    data = {"reasons_count": {}, "excluded_companies": [], "history": []}
    if prefs_file.exists():
        try:
            with open(prefs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass

    data["reasons_count"][reason_code] = data["reasons_count"].get(reason_code, 0) + 1

    # Se for motivo de empresa, registra a empresa para exclusão automática
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, company FROM jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    conn.close()

    comp = row["company"] if row else ""
    if reason_code == "company" and comp and comp not in data["excluded_companies"]:
        data["excluded_companies"].append(comp)

    data["history"].append({
        "job_id": job_id,
        "company": comp,
        "reason": reason_code,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })

    with open(prefs_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def handle_skip(chat_id: str, job_id: str, message_id: Optional[int] = None):
    """Descarta a vaga e solicita motivo para calibrar preferências (Negative Feedback)."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, company, status FROM jobs WHERE job_id = ?", (job_id,))
    job = cur.fetchone()
    if not job or job["status"] in ("applied", "in_progress"):
        conn.close()
        send_message(chat_id, f"A vaga `{job_id}` não pode ser pulada agora.")
        return
    cur.execute("UPDATE jobs SET status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (job_id,))
    conn.commit()
    conn.close()

    title = job["title"]
    comp = job["company"]

    text = (
        f"🗑️ *Vaga pulada:* {title} @ {comp}\n\n"
        f"Qual o motivo principal? Se for a empresa, ela sai das próximas recomendações."
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "💸 Salário / Nível errado", "callback_data": f"skip_why:{job_id}:salary"},
                {"text": "🏢 Empresa / Reputação", "callback_data": f"skip_why:{job_id}:company"}
            ],
            [
                {"text": "🧩 Falta Requisito Técnico", "callback_data": f"skip_why:{job_id}:tech"},
                {"text": "📍 Modelo (Presencial/Híbrido)", "callback_data": f"skip_why:{job_id}:location"}
            ],
            [
                {"text": "↩️ Outro / Pular sem motivo", "callback_data": f"skip_why:{job_id}:dismiss"}
            ]
        ]
    }
    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
    else:
        send_message(chat_id, text, reply_markup=reply_markup)


def handle_profile_memory(chat_id: str):
    """Exibe o perfil executivo do candidato e as respostas salvas na memória permanente."""
    profile_path = queue_guard.get_data_dir() / "profile.json"
    learned_path = queue_guard.get_data_dir() / "learned_answers.json"

    prof_data = {}
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                prof_data = json.load(f)
        except Exception:
            pass

    learned_data = {}
    if learned_path.exists():
        try:
            with open(learned_path, "r", encoding="utf-8") as f:
                learned_data = json.load(f)
        except Exception:
            pass

    personal = prof_data.get("personal", {})
    name = personal.get("display_name", "")
    headline = personal.get("headline", "")
    loc = personal.get("location", "")
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    salaries = prof_data.get("search_criteria", {}).get("salary_expectations", {})
    sal_pj = salaries.get("brl_pj_monthly_min", 0)

    learned_count = len(learned_data)
    learned_samples = list(learned_data.items())[-3:]
    samples_text = ""
    if learned_samples:
        samples_text = "\n\n*Últimas respostas memorizadas pelo bot:*\n" + "\n".join([f"• _{q[:45]}..._ -> `{a}`" for q, a in learned_samples])

    text = (
        f"⚙️ *Seu Perfil Executivo & Memória de Triagem*\n\n"
        f"👤 *Nome:* {name}\n"
        f"💼 *Posição:* {headline[:65]}...\n"
        f"📍 *Localização:* {loc}\n"
        f"📧 *E-mail:* `{email}`\n"
        f"📱 *Telefone:* `{phone}`\n"
        f"💰 *Pretensão Mínima:* `R$ {sal_pj:,} / mês PJ` (negociável)\n\n"
        f"🧠 *Memória Ativa:* *{learned_count}* respostas automáticas aprendidas.{samples_text}\n\n"
        f"_Para alterar estes dados, edite data/profile.json._"
    )
    send_message(chat_id, text, reply_markup=get_main_keyboard())
    if learned_count:
        send_message(chat_id, "Quer revisar o que o bot responde por você?", reply_markup={"inline_keyboard": [[
            {"text": f"📝 Minhas respostas ({learned_count})", "callback_data": "mem:l:0"}]]})


MEMORY_PAGE_SIZE = 8


def memory_key(question: str) -> str:
    """Identificador curto e estável da pergunta; cabe no limite de 64 bytes do callback."""
    return hashlib.sha1(queue_guard.normalize_question(question).encode("utf-8")).hexdigest()[:10]


def find_learned_by_key(key: str) -> Optional[Tuple[str, str]]:
    for question, answer in queue_guard.read_learned_answers_raw().items():
        if memory_key(question) == key:
            return question, answer
    return None


def render_memory_list(page: int = 0) -> Tuple[str, Dict]:
    items = list(queue_guard.read_learned_answers_raw().items())
    if not items:
        return "Nenhuma resposta aprendida ainda. Elas aparecem aqui quando você responde uma pergunta de vaga.", {}
    pages = (len(items) + MEMORY_PAGE_SIZE - 1) // MEMORY_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = items[page * MEMORY_PAGE_SIZE:(page + 1) * MEMORY_PAGE_SIZE]
    rows = [[{"text": f"{q[:38]} → {a[:18]}", "callback_data": f"mem:v:{memory_key(q)}:{page}"}] for q, a in chunk]
    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Anteriores", "callback_data": f"mem:l:{page - 1}"})
    if page < pages - 1:
        nav.append({"text": "Próximas ➡️", "callback_data": f"mem:l:{page + 1}"})
    if nav:
        rows.append(nav)
    text = (f"📝 *Minhas respostas* ({len(items)}) — página {page + 1}/{pages}\n"
            f"Toque em uma resposta para ver, editar ou apagar.")
    return text, {"inline_keyboard": rows}


def handle_memory_list(chat_id: str, page: int = 0, message_id: Optional[int] = None):
    text, markup = render_memory_list(page)
    if message_id:
        edit_message_text(chat_id, message_id, text, reply_markup=markup or None)
    else:
        send_message(chat_id, text, reply_markup=markup or get_main_keyboard())


def handle_memory_callback(chat_id: str, cb_id: str, msg_id: int, data: str):
    """mem:l:<pág> lista · mem:v:<chave>:<pág> detalhe · mem:e editar · mem:d confirmar · mem:dok apagar."""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "l":
        answer_callback_query(cb_id)
        handle_memory_list(chat_id, int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0, message_id=msg_id)
        return

    key = parts[2] if len(parts) > 2 else ""
    page = parts[3] if len(parts) > 3 and parts[3].isdigit() else "0"
    found = find_learned_by_key(key)
    if not found:
        answer_callback_query(cb_id, text="Essa resposta não existe mais.")
        handle_memory_list(chat_id, int(page), message_id=msg_id)
        return
    question, answer = found
    back = {"text": "⬅️ Voltar", "callback_data": f"mem:l:{page}"}

    if action == "v":
        answer_callback_query(cb_id)
        edit_message_text(chat_id, msg_id, f"❓ *{question}*\n\n💬 Resposta atual: *{answer}*", reply_markup={"inline_keyboard": [
            [{"text": "✏️ Editar", "callback_data": f"mem:e:{key}:{page}"},
             {"text": "🗑️ Apagar", "callback_data": f"mem:d:{key}:{page}"}],
            [back]]})
    elif action == "e":
        AWAITING_MEMORY_EDIT[chat_id] = question
        answer_callback_query(cb_id, text="Aguardando a nova resposta...")
        send_message(chat_id, f"Digite a nova resposta para *\"{question}\"*.\nResposta atual: *{answer}*")
    elif action == "d":
        answer_callback_query(cb_id)
        edit_message_text(chat_id, msg_id,
                          f"Apagar a resposta de *\"{question}\"*?\nO bot volta a te perguntar na próxima vaga que fizer essa pergunta.",
                          reply_markup={"inline_keyboard": [
                              [{"text": "🗑️ Sim, apagar", "callback_data": f"mem:dok:{key}:{page}"}, back]]})
    elif action == "dok":
        deleted = queue_guard.delete_learned_answer(question)
        answer_callback_query(cb_id, text="Resposta apagada." if deleted else "Essa resposta não existe mais.")
        handle_memory_list(chat_id, int(page), message_id=msg_id)


def finish_memory_edit(chat_id: str, question: str, new_answer: str):
    queue_guard.save_learned_answer(question, new_answer)
    send_message(chat_id, f"✅ Resposta atualizada:\n❓ *{question}*\n💬 *{new_answer}*", reply_markup=get_main_keyboard())


def handle_interview_start(chat_id: str, job_id: str, message_id: Optional[int] = None):
    """Inicia simulação rápida de entrevista de 3 minutos em áudio."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT title, company, description, match_reasons, gaps FROM jobs WHERE job_id = ?", (job_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        send_message(chat_id, "Vaga não encontrada para simulação.")
        return

    title = row["title"]
    comp = row["company"]

    q1 = (
        f"🎙️ *Treino de Entrevista — {title} @ {comp}*\n\n"
        f"🤖 *Pergunta:*\n"
        f"_'Para começarmos, conte sobre um desafio complexo que você enfrentou com Python e IA em produção e qual foi o impacto mensurável no negócio.'_\n\n"
        f"👉 *Grave um áudio de até 1 minuto respondendo como se estivesse na entrevista!*\n"
        f"Vou transcrever sua fala e mostrar métricas objetivas da resposta: tamanho, números citados e termos da vaga."
    )
    ACTIVE_INTERVIEWS[chat_id] = {
        "job_id": job_id,
        "title": title,
        "company": comp,
        "description": row["description"] or "",
        "step": 1,
        "question": q1
    }
    if message_id:
        edit_message_text(chat_id, message_id, q1)
    else:
        send_message(chat_id, q1)


def build_interview_feedback(interview: Dict[str, Any], transcript: str) -> str:
    """Devolve métricas objetivas da resposta. Não há avaliação de conteúdo, então não há nota."""
    from tailor_resume import extract_keywords_from_job, has_term, normalize_text

    words = len(transcript.split())
    numbers = re.findall(r"\d+(?:[.,]\d+)?%?", transcript)
    transcript_norm = normalize_text(transcript)
    job_terms = extract_keywords_from_job(interview["title"], interview["description"])
    mentioned = [t for t in job_terms if has_term(t, transcript_norm)]
    missing = [t for t in job_terms if t not in mentioned]

    tips = []
    if words < 80:
        tips.append("Resposta curta: inclua contexto, sua ação e o resultado.")
    if not numbers:
        tips.append("Nenhum número citado: traga uma métrica real do resultado.")
    if missing:
        tips.append(f"Termos da vaga que não apareceram: {', '.join(missing[:5]).upper()}.")
    tips_text = "\n".join(f"• {t}" for t in tips) or "• Tamanho, números e termos da vaga estão presentes."

    return (
        f"🎯 *Métricas da sua resposta — {interview['title']} @ {interview['company']}*\n\n"
        f"📝 *Palavras:* {words}\n"
        f"🔢 *Números citados:* {', '.join(numbers[:5]) or 'nenhum'}\n"
        f"🧩 *Termos da vaga citados:* {', '.join(mentioned[:5]).upper() or 'nenhum'}\n\n"
        f"💡 *Para ajustar:*\n{tips_text}\n\n"
        f"_Estas métricas são automáticas e não avaliam a qualidade do conteúdo._"
    )


def handle_callback(cb: Dict[str, Any]):
    """Trata cliques em botões inline."""
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg_id = cb["message"]["message_id"]
    chat_id = str(cb["message"]["chat"]["id"])

    if data.startswith("approve:"):
        answer_callback_query(cb_id)
        handle_approve(chat_id, data.split(":", 1)[1], message_id=msg_id)
    elif data.startswith("skip:"):
        answer_callback_query(cb_id, text="Vaga descartada.")
        handle_skip(chat_id, data.split(":", 1)[1], message_id=msg_id)
    elif data.startswith("skip_why:"):
        _, jid, reason_code = data.split(":", 2)
        labels = {
            "salary": "Salário / Senioridade",
            "company": "Empresa / Cultura",
            "tech": "Requisito Técnico",
            "location": "Modelo de Trabalho (Presencial/Híbrido)",
            "dismiss": "Geral"
        }
        label = labels.get(reason_code, reason_code)
        answer_callback_query(cb_id, text=f"Motivo registrado: {label}")
        record_negative_feedback(jid, reason_code)
        result = "Essa empresa não aparecerá nas próximas recomendações." if reason_code == "company" else "Motivo salvo no seu histórico."
        edit_message_text(chat_id, msg_id, f"✅ *Motivo registrado:* {label}.\n{result}")
    elif data.startswith("interview_start:"):
        answer_callback_query(cb_id, text="Iniciando treino de entrevista...")
        handle_interview_start(chat_id, data.split(":", 1)[1], message_id=msg_id)
    elif data.startswith("ans_prompt:"):
        parts = data.split(":")
        jid = parts[1]
        q_idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        AWAITING_ANSWER[chat_id] = (jid, q_idx)
        questions = queue_guard.get_pending_questions(jid)
        question = questions[q_idx]["question"] if q_idx < len(questions) else ""
        answer_callback_query(cb_id, text="Aguardando sua resposta...")
        about = f" para *\"{question}\"*" if question else ""
        send_message(chat_id, f"Digite sua resposta{about} ou grave um áudio. A próxima mensagem será usada como resposta.")
    elif data.startswith("anss:"):
        # Usuário confirmou a sugestão de uma pergunta parecida: a resposta passa a valer para esta pergunta
        parts = data.split(":")
        jid, q_idx = parts[1], parts[2] if len(parts) > 2 else "0"
        questions = queue_guard.get_pending_questions(jid)
        suggestion = questions[int(q_idx)].get("suggestion") if q_idx.isdigit() and int(q_idx) < len(questions) else None
        if not suggestion:
            answer_callback_query(cb_id, text="Sugestão indisponível.")
            return
        answer_callback_query(cb_id, text=f"Resposta: {suggestion['answer'][:40]}")
        submit_screening_answer(chat_id, jid, suggestion["answer"], message_id=msg_id, question_index=int(q_idx))
    elif data.startswith("ansi:"):
        parts = data.split(":")
        jid, q_idx, idx = (parts[1], "0", parts[2]) if len(parts) == 3 else (parts[1], parts[2], parts[3])
        questions = queue_guard.get_pending_questions(jid)
        options = questions[int(q_idx)]["options"] if q_idx.isdigit() and int(q_idx) < len(questions) else []
        if not idx.isdigit() or int(idx) >= len(options):
            answer_callback_query(cb_id, text="Opção inválida.")
            return
        answer_callback_query(cb_id, text=f"Resposta: {options[int(idx)][:40]}")
        submit_screening_answer(chat_id, jid, options[int(idx)], message_id=msg_id, question_index=int(q_idx))
    elif data.startswith("ans:"):
        parts = data.split(":", 3)
        jid, q_idx, ans_val = (parts[1], "0", parts[2]) if len(parts) == 3 else (parts[1], parts[2], parts[3])
        answer_callback_query(cb_id, text=f"Resposta: {ans_val}")
        submit_screening_answer(chat_id, jid, ans_val, message_id=msg_id,
                                question_index=int(q_idx) if q_idx.isdigit() else 0)
    elif data.startswith("mem:"):
        handle_memory_callback(chat_id, cb_id, msg_id, data)
    elif data.startswith("ext_applied:"):
        jid = data.split(":", 1)[1]
        saved = queue_guard.mark_external_applied(jid)
        answer_callback_query(cb_id, text="Candidatura registrada." if saved else "Essa vaga já foi registrada.")
        if saved:
            send_message(chat_id, f"✅ Candidatura externa registrada: `{jid}`.\nQuando tiver retorno, use /resultados.")
    elif data.startswith("outcome:"):
        _, jid, outcome = data.split(":", 2)
        saved = queue_guard.record_outcome(jid, outcome)
        answer_callback_query(cb_id, text="Resultado registrado." if saved else "Não foi possível registrar.")
        if saved:
            edit_message_text(chat_id, msg_id, f"✅ Resultado registrado: {OUTCOME_LABELS[outcome]}")


def handle_voice(chat_id: str, voice_obj: Dict[str, Any]):
    """Transcreve áudio e o trata como resposta de entrevista ou comando em linguagem natural."""
    send_message(chat_id, "Ouvindo seu áudio...")

    tmp_file = TMP_AUDIO_DIR / f"voice_{int(time.time())}.oga"
    if not download_telegram_file(voice_obj["file_id"], tmp_file):
        send_message(chat_id, "Erro ao baixar o áudio do Telegram. Tente novamente.")
        return

    transcribed_text = transcribe_voice(tmp_file)
    try:
        tmp_file.unlink(missing_ok=True)
    except Exception:
        pass

    if not transcribed_text:
        send_message(chat_id, "Não consegui compreender o áudio com clareza. Pode repetir ou digitar?")
        return

    send_message(chat_id, f"🎤 *Você disse:*\n_\"{transcribed_text}\"_")
    if chat_id in ACTIVE_INTERVIEWS:
        interview = ACTIVE_INTERVIEWS.pop(chat_id)
        send_message(chat_id, build_interview_feedback(interview, transcribed_text), reply_markup=get_main_keyboard())
        return
    parse_and_execute_natural_language(chat_id, transcribed_text)


def handle_text(chat_id: str, text: str):
    """Roteia botões fixos e comandos; o restante segue para linguagem natural."""
    if text in ["/start", "/menu", "ajuda", "menu"]:
        handle_start(chat_id)
    elif text in ["📈 Meu Desempenho", "Status & FUP", "/status", "status", "fup"]:
        handle_status_fup(chat_id)
    elif text in ["Relatório Diário", "/diario", "relatorio", "relatório"]:
        handle_daily_report(chat_id)
    elif text.startswith("🔍 Buscar Novas Vagas") or text in ["Buscar Vagas", "/buscar", "buscar"]:
        send_message(chat_id, "Iniciando mineração de novas vagas de tecnologia e IA...")
        handle_search_custom(chat_id, "Senior Python AI Engineer")
    elif text.startswith("📥 Vagas Prontas") or text in ["Vagas Prontas", "/prontas", "prontas"]:
        handle_show_ready_jobs(chat_id, limit=3)
    elif text in ["⚙️ Meu Perfil & Memória", "Meu Perfil", "perfil", "memória", "memoria"]:
        handle_profile_memory(chat_id)
    elif text.lower() in ["/respostas", "respostas", "minhas respostas"]:
        handle_memory_list(chat_id)
    elif text in ["📊 Registrar Resultados", "/resultados", "resultados"]:
        handle_outcomes(chat_id)
    else:
        parse_and_execute_natural_language(chat_id, text)


def handle_update(update: Dict[str, Any]):
    """Processa um update do Telegram. Atualizações de outros chats são ignoradas."""
    if "callback_query" in update:
        message = update["callback_query"].get("message") or {}
    else:
        message = update.get("message") or {}

    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != common.authorized_chat_id():
        if chat_id:
            print("🚫 [TELEGRAM BOT] Update de chat não autorizado ignorado.", flush=True)
        return

    if "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "voice" in message or "audio" in message:
        handle_voice(chat_id, message.get("voice") or message.get("audio"))
    elif "text" in message:
        handle_text(chat_id, message["text"].strip())


def process_updates():
    """Loop contínuo de escuta (Long Polling) do Telegram com zero lag."""
    print("🤖 [TELEGRAM BOT] Listener ativo com suporte a Áudio e Comandos Livres...", flush=True)

    interrupted = queue_guard.recover_interrupted_jobs()
    if interrupted:
        send_message(
            common.authorized_chat_id(),
            f"⚠️ {interrupted} candidatura(s) foram interrompidas no último desligamento e marcadas como falha. "
            f"Confira no LinkedIn se o envio aconteceu antes de aprovar de novo."
        )

    # Inicia pré-aquecimento do Whisper em background
    warmup_whisper_in_background()

    # Inicia worker sequencial de submissão no browser
    threading.Thread(target=apply_worker, daemon=True).start()

    offset = 0

    while True:
        try:
            params = {"timeout": 20}
            if offset > 0:
                params["offset"] = offset

            res = telegram_api_call("getUpdates", params)
            if not res or not res.get("ok"):
                time.sleep(2)
                continue

            for u in res.get("result", []):
                offset = max(offset, u["update_id"] + 1)
                handle_update(u)

        except Exception as e:
            print(f"[Loop Exception]: {e}", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    process_updates()
