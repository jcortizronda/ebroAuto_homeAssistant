"""Tests de `coordinator.py`.

Dos bloques bien distintos:

* **helpers puros** — la máquina de estados del sondeo y los formateadores son funciones de
  `self.data`, sin red: se prueban directo sobre el coordinator ya montado;
* **`_on_car_message`** — el punto de entrada de TODO lo que el coche empuja. Se alimenta con
  payloads de bytes JSON reales, que es exactamente lo que recibe de paho.

Nunca se llama a `async_refresh()`: el coordinator es push-only (`update_interval=None`, sin
`_async_update_data`) y reventaría con `NotImplementedError`.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ebro.const import (
    CHARGE_LIMIT_NEAR_MIN,
    MOVE_BURST_COUNT,
    MOVE_BURST_WINDOW_S,
)
from custom_components.ebro.vehicle import coordinator as coord_mod, telemetry
from custom_components.ebro.vehicle.poll_policy import interval_seconds

# ⚠️ NO se congela el reloj a nivel de módulo, a diferencia de los tests de plataforma.
# `_settle_after_command` (la pausa entre comandos de la cola) hace
# `while time.monotonic() < deadline: await asyncio.sleep(0.5)`, y freezegun congela también
# `time.monotonic` → con el reloj parado ese bucle NO TERMINA NUNCA y el test se cuelga.
# Los tests de plataforma no se ven afectados porque todos sustituyen `async_send_command`.
# Aquí se congela solo donde hace falta, con la marca puesta en cada test.


@pytest.fixture
def platforms() -> list[str]:
    """Sin plataformas: estos tests miran el coordinator, no las entidades."""
    return []


@pytest.fixture
def coordinator(hass: HomeAssistant, init_integration: MockConfigEntry):
    return init_integration.runtime_data


# ───────────────────────── helpers de módulo (puros) ─────────────────────────


# ───────────────────────── máquina de estados del sondeo ─────────────────────────


def _estado(coordinator, *, realtime=None, fields=None):
    coordinator.data = {**coordinator.data, "realtime": realtime or {}}
    coordinator.state.seed(fields=fields or {})
    return coordinator.polling.state_and_minutes()


def test_poll_state_cargando(coordinator) -> None:
    """Cargar de verdad EXIGE el cable: `chargeState=1` sin él es recuperación en marcha."""
    assert _estado(
        coordinator, realtime={"chargeState": "1", "chargeGunState": "1"}
    ) == ("cargando", coordinator.poll_charging_min)


def test_recuperar_en_marcha_no_es_cargar(coordinator) -> None:
    """`chargeState=1` SIN cable: un híbrido recargando con el freno regenerativo o el motor.
    Registrado en campo alternando con «en marcha» cada pocos minutos, y relajaba el sondeo de
    3 a 15 min justo al circular."""
    label, mins = _estado(
        coordinator, realtime={"chargeState": "1", "chargeGunState": "0", "hVoltageState": "1"}
    )

    assert label != "cargando"
    assert mins == coordinator.poll_moving_idle_min


def test_poll_state_enchufado_con_alta_tension_es_cargando(coordinator) -> None:
    """Cargar también enciende la alta tensión → enchufado + AT es CARGA, no marcha."""
    assert _estado(
        coordinator, realtime={"chargeGunState": "1", "hVoltageState": "1"}
    ) == ("cargando", coordinator.poll_charging_min)


def test_poll_state_enchufado_sin_cargar(coordinator) -> None:
    assert _estado(coordinator, realtime={"chargeGunState": "1"}) == (
        "enchufado",
        coordinator.poll_plugged_min,
    )


def test_poll_state_en_marcha_detenido(coordinator) -> None:
    """AT encendida, sin cable y SIN ráfaga: semáforo, o te has bajado con el coche en marcha."""
    assert _estado(coordinator, realtime={"hVoltageState": "1"}) == (
        "en marcha detenido",
        coordinator.poll_moving_idle_min,
    )


def test_poll_state_en_marcha_con_rafaga(coordinator) -> None:
    """La MARCHA se detecta por la ráfaga de «Último contacto» que el coche late al circular."""
    ahora = time.time()
    for _ in range(MOVE_BURST_COUNT):
        coordinator.polling.note_message(ahora)

    assert _estado(coordinator, realtime={"hVoltageState": "1"}) == (
        "en marcha",
        coordinator.poll_moving_min,
    )


def test_poll_state_parado(coordinator) -> None:
    assert _estado(coordinator) == ("parado", coordinator.poll_parked_min)


def test_poll_state_cerca_del_limite_afina_el_sondeo(coordinator) -> None:
    """A menos de 5 puntos del objetivo se sondea cada 5 min para clavar el corte."""
    coordinator.charge_limit_enabled = True
    coordinator.charge_limit_soc = 80

    label, mins = _estado(
        coordinator,
        realtime={"chargeState": "1", "chargeGunState": "1", "dumpEnergy": "77"},
    )

    assert label == "cargando"
    assert mins == CHARGE_LIMIT_NEAR_MIN


def test_intervalo_cero_significa_no_tocar_el_coche(coordinator) -> None:
    """`poll_normal_min = 0` significa «no tocar el coche estando parado».

    Se afirma sobre `_poll_state` + `interval_seconds`, que es el camino que recorre el código
    real: antes había un `_active_interval_s()` que solo existía porque lo llamaban estos dos
    tests, y que por tanto no probaba nada de lo que ocurre en producción."""
    coordinator.poll_parked_min = 0
    _, mins = _estado(coordinator)

    assert interval_seconds(mins) is None


def test_intervalo_en_segundos(coordinator) -> None:
    _, mins = _estado(coordinator, realtime={"chargeState": "1", "chargeGunState": "1"})

    assert interval_seconds(mins) == coordinator.poll_charging_min * 60


def test_is_plugged_basta_una_fuente(coordinator) -> None:
    """Una lectura realtime rancia con `chargeGunState=0` no debe hacer que una carga se
    confunda con «marcha»: basta que UNA de las dos fuentes diga enchufado."""
    coordinator.data = {**coordinator.data, "realtime": {"chargeGunState": "0"}}
    coordinator.state.seed(fields={"chargeGunState": "1"})
    assert coordinator.polling.is_plugged() is True

    coordinator.state.seed(fields={"chargeGunState": "0"})
    assert coordinator.polling.is_plugged() is False


def test_is_hv_on_por_velocidad(coordinator) -> None:
    """Red de seguridad verificada en vivo: 38 km/h con `engineState=0` y `hVoltageState=1`."""
    coordinator.data = {
        **coordinator.data,
        "realtime": {"engineState": "0", "vehicleSpeed": "38"},
    }
    coordinator.state.seed(fields={})

    assert coordinator.polling.is_hv_on() is True


def test_burst_poda_las_marcas_viejas(coordinator, freezer: FrozenDateTimeFactory) -> None:
    for _ in range(MOVE_BURST_COUNT):
        coordinator.polling.note_message(time.time())
    assert coordinator.polling._burst.is_active() is True

    freezer.tick(MOVE_BURST_WINDOW_S + 1)

    assert coordinator.polling._burst.is_active() is False


def test_plugged_timeout(coordinator, freezer: FrozenDateTimeFactory) -> None:
    """Tope de «enchufado sin cargar»: no sondear indefinidamente un coche que nunca carga."""
    coordinator._plugged_wait_max_s = 3600
    coordinator.polling._plugged_since = time.time()
    assert coordinator.polling.plugged_timed_out() is False

    freezer.tick(3601)
    assert coordinator.polling.plugged_timed_out() is True


def test_plugged_timeout_desactivado_por_defecto(coordinator) -> None:
    """0 = sin límite: la carga programada puede arrancar horas después de enchufar y un tope
    corto la haría perderse."""
    coordinator._plugged_wait_max_s = 0
    coordinator.polling._plugged_since = 1.0

    assert coordinator.polling.plugged_timed_out() is False


@pytest.mark.parametrize(
    ("dump_energy", "esperado"),
    [("64.5", 64.5), ("0", None), ("0.0", None), (None, None), ("no-numero", None)],
)
def test_current_soc(coordinator, dump_energy, esperado) -> None:
    """`dumpEnergy = 0` es el marcador «alta tensión apagada», no un 0 % real."""
    coordinator.data = {**coordinator.data, "realtime": {"dumpEnergy": dump_energy}}

    assert coordinator.current_soc() == esperado


# ───────────────────────── plan de carga ─────────────────────────


def test_check_charge_limit_para_la_carga_una_sola_vez(coordinator) -> None:
    """El coordinator conecta la decisión (ChargeLimiter, ver tests/test_charging.py) con el
    envío del comando: aquí se comprueba justamente ese cableado."""
    coordinator.charge_limit_enabled = True
    coordinator.charge_limit_soc = 80
    coordinator.data = {
        **coordinator.data,
        "realtime": {"chargeState": "1", "dumpEnergy": "82"},
    }

    with patch.object(
        coordinator, "async_stop_charge_via_schedule", AsyncMock()
    ) as stop:
        coordinator.check_charge_limit()
        coordinator.check_charge_limit()

    assert stop.call_count == 1


def test_check_charge_limit_se_rearma_al_dejar_de_cargar(coordinator) -> None:
    """Tras una sesión de carga cortada, la siguiente vuelve a poder cortarse."""
    coordinator.charge_limit_enabled = True
    coordinator.charge_limit_soc = 80
    coordinator.data = {
        **coordinator.data,
        "realtime": {"chargeState": "1", "dumpEnergy": "82"},
    }
    with patch.object(coordinator, "async_stop_charge_via_schedule", AsyncMock()):
        coordinator.check_charge_limit()

    coordinator.data = {**coordinator.data, "realtime": {"chargeState": "0"}}
    coordinator.check_charge_limit()          # deja de cargar → rearme

    coordinator.data = {
        **coordinator.data,
        "realtime": {"chargeState": "1", "dumpEnergy": "85"},
    }
    with patch.object(
        coordinator, "async_stop_charge_via_schedule", AsyncMock()
    ) as stop:
        coordinator.check_charge_limit()

    assert stop.call_count == 1


def test_check_charge_limit_apagado_no_hace_nada(coordinator) -> None:
    coordinator.charge_limit_enabled = False
    coordinator.data = {
        **coordinator.data,
        "realtime": {"chargeState": "1", "dumpEnergy": "99"},
    }

    with patch.object(coordinator, "async_stop_charge_via_schedule", AsyncMock()) as stop:
        coordinator.check_charge_limit()

    stop.assert_not_called()


# ───────────────────────── _on_car_message ─────────────────────────


def _mensaje(service_type: str, data: dict) -> bytes:
    """El payload en crudo de un mensaje MQTT, tal como lo entrega `EbroMqttClient`.

    Antes esto fabricaba un MagicMock con la firma de callback de paho (`cl, userdata, msg`).
    Ahora el cliente MQTT vive en su propio módulo y entrega solo los bytes: el coordinator ya
    no sabe que debajo hay paho, y el test tampoco tiene que saberlo."""
    return json.dumps({"content": {"serviceType": service_type, "data": data}}).encode()


async def test_mensaje_de_posicion(hass: HomeAssistant, coordinator) -> None:
    """`serviceType 1301` = fix GPS."""
    coordinator._on_car_message(_mensaje("1301", {"lat": "40.4", "lon": "-3.7"}))
    await hass.async_block_till_done()

    assert coordinator.data["position"]["lat"] == "40.4"
    assert coordinator.data["last_pos_fix"] is not None


async def test_mensaje_de_telemetria(hass: HomeAssistant, coordinator) -> None:
    coordinator._on_car_message(_mensaje("5A02", {"doorLock": "1", "frontLeftDoor": "0"}))
    await hass.async_block_till_done()

    assert coordinator.data["fields"]["doorLock"] == "1"
    assert coordinator.data["last_seen"] is not None


async def test_confirmacion_de_comando(hass: HomeAssistant, coordinator) -> None:
    """Los metadatos de confirmación NO son telemetría de estado: no deben ensuciar `fields`."""
    coordinator._on_car_message(
        _mensaje("1105", {"result": "1", "seq": "VIN-123", "resultTime": "x", "hasAsy": "0"})
    )
    await hass.async_block_till_done()

    assert coordinator.data["cmd_status"] == "Comando ejecutado y confirmado por el coche ✅"
    for meta in telemetry.CMD_CONFIRM_META:
        assert meta not in coordinator.data["fields"]


async def test_latido_solo_con_time_no_mueve_last_seen(
    hass: HomeAssistant, coordinator
) -> None:
    """El coche emite un push de solo `time` cada pocos segundos MIENTRAS CIRCULA.

    No trae telemetría, así que no debe mover «Último contacto» ni escribir en el recorder
    cada pocos segundos durante todo un viaje. `last_seen` se saca del patch (se CONSERVA el
    valor anterior, no se borra) y, con el coche ya despierto, ni siquiera se emite una
    actualización de estado.
    """
    anterior = coord_mod.dt_util.utcnow()
    coordinator._apply_update({"last_seen": anterior})

    coordinator._on_car_message(_mensaje("5A02", {"time": "12:00:00"}))
    await hass.async_block_till_done()

    assert coordinator.data["last_seen"] == anterior
    assert "time" not in coordinator.data["fields"]


async def test_latido_en_el_flanco_de_despertar_si_actualiza(
    hass: HomeAssistant, coordinator
) -> None:
    """Con el coche dormido, hasta un heartbeat vacío tiene que encender «Coche despierto» —
    pero sigue sin tocar `last_seen`, que significa «último contacto CON DATOS»."""
    anterior = coord_mod.dt_util.utcnow()
    coordinator._apply_update({"last_seen": anterior, "awake": False})
    coordinator.state.seed(last_msg_ts=0.0)  # dormido

    coordinator._on_car_message(_mensaje("5A02", {"time": "12:00:00"}))
    await hass.async_block_till_done()

    assert coordinator.data["awake"] is True
    assert coordinator.data["last_seen"] == anterior


async def test_payload_ilegible_no_revienta(hass: HomeAssistant, coordinator) -> None:
    """Corre en el hilo de paho: una excepción aquí mataría el cliente MQTT en silencio."""
    coordinator._on_car_message(b"esto no es JSON")
    await hass.async_block_till_done()


# ───────────────────────── cola de comandos ─────────────────────────


async def test_comandos_serializados(hass: HomeAssistant, coordinator) -> None:
    """El coche ejecuta UN comando cada vez (A00082 = ocupado): el segundo espera turno en vez
    de ser rechazado."""
    assert coordinator._cmd_gate.locked() is False

    # `COMMAND_SETTLE_S = 0` para no esperar de verdad los 5 s de la pausa entre comandos
    with (
        patch.object(coordinator, "_send_command", return_value="ok"),
        patch("custom_components.ebro.vehicle.coordinator.COMMAND_SETTLE_S", 0),
    ):
        assert await coordinator.async_send_command("bloquear") == "ok"
        await hass.async_block_till_done()

    # el hueco de la cola se libera en la tarea de fondo, no en el retorno del llamador
    assert coordinator._cmd_gate.locked() is False


async def test_comando_en_cola_demasiado_tiempo(hass: HomeAssistant, coordinator) -> None:
    """Pasado `COMMAND_QUEUE_WAIT` el comando falla con un mensaje claro en vez de colgarse."""
    await coordinator._cmd_gate.acquire()
    try:
        with (
            patch(
                "custom_components.ebro.vehicle.coordinator.COMMAND_QUEUE_WAIT", 0.01
            ),
            pytest.raises(HomeAssistantError, match="sigue ocupado"),
        ):
            await coordinator.async_send_command("bloquear")
    finally:
        coordinator._cmd_gate.release()


# ───────────────────────── sesión ─────────────────────────


async def test_sesion_caducada_abre_la_reautenticacion(
    hass: HomeAssistant, coordinator, mock_core
) -> None:
    """`status == EXPIRED` (marcador estable) es lo que dispara la reauth, nunca el texto."""
    mock_core["session_check"].return_value = (False, "Sesión caducada ❌", "EXPIRED")

    with patch.object(coordinator.entry, "async_start_reauth") as reauth:
        ok, _detalle = await coordinator.async_check_session()
        await hass.async_block_till_done()

    assert ok is False
    assert coordinator.data["session_ok"] is False
    reauth.assert_called_once()


async def test_error_de_red_no_abre_la_reautenticacion(
    hass: HomeAssistant, coordinator, mock_core
) -> None:
    """Un corte de red NO debe hacer aparecer la tarjeta «Reautenticar»: el usuario
    reautenticaría en balde por algo que se arregla solo."""
    mock_core["session_check"].return_value = (False, "error de red", "NET_ERROR")

    with patch.object(coordinator.entry, "async_start_reauth") as reauth:
        await coordinator.async_check_session()
        await hass.async_block_till_done()

    reauth.assert_not_called()


def test_el_marcador_expired_no_ha_derivado(coordinator) -> None:
    """`session_manager` tiene su propia copia de `EXPIRED`; si `core/session.py` cambiara el
    valor y aquí no, la reautenticación dejaría de dispararse en silencio.

    El marcador vive en `session_manager` desde que la gestión de sesión salió del coordinator;
    el reexport que quedaba en `coordinator` era una muleta sin usuarios y se ha ido."""
    from custom_components.ebro.core import session
    from custom_components.ebro.vehicle import session_manager

    assert session_manager.STATUS_EXPIRED == session.STATUS_EXPIRED


# ───────────────── dispatch por topic (descubrimiento) ─────────────────


async def test_un_mensaje_de_otro_topic_no_toca_el_estado(
    hass: HomeAssistant, coordinator
) -> None:
    """Con el descubrimiento activo llegan topics cuyo formato NO conocemos. Se apuntan, y ya:
    dar por hecho la forma de un canal que no hemos deducido sería inventarse la telemetría."""
    coordinator._car_topic = "app/4/U123/account/msgCenter/msg"
    campos_antes = dict(coordinator.data["fields"])
    visto_antes = coordinator.data["last_seen"]

    coordinator._on_car_message(
        _mensaje("5A02", {"doorLock": "9"}), "app/4/U123/algo/desconocido"
    )
    await hass.async_block_till_done()

    assert coordinator.data["fields"] == campos_antes
    assert coordinator.data["last_seen"] == visto_antes


async def test_el_topic_conocido_se_procesa_igual_que_siempre(
    hass: HomeAssistant, coordinator
) -> None:
    """El dispatch por topic no puede cambiar el camino normal."""
    coordinator._car_topic = "app/4/U123/account/msgCenter/msg"

    coordinator._on_car_message(_mensaje("5A02", {"doorLock": "1"}), coordinator._car_topic)
    await hass.async_block_till_done()

    assert coordinator.data["fields"]["doorLock"] == "1"


async def test_sin_topic_se_procesa_como_siempre(hass: HomeAssistant, coordinator) -> None:
    """Compatibilidad: quien entrega solo los bytes sigue funcionando."""
    coordinator._on_car_message(_mensaje("5A02", {"doorLock": "1"}))
    await hass.async_block_till_done()

    assert coordinator.data["fields"]["doorLock"] == "1"


async def test_el_monitor_apunta_el_tipo_de_cada_mensaje(
    hass: HomeAssistant, coordinator
) -> None:
    """Responde a «¿este coche empuja posición por MQTT o solo estado?», que es lo que decide
    si el mapa puede mantenerse vivo sin tocar el coche. Sin coordenadas en el registro."""
    from unittest.mock import MagicMock

    coordinator._diag_monitor._recorder = grabador = MagicMock()

    coordinator._on_car_message(_mensaje("1301", {"lat": "40.4", "lon": "-3.7"}))
    await hass.async_block_till_done()

    tipo, campos = grabador.record.call_args[0], grabador.record.call_args[1]
    assert tipo[0] == "mqtt_msg"
    assert campos["svc"] == "1301"
    assert campos["geo"] is True
    assert "40.4" not in str(campos)
