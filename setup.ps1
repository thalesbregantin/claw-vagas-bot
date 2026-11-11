# ==============================================================================
# setup.ps1 — Script de Instalação e Ativação do LinkedIn OpenClaw Agent (PowerShell)
# Configura o ambiente no runtime oficial do OpenClaw ($HOME\.openclaw\) no Windows.
# ==============================================================================

$ErrorActionPreference = "Stop"

$OpenClawDir = Join-Path $HOME ".openclaw"
$WorkspaceDir = Join-Path $OpenClawDir "workspace"
$ConfigFile = Join-Path $OpenClawDir "openclaw.json"
$ScriptDir = $PSScriptRoot

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "    INICIALIZAÇÃO DO LINKEDIN AGENT NO RUNTIME OPENCLAW (WINDOWS) " -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

# 1. Cria diretórios do OpenClaw
Write-Host "[1/6] Criando diretórios base em $OpenClawDir..." -ForegroundColor Yellow
$Directories = @(
    (Join-Path $WorkspaceDir "db"),
    (Join-Path $WorkspaceDir "data"),
    (Join-Path $WorkspaceDir "templates"),
    (Join-Path $WorkspaceDir "scripts"),
    (Join-Path $WorkspaceDir "skills\job-curator"),
    (Join-Path $WorkspaceDir "skills\linkedin-easy-apply"),
    (Join-Path $WorkspaceDir "output\resumes"),
    (Join-Path $WorkspaceDir "output\logs")
)

foreach ($dir in $Directories) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

# 2. Configura ambiente virtual Python e dependências
Write-Host "[2/6] Configurando ambiente Python em $WorkspaceDir\.venv..." -ForegroundColor Yellow
$VenvDir = Join-Path $WorkspaceDir ".venv"
if (-not (Test-Path $VenvDir)) {
    python -m venv $VenvDir
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    $VenvPython = "python"
}

Write-Host " -> Instalando dependências de requirements.txt..."
& $VenvPython -m pip install --upgrade pip --quiet
& $VenvPython -m pip install -r (Join-Path $ScriptDir "requirements.txt") --quiet

# 3. Sincroniza arquivos do workspace
Write-Host "[3/6] Sincronizando workspace para $WorkspaceDir..." -ForegroundColor Yellow
# Banco, perfil, respostas, segredos e PDFs existentes no destino nunca são sobrescritos
$Preserved = @("jobs.db", "jobs.db-wal", "jobs.db-shm", "profile.json", "EXPERIENCE_ANSWERS.md", "learned_answers.json", "user_negative_preferences.json", ".env")
$SourceRoot = Join-Path $ScriptDir "workspace"
Get-ChildItem -Path $SourceRoot -Recurse -File -Force |
    Where-Object { $_.FullName -notmatch '\\output\\' -and $_.FullName -notmatch '\\__pycache__\\' } |
    ForEach-Object {
        $Target = Join-Path $WorkspaceDir $_.FullName.Substring($SourceRoot.Length + 1)
        if (($Preserved -contains $_.Name) -and (Test-Path $Target)) { return }
        New-Item -ItemType Directory -Path (Split-Path $Target) -Force | Out-Null
        Copy-Item -Path $_.FullName -Destination $Target -Force
    }

# 4. Inicializa o banco de dados SQLite jobs.db
Write-Host "[4/6] Inicializando banco SQLite e cotas diárias..." -ForegroundColor Yellow
& $VenvPython (Join-Path $WorkspaceDir "scripts\init_db.py")

# 5. Mescla configuração de MCPs no openclaw.json
Write-Host "[5/6] Configurando servidores MCP em $ConfigFile..." -ForegroundColor Yellow
$PatchFile = Join-Path $ScriptDir "config\openclaw.mcp.patch.json"

$MergeScript = @"
import json, os

config_path = r'$ConfigFile'
patch_path = r'$PatchFile'
home_openclaw = r'$OpenClawDir'.replace('\\', '/')

if os.path.exists(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
else:
    config = {}

with open(patch_path, 'r', encoding='utf-8') as f:
    patch = json.load(f)

if 'mcp' not in config:
    config['mcp'] = {}
if 'servers' not in config['mcp']:
    config['mcp']['servers'] = {}

for name, srv in patch.get('mcp', {}).get('servers', {}).items():
    if 'args' in srv:
        srv['args'] = [a.replace('~/.openclaw', home_openclaw) for a in srv['args']]
    if os.name == 'nt' and srv.get('command') == 'python3':
        srv['command'] = 'python'
    config['mcp']['servers'][name] = srv

os.makedirs(os.path.dirname(config_path), exist_ok=True)
with open(config_path, 'w', encoding='utf-8') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print(' -> Merge de openclaw.json concluído com caminhos do Windows.')
"@

& $VenvPython -c $MergeScript

# 6. Validação e diagnóstico
Write-Host "[6/6] Verificando ambiente OpenClaw..." -ForegroundColor Yellow
if (Get-Command openclaw -ErrorAction SilentlyContinue) {
    Write-Host " -> Executando openclaw config validate..."
    openclaw config validate
    Write-Host " -> Executando openclaw mcp doctor..."
    openclaw mcp doctor
} else {
    Write-Host " -> [INFO] O comando 'openclaw' não foi detectado no PATH do Windows." -ForegroundColor Gray
    Write-Host "    Os arquivos e configurações foram posicionados com sucesso em $OpenClawDir." -ForegroundColor Gray
}

Write-Host "==================================================================" -ForegroundColor Green
Write-Host "    INSTALAÇÃO CONCLUÍDA COM SUCESSO!                             " -ForegroundColor Green
Write-Host "    Workspace configurado em: $WorkspaceDir                       " -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
