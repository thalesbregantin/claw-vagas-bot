"""
tests/test_openclaw_contracts.py
Validação de contratos do OpenClaw v2026:
1. HEARTBEAT.md existe e tem estritamente menos de 50 linhas
2. YAML frontmatter de cada SKILL.md (name, version, description, triggers, tools)
3. Configuração de MCPs no openclaw.mcp.patch.json
4. Presença e completude dos arquivos de governança do workspace
"""

import json
from pathlib import Path
import pytest
import yaml

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent / "workspace"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def test_heartbeat_line_limit_contract():
    """
    HEARTBEAT.md deve existir e ter estritamente menos de 50 linhas.
    Diretriz oficial OpenClaw para evitar loops e custos desnecessários no scheduler.
    """
    heartbeat_path = WORKSPACE_ROOT / "HEARTBEAT.md"
    assert heartbeat_path.exists(), "HEARTBEAT.md não encontrado no workspace"
    
    lines = heartbeat_path.read_text(encoding="utf-8").splitlines()
    num_lines = len(lines)
    assert num_lines < 50, f"HEARTBEAT.md deve ter menos de 50 linhas, mas possui {num_lines} linhas"
    assert any("queue_guard.py" in l for l in lines), "HEARTBEAT.md deve acionar as travas do queue_guard"


def test_skill_frontmatter_contracts():
    """
    Valida que todas as skills do workspace possuem YAML frontmatter válido
    com name, version, description, triggers e tools.
    """
    skills_dir = WORKSPACE_ROOT / "skills"
    assert skills_dir.exists(), "Pasta skills não encontrada"
    
    expected_skills = ["job-curator", "linkedin-easy-apply"]
    
    for skill_name in expected_skills:
        skill_file = skills_dir / skill_name / "SKILL.md"
        assert skill_file.exists(), f"SKILL.md ausente para {skill_name}"
        
        content = skill_file.read_text(encoding="utf-8")
        assert content.startswith("---"), f"{skill_name}/SKILL.md deve iniciar com frontmatter '---'"
        
        parts = content.split("---", 2)
        assert len(parts) >= 3, f"{skill_name}/SKILL.md não possui bloco frontmatter fechado com '---'"
        
        frontmatter_raw = parts[1]
        data = yaml.safe_load(frontmatter_raw)
        assert isinstance(data, dict), f"Frontmatter de {skill_name} inválido"
        
        # Validação dos campos obrigatórios
        assert data.get("name") == skill_name, f"Campo 'name' deve ser {skill_name}"
        assert "version" in data, f"Campo 'version' ausente em {skill_name}"
        assert "description" in data and len(data["description"]) > 10, f"Campo 'description' ausente em {skill_name}"
        assert "triggers" in data and isinstance(data["triggers"], list), f"Triggers ausentes em {skill_name}"
        assert len(data["triggers"]) > 0, f"Deve haver pelo menos 1 trigger em {skill_name}"
        assert "tools" in data and isinstance(data["tools"], list), f"Tools ausentes em {skill_name}"
        assert len(data["tools"]) > 0, f"Deve haver pelo menos 1 tool declarada em {skill_name}"


def test_mcp_config_patch_contract():
    """
    Valida a integridade do arquivo config/openclaw.mcp.patch.json:
    - Sintaxe JSON válida
    - Chaves mcp -> servers
    - Contém os servidores essenciais (job-search-mcp-jobspy, linkedin-mcp-server, sqlite, filesystem)
    """
    patch_file = CONFIG_DIR / "openclaw.mcp.patch.json"
    assert patch_file.exists(), "openclaw.mcp.patch.json não encontrado em config/"
    
    data = json.loads(patch_file.read_text(encoding="utf-8"))
    assert "mcp" in data
    assert "servers" in data["mcp"]
    
    servers = data["mcp"]["servers"]
    expected_servers = ["job-search-mcp-jobspy", "linkedin-mcp-server", "sqlite", "filesystem"]
    
    for s in expected_servers:
        assert s in servers, f"Servidor MCP '{s}' não configurado em openclaw.mcp.patch.json"
        assert "command" in servers[s], f"Servidor '{s}' não possui comando definido"
        assert "description" in servers[s], f"Servidor '{s}' deve ter descrição clara"


def test_core_governance_files_presence():
    """
    Valida a presença de todos os arquivos de governança e personalidade do OpenClaw:
    IDENTITY.md, SOUL.md, AGENTS.md, USER.example.md, TOOLS.md.
    """
    governance_files = [
        "IDENTITY.md",
        "SOUL.md",
        "AGENTS.md",
        "USER.example.md",
        "TOOLS.md"
    ]
    
    for gf in governance_files:
        fpath = WORKSPACE_ROOT / gf
        assert fpath.exists(), f"Arquivo de governança obrigatório ausente: {gf}"
        content = fpath.read_text(encoding="utf-8").strip()
        assert len(content) > 50, f"Arquivo {gf} está vazio ou incompleto"
