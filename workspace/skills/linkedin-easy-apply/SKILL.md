---
name: linkedin-easy-apply
version: 2026.1.0
author: Senior AI & Automation Engineer
description: Execução de candidatura assistida Easy Apply no LinkedIn via OpenClaw Browser Relay, com verificação pré-submit de currículo, trava contra alucinação needs_input e travas anti-ban.
triggers:
  - chat_command: "APROVAR *"
  - queue_event: approved_job_ready
tools:
  - openclaw:browser_relay
  - cli:workspace/scripts/queue_guard.py
  - mcp:sqlite
  - mcp:filesystem
---

# Skill: LinkedIn Easy Apply (Estágio 4)

Esta skill executa a automação segura da candidatura no LinkedIn através do **OpenClaw Browser Relay**, utilizando a sessão real autenticada do usuário no Google Chrome local.

---

## 1. Verificações de Segurança e Trava Anti-Ban

Antes de qualquer ação no navegador:
1. Valide as travas de proteção:
   ```bash
   python3 workspace/scripts/queue_guard.py --can-apply
   ```
2. Se o retorno for negativo (`allowed: false`):
   - Não force o envio. Respeite o teto de 15 aplicações/dia, horário (08:00 às 20:00) ou intervalo de resfriamento (180s a 300s).
3. Obtenha a vaga e marque como em progresso:
   ```bash
   python3 workspace/scripts/queue_guard.py --mark-in-progress <job_id>
   ```

---

## 2. Verificação Pré-Submit Obrigatória

Antes de prosseguir para a submissão, valide a existência física e o tamanho do currículo gerado:
```bash
python3 workspace/scripts/queue_guard.py --verify-resume <job_id>
```
Se o arquivo `workspace/output/resumes/<job_id>.pdf` não for válido (&gt; 1000 bytes):
- Interrompa a candidatura.
- Execute:
  ```bash
  python3 workspace/scripts/queue_guard.py --mark-failed <job_id> "Currículo em PDF ausente ou corrompido antes do submit."
  ```

---

## 3. Fluxo de Navegação no OpenClaw Browser Relay

1. **Abertura da Vaga:**
   - Navegue para a `job_url` registrada no banco. Sempre navegue, mesmo com um modal aberto: ele pode ser de outra vaga.
   - Aguarde o carregamento dos seletores do topo da vaga.
   - Localize o botão de candidatura simplificada (`button.jobs-apply-button` ou texto `"Easy Apply"` / `"Candidatura simplificada"`).
   - Se constar o selo *"Candidatou-se"* ou *"Applied X days ago"*, registre como aplicada e encerre. A palavra "Applied" em um cargo não conta.
   - Clique no botão para abrir o modal.

2. **Etapa de Contato (*Contact Info*):**
   - Confirme o e-mail e telefone cadastrados em `workspace/data/profile.json`.
   - Aguarde 2 segundos (jitter humano) e clique em `"Avançar"` / `"Next"`.

3. **Etapa de Currículo (*Resume Upload*):**
   - Na seleção de currículo, anexe o arquivo específico da vaga:
     `workspace/output/resumes/<job_id>.pdf`
   - Aguarde o nome do arquivo aparecer no modal. Sem essa confirmação, marque `failed` e não envie.
   - Clique em `"Avançar"` / `"Next"`.

4. **Etapa de Triagem (*Screening Questions*) e Trava contra Alucinação:**
   - Para cada pergunta apresentada, consulte **exclusivamente** `workspace/data/EXPERIENCE_ANSWERS.md`:
     - Anos em Python: `5`
     - Anos em Engenharia de Software / Dados: `5`
     - Anos em FastAPI / APIs REST: `3`
     - Anos em Agentes de IA / LangChain / RAG: `2`
     - Anos em Automação Web / Playwright: `3`
     - Anos em n8n / RPA / Orquestração: `4`
     - Anos em Power BI / BI: `5`
     - Anos em Docker / Linux: `5`
     - Gênero / Sexo: `"Homem"` / `"Masculino"` / `"Male"` / `"Cisgender"`
     - Orientação Sexual: `"Heterossexual"` / `"Heterosexual"`
     - Raça / Cor / Etnia: `"Branca"` / `"White"` / `"Caucasian"`
     - Pessoa com Deficiência (PCD): `"Não"` / `"Não possuo deficiência"` / `"No"`
     - Autorização para trabalhar no Brasil: `"Yes"` / `"Sim"`
     - Necessidade de visto: `"No"` / `"Não"`
     - Inglês: `"Full Professional"` / `"Fluent"`
     - Aviso prévio: `"2 weeks"` / `"15 dias"`
     - Pretensão PJ: `"18000"` (numérico) ou `"R$ 18.000 / mês negociável"`
     - Pretensão CLT: `"14000"` (numérico) ou `"R$ 14.000 / mês negociável"`
   - ⚠️ **TRAVA DE ALUCINAÇÃO:** Se surgir qualquer pergunta eliminatória não prevista em `EXPERIENCE_ANSWERS.md`:
     - **NÃO ADIVINHE E NÃO CLIQUE EM SUBMETER.**
     - Execute no terminal:
       ```bash
       python3 workspace/scripts/queue_guard.py --mark-needs-input <job_id> "<texto_exato_da_pergunta>"
       ```
     - Envie no chat:
       ```text
       ⏸️ CANDIDATURA PAUSADA — PERGUNTA NÃO MAPEADA
       Vaga: {title} ({company}) - ID: {job_id}
       Pergunta: "{texto_exato_da_pergunta}"
       Por favor, responda com o valor para eu atualizar a base e prosseguir.
       ```
     - Encerre a execução e aguarde a resposta do usuário.

5. **Etapa de Revisão e Envio (*Review & Submit*):**
   - Localize a caixa de seleção para seguir a empresa contratante no LinkedIn.
   - **Desmarque a caixa** para não poluir o feed do usuário.
   - Clique em `"Enviar candidatura"` / `"Submit application"`.
   - Aguarde o feedback de sucesso (`.artdeco-inline-feedback--success` ou modal de confirmação). Sem ele, marque `failed` e peça conferência manual; não registre `applied`.

---

## 4. Finalização e Auditoria

1. Atualize a cota diária e o status para `applied`:
   ```bash
   python3 workspace/scripts/queue_guard.py --mark-applied <job_id>
   ```
2. Envie a confirmação no chat do OpenClaw:
   ```text
   🎉 CANDIDATURA CONCLUÍDA COM SUCESSO!
   💼 Vaga: {title}
   🏢 Empresa: {company}
   🆔 ID: {job_id}
   📄 Currículo enviado: workspace/output/resumes/{job_id}.pdf
   ```
