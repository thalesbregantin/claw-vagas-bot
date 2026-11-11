---
name: job-curator
version: 2026.1.0
author: Senior AI & Automation Engineer
description: Curadoria, enriquecimento de vagas, cálculo de match score ATS, personalização de currículo em PDF e envio estruturado para curadoria humana no chat do OpenClaw.
triggers:
  - heartbeat: scheduled
  - event: new_job_discovered
tools:
  - mcp:job-search-mcp-jobspy
  - mcp:linkedin-mcp-server
  - mcp:sqlite
  - mcp:filesystem
  - cli:workspace/scripts/tailor_resume.py
---

# Skill: Job Curator (Estágios 1, 2 e 3)

Esta skill executa o pipeline autônomo de busca, enriquecimento de detalhes, cálculo de fit técnico, geração de currículo ATS sob medida e despacho para aprovação humana no chat nativo do OpenClaw.

---

## Procedimento de Execução

### 1. Descoberta Off-Account
1. Leia `workspace/data/profile.json` e `workspace/USER.md`.
2. Acione `job-search-mcp-jobspy` para coletar vagas recentes no LinkedIn:
   - Cargos: "Senior AI Engineer", "AI Automation Engineer", "Senior Python Engineer", "LLM Architect".
   - Localidade: "Brazil" / "São Paulo" (remoto e híbrido).
   - Janela: últimas 48 horas.
3. Consulte `workspace/db/jobs.db` via MCP `sqlite`:
   ```sql
   SELECT id FROM jobs WHERE job_id = ?;
   ```
4. Se a vaga já existir, ignore. Se for inédita, insira:
   ```sql
   INSERT INTO jobs (job_id, title, company, location, workplace_type, job_url, status)
   VALUES (?, ?, ?, ?, ?, ?, 'discovered');
   ```

### 2. Enriquecimento e Classificação
Para cada vaga com status `discovered`:
1. Use `stickerdaniel/linkedin-mcp-server`:
   - Execute `get_job_details` para extrair descrição completa e requisitos.
   - Execute `get_job_apply_url` para identificar o método de candidatura.
2. Se a aplicação for feita diretamente no modal do LinkedIn:
   - Defina `apply_type = 'EASY_APPLY'` e `apply_url = NULL`.
3. Se redirecionar para portal externo (Gupy, Greenhouse, Workday, Lever, etc.):
   - Defina `apply_type = 'EXTERNAL_ATS'` e armazene a URL direta em `apply_url`.
4. Atualize o registro no SQLite:
   ```sql
   UPDATE jobs SET description = ?, apply_type = ?, apply_url = ? WHERE job_id = ?;
   ```

### 3. Match Score e Tailoring de Currículo
1. Execute o script de personalização de currículo:
   ```bash
   python3 workspace/scripts/tailor_resume.py --job-id <job_id>
   ```
2. O script avalia a aderência (0 a 100):
   - **Se `match_score < 75`:** o script marca `status = 'rejected_score'`. Encerre o processamento desta vaga.
   - **Se `match_score >= 75`:**
     - O script gera o PDF customizado em `workspace/output/resumes/<job_id>.pdf` destacando competências reais do candidato.
     - Atualiza `status = 'scored'` e armazena o caminho do currículo no banco.

### 4. Notificação e Curadoria no Chat do OpenClaw
Para vagas pontuadas (`status = 'scored'`):
1. Monte a mensagem formatada para o conector de chat do OpenClaw (Telegram ou WhatsApp):

```text
🎯 NOVA VAGA QUALIFICADA — MATCH SCORE: {match_score}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💼 Cargo: {title}
🏢 Empresa: {company}
📍 Local: {location} ({workplace_type})
🏷️ Tipo: {apply_type}
🆔 ID: {job_id}

🌟 Top 3 Motivos de Aderência:
1. {match_reasons[0]}
2. {match_reasons[1]}
3. {match_reasons[2]}

⚠️ Gaps Notados:
{gaps}

📄 Currículo Otimizado Gerado:
workspace/output/resumes/{job_id}.pdf
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

2. **Roteamento Condicional:**
   - **Se `EXTERNAL_ATS`:**
     - Anexe o arquivo PDF gerado na mensagem do chat.
     - Envie o link direto para candidatura manual do usuário.
     - Atualize o status no SQLite:
       ```sql
       UPDATE jobs SET status = 'notified_external', notified_at = CURRENT_TIMESTAMP WHERE job_id = ?;
       ```
   - **Se `EASY_APPLY`:**
     - Inclua os comandos de ação no rodapé:
       ```text
       ⚙️ Comandos de Aprovação:
       • Digite "APROVAR {job_id}" para candidatar via Chrome Relay
       • Digite "PULAR {job_id}" para ignorar a vaga
       ```
     - Atualize o status no SQLite:
       ```sql
       UPDATE jobs SET status = 'pending_approval', notified_at = CURRENT_TIMESTAMP WHERE job_id = ?;
       ```
