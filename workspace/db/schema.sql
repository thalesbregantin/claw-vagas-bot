-- ==============================================================================
-- Schema do Banco de Dados - LinkedIn OpenClaw Agent
-- Arquivo: workspace/db/schema.sql
-- Tabelas obrigatórias: jobs, application_logs, daily_quotas
-- ==============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ------------------------------------------------------------------------------
-- 1. Tabela Principal de Vagas (jobs)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,                 -- ID exclusivo da vaga no LinkedIn
    title TEXT NOT NULL,                        -- Título do cargo
    company TEXT NOT NULL,                      -- Nome da empresa contratante
    location TEXT,                              -- Localização divulgada
    workplace_type TEXT DEFAULT 'Remote',       -- 'Remote', 'Hybrid', 'On-site'
    job_url TEXT NOT NULL,                      -- URL original no LinkedIn
    apply_url TEXT,                             -- URL externa de candidatura direta (se ATS externa)
    apply_type TEXT NOT NULL DEFAULT 'UNKNOWN',  -- 'EASY_APPLY', 'EXTERNAL_ATS', 'UNKNOWN' (LinkedIn: confirmado ao abrir a vaga)
    description TEXT,                           -- Descrição completa da vaga
    match_score INTEGER DEFAULT 0,              -- Pontuação de aderência (0 a 100)
    match_reasons TEXT,                         -- JSON array com top motivos de aderência
    gaps TEXT,                                  -- JSON array com lacunas/requisitos faltantes
    tailored_resume_path TEXT,                  -- Caminho do PDF gerado (output/resumes/<job_id>.pdf)
    status TEXT NOT NULL DEFAULT 'discovered',  -- Ciclo de vida:
                                                -- 'discovered'       -> Coletada no Estágio 1
                                                -- 'scored'           -> Avaliada e currículo gerado
                                                -- 'rejected_score'   -> Match score < 75
                                                -- 'notified_external'-> ATS externa enviada ao chat com PDF
                                                -- 'pending_approval' -> Easy Apply aguardando comando APROVAR
                                                -- 'approved'         -> Aprovada pelo usuário
                                                -- 'skipped'          -> Descartada pelo usuário (PULAR)
                                                -- 'in_progress'      -> Em execução pelo Browser Relay
                                                -- 'applied'          -> Candidatura submetida com sucesso
                                                -- 'failed'           -> Erro técnico durante execução
                                                -- 'needs_input'      -> Pergunta desconhecida aguardando resposta humana
    chat_message_id TEXT,                       -- ID da mensagem no Telegram/WhatsApp
    error_log TEXT,                             -- Detalhes de falha ou pergunta pendente
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scored_at TIMESTAMP,
    notified_at TIMESTAMP,
    applied_at TIMESTAMP,
    outcome TEXT,                               -- Resultado pós-envio: 'response', 'interview', 'rejected', 'no_response'
    outcome_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs (job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_match_score ON jobs (match_score);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs (discovered_at);

-- ------------------------------------------------------------------------------
-- 2. Tabela de Logs e Auditoria Estruturada (application_logs)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS application_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    step TEXT NOT NULL,                         -- 'discovery', 'enrichment', 'scoring', 'tailoring', 'notification', 'easy_apply_step', 'pre_submit_check', 'applied', 'failed', 'needs_input'
    status TEXT NOT NULL,                       -- 'info', 'success', 'warning', 'error'
    message TEXT NOT NULL,                      -- Registro descritivo da ação
    payload TEXT,                               -- Dados JSON complementares
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs (job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_logs_job_id ON application_logs (job_id);
CREATE INDEX IF NOT EXISTS idx_logs_created_at ON application_logs (created_at);

-- ------------------------------------------------------------------------------
-- 3. Tabela de Controle de Cotas Diárias Anti-Ban (daily_quotas)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_quotas (
    date TEXT PRIMARY KEY,                      -- Formato 'YYYY-MM-DD'
    count INTEGER NOT NULL DEFAULT 0,           -- Total de candidaturas realizadas na data
    max_limit INTEGER NOT NULL DEFAULT 15,      -- Teto máximo configurável (padrão: 15)
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ------------------------------------------------------------------------------
-- 4. Inteligência de Mercado (market_skills)
-- ------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL UNIQUE,
    category TEXT,
    mention_count INTEGER DEFAULT 0,
    demand_percentage REAL DEFAULT 0.0,
    in_thales_profile BOOLEAN DEFAULT 0,
    priority TEXT DEFAULT 'LOW',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Trigger para manter jobs.updated_at atualizado
CREATE TRIGGER IF NOT EXISTS trg_jobs_updated_at
AFTER UPDATE ON jobs
FOR EACH ROW
BEGIN
    UPDATE jobs SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
END;
