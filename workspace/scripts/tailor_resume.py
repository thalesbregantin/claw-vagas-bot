#!/usr/bin/env python3
"""
workspace/scripts/tailor_resume.py
Motor de personalização e geração de currículo sob medida para ATS (Estágio 2).
Cruza os requisitos da vaga com data/profile.json, calcula match_score (0 a 100),
seleciona e reorganiza competências/conquistas, renderiza via Jinja2
(templates/resume_ats.html) e compila o PDF otimizado em output/resumes/<job_id>.pdf.
"""

import os
import sys
import json
import sqlite3
import argparse
import subprocess
import tempfile
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ["OPENCLAW_DATA_DIR"]) if os.environ.get("OPENCLAW_DATA_DIR") else (WORKSPACE_ROOT / "data")
PROFILE_PATH = DATA_DIR / "profile.json"
TEMPLATES_DIR = WORKSPACE_ROOT / "templates"
OUTPUT_DIR = Path(os.environ["OPENCLAW_OUTPUT_DIR"]) if os.environ.get("OPENCLAW_OUTPUT_DIR") else (WORKSPACE_ROOT / "output")
RESUMES_DIR = OUTPUT_DIR / "resumes"
LOGS_DIR = OUTPUT_DIR / "logs"
DB_PATH = Path(os.environ["OPENCLAW_DB_PATH"]) if os.environ.get("OPENCLAW_DB_PATH") else (WORKSPACE_ROOT / "db" / "jobs.db")

TRACKED_KEYWORDS = [
    "python", "fastapi", "flask", "django", "langchain", "llamaindex",
    "openclaw", "agents", "agentes", "autonomous agents", "rag", "vector db",
    "qdrant", "pinecone", "chromadb", "pgvector", "docker", "kubernetes",
    "k8s", "aws", "gcp", "azure", "postgresql", "postgres", "sql", "redis",
    "celery", "playwright", "selenium", "scraping", "web scraping",
    "machine learning", "ml", "ai", "ia", "artificial intelligence", "deep learning",
    "pytorch", "tensorflow", "prompt engineering", "mcp", "model context protocol",
    "rest", "restful", "api", "microservices", "microsserviços", "ci/cd",
    "github actions", "linux", "bash", "typescript", "node.js", "graphql",
    "n8n", "ollama", "rpa", "uipath", "pytest", "pandas", "opencv", "bigquery",
    "etl", "power bi", "looker"
]

# Stack principal que não existe no perfil: no título da vaga, indica requisito central ausente
FOREIGN_STACK = [
    "java", "spring", "golang", "c#", ".net", "ruby", "php", "scala", "kotlin", "swift",
    "rust", "c++", "salesforce", "sap", "abap", "cobol", "flutter", "react native",
    "angular", "vue", "android", "ios"
]

OFF_TARGET_TITLE_TERMS = [
    "sales", "pre-sales", "post-sales", "vendas", "comercial", "marketing", "recruiter",
    "recrutador", "account manager", "customer success", "logic design", "hardware",
    "mainframe", "trainer", "retail", "seo", "writer", "redator", "redatora", "content",
    "operations manager", "it service", "administrator", "consultant"
]

TARGET_TITLE_TERMS = ["ai", "ia", "python", "llm", "genai", "agentic", "machine learning", "inteligência artificial"]
TECH_TITLE_TERMS = [
    "software", "backend", "back-end", "developer", "desenvolvedor", "desenvolvedora", "engineer",
    "engenheiro", "engenheira", "automation", "automação", "data", "dados", "analytics", "architect", "arquiteto"
]

KEYWORD_ALIASES = {
    "ai": ["ia", "inteligência artificial"],
    "ia": ["ai"],
    "artificial intelligence": ["inteligência artificial"],
    "agents": ["agentes"],
    "agentes": ["agents"],
    "postgres": ["postgresql"],
    "api": ["apis"],
    "rest": ["restful"],
    "microservices": ["microsserviços"],
    "microsserviços": ["microservices"],
    "scraping": ["playwright"],
    "web scraping": ["playwright"],
    "prompt engineering": ["engenharia de prompt"],
    "looker": ["looker studio"]
}

SCORE_CAP_OFF_TARGET = 60
SCORE_CAP_INTERN = 40


def load_profile() -> Dict[str, Any]:
    """Carrega dados do perfil e valida campos essenciais."""
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(f"Arquivo de perfil não encontrado em: {PROFILE_PATH}")
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    """Normaliza texto para comparações."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[\r\n\t]+', ' ', text)
    return text


def has_term(term: str, text: str) -> bool:
    """Busca por termo inteiro: 'ai' não casa com 'retail', nem 'ml' com 'html'."""
    return re.search(r"(?<![\w+#.])" + re.escape(term) + r"(?![\w+#])", text) is not None


def has_any_term(terms: List[str], text: str) -> bool:
    return any(has_term(t, text) for t in terms)


def extract_keywords_from_job(title: str, description: str) -> List[str]:
    """Extrai palavras-chave tecnológicas do título e descrição da vaga."""
    full_text = normalize_text(f"{title} {description}")
    return [kw for kw in TRACKED_KEYWORDS + FOREIGN_STACK if has_term(kw, full_text)]


def build_profile_text(profile: Dict[str, Any]) -> str:
    """Reúne tudo que o candidato de fato declara ter feito ou conhecer."""
    parts = [profile.get("summary", "")]
    for skill_list in profile.get("skills", {}).values():
        parts.extend(skill_list)
    for experience in profile.get("experience", []):
        parts.append(experience.get("description", ""))
        parts.extend(experience.get("achievements", []))
    parts.extend(profile.get("search_criteria", {}).get("target_keywords", []))
    return normalize_text(" ".join(parts))


def profile_has_keyword(keyword: str, profile_text: str) -> bool:
    return has_any_term([keyword] + KEYWORD_ALIASES.get(keyword, []), profile_text)


def matched_job_keywords(profile: Dict[str, Any], title: str, description: str) -> List[str]:
    """Palavras-chave da vaga comprovadas pelo perfil. É o único conteúdo que pode ir ao currículo."""
    profile_text = build_profile_text(profile)
    return [kw for kw in extract_keywords_from_job(title, description) if profile_has_keyword(kw, profile_text)]


def calculate_match_score(
    profile: Dict[str, Any],
    job_title: str,
    job_description: str,
    workplace_type: str = "Remote",
    location: str = ""
) -> Tuple[int, List[str], List[str]]:
    """Calcula o match_score (0 a 100) de forma determinística."""
    score = 0
    reasons = []
    gaps = []
    max_score = 100

    title_norm = normalize_text(job_title)
    desc_norm = normalize_text(job_description)
    target_roles = [r.lower() for r in profile.get("search_criteria", {}).get("target_roles", [])]

    # 1. Título do Cargo (máx 25 pts)
    foreign_in_title = [t for t in FOREIGN_STACK if has_term(t, title_norm)]
    if has_any_term(OFF_TARGET_TITLE_TERMS, title_norm):
        score += 5
        max_score = SCORE_CAP_OFF_TARGET
        gaps.append(f"Área fora do foco técnico ({job_title})")
    elif foreign_in_title:
        score += 5
        max_score = SCORE_CAP_OFF_TARGET
        gaps.append(f"Stack principal fora do perfil: {', '.join(foreign_in_title).upper()}")
    elif any(role in title_norm for role in target_roles) or has_any_term(TARGET_TITLE_TERMS, title_norm):
        score += 25
        reasons.append(f"Título da vaga alinhado aos cargos-alvo ({job_title})")
    elif has_any_term(TECH_TITLE_TERMS, title_norm):
        score += 15
        reasons.append(f"Título compatível com engenharia de software ({job_title})")
    else:
        score += 8
        gaps.append(f"Título fora do foco primário ({job_title})")

    # 2. Competências Técnicas (máx 45 pts)
    job_keywords = extract_keywords_from_job(job_title, job_description)
    if job_keywords:
        profile_text = build_profile_text(profile)
        matched_skills = [kw for kw in job_keywords if profile_has_keyword(kw, profile_text)]
        unmatched_skills = [kw for kw in job_keywords if kw not in matched_skills]

        # Com menos de 3 tecnologias em comum, a proporção sozinha não comprova aderência
        ratio = len(matched_skills) / len(job_keywords)
        confidence = min(1.0, len(matched_skills) / 3)
        score += int(ratio * confidence * 45)

        if matched_skills:
            reasons.append(f"Tecnologias da vaga presentes no seu perfil: {', '.join(matched_skills[:5]).upper()}")
        if unmatched_skills:
            gaps.append(f"Tecnologias da vaga ausentes do seu perfil: {', '.join(unmatched_skills[:3]).upper()}")
    else:
        score += 15
        gaps.append("Nenhuma tecnologia reconhecida na descrição da vaga")

    # 3. Formato de Trabalho (máx 15 pts)
    wp_norm = normalize_text(workplace_type)
    place_norm = normalize_text(location)
    in_sao_paulo = "são paulo" in place_norm or "sao paulo" in place_norm or has_term("sp", place_norm)
    if "remote" in wp_norm or any(k in desc_norm for k in ["100% remot", "totalmente remot", "trabalho remoto", "fully remote", "full remote"]):
        score += 15
        reasons.append("Modelo remoto compatível com sua preferência")
    elif "on-site" in wp_norm or "presencial" in desc_norm:
        score += 3
        gaps.append("Modelo presencial")
    elif in_sao_paulo:
        score += 12
        reasons.append("Modelo híbrido em São Paulo")
    else:
        score += 6
        gaps.append("Modelo híbrido fora de São Paulo")

    # 4. Senioridade (máx 15 pts)
    if has_any_term(["estágio", "estagiário", "estagiária", "intern", "internship", "trainee"], title_norm):
        score += 2
        max_score = min(max_score, SCORE_CAP_INTERN)
        gaps.append("Vaga de estágio ou trainee")
    elif has_any_term(["junior", "júnior", "jr"], title_norm):
        score += 2
        gaps.append("Nível júnior abaixo da senioridade do candidato")
    elif has_any_term(["senior", "sênior", "sr", "lead", "staff", "especialista", "principal"], title_norm):
        score += 15
        reasons.append("Senioridade do cargo compatível com seu histórico")
    elif has_any_term(["pleno", "mid", "mid-level"], title_norm):
        score += 12
        reasons.append("Nível pleno compatível com seu histórico")
    else:
        score += 10

    final_score = max(0, min(max_score, score))
    return final_score, reasons[:3], gaps[:3]


def tailor_resume_content(
    profile: Dict[str, Any],
    job_title: str,
    job_company: str,
    job_id: str,
    job_description: str
) -> Dict[str, Any]:
    """Reorganiza competências para destacar relevância à vaga sem inventar dados."""
    job_keywords = matched_job_keywords(profile, job_title, job_description)

    tailored_summary = profile.get("summary", "")
    if job_keywords:
        lead_keywords = ", ".join(k.upper() for k in job_keywords[:4])
        tailored_summary = f"{tailored_summary} Experiência prática com {lead_keywords}."

    skills_dict = profile.get("skills", {})

    def sort_skills_by_relevance(skills_list: List[str]) -> List[str]:
        def match_count(skill: str) -> int:
            norm = skill.lower()
            return sum(1 for kw in job_keywords if kw in norm)
        return sorted(skills_list, key=match_count, reverse=True)

    skills_ai_llm = sort_skills_by_relevance(skills_dict.get("ai_and_llm", []))
    skills_backend = sort_skills_by_relevance(skills_dict.get("backend_and_languages", []))
    skills_automation = sort_skills_by_relevance(skills_dict.get("automation_and_scraping", []))
    
    dados_qualidade = skills_dict.get("dados_bi_e_qualidade", [])
    cloud_list = skills_dict.get("cloud_and_devops", [])
    db_list = skills_dict.get("databases", [])
    skills_cloud_db = sort_skills_by_relevance(dados_qualidade or (cloud_list + db_list))

    return {
        "personal": profile.get("personal", {}),
        "target_job": {
            "title": job_title,
            "company": job_company,
            "job_id": job_id
        },
        "tailored_summary": tailored_summary,
        "skills_ai_llm": skills_ai_llm,
        "skills_backend": skills_backend,
        "skills_automation": skills_automation,
        "skills_cloud_db": skills_cloud_db,
        "experiences": profile.get("experience", []),
        "education": profile.get("education", []),
        "certifications": profile.get("certifications", []),
        "languages": profile.get("languages", [])
    }


def render_html(template_data: Dict[str, Any]) -> str:
    """Renderiza HTML via Jinja2."""
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("resume_ats.html")
    return template.render(**template_data)


def render_pdf(html_content: str, output_pdf_path: Path) -> bool:
    """Compilação resiliente: WeasyPrint com fallback automático para Chrome/Edge Headless."""
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Tentativa WeasyPrint
    try:
        import io
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            import weasyprint
            weasyprint.HTML(string=html_content, base_url=str(TEMPLATES_DIR)).write_pdf(str(output_pdf_path))
        if output_pdf_path.exists() and output_pdf_path.stat().st_size > 1000:
            return True
    except (ImportError, OSError, Exception):
        pass

    # 2. Fallback Chrome/Edge Headless
    browser_executables = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "google-chrome",
        "chrome",
        "msedge"
    ]

    selected_browser = None
    for b in browser_executables:
        if os.path.exists(b):
            selected_browser = b
            break

    if not selected_browser:
        import shutil
        selected_browser = shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("msedge")

    if not selected_browser:
        raise RuntimeError("Nenhum motor de compilação PDF disponível (instale Chrome, Edge ou bibliotecas WeasyPrint).")

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as temp_html:
        temp_html.write(html_content)
        temp_html_path = temp_html.name

    try:
        cmd = [
            selected_browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={str(output_pdf_path.resolve())}",
            temp_html_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return res.returncode == 0 and output_pdf_path.exists() and output_pdf_path.stat().st_size > 1000
    finally:
        if os.path.exists(temp_html_path):
            try:
                os.remove(temp_html_path)
            except Exception:
                pass


def process_job_tailoring(job_id: str, force_pdf: bool = False) -> Dict[str, Any]:
    """Processa scoring e tailoring. force_pdf gera o PDF de vagas aprovadas abaixo do corte."""
    profile = load_profile()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT title, company, description, workplace_type, status, location FROM jobs WHERE job_id = ?", (job_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise ValueError(f"Vaga {job_id} não encontrada em {DB_PATH}")

    job_title, job_company, job_desc, workplace_type, current_status, location = row
    job_desc = job_desc or ""

    match_score, match_reasons, gaps = calculate_match_score(
        profile=profile,
        job_title=job_title,
        job_description=job_desc,
        workplace_type=workplace_type or "Remote",
        location=location or ""
    )

    min_score = profile.get("search_criteria", {}).get("min_match_score", 75)
    pdf_rel_path = None
    new_status = current_status

    if match_score >= min_score or force_pdf:
        template_data = tailor_resume_content(
            profile=profile,
            job_title=job_title,
            job_company=job_company,
            job_id=job_id,
            job_description=job_desc
        )
        html_content = render_html(template_data)
        # O job_id garante um arquivo por vaga: cargos iguais em empresas diferentes não colidem
        if render_pdf(html_content, RESUMES_DIR / f"{job_id}.pdf"):
            pdf_rel_path = f"output/resumes/{job_id}.pdf"

    # Só a triagem inicial muda o status: vagas aprovadas ou em andamento seguem seu fluxo
    if current_status in ["discovered", "scored", "rejected_score"]:
        new_status = "scored" if match_score >= min_score else "rejected_score"

    cursor.execute("""
        UPDATE jobs
        SET match_score = ?,
            match_reasons = ?,
            gaps = ?,
            tailored_resume_path = COALESCE(?, tailored_resume_path),
            status = ?,
            scored_at = CURRENT_TIMESTAMP
        WHERE job_id = ?
    """, (
        match_score,
        json.dumps(match_reasons, ensure_ascii=False),
        json.dumps(gaps, ensure_ascii=False),
        pdf_rel_path,
        new_status,
        job_id
    ))

    cursor.execute("""
        INSERT INTO application_logs (job_id, step, status, message, payload)
        VALUES (?, 'scoring', 'info', ?, ?)
    """, (
        job_id,
        f"Scoring concluído ({match_score}%). Status: {new_status}",
        json.dumps({"match_score": match_score, "resume_path": pdf_rel_path, "top_reasons": match_reasons}, ensure_ascii=False)
    ))

    conn.commit()
    conn.close()

    return {
        "job_id": job_id,
        "match_score": match_score,
        "status": new_status,
        "match_reasons": match_reasons,
        "gaps": gaps,
        "tailored_resume_path": pdf_rel_path
    }


def main():
    parser = argparse.ArgumentParser(description="Tailor de Currículo ATS do OpenClaw")
    parser.add_argument("--job-id", required=True, help="ID da vaga no banco jobs.db")
    parser.add_argument("--force-pdf", action="store_true", help="Gera o PDF mesmo abaixo do corte de match_score")
    args = parser.parse_args()

    try:
        res = process_job_tailoring(args.job_id, force_pdf=args.force_pdf)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[ERRO]: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
