"""Tests de `core/context.py` — el `CoreCtx` de un vehículo.

⚠️ Las rutas por defecto de `CoreCtx` apuntan DENTRO del paquete instalado
(`core/token.json`, `core/data/taskid.txt`). Todo test que vaya a escribir debe pasar
`tmp_path` explícitamente; aquí solo se comprueba el cálculo de la ruta, sin tocar disco.
"""

from __future__ import annotations

import os
from unittest.mock import Mock

from custom_components.ebro.core.context import (
    DEFAULT_BFF,
    DEFAULT_CHANNEL_ID,
    DEFAULT_TSP_HOST,
    HERE,
    CoreCtx,
)


def test_rutas_por_defecto_dentro_del_paquete() -> None:
    """Solo para el uso por línea de comandos: en HA las fija siempre el coordinator."""
    ctx = CoreCtx(vin="V")

    assert ctx.token_path == os.path.join(HERE, "token.json")
    assert ctx.taskid_file == os.path.join(HERE, "data", "taskid.txt")


def test_rutas_explicitas_ganan(tmp_path) -> None:
    ctx = CoreCtx(
        vin="V",
        token_path=str(tmp_path / "token.json"),
        taskid_file=str(tmp_path / "taskid.txt"),
    )

    assert ctx.token_path == str(tmp_path / "token.json")
    assert ctx.taskid_file == str(tmp_path / "taskid.txt")


def test_defaults_de_region() -> None:
    ctx = CoreCtx()
    assert ctx.bff == DEFAULT_BFF
    assert ctx.tsp_host == DEFAULT_TSP_HOST
    assert ctx.channel_id == DEFAULT_CHANNEL_ID
    assert ctx.mint_taskid is True


def test_cada_contexto_tiene_su_propio_estado() -> None:
    """La razón de ser del módulo: con dos coches configurados, los errores de PIN de uno no
    deben bloquear los comandos del otro, ni su taskId acabar en el vehículo equivocado."""
    a = CoreCtx(vin="VIN_A")
    b = CoreCtx(vin="VIN_B")

    assert a.state is not b.state
    assert a.lockout is not b.lockout
    assert a.state.wake_lock is not b.state.wake_lock

    a.state.taskid = "task-a"
    assert b.state.taskid is None


def test_lockout_es_el_del_estado() -> None:
    ctx = CoreCtx()
    assert ctx.lockout is ctx.state.lockout


def test_invalidate_taskid() -> None:
    ctx = CoreCtx()
    ctx.state.taskid = "task-123"
    ctx.state.taskid_ts = 1_700_000_000.0

    ctx.invalidate_taskid()

    assert ctx.state.taskid is None
    assert ctx.state.taskid_ts == 0.0


def test_reset_pin_lockout_limpia_contador_y_taskid() -> None:
    """El taskId está ligado al PIN: cambiarlo sin descartarlo dejaría uno inservible en caché."""
    ctx = CoreCtx()
    with ctx.lockout.attempt() as attempt:
        attempt.record_failure()
    ctx.state.taskid = "task-viejo"

    ctx.reset_pin_lockout()

    assert ctx.lockout.failed_attempts == 0
    assert ctx.state.taskid is None


def test_diag_sin_hook_no_hace_nada() -> None:
    """Con el monitor dormido el coste debe ser un simple `is None`."""
    CoreCtx().diag("command", key="bloquear")  # no debe lanzar


def test_diag_llama_al_hook() -> None:
    hook = Mock()
    ctx = CoreCtx(diag_hook=hook)

    ctx.diag("command", key="bloquear", ok=True)

    hook.assert_called_once_with("command", key="bloquear", ok=True)


def test_diag_traga_un_hook_roto() -> None:
    """El monitor observa, no participa: si revienta no puede tumbar un comando."""
    ctx = CoreCtx(diag_hook=Mock(side_effect=RuntimeError("monitor roto")))

    ctx.diag("command", key="bloquear")  # no debe lanzar


def test_ctx_from_environ_lee_el_entorno(monkeypatch) -> None:
    """Camino exclusivo de la línea de comandos: HA nunca pasa por aquí (el PIN no debe
    acabar nunca en el entorno del proceso de Home Assistant)."""
    from custom_components.ebro.core.context import ctx_from_environ

    monkeypatch.setenv("VIN", "VIN_CLI")
    monkeypatch.setenv("EBRO_PIN", "9999")
    monkeypatch.setenv("EBRO_MINT_TASKID", "0")

    ctx = ctx_from_environ()

    assert ctx.vin == "VIN_CLI"
    assert ctx.pin == "9999"
    assert ctx.mint_taskid is False
