#!/usr/bin/env bash
# ==============================================================================
# setup.sh — Script de Instalação e Ativação do LinkedIn OpenClaw Agent
# Configura o ambiente, inicializa banco SQLite, mescla MCPs e sincroniza
# os arquivos diretamente no runtime oficial do OpenClaw (~/.openclaw/).
# ==============================================================================

set -e

OPENCLAW_DIR="$HOME/.openclaw"
WORKSPACE_DIR="$OPENCLAW_DIR/workspace"
CONFIG_FILE="$OPENCLAW_DIR/openclaw.json"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=================================================================="
echo "    INICIALIZAÇÃO DO LINKEDIN AGENT NO RUNTIME OPENCLAW          "
echo "=================================================================="

# 1. Garante que os diretórios do OpenClaw existam
echo "[1/6] Criando diretórios base em $OPENCLAW_DIR..."
mkdir -p "$WORKSPACE_DIR/db"
mkdir -p "$WORKSPACE_DIR/data"
mkdir -p "$WORKSPACE_DIR/templates"
mkdir -p "$WORKSPACE_DIR/scripts"
mkdir -p "$WORKSPACE_DIR/skills/job-curator"
mkdir -p "$WORKSPACE_DIR/skills/linkedin-easy-apply"
mkdir -p "$WORKSPACE_DIR/output/resumes"
mkdir -p "$WORKSPACE_DIR/output/logs"

# 2. Configura ambiente virtual Python e dependências para os scripts
echo "[2/6] Configurando ambiente Python venv em $WORKSPACE_DIR/.venv..."
python3 -m venv "$WORKSPACE_DIR/.venv"
source "$WORKSPACE_DIR/.venv/bin/activate" || true
pip install --upgrade pip
pip install -r "$SOURCE_DIR/requirements.txt"

# 3. Sincroniza arquivos do workspace
echo "[3/6] Sincronizando workspace para $WORKSPACE_DIR..."
# Banco, perfil, respostas, segredos e PDFs existentes no destino nunca são sobrescritos
(cd "$SOURCE_DIR/workspace" && find . -type f -not -path './output/*' -not -path '*/__pycache__/*') | while read -r rel_path; do
    target="$WORKSPACE_DIR/$rel_path"
    case "$(basename "$rel_path")" in
        jobs.db|jobs.db-wal|jobs.db-shm|profile.json|EXPERIENCE_ANSWERS.md|learned_answers.json|user_negative_preferences.json|.env)
            [ -e "$target" ] && continue ;;
    esac
    mkdir -p "$(dirname "$target")"
    cp "$SOURCE_DIR/workspace/$rel_path" "$target"
done

# 4. Inicializa o banco de dados SQLite jobs.db
echo "[4/6] Inicializando banco SQLite e cotas diárias..."
python3 "$WORKSPACE_DIR/scripts/init_db.py"

# 5. Mescla configuração de MCPs no openclaw.json
echo "[5/6] Configurando servidores MCP no OpenClaw..."
if [ -f "$CONFIG_FILE" ]; then
    echo " -> openclaw.json localizado. Realizando merge seguro..."
    python3 -c "
import json, os

config_path = os.path.expanduser('~/.openclaw/openclaw.json')
patch_path = os.path.join('$SOURCE_DIR', 'config', 'openclaw.mcp.patch.json')

with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)

with open(patch_path, 'r', encoding='utf-8') as f:
    patch = json.load(f)

if 'mcp' not in config:
    config['mcp'] = {}
if 'servers' not in config['mcp']:
    config['mcp']['servers'] = {}

config['mcp']['servers'].update(patch['mcp']['servers'])

with open(config_path, 'w', encoding='utf-8') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print(' -> Merge de MCPs concluído com sucesso.')
"
else
    echo " -> openclaw.json não encontrado. Criando novo arquivo base..."
    mkdir -p "$OPENCLAW_DIR"
    cp "$SOURCE_DIR/config/openclaw.mcp.patch.json" "$CONFIG_FILE"
fi

# Instruções de CLI nativa caso o usuário use openclaw mcp add
echo ""
echo " -> Comandos alternativos via CLI nativa do OpenClaw:"
echo "    openclaw mcp add sqlite npx -y @modelcontextprotocol/server-sqlite --db-path $WORKSPACE_DIR/db/jobs.db"
echo "    openclaw mcp add filesystem npx -y @modelcontextprotocol/server-filesystem $WORKSPACE_DIR/data $WORKSPACE_DIR/templates $WORKSPACE_DIR/output"
echo "    openclaw mcp add linkedin-mcp-server npx -y @stickerdaniel/linkedin-mcp-server"
echo "    openclaw mcp add job-search-mcp-jobspy python3 -m jobspy_mcp"
echo ""

# 6. Validação e diagnóstico do OpenClaw
echo "[6/6] Validando ambiente e servidores MCP..."
if command -v openclaw &> /dev/null; then
    echo " -> Executando openclaw config validate..."
    openclaw config validate || true
    echo " -> Executando openclaw mcp doctor --probe..."
    openclaw mcp doctor --probe || true
else
    echo " -> [INFO] Binário 'openclaw' não detectado no PATH atual."
    echo "    Quando ativar o OpenClaw, execute manualmente:"
    echo "    openclaw config validate"
    echo "    openclaw mcp doctor --probe"
fi

echo "=================================================================="
echo "    INSTALAÇÃO CONCLUÍDA COM SUCESSO!                             "
echo "    Seu workspace OpenClaw está pronto em: $WORKSPACE_DIR         "
echo "=================================================================="
