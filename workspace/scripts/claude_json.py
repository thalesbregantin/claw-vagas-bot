#!/usr/bin/env python3
"""
workspace/scripts/claude_json.py
Chamada única ao Claude com resposta em JSON validado por schema.

Usada só para leitura e sugestão (visão e perguntas parecidas). Qualquer falha devolve None:
quem chama sempre tem um caminho que funciona sem a API.
"""

import os
from typing import Any, List, Optional

DEFAULT_MODEL = "claude-opus-5-5"
REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def ai_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())


def new_client():
    import anthropic
    return anthropic.Anthropic(timeout=60.0, max_retries=1)


def ask_json(client: Any, *, system: str, content: List[dict], schema: dict, tag: str,
             model: Optional[str] = None) -> Optional[str]:
    """Devolve o texto JSON da resposta, ou None se a API falhar, recusar ou não terminar a resposta."""
    import anthropic
    try:
        response = client.beta.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=16000,
            betas=[REFUSAL_FALLBACK_BETA],
            fallbacks="default",
            system=system,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError:
        print(f"⚠️ [{tag}] Chave da API recusada. Confira ANTHROPIC_API_KEY em workspace/.env.", flush=True)
        return None
    except anthropic.RateLimitError:
        print(f"⚠️ [{tag}] Limite de uso da API atingido.", flush=True)
        return None
    except anthropic.APIStatusError as e:
        print(f"⚠️ [{tag}] API respondeu {e.status_code}.", flush=True)
        return None
    except anthropic.APIConnectionError:
        print(f"⚠️ [{tag}] Sem conexão com a API (ou tempo esgotado).", flush=True)
        return None

    if response.stop_reason != "end_turn":
        print(f"⚠️ [{tag}] Resposta incompleta ({response.stop_reason}).", flush=True)
        return None
    return next((b.text for b in response.content if b.type == "text"), None)
