"""Tests de `poll_policy.py` — cada cuánto se sondea el canal realtime.

Esta decisión tiene un coste real cuando se equivoca: sondear de más consume la batería de
12 V del coche y desconecta la app oficial del usuario (la nube de Chery admite una sola
sesión por cuenta). Merece tests directos, y ahora que la clasificación es una función de sus
argumentos se pueden escribir sin `hass`, sin MQTT y sin construir un coordinator.
"""

from __future__ import annotations

import pytest

from custom_components.ebro.vehicle.poll_policy import (
    BurstTracker,
    CarConditions,
    PollIntervals,
    PollState,
    classify,
    interval_seconds,
)

INTERVALOS = PollIntervals(parked=0, plugged=30, charging=15, moving=3, moving_idle=5)


@pytest.mark.parametrize(
    ("condiciones", "esperado"),
    [
        # Cargar tambien enciende la alta tension: enchufado + AT es CARGA, nunca marcha.
        # Es la confusion que hacia sondear cada 3 minutos toda una noche de carga.
        (CarConditions(plugged=True, charging=False, hv_on=True, burst_active=False),
         PollState.CHARGING),
        (CarConditions(plugged=True, charging=True, hv_on=False, burst_active=False),
         PollState.CHARGING),
        # Cable conectado sin cargar aun (carga programada esperando su hora).
        (CarConditions(plugged=True, charging=False, hv_on=False, burst_active=False),
         PollState.PLUGGED),
        # Alta tension + rafaga de MQTT + sin cable = circulando.
        (CarConditions(plugged=False, charging=False, hv_on=True, burst_active=True),
         PollState.MOVING),
        # Alta tension SIN rafaga: semaforo, o te has bajado con el coche en marcha.
        (CarConditions(plugged=False, charging=False, hv_on=True, burst_active=False),
         PollState.MOVING_IDLE),
        # RECUPERACION en marcha: chargeState=1 SIN cable. Un hibrido recarga la bateria con
        # el freno regenerativo o el motor termico, y eso no es una recarga que sondear.
        # Registrado en campo decenas de veces alternando con "en marcha" cada pocos minutos:
        # el sondeo se relajaba de 3 a 15 min justo al circular.
        (CarConditions(plugged=False, charging=True, hv_on=True, burst_active=True),
         PollState.MOVING),
        (CarConditions(plugged=False, charging=True, hv_on=True, burst_active=False),
         PollState.MOVING_IDLE),
        # ...y sin alta tension tampoco: sin cable no hay nada que cargar.
        (CarConditions(plugged=False, charging=True, hv_on=False, burst_active=False),
         PollState.PARKED),
        # Alta tension apagada = parado, pase lo que pase con la rafaga.
        (CarConditions(plugged=False, charging=False, hv_on=False, burst_active=True),
         PollState.PARKED),
        (CarConditions(plugged=False, charging=False, hv_on=False, burst_active=False),
         PollState.PARKED),
    ],
)
def test_classify(condiciones: CarConditions, esperado: PollState) -> None:
    assert classify(condiciones) is esperado


def test_el_tope_de_enchufado_sin_cargar_devuelve_al_estado_parado() -> None:
    """Un coche enchufado que nunca llega a cargar no se sondea indefinidamente."""
    esperando = CarConditions(plugged=True, charging=False, hv_on=False, burst_active=False)
    vencido = CarConditions(
        plugged=True, charging=False, hv_on=False, burst_active=False, plugged_timed_out=True
    )

    assert classify(esperando) is PollState.PLUGGED
    assert classify(vencido) is PollState.PARKED


def test_el_tope_no_afecta_a_una_carga_real_en_curso() -> None:
    """Si el coche EMPIEZA a cargar, el tope de espera es irrelevante: prioridad a la carga."""
    vencido_pero_cargando = CarConditions(
        plugged=True, charging=True, hv_on=True, burst_active=False, plugged_timed_out=True
    )

    assert classify(vencido_pero_cargando) is PollState.CHARGING


@pytest.mark.parametrize(
    ("state", "minutos"),
    [
        (PollState.CHARGING, 15),
        (PollState.PLUGGED, 30),
        (PollState.MOVING, 3),
        (PollState.MOVING_IDLE, 5),
        (PollState.PARKED, 0),
    ],
)
def test_intervalo_por_estado(state: PollState, minutos: int) -> None:
    assert INTERVALOS.for_state(state) == minutos


@pytest.mark.parametrize(
    ("minutos", "segundos"),
    [(15, 900), (3, 180), (1, 60), (0, None), (-1, None)],
)
def test_interval_seconds(minutos: int, segundos: int | None) -> None:
    """`None` no es un error: es «no toques el coche hasta el próximo evento MQTT gratis»,
    que es justamente el valor por defecto con el coche parado."""
    assert interval_seconds(minutos) == segundos


# ───────────────────────── detección de marcha por ráfaga ─────────────────────────


def test_burst_tracker_necesita_el_umbral_completo() -> None:
    tracker = BurstTracker(count=5, window_s=30)
    ahora = 1_000.0

    for i in range(4):
        assert tracker.record(ahora + i) is False
    assert tracker.record(ahora + 4) is True


def test_burst_tracker_poda_las_marcas_fuera_de_ventana() -> None:
    """Cuatro mensajes viejos más uno nuevo NO son una ráfaga: el coche está parado."""
    tracker = BurstTracker(count=5, window_s=30)
    for i in range(4):
        tracker.record(1_000.0 + i)

    assert tracker.record(1_100.0) is False
    assert tracker.is_active(1_100.0) is False


def test_burst_tracker_se_apaga_solo_al_pasar_la_ventana() -> None:
    """Sin la poda, un viaje dejaba la ráfaga «activa» para siempre y el coche seguía
    sondeándose al ritmo de marcha con el motor ya apagado."""
    tracker = BurstTracker(count=3, window_s=30)
    for i in range(3):
        tracker.record(1_000.0 + i)

    assert tracker.is_active(1_010.0) is True
    assert tracker.is_active(1_060.0) is False
