# TOOLS.md — Ferramentas Locais e Utilitários do Workspace

Este documento referencia os caminhos exatos dos recursos, scripts auxiliares e comandos disponíveis para o OpenClaw dentro de `workspace/`.

---

## 1. Banco de Dados SQLite
- **Caminho:** `workspace/db/jobs.db`
- **Schema:** `workspace/db/schema.sql`
- **Tabelas Principais:**
  - `jobs`: Ciclo de vida completo das vagas, match score e paths de currículos.
  - `application_logs`: Registro cronológico estruturado de cada ação.
  - `daily_quotas`: Controle de volume diário de candidaturas para proteção anti-ban.

---

## 2. Dados e Configurações de Perfil
- **Perfil do Candidato:** `workspace/data/profile.json`
- **Matriz de Respostas de Triagem:** `workspace/data/EXPERIENCE_ANSWERS.md`
- **Template de Currículo ATS:** `workspace/templates/resume_ats.html`
- **Diretório de PDFs Gerados:** `workspace/output/resumes/<job_id>.pdf`
- **Diretório de Logs de Depuração:** `workspace/output/logs/`

---

## 3. Scripts Utilitários Locais (CLI)

### 3.1 `workspace/scripts/init_db.py`
Inicializador do banco de dados e diretórios.
- Criar banco e tabelas:
  ```bash
  python3 workspace/scripts/init_db.py
  ```
- Resetar banco do zero:
  ```bash
  python3 workspace/scripts/init_db.py --reset
  ```
- Inserir dados de teste para validação:
  ```bash
  python3 workspace/scripts/init_db.py --seed
  ```

### 3.2 `workspace/scripts/tailor_resume.py`
Motor de scoring ATS e compilação do currículo em PDF sob medida.
- Executar scoring e tailoring para uma vaga:
  ```bash
  python3 workspace/scripts/tailor_resume.py --job-id <job_id>
  ```
- Retorno: JSON estruturado contendo `match_score`, `status`, `match_reasons`, `gaps` e `tailored_resume_path`.

### 3.3 `workspace/scripts/apply_job.py`
Executa uma vaga com status `approved`, sempre respeitando as travas do `queue_guard.py`.
- Candidatar:
  ```bash
  python3 workspace/scripts/apply_job.py --job-id <job_id>
  ```
- Percorrer o modal e anexar o PDF sem enviar:
  ```bash
  python3 workspace/scripts/apply_job.py --job-id <job_id> --dry-run
  ```

### 3.4 `workspace/scripts/migrate_db.py`
Atualiza um `jobs.db` existente para o formato atual. Cria backup e pode ser repetido.
```bash
python3 workspace/scripts/migrate_db.py
```

### 3.5 `workspace/scripts/queue_guard.py`
Guarda de proteção anti-ban e rate-limiting.
- Checar se é permitido aplicar agora (cota, horário 08h-20h, jitter 180-300s):
  ```bash
  python3 workspace/scripts/queue_guard.py --can-apply
  ```
- Recuperar próxima vaga aprovada da fila:
  ```bash
  python3 workspace/scripts/queue_guard.py --get-next
  ```
- Verificar pré-anexo do PDF antes de submeter:
  ```bash
  python3 workspace/scripts/queue_guard.py --verify-resume <job_id>
  ```
- Concluir candidatura e incrementar cota diária:
  ```bash
  python3 workspace/scripts/queue_guard.py --mark-applied <job_id>
  ```
- Registrar vaga em espera por resposta humana:
  ```bash
  python3 workspace/scripts/queue_guard.py --mark-needs-input <job_id> "<pergunta>"
  ```
- Registrar erro técnico:
  ```bash
  python3 workspace/scripts/queue_guard.py --mark-failed <job_id> "<motivo>"
  ```
- Exibir relatório de status:
  ```bash
  python3 workspace/scripts/queue_guard.py --status
  ```
