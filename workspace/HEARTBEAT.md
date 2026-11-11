# HEARTBEAT.md — OpenClaw Scheduled Checklist

> Executado periodicamente pelo scheduler nativo do OpenClaw. Consulte `AGENTS.md` para a máquina de estados.

## Checklist Operacional por Batimento

1. **Janela Operacional:**
   - Execute `python3 workspace/scripts/queue_guard.py --can-apply`. Se fora do horário (08h-20h), encerre este ciclo.

2. **Estágio 1 — Descoberta Off-Account:**
   - Chame o MCP `job-search-mcp-jobspy` com os filtros de `USER.md` e `workspace/data/profile.json` (LinkedIn, 48h).
   - Consulte `workspace/db/jobs.db` via MCP `sqlite` e descarte `job_id` já cadastrados.
   - Insira novas vagas com status `discovered`.

3. **Estágios 2 e 3 — Curadoria, Tailoring e Notificação:**
   - Para cada vaga com status `discovered`, acione a skill `skills/job-curator/SKILL.md`.
   - Enriquecimento via `stickerdaniel/linkedin-mcp-server` e execução de `python3 workspace/scripts/tailor_resume.py`.
   - Se `EXTERNAL_ATS`: despache mensagem com PDF e link no chat e grave `notified_external`.
   - Se `EASY_APPLY`: envie mensagem com botões de aprovação e grave `pending_approval`.

4. **Estágio 4 — Candidatura Assistida da Fila Aprovada:**
   - Verifique vagas com status `approved` via `python3 workspace/scripts/queue_guard.py --get-next`.
   - Se houver vaga e `queue_guard.py --can-apply` autorizar:
     - Acione a skill `skills/linkedin-easy-apply/SKILL.md` conectada ao OpenClaw Browser Relay.
   - Se surgir pergunta sem mapeamento em `EXPERIENCE_ANSWERS.md`:
     - Marque `needs_input`, envie a pergunta no chat e aguarde intervenção humana.
