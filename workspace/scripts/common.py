#!/usr/bin/env python3
"""
workspace/scripts/common.py
Infraestrutura compartilhada pelos scripts do workspace:
1. Configuração via workspace/.env (segredos nunca ficam no código).
2. Cliente do Telegram com verificação TLS ativa.
3. Normalização e persistência de vagas coletadas pelo JobSpy.
"""

import json
import math
import os
import ssl
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = WORKSPACE_ROOT / ".env"
TELEGRAM_API_URL = "https://api.telegram.org"


def load_env(path: Path = ENV_PATH) -> None:
    """Carrega KEY=VALUE de workspace/.env sem sobrescrever o ambiente do processo."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável {name} ausente. Configure workspace/.env (veja .env.example).")
    return value


def easy_apply_only() -> bool:
    """Foco em Easy Apply do LinkedIn (padrão). EASY_APPLY_ONLY=0 no .env volta a incluir vagas externas."""
    return os.environ.get("EASY_APPLY_ONLY", "1").strip().lower() not in ("0", "false", "nao", "não", "no")


def scrape_options() -> Dict[str, Any]:
    """Sites e filtro do JobSpy conforme o foco. easy_apply usa o filtro f_AL do próprio LinkedIn."""
    if easy_apply_only():
        return {"site_name": ["linkedin"], "easy_apply": True}
    return {"site_name": ["linkedin", "indeed"]}


def recommendable_sql() -> str:
    """Trecho SQL para esconder vagas externas das sugestões quando o foco é Easy Apply."""
    return " AND apply_type != 'EXTERNAL_ATS'" if easy_apply_only() else ""


def telegram_token() -> str:
    return require_env("TELEGRAM_BOT_TOKEN")


def authorized_chat_id() -> str:
    return require_env("TELEGRAM_CHAT_ID")


def build_ssl_context() -> ssl.SSLContext:
    """Usa o bundle do certifi: o repositório padrão do Python falha nesta máquina."""
    import certifi
    return ssl.create_default_context(cafile=certifi.where())


SSL_CTX = build_ssl_context()


def telegram_request(method: str, params: Dict[str, Any], timeout: int = 25) -> Optional[Dict[str, Any]]:
    """Chama a API do Telegram via POST. Retorna None em falha de rede."""
    url = f"{TELEGRAM_API_URL}/bot{telegram_token()}/{method}"
    payload = json.dumps(params).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        if "timed out" not in str(e).lower():
            print(f"[Telegram API Error - {method}]: {type(e).__name__}", flush=True)
        return None


def send_message(chat_id: str, text: str, reply_markup: Optional[Dict] = None, parse_mode: str = "Markdown") -> Optional[Dict]:
    """Envia mensagem; se o Markdown for rejeitado, reenvia como texto puro."""
    params: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if reply_markup:
        params["reply_markup"] = reply_markup

    res = telegram_request("sendMessage", params)
    if (not res or not res.get("ok")) and parse_mode:
        params.pop("parse_mode", None)
        return telegram_request("sendMessage", params)
    return res


def send_document(chat_id: str, file_path: Path, caption: str = "", filename: Optional[str] = None) -> bool:
    """Envia um PDF no chat. O erro não é impresso por extenso porque a URL contém o token."""
    import certifi
    import requests

    url = f"{TELEGRAM_API_URL}/bot{telegram_token()}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": (filename or file_path.name, f, "application/pdf")}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
            res = requests.post(url, files=files, data=data, timeout=30, verify=certifi.where())
        return res.ok
    except Exception as e:
        print(f"[Telegram sendDocument Error]: {type(e).__name__}", flush=True)
        return False


def clean_value(value: Any) -> str:
    """Converte valores do DataFrame em texto, tratando None e NaN como vazio."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def canonical_job_id(raw_id: str) -> str:
    """Padroniza IDs legados (linkedin-li-1, indeed-in-1, 1) no formato do JobSpy (li-1, in-1)."""
    job_id = raw_id.replace("urn:li:jobPosting:", "").strip()
    for prefix in ("linkedin-", "indeed-"):
        if job_id.startswith(prefix):
            job_id = job_id[len(prefix):]
    if job_id.isdigit():
        return f"li-{job_id}"
    return job_id


def detect_apply_type(site: str) -> str:
    """O JobSpy não informa Easy Apply no LinkedIn: o tipo é confirmado na página da vaga."""
    return "EXTERNAL_ATS" if site == "indeed" else "UNKNOWN"


def parse_jobspy_row(row: Any, default_location: str = "Brasil") -> Optional[Dict[str, str]]:
    """Converte uma linha do JobSpy em um registro da tabela jobs."""
    job_id = canonical_job_id(clean_value(row.get("id")))
    title = clean_value(row.get("title"))
    company = clean_value(row.get("company"))
    job_url = clean_value(row.get("job_url"))
    if not job_id or not title or not company or not job_url:
        return None

    site = clean_value(row.get("site")) or "linkedin"
    location = clean_value(row.get("location")) or default_location
    searchable = f"{location} {title}".lower()
    is_remote = row.get("is_remote") is True or "remote" in searchable or "remoto" in searchable

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "workplace_type": "Remote" if is_remote else "Hybrid",
        "job_url": job_url,
        "apply_url": clean_value(row.get("job_url_direct")),
        "apply_type": detect_apply_type(site),
        "description": clean_value(row.get("description")),
    }


def save_job(cursor: Any, job: Dict[str, str]) -> Tuple[str, bool]:
    """Insere a vaga se for inédita por ID, URL ou cargo+empresa+local. Retorna (job_id, is_new)."""
    cursor.execute("""
        SELECT job_id FROM jobs
        WHERE job_id = ? OR job_url = ?
           OR (lower(title) = lower(?) AND lower(company) = lower(?) AND lower(COALESCE(location, '')) = lower(?))
        LIMIT 1
    """, (job["job_id"], job["job_url"], job["title"], job["company"], job["location"]))
    existing = cursor.fetchone()

    if existing:
        existing_id = existing[0]
        if len(job["description"]) > 50:
            cursor.execute("""
                UPDATE jobs SET description = ?
                WHERE job_id = ? AND (description IS NULL OR length(description) < 50)
            """, (job["description"], existing_id))
        return existing_id, False

    cursor.execute("""
        INSERT INTO jobs (job_id, title, company, location, workplace_type, job_url, apply_url, apply_type, description, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered')
    """, (
        job["job_id"], job["title"], job["company"], job["location"], job["workplace_type"],
        job["job_url"], job["apply_url"] or None, job["apply_type"], job["description"]
    ))
    return job["job_id"], True
