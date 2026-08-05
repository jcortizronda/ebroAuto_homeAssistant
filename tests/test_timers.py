"""Tests de `timers.py` — el registro único de timers del coordinator.

`TimerRegistry` no importa nada de Home Assistant, así que estos tests son puros y rápidos:
basta un `Mock` haciendo de factory de unsub.
"""

from __future__ import annotations

from unittest.mock import Mock

from custom_components.ebro.vehicle.timers import (
    AWAKE,
    HV_POLL,
    KEEPALIVE,
    POLL_GROUP,
    STARTUP_PROBE,
    TimerRegistry,
)


def _factory() -> tuple[Mock, Mock]:
    """Devuelve `(factory, unsub)`: la factory produce siempre el mismo unsub espiable."""
    unsub = Mock(name="unsub")
    return Mock(name="factory", return_value=unsub), unsub


def test_arm_programa_y_registra() -> None:
    reg = TimerRegistry()
    factory, unsub = _factory()

    assert reg.arm(HV_POLL, factory) is True
    factory.assert_called_once_with()
    assert reg.is_armed(HV_POLL)
    assert reg.armed() == {HV_POLL}
    unsub.assert_not_called()


def test_arm_es_idempotente_por_nombre() -> None:
    """Rearmar cancela el viejo en vez de dejar dos en vuelo.

    Con un timer auto-reprogramable como el seguimiento HV, dos copias duplicarían las
    lecturas a la nube en cada vuelta.
    """
    reg = TimerRegistry()
    factory1, unsub1 = _factory()
    factory2, unsub2 = _factory()

    reg.arm(HV_POLL, factory1)
    reg.arm(HV_POLL, factory2)

    unsub1.assert_called_once_with()
    unsub2.assert_not_called()
    assert reg.armed() == {HV_POLL}


def test_arm_rechazado_tras_close_y_ni_siquiera_llama_a_la_factory() -> None:
    """El invariante central del módulo, y la razón de que `arm()` reciba una factory.

    Si recibiera un unsub ya creado, el timer se habría programado para tirarlo justo después
    — dejando una callback en vuelo mientras tanto. Ese es el sondeo huérfano que el módulo
    existe para evitar.
    """
    reg = TimerRegistry()
    reg.close()
    factory, unsub = _factory()

    assert reg.arm(HV_POLL, factory) is False
    factory.assert_not_called()
    unsub.assert_not_called()
    assert reg.armed() == set()


def test_cancel_devuelve_si_estaba_armado() -> None:
    reg = TimerRegistry()
    factory, unsub = _factory()
    reg.arm(HV_POLL, factory)

    assert reg.cancel(HV_POLL) is True
    unsub.assert_called_once_with()
    assert not reg.is_armed(HV_POLL)

    # segunda cancelación del mismo nombre, y nombre nunca armado
    assert reg.cancel(HV_POLL) is False
    assert reg.cancel("nombre-inventado") is False


def test_cancel_traga_la_excepcion_del_unsub_y_aun_asi_elimina() -> None:
    """Un unsub que protesta no debe bloquear el teardown ni dejar la entrada colgada."""
    reg = TimerRegistry()
    unsub = Mock(side_effect=RuntimeError("timer ya cancelado"))
    reg.arm(HV_POLL, Mock(return_value=unsub))

    assert reg.cancel(HV_POLL) is True
    assert not reg.is_armed(HV_POLL)


def test_cancel_many_solo_toca_el_grupo() -> None:
    """El keep-alive NO está en `GRUPO_POLL`: apagar «Actualización automática» no debe dejar
    caducar el token, o el usuario tendría que reautenticar sin motivo."""
    reg = TimerRegistry()
    unsubs = {}
    for name in (*POLL_GROUP, KEEPALIVE, AWAKE):
        factory, unsub = _factory()
        unsubs[name] = unsub
        reg.arm(name, factory)

    reg.cancel_many(POLL_GROUP)

    assert reg.armed() == {KEEPALIVE, AWAKE}
    for name in POLL_GROUP:
        unsubs[name].assert_called_once_with()
    unsubs[KEEPALIVE].assert_not_called()
    unsubs[AWAKE].assert_not_called()


def test_grupo_poll_no_incluye_keepalive_ni_awake() -> None:
    assert set(POLL_GROUP) == {HV_POLL, STARTUP_PROBE}
    assert KEEPALIVE not in POLL_GROUP
    assert AWAKE not in POLL_GROUP


def test_close_cancela_todo_y_marca_closing() -> None:
    reg = TimerRegistry()
    assert reg.closing is False
    factory, unsub = _factory()
    reg.arm(KEEPALIVE, factory)

    reg.close()

    assert reg.closing is True
    assert reg.armed() == set()
    unsub.assert_called_once_with()


def test_cancel_all_no_cierra_el_registro() -> None:
    """`cancel_all()` es reversible; `close()` no. La diferencia importa: el interruptor de
    sondeo cancela, la descarga del entry cierra."""
    reg = TimerRegistry()
    reg.arm(HV_POLL, Mock(return_value=Mock()))

    reg.cancel_all()

    assert reg.closing is False
    assert reg.arm(HV_POLL, Mock(return_value=Mock())) is True
