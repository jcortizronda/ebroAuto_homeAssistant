"""Tests de `charging.py` — carga programada y límite de batería por software.

Aquí se concentran los dos errores caros de esta integración:

* **la hora en UTC.** El backend interpreta `startTime` en UTC y HA guarda el reloj de pared
  local: verificado en campo que con UTC+2 una carga puesta a las 03:00 arrancaba a las 05:00.
* **la parada de la carga.** Este coche no acepta `chargeStartStopControl` (A00084), así que
  la única forma de cortar es imponer una programación cuya ventana ya terminó. Si la ventana
  se construye mal, el límite de carga simplemente no funciona y la batería sigue subiendo.

Ambas cosas eran métodos del coordinator y solo se probaban a través de él.
"""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest

from custom_components.ebro.const import (
    CHARGE_MIN_DURATION_MIN,
    CHARGE_STOP_DURATION_MIN,
    CHARGE_STOP_START_BACK_MIN,
    MINUTES_PER_DAY,
)
from custom_components.ebro.vehicle import charging
from custom_components.ebro.vehicle.charging import (
    ALL_WEEKDAYS,
    ChargeLimiter,
    build_plan,
    build_stop_plan,
    local_minutes_to_utc,
)

from .const import FROZEN_TIME

# ───────────────────────── hora local → UTC ─────────────────────────


@pytest.mark.freeze_time(FROZEN_TIME)
@pytest.mark.parametrize(
    ("zona", "local_min", "utc_min"),
    [
        # invierno en España = UTC+1 → 03:00 local son las 02:00 UTC
        ("Europe/Madrid", 3 * 60, 2 * 60),
        ("UTC", 3 * 60, 3 * 60),
    ],
)
async def test_local_minutes_to_utc(
    hass: HomeAssistant, zona: str, local_min: int, utc_min: int
) -> None:
    await hass.config.async_set_time_zone(zona)

    assert local_minutes_to_utc(local_min) == utc_min


async def test_local_minutes_to_utc_respeta_el_horario_de_verano(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Se usa la fecha de HOY justamente para que el desfase siga el horario vigente."""
    await hass.config.async_set_time_zone("Europe/Madrid")

    freezer.move_to("2026-07-15 12:00:00+00:00")  # verano = UTC+2
    assert local_minutes_to_utc(3 * 60) == 1 * 60


async def test_local_minutes_to_utc_da_la_vuelta_a_medianoche(hass: HomeAssistant) -> None:
    assert local_minutes_to_utc(25 * 60) == local_minutes_to_utc(60)


# ───────────────────────── plan de carga ─────────────────────────


@pytest.mark.freeze_time(FROZEN_TIME)
async def test_build_plan(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Madrid")

    plan = build_plan(start_minutes=3 * 60, duration_minutes=360, switch_status=1)

    assert plan == {
        "cycleData": ALL_WEEKDAYS,
        "startTime": 2 * 60,  # 02:00 UTC en invierno
        "switchStatus": 1,
        "timeConsuming": 360,
    }


async def test_build_plan_fuerza_la_duracion_minima(hass: HomeAssistant) -> None:
    """Menos de 1 h lo rechaza el coche con code 89: se corrige antes de enviar, no después."""
    plan = build_plan(start_minutes=0, duration_minutes=30, switch_status=1)

    assert plan["timeConsuming"] == CHARGE_MIN_DURATION_MIN


async def test_build_plan_la_duracion_no_se_convierte_a_utc(hass: HomeAssistant) -> None:
    """`timeConsuming` es un delta, no una hora del día: convertirlo sería un bug de husos."""
    await hass.config.async_set_time_zone("Europe/Madrid")

    assert build_plan(start_minutes=0, duration_minutes=240, switch_status=1)["timeConsuming"] == 240


# ───────────────────────── parada por ventana vencida ─────────────────────────


@pytest.mark.freeze_time(FROZEN_TIME)
async def test_build_stop_plan_deja_la_ventana_ya_cerrada(hass: HomeAssistant) -> None:
    """La ventana debe terminar ANTES de ahora, o el coche seguiría cargando.

    Se comprueba la propiedad, no los números: [inicio, inicio+duración] tiene que quedar por
    detrás del minuto actual.
    """
    from homeassistant.util import dt as dt_util

    plan = build_stop_plan()
    ahora = dt_util.utcnow().hour * 60 + dt_util.utcnow().minute

    inicio = plan["startTime"]
    fin = inicio + plan["timeConsuming"]
    assert inicio == (ahora - CHARGE_STOP_START_BACK_MIN) % MINUTES_PER_DAY
    assert plan["timeConsuming"] == CHARGE_STOP_DURATION_MIN
    # la ventana cerró hace CHARGE_STOP_START_BACK_MIN - CHARGE_STOP_DURATION_MIN minutos
    assert CHARGE_STOP_START_BACK_MIN - CHARGE_STOP_DURATION_MIN > 0
    assert fin < ahora + MINUTES_PER_DAY


async def test_build_stop_plan_respeta_el_minimo_del_coche(hass: HomeAssistant) -> None:
    """El truco se apoya en desplazar el INICIO, no en acortar la ventana: por debajo de 1 h
    el backend rechaza el plan con code 89 y la carga no se para."""
    assert build_stop_plan()["timeConsuming"] >= CHARGE_MIN_DURATION_MIN


# ───────────────────────── límite de carga por software ─────────────────────────


def test_el_limite_corta_una_sola_vez_por_sesion() -> None:
    """Sin esto, cada lectura reenviaría el stop mientras el coche tarda en obedecer."""
    limiter = ChargeLimiter(enabled=True, target_soc=80)

    assert limiter.should_stop(charging=True, soc=82.0) is True
    assert limiter.should_stop(charging=True, soc=83.0) is False


def test_el_limite_se_rearma_al_dejar_de_cargar() -> None:
    limiter = ChargeLimiter(enabled=True, target_soc=80)
    limiter.should_stop(charging=True, soc=82.0)

    limiter.should_stop(charging=False, soc=82.0)   # desenchufado / carga terminada

    assert limiter.should_stop(charging=True, soc=85.0) is True


def test_el_limite_desactivado_no_corta_nunca() -> None:
    limiter = ChargeLimiter(enabled=False, target_soc=80)

    assert limiter.should_stop(charging=True, soc=99.0) is False


def test_sin_lectura_de_bateria_no_se_corta_a_ciegas() -> None:
    """Con la alta tensión apagada el coche manda 0 como marcador, no como carga real: se
    traduce a None y NO debe interpretarse como objetivo alcanzado."""
    limiter = ChargeLimiter(enabled=True, target_soc=80)

    assert limiter.should_stop(charging=True, soc=None) is False


def test_por_debajo_del_objetivo_no_se_corta() -> None:
    limiter = ChargeLimiter(enabled=True, target_soc=80)

    assert limiter.should_stop(charging=True, soc=79.9) is False


@pytest.mark.parametrize(
    ("soc", "cerca"),
    [(78.0, True), (80.0, True), (75.0, True), (74.9, False), (81.0, False), (None, False)],
)
def test_near_target(soc: float | None, cerca: bool) -> None:
    """Cerca del objetivo se sondea más fino, para clavar el corte."""
    limiter = ChargeLimiter(enabled=True, target_soc=80)

    assert limiter.near_target(soc, margin=5) is cerca


def test_near_target_desactivado() -> None:
    assert ChargeLimiter(enabled=False, target_soc=80).near_target(79.0, margin=5) is False


# ───────────── la programación que tiene el COCHE ─────────────
# Las entidades de hora y duración son la PREFERENCIA: lo que se enviará. Si la programación se
# cambia desde la app oficial o desde el propio coche, no se enteran — y hasta ahora no había
# forma de saberlo desde Home Assistant.

RESPUESTA = {
    "mainSwitch": 1,
    "vin": "LSJA0000000000001",
    "chargeAppointPlans": [
        {"startTime": 300, "timeConsuming": 540, "switchStatus": 1,
         "cycleData": [1, 2, 3, 4, 5, 6, 7]}
    ],
}


def test_parse_schedule_convierte_la_hora_a_reloj_de_pared() -> None:
    """El coche guarda `startTime` en UTC. Enseñarlo tal cual haría que una carga puesta a las
    03:00 desde la app apareciera a las 01:00, y parecería un fallo de la integración."""
    horario = charging.parse_schedule(RESPUESTA)

    assert horario.enabled is True
    assert horario.start_minutes == charging.utc_minutes_to_local(300)
    assert horario.duration_minutes == 540
    assert horario.days == (1, 2, 3, 4, 5, 6, 7)


def test_las_dos_conversiones_de_hora_son_inversas() -> None:
    for minutos in (0, 1, 59, 300, 465, 720, 1439):
        ida = charging.local_minutes_to_utc(minutos)
        assert charging.utc_minutes_to_local(ida) == minutos


def test_el_interruptor_general_y_el_del_plan_deben_estar_los_dos() -> None:
    """`mainSwitch` y `switchStatus` son cosas distintas: con uno apagado el coche no carga."""
    assert charging.parse_schedule({**RESPUESTA, "mainSwitch": 0}).enabled is False

    plan_off = [{**RESPUESTA["chargeAppointPlans"][0], "switchStatus": 0}]
    assert charging.parse_schedule({**RESPUESTA, "chargeAppointPlans": plan_off}).enabled is False


def test_parse_schedule_aguanta_una_respuesta_pobre() -> None:
    """Es información de solo lectura: nada de esto puede reventar una sonda."""
    assert charging.parse_schedule(None) is None
    assert charging.parse_schedule("no soy un dict") is None

    vacio = charging.parse_schedule({"mainSwitch": 1})
    assert vacio.enabled is False          # sin plan no hay programación activa
    assert vacio.start_minutes is None
    assert vacio.days == ()
