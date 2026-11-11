"""
tests/test_queue_guard.py
Testes unitários e de integração para queue_guard.py:
1. Validação de Fuso Horário Local (America/Sao_Paulo / UTC-3)
2. Janela Comercial (08h às 20h BRT)
3. Limite de Cota Diária (15/dia) e virada de dia
4. Jitter e Cooldown Anti-Ban (180s - 300s)
"""

import sqlite3
from datetime import datetime, time, date, timedelta, timezone
from pathlib import Path
import pytest
import queue_guard


def test_timezone_sao_paulo_18h30_allowed(isolated_workspace, monkeypatch):
    """
    Simula 18:30 no horário de São Paulo (21:30 UTC).
    O disparo DEVE retornar AUTORIZADO (allowed=True), garantindo
    que o sistema não bloqueie após as 17h BRT por usar UTC puro.
    """
    fixed_dt = datetime(2026, 10, 29, 18, 30, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: fixed_dt)
    
    allowed, reason, meta = queue_guard.check_can_apply()
    assert allowed is True, f"Deveria estar autorizado às 18:30 BRT, mas retornou: {reason}"
    assert meta["code"] == "OK"


@pytest.mark.parametrize("simulated_time,expected_code", [
    (time(7, 59, 59), "OUTSIDE_HOURS"),
    (time(20, 1, 0), "OUTSIDE_HOURS"),
    (time(23, 0, 0), "OUTSIDE_HOURS"),
    (time(3, 30, 0), "OUTSIDE_HOURS"),
])
def test_outside_operating_hours_blocked(isolated_workspace, monkeypatch, simulated_time, expected_code):
    """
    Simula horários fora de 08:00 às 20:00 BRT.
    Todos DEVEM retornar BLOQUEADO.
    """
    fixed_dt = datetime(2026, 10, 29, simulated_time.hour, simulated_time.minute, simulated_time.second, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: fixed_dt)
    
    allowed, reason, meta = queue_guard.check_can_apply()
    assert allowed is False
    assert meta["code"] == expected_code
    assert "Fora do horário comercial" in reason


def test_daily_quota_limit_and_midnight_rollover(isolated_workspace, monkeypatch):
    """
    15ª aplicação no mesmo dia BRT -> AUTORIZADO.
    16ª aplicação -> BLOQUEADO por cota diária excedida.
    Virada de dia à 00:01 BRT zera a cota.
    """
    day_1 = datetime(2026, 10, 29, 10, 0, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: day_1)
    
    conn = queue_guard.get_db()
    
    # 1. Simula 14 aplicações feitas hoje
    conn.execute("INSERT OR REPLACE INTO daily_quotas (date, count, max_limit) VALUES (?, 14, 15)", (day_1.date().isoformat(),))
    conn.commit()
    conn.close()
    
    # 15ª aplicação deve ser permitida
    allowed, reason, meta = queue_guard.check_can_apply()
    assert allowed is True
    assert meta["today_count"] == 14
    
    # Simula aplicação da 15ª (count passa para 15)
    conn = queue_guard.get_db()
    conn.execute("UPDATE daily_quotas SET count = 15 WHERE date = ?", (day_1.date().isoformat(),))
    conn.commit()
    conn.close()
    
    # 16ª aplicação deve ser bloqueada
    allowed, reason, meta = queue_guard.check_can_apply()
    assert allowed is False
    assert meta["code"] == "QUOTA_EXCEEDED"
    assert "Cota diária" in reason
    
    # Virada do dia: 2026-10-30 às 00:01 BRT
    day_2_midnight = datetime(2026, 10, 30, 0, 1, 0, tzinfo=queue_guard.BRT_TZ)
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: day_2_midnight)
    
    conn = queue_guard.get_db()
    count_day_2, limit_day_2 = queue_guard.get_today_quota(conn)
    conn.close()
    
    # A cota para o novo dia deve estar zerada
    assert count_day_2 == 0
    assert limit_day_2 == 15


def test_jitter_anti_ban_cooldown(isolated_workspace, monkeypatch):
    """
    Disparo 30s após última aplicação -> BLOQUEADO (cooldown ativo).
    Disparo 301s após última aplicação -> AUTORIZADO.
    """
    # Fixa horário de teste às 14:00 BRT (17:00 UTC)
    now_brt = datetime(2026, 10, 29, 14, 0, 0, tzinfo=queue_guard.BRT_TZ)
    now_utc = datetime(2026, 10, 29, 17, 0, 0)
    
    monkeypatch.setattr(queue_guard, "get_now_brt", lambda: now_brt)
    
    # Inserir vaga anterior aplicada há apenas 30 segundos
    applied_30s_ago = (now_utc - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = queue_guard.get_db()
    conn.execute("""
        INSERT INTO jobs (job_id, title, company, job_url, apply_type, status, applied_at)
        VALUES ('job-recent', 'AI Eng', 'Tech', 'https://job', 'EASY_APPLY', 'applied', ?)
    """, (applied_30s_ago,))
    conn.commit()
    conn.close()
    
    # Mock do datetime.now(timezone.utc) para retornar now_utc
    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz == timezone.utc:
                return now_utc.replace(tzinfo=timezone.utc)
            return now_brt

    monkeypatch.setattr(queue_guard, "datetime", MockDatetime)
    
    allowed, reason, meta = queue_guard.check_can_apply()
    assert allowed is False
    assert meta["code"] == "COOLDOWN_ACTIVE"
    assert "intervalo de proteção anti-ban" in reason
    
    # Agora altera o applied_at para 301s atrás (fora do jitter máximo de 300s)
    applied_301s_ago = (now_utc - timedelta(seconds=301)).strftime("%Y-%m-%d %H:%M:%S")
    conn = queue_guard.get_db()
    conn.execute("UPDATE jobs SET applied_at = ? WHERE job_id = 'job-recent'", (applied_301s_ago,))
    conn.commit()
    conn.close()
    
    allowed_after, reason_after, meta_after = queue_guard.check_can_apply()
    assert allowed_after is True, f"Após 301s deveria liberar, mas retornou: {reason_after}"
    assert meta_after["code"] == "OK"
