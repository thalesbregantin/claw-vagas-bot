#!/usr/bin/env python3
"""
workspace/scripts/generate_dashboard.py
Gerador de Dashboard Web Interativo de Follow-Up (FUP) e Inteligência de Vagas.
Gera output/dashboard.html para visualização limpa e executiva do funil e das candidaturas.
"""

import os
import sys
import html
import sqlite3
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ["OPENCLAW_DB_PATH"]) if os.environ.get("OPENCLAW_DB_PATH") else (WORKSPACE_ROOT / "db" / "jobs.db")
OUTPUT_DIR = WORKSPACE_ROOT / "output"
DASHBOARD_HTML_PATH = OUTPUT_DIR / "dashboard.html"

STATUS_LABELS = {
    "applied": ("Candidatado", "badge-applied"),
    "approved": ("Aprovado na Fila", "badge-approved"),
    "in_progress": ("Enviando", "badge-approved"),
    "needs_input": ("Aguardando Resposta", "badge-pending"),
}
OUTCOME_LABELS = {
    "response": "Resposta",
    "interview": "Entrevista",
    "rejected": "Recusada",
    "no_response": "Sem retorno",
}


def esc(value) -> str:
    """Escapa dados raspados das vagas antes de inserir no HTML."""
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(value) -> str:
    url = (value or "").strip()
    return esc(url) if url.lower().startswith(("http://", "https://")) else "#"


def table_exists(cur, name: str) -> bool:
    return cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def column_exists(cur, table: str, column: str) -> bool:
    return any(row[1] == column for row in cur.execute(f"PRAGMA table_info({table})"))


def generate_dashboard():
    if not DB_PATH.exists():
        print(f"[ERRO]: Banco jobs.db não encontrado em {DB_PATH}. Execute init_db.py primeiro.")
        return None
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    total_jobs = cur.execute("SELECT count(*) FROM jobs").fetchone()[0]
    scored = cur.execute("SELECT count(*) FROM jobs WHERE match_score >= 75").fetchone()[0]
    approved = cur.execute("SELECT count(*) FROM jobs WHERE status = 'approved'").fetchone()[0]
    applied = cur.execute("SELECT count(*) FROM jobs WHERE status = 'applied'").fetchone()[0]
    has_outcome = column_exists(cur, "jobs", "outcome")
    outcome_col = "outcome" if has_outcome else "NULL AS outcome"

    # Vagas para tabela de FUP
    cur.execute(f"""
        SELECT job_id, title, company, location, workplace_type, match_score, apply_type, status, job_url,
               applied_at, updated_at, {outcome_col}
        FROM jobs
        WHERE status IN ('applied', 'approved', 'in_progress', 'needs_input', 'scored', 'pending_approval')
        ORDER BY match_score DESC, id DESC
        LIMIT 50
    """)
    fup_rows = cur.fetchall()

    # Retorno pós-envio por faixa de Fit
    bands = []
    if has_outcome:
        bands = cur.execute("""
            SELECT (match_score / 10) * 10 AS band,
                   COUNT(*) AS applied,
                   SUM(outcome IS NOT NULL) AS decided,
                   SUM(outcome IN ('response', 'interview')) AS positive
            FROM jobs WHERE status = 'applied'
            GROUP BY band ORDER BY band DESC
        """).fetchall()

    # Top Habilidades
    skills = []
    if table_exists(cur, "market_skills"):
        skills = cur.execute("""
            SELECT skill_name, mention_count, demand_percentage, in_thales_profile, priority
            FROM market_skills
            ORDER BY mention_count DESC
            LIMIT 10
        """).fetchall()
    conn.close()

    # Monta linhas da tabela
    table_rows_html = []
    for r in fup_rows:
        status_text, status_badge = STATUS_LABELS.get(r["status"], ("Aguardando Aprovação", "badge-pending"))
        type_badge = {"EASY_APPLY": "Easy Apply", "EXTERNAL_ATS": "ATS Externa"}.get(r["apply_type"], "A confirmar")
        date_str = str(r["applied_at"] or r["updated_at"] or "")[:10]
        outcome_text = OUTCOME_LABELS.get(r["outcome"], "Pendente") if r["status"] == "applied" else "—"

        table_rows_html.append(f"""
        <tr>
            <td><strong>{esc(r['title'])}</strong></td>
            <td>{esc(r['company'])}</td>
            <td>{esc(r['location'])} ({esc(r['workplace_type'])})</td>
            <td><span class="score-pill">{int(r['match_score'] or 0)}%</span></td>
            <td><span class="type-pill">{type_badge}</span></td>
            <td><span class="badge {status_badge}">{status_text}</span></td>
            <td>{outcome_text}</td>
            <td>{esc(date_str)}</td>
            <td><a href="{safe_url(r['job_url'])}" target="_blank" rel="noopener noreferrer" class="action-btn">Ver Vaga</a></td>
        </tr>
        """)
    if not table_rows_html:
        table_rows_html.append('<tr><td colspan="9" class="empty">Nenhuma vaga em acompanhamento.</td></tr>')

    # Monta tabela de retorno por faixa de Fit
    bands_html = []
    for b in bands:
        band = int(b["band"] or 0)
        decided = b["decided"] or 0
        positive = b["positive"] or 0
        rate = f"{round(100 * positive / decided)}%" if decided else "—"
        bands_html.append(f"""
        <tr>
            <td>{band}–{min(band + 9, 100)}%</td>
            <td>{b['applied']}</td>
            <td>{decided}</td>
            <td>{positive}</td>
            <td><strong>{rate}</strong></td>
        </tr>
        """)
    if not bands_html:
        bands_html.append('<tr><td colspan="5" class="empty">Registre resultados com /resultados no Telegram.</td></tr>')

    # Monta barras de habilidades
    skills_bars_html = []
    for s in skills:
        is_profile = s["in_thales_profile"] == 1
        bar_color = "var(--accent-blue)" if is_profile else "var(--accent-amber)"
        tag_text = "No Perfil" if is_profile else "Oportunidade / Gap"
        tag_class = "tag-profile" if is_profile else "tag-gap"

        skills_bars_html.append(f"""
        <div class="skill-item">
            <div class="skill-header">
                <span class="skill-name">{esc(s['skill_name'])} <span class="tag {tag_class}">{tag_text}</span></span>
                <span class="skill-val">{float(s['demand_percentage'] or 0):g}% ({int(s['mention_count'] or 0)} vagas)</span>
            </div>
            <div class="progress-bar-bg">
                <div class="progress-bar-fill" style="width: {min(max(float(s['demand_percentage'] or 0), 0), 100):g}%; background: {bar_color};"></div>
            </div>
        </div>
        """)

    html_content = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenClaw — Painel de Follow-Up e Candidaturas</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --surface-hover: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --border: #334155;
            --accent-blue: #38bdf8;
            --accent-green: #34d399;
            --accent-amber: #fbbf24;
            --accent-purple: #c084fc;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ background: var(--bg); color: var(--text-main); padding: 32px 24px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        header {{ margin-bottom: 32px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 20px; }}
        h1 {{ font-size: 24px; font-weight: 600; color: var(--text-main); }}
        .header-meta {{ color: var(--text-muted); font-size: 14px; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 32px; }}
        .stat-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
        .stat-label {{ color: var(--text-muted); font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }}
        .stat-value {{ font-size: 28px; font-weight: 700; color: var(--text-main); }}
        .stat-sub {{ font-size: 13px; color: var(--text-muted); margin-top: 4px; }}
        .section-title {{ font-size: 18px; font-weight: 600; margin-bottom: 16px; }}
        .content-grid {{ display: grid; grid-template-columns: 2fr 1fr; gap: 24px; margin-bottom: 32px; }}
        @media (max-width: 900px) {{ .content-grid {{ grid-template-columns: 1fr; }} }}
        .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; }}
        .skill-item {{ margin-bottom: 16px; }}
        .skill-header {{ display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 6px; }}
        .progress-bar-bg {{ background: #0f172a; height: 8px; border-radius: 4px; overflow: hidden; }}
        .progress-bar-fill {{ height: 100%; border-radius: 4px; }}
        .tag {{ font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }}
        .tag-profile {{ background: rgba(56, 189, 248, 0.2); color: var(--accent-blue); }}
        .tag-gap {{ background: rgba(251, 191, 36, 0.2); color: var(--accent-amber); }}
        table {{ width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; }}
        th {{ background: #0f172a; color: var(--text-muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; padding: 12px 14px; border-bottom: 1px solid var(--border); }}
        td {{ padding: 14px; border-bottom: 1px solid var(--border); }}
        tr:hover td {{ background: var(--surface-hover); }}
        .score-pill {{ background: rgba(52, 211, 153, 0.15); color: var(--accent-green); padding: 4px 8px; border-radius: 6px; font-weight: 600; font-size: 13px; }}
        .type-pill {{ background: rgba(192, 132, 252, 0.15); color: var(--accent-purple); padding: 4px 8px; border-radius: 6px; font-size: 12px; }}
        .badge {{ padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: 500; }}
        .badge-applied {{ background: rgba(52, 211, 153, 0.2); color: var(--accent-green); }}
        .badge-approved {{ background: rgba(56, 189, 248, 0.2); color: var(--accent-blue); }}
        .badge-pending {{ background: rgba(251, 191, 36, 0.2); color: var(--accent-amber); }}
        .action-btn {{ color: var(--accent-blue); text-decoration: none; font-size: 13px; }}
        .action-btn:hover {{ text-decoration: underline; }}
        .empty {{ color: var(--text-muted); text-align: center; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>Painel de Acompanhamento & Follow-Up (FUP)</h1>
                <div class="header-meta">Atualizado em {datetime.now().strftime('%d/%m/%Y às %H:%M')}</div>
            </div>
        </header>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Total de Vagas Mineradas</div>
                <div class="stat-value">{total_jobs}</div>
                <div class="stat-sub">Mercado Brasil & Remoto</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Qualificadas (Fit ≥ 75%)</div>
                <div class="stat-value">{scored}</div>
                <div class="stat-sub">Com currículo sob medida gerado</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Candidaturas Realizadas</div>
                <div class="stat-value" style="color: var(--accent-green);">{applied}</div>
                <div class="stat-sub">Submetidas no LinkedIn / ATS</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Em Fila de Aprovação</div>
                <div class="stat-value" style="color: var(--accent-blue);">{approved}</div>
                <div class="stat-sub">Prontas para envio no navegador</div>
            </div>
        </div>

        <div class="content-grid">
            <div class="card">
                <h2 class="section-title">Acompanhamento de Vagas (Follow-Up)</h2>
                <div style="overflow-x: auto;">
                    <table>
                        <thead>
                            <tr>
                                <th>Cargo</th>
                                <th>Empresa</th>
                                <th>Local</th>
                                <th>Fit</th>
                                <th>Tipo</th>
                                <th>Status</th>
                                <th>Resultado</th>
                                <th>Data</th>
                                <th>Ação</th>
                            </tr>
                        </thead>
                        <tbody>
                            {"".join(table_rows_html)}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="card">
                <h2 class="section-title">Top Demandas de Mercado</h2>
                <div style="margin-bottom: 16px; font-size: 13px; color: var(--text-muted);">
                    Frequência de requisitos nas {total_jobs} vagas do banco:
                </div>
                {"".join(skills_bars_html) or '<div class="empty">Rode market_intelligence.py para preencher.</div>'}
            </div>
        </div>

        <div class="card" style="margin-bottom: 32px;">
            <h2 class="section-title">Retorno por Faixa de Fit</h2>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr><th>Fit</th><th>Enviadas</th><th>Com resultado</th><th>Resposta/Entrevista</th><th>Taxa</th></tr>
                    </thead>
                    <tbody>
                        {"".join(bands_html)}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
"""

    with open(DASHBOARD_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"✅ Dashboard FUP gerado em: {DASHBOARD_HTML_PATH}")
    return DASHBOARD_HTML_PATH


if __name__ == "__main__":
    generate_dashboard()
