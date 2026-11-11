# AGENTS.md — Instruções Operacionais e Máquina de Estados (OpenClaw)

Este documento governa a execução operacional autônoma do agente no OpenClaw, sua máquina de estados e o protocolo de interação com o usuário no chat.

---

## 1. Máquina de Estados da Vaga (`jobs.status`)

| Status | Origem | Descrição e Próximo Passo |
| :--- | :--- | :--- |
| `discovered` | Estágio 1 | Vaga inédita coletada via JobSpy; aguarda enriquecimento no Estágio 2. |
| `scored` | Estágio 2 | Match score avaliado (&ge; 75) e PDF gerado; pronta para envio ao chat. |
| `rejected_score`| Estágio 2 | Match score &lt; 75; descartada silenciosamente. |
| `notified_external`| Estágio 3 | ATS externa enviada no chat com PDF e link direto; aguarda o usuário enviar no site e tocar em "Já me candidatei", que move para `applied` (sem consumir a cota). |
| `pending_approval`| Estágio 3 | Easy Apply enviada no chat; aguarda `APROVAR <id>` ou `PULAR <id>`. |
| `approved` | Chat | Usuário aprovou a vaga; apta para execução pelo Browser Relay. |
| `skipped` | Chat | Usuário descartou a vaga; não será executada. |
| `in_progress` | Estágio 4 | Em preenchimento no navegador pelo Browser Relay. Interrompida no boot, vira `failed` e exige conferência manual. |
| `applied` | Estágio 4 | Candidatura confirmada na tela pelo LinkedIn. |
| `needs_input` | Estágio 4 | Pergunta desconhecida encontrada; pausada até resposta do usuário. |
| `failed` | Estágio 4 | Erro técnico de execução; registrado em `application_logs`. |

---

## 2. Fluxo Operacional dos 4 Estágios

### Estágio 1 — Coleta Off-Account
1. O gatilho do `HEARTBEAT.md` ativa a ferramenta de busca do MCP `job-search-mcp-jobspy` usando os filtros definidos em `USER.md` e `data/profile.json`.
2. Para cada vaga encontrada, consulta o banco `./db/jobs.db` via MCP `sqlite`. Se o `job_id` já existir, descarte imediatamente.
3. Se for inédita, grava no banco com status `discovered`.

### Estágio 2 — Enriquecimento e Tailoring de Currículo
1. Invoca a skill `skills/job-curator/SKILL.md`.
2. Utiliza o MCP `linkedin-mcp-server` (`get_job_details` e `get_job_apply_url`) para obter descrição e classificar como `EASY_APPLY` ou `EXTERNAL_ATS`. Sem essa confirmação, o tipo fica `UNKNOWN` e é definido ao abrir a vaga no Estágio 4.
3. Executa `python3 scripts/tailor_resume.py --job-id <job_id>`:
   - Se `match_score < 75`: altera status para `rejected_score`.
   - Se `match_score >= 75`: compila `output/resumes/<job_id>.pdf` e altera status para `scored`.

### Estágio 3 — Curadoria Humana via Chat Conectado
1. O agente formata e despacha uma mensagem para o canal do usuário (Telegram/WhatsApp):
   - **Cabeçalho:** Cargo, Empresa, Localidade, Modelo, Match Score (0-100%).
   - **Pontos de Fit:** Top 3 motivos de aderência e eventuais gaps.
   - **Roteamento:**
     - Se `EXTERNAL_ATS`: envia PDF em anexo + link direto de candidatura; define status `notified_external`.
     - Se `EASY_APPLY`: informa os comandos disponíveis (`APROVAR <id>` / `PULAR <id>`); define status `pending_approval`.

### Estágio 4 — Candidatura Assistida no Browser Relay
1. Ao receber `APROVAR <job_id>`:
   - Move status para `approved`.
   - Invoca `python3 scripts/queue_guard.py --can-apply`.
   - Se bloqueado por cota (15/dia), horário (fora de 08h-20h) ou jitter (180s-300s), a vaga continua em `approved` até o próximo ciclo seguro.
2. Quando liberado:
   - Invoca a skill `skills/linkedin-easy-apply/SKILL.md` conectada ao **OpenClaw Browser Relay** (Google Chrome local).
   - Valida pré-anexo de `output/resumes/<job_id>.pdf` via `queue_guard.py --verify-resume <id>`.
   - Preenche dados e perguntas de triagem consultando estritamente `data/EXPERIENCE_ANSWERS.md`.
   - Desmarca "Seguir empresa" e submete a candidatura.
   - Atualiza status para `applied` via `queue_guard.py --mark-applied <id>` e notifica sucesso no chat.

---

## 3. Protocolo de Tratamento de Mensagens no Chat

- **Recebido `APROVAR <job_id>`:**
  - Verifica se a vaga existe e não está em `applied`, `in_progress` ou `approved`.
  - Executa: `UPDATE jobs SET status = 'approved' WHERE job_id = ?;`
  - Responde: `✅ Vaga <job_id> aprovada! Ela entrará na fila de candidatura segura.`
- **Recebido `PULAR <job_id>`:**
  - Executa: `UPDATE jobs SET status = 'skipped' WHERE job_id = ?;`
  - Responde: `🗑️ Vaga <job_id> ignorada.`
- **Recebido resposta para vaga em `needs_input`:**
  - O usuário responde pelos botões da pergunta ou toca em "Digitar resposta" e envia a resposta. Outras mensagens seguem o fluxo normal.
  - Todas as perguntas sem resposta de uma etapa do modal chegam juntas (uma mensagem por pergunta); nada é preenchido na tela enquanto houver pendência.
  - Cada resposta vai para `data/learned_answers.json` e `data/EXPERIENCE_ANSWERS.md` (substituindo a anterior). Quando todas as perguntas da pausa têm resposta, a vaga volta para `approved` e a candidatura recomeça do início.
  - Se o usuário já respondeu uma pergunta equivalente, a mensagem traz a sugestão e o botão "✅ Usar". A sugestão nunca é preenchida sozinha: só depois do toque ela é gravada para a pergunta nova, que passa a casar por texto exato.
- **Recebido `/respostas` (ou botão "📝 Minhas respostas" em Meu Perfil):** lista as respostas aprendidas com opções de editar e apagar.
- **Recebido `STATUS`:**
  - Executa `python3 scripts/queue_guard.py --status` e retorna o relatório consolidado de cotas e fila.
