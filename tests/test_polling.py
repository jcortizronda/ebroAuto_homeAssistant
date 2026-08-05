"""Tests de `vehicle/polling.py` — cuándo se le pregunta al coche.

Equivocarse aquí tiene dos costes medidos en campo: consume la batería de 12 V del coche y
desconecta la app oficial del usuario, porque la nube de Chery admite una sola sesión por
cuenta. Los tres comportamientos que se fijan abajo solo se cubrían de rebote, a través del
coordinator, cuando el bucle vivía dentro de él.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ebro.const import CHARGE_LIMIT_NEAR_MIN
from custom_components.ebro.vehicle.timers import HV_POLL, STARTUP_PROBE

from .conftest import get_coordinator


@pytest.fixture
def platforms() -> list[str]:
    """Sin plataformas: estos tests miran el bucle, no las entidades."""
    return []


@pytest.mark.usefixtures("init_integration")
async def test_apagar_el_interruptor_detiene_el_bucle(hass: HomeAssistant) -> None:
    """«Actualización automática» apagado tiene que dejar el grupo de sondeo sin un solo timer.

    Es el caso que motivó el `TimerRegistry`: apagando el interruptor durante una carga quedaba
    activo justo el ciclo más frecuente, sin dar ningún error en el log."""
    coordinator = get_coordinator(hass)
    coordinator.polling.set_enabled(True)
    coordinator.polling.schedule_next()

    coordinator.polling.set_enabled(False)

    assert coordinator.timers.is_armed(HV_POLL) is False
    assert coordinator.timers.is_armed(STARTUP_PROBE) is False
    assert coordinator.polling.enabled is False


@pytest.mark.usefixtures("init_integration")
async def test_una_sonda_en_vuelo_al_descargar_no_rearma_el_bucle(hass: HomeAssistant) -> None:
    """El sondeo huérfano: una lectura que regresa DESPUÉS del stop no debe reprogramar nada.

    Pasó de verdad — el ciclo siguió interrogando a la nube durante horas con la integración
    apagada. El invariante vive en `TimerRegistry.close()`, y esto comprueba que el bucle lo
    respeta."""
    coordinator = get_coordinator(hass)
    coordinator.polling.set_enabled(True)

    # el stop llega mientras la sonda está en el executor
    coordinator.timers.close()
    coordinator.polling.schedule_next()

    assert coordinator.timers.is_armed(HV_POLL) is False
    assert coordinator.timers.armed() == set()


@pytest.mark.usefixtures("init_integration")
async def test_cerca_del_limite_de_carga_se_afina_el_intervalo(hass: HomeAssistant) -> None:
    """A pocos puntos del objetivo se sondea más fino, para clavar el corte.

    Sin esto, con el intervalo de carga en 15 min la batería puede pasarse del límite antes de
    la siguiente lectura."""
    coordinator = get_coordinator(hass)
    coordinator.charge_limit_enabled = True
    coordinator.charge_limit_soc = 80
    coordinator._apply_update({"realtime": {"chargeState": "1", "dumpEnergy": "77"}})

    etiqueta, minutos = coordinator.polling.state_and_minutes()

    assert etiqueta == "cargando"
    assert minutos == CHARGE_LIMIT_NEAR_MIN


@pytest.mark.usefixtures("init_integration")
async def test_lejos_del_limite_se_usa_el_intervalo_normal(hass: HomeAssistant) -> None:
    coordinator = get_coordinator(hass)
    coordinator.charge_limit_enabled = True
    coordinator.charge_limit_soc = 80
    coordinator._apply_update({"realtime": {"chargeState": "1", "dumpEnergy": "40"}})

    _etiqueta, minutos = coordinator.polling.state_and_minutes()

    assert minutos == coordinator.poll_charging_min


@pytest.mark.usefixtures("init_integration")
async def test_el_flanco_de_rafaga_se_dispara_una_sola_vez(hass: HomeAssistant) -> None:
    """Lo que interesa es el FLANCO: dentro de la ráfaga no se vuelve a disparar.

    Si cada mensaje de un viaje disparara una sonda, el coche recibiría decenas de lecturas por
    minuto — justo lo contrario de lo que el sondeo por estado pretende."""
    from custom_components.ebro.const import MOVE_BURST_COUNT

    coordinator = get_coordinator(hass)
    flancos = [coordinator.polling.note_message(1_000.0 + i)
               for i in range(MOVE_BURST_COUNT + 3)]

    assert flancos.count(True) == 1
    assert flancos[MOVE_BURST_COUNT - 1] is True


@pytest.mark.usefixtures("init_integration")
async def test_el_cable_desconectado_reinicia_el_tope_de_espera(hass: HomeAssistant) -> None:
    """El tope de «enchufado sin cargar» se cuenta desde que se conecta el cable; al soltarlo
    tiene que volver a cero, o la próxima sesión nacería ya vencida."""
    coordinator = get_coordinator(hass)

    coordinator.polling.note_plug_change(True, now=1_000.0)
    assert coordinator.polling._plugged_since == 1_000.0

    coordinator.polling.note_plug_change(False, now=1_100.0)
    assert coordinator.polling._plugged_since == 0.0


@pytest.mark.usefixtures("init_integration")
async def test_seguir_enchufado_no_reinicia_el_reloj(hass: HomeAssistant) -> None:
    """Cada 5A02 con el cable puesto repite `chargeGunState=1`: si eso reiniciara la marca, el
    tope no vencería nunca."""
    coordinator = get_coordinator(hass)

    coordinator.polling.note_plug_change(True, now=1_000.0)
    coordinator.polling.note_plug_change(True, now=1_500.0)

    assert coordinator.polling._plugged_since == 1_000.0


@pytest.mark.usefixtures("init_integration")
async def test_el_bucle_se_reprograma_tras_cada_lectura(hass: HomeAssistant) -> None:
    """No hay timer periódico: cada sonda programa la siguiente según lo que acaba de leer."""
    coordinator = get_coordinator(hass)
    coordinator.polling.set_enabled(True)
    coordinator._apply_update({"realtime": {"chargeState": "1", "dumpEnergy": "50"}})

    with patch.object(coordinator, "_probe"):
        await coordinator.async_probe(force=True)

    assert coordinator.timers.is_armed(HV_POLL) is True


@pytest.mark.usefixtures("init_integration")
async def test_parado_con_intervalo_cero_no_programa_nada(hass: HomeAssistant) -> None:
    """«No tocar el coche estando parado» es el valor por defecto, y significa exactamente eso:
    ningún timer hasta el próximo evento MQTT gratis."""
    coordinator = get_coordinator(hass)
    coordinator.polling.set_enabled(True)
    coordinator.poll_parked_min = 0
    coordinator.polling._intervals = type(coordinator.polling._intervals)(
        parked=0, plugged=30, charging=15, moving=3, moving_idle=5)
    coordinator._apply_update({"realtime": {}})
    coordinator.state.seed(fields={})

    coordinator.polling.schedule_next()

    assert coordinator.timers.is_armed(HV_POLL) is False


@pytest.mark.usefixtures("init_integration")
async def test_la_semilla_no_se_arma_con_el_interruptor_apagado(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """El sondeo es de solo lectura, pero aun así lo activa el usuario: apagado, ni la sonda
    semilla del arranque debe programarse."""
    coordinator = get_coordinator(hass)
    coordinator.timers.cancel(STARTUP_PROBE)
    coordinator.polling.enabled = False

    coordinator.polling.start()

    assert coordinator.timers.is_armed(STARTUP_PROBE) is False


@pytest.mark.usefixtures("init_integration")
async def test_el_limite_de_carga_se_comprueba_en_cada_reprogramacion(
    hass: HomeAssistant,
) -> None:
    """El corte se decide con datos frescos: la comprobación va justo tras la lectura."""
    coordinator = get_coordinator(hass)
    coordinator.polling.set_enabled(True)

    with patch.object(coordinator, "check_charge_limit") as check:
        coordinator.polling.schedule_next()

    check.assert_called_once()


@pytest.mark.usefixtures("init_integration")
async def test_enchufado_con_alta_tension_es_carga_y_no_marcha(hass: HomeAssistant) -> None:
    """Cargar también enciende la alta tensión. Confundirlo con «marcha» hacía sondear cada 3
    minutos toda una noche de carga."""
    coordinator = get_coordinator(hass)
    coordinator._apply_update({"realtime": {"chargeGunState": "1", "hVoltageState": "1"}})

    etiqueta, minutos = coordinator.polling.state_and_minutes()

    assert etiqueta == "cargando"
    assert minutos == coordinator.poll_charging_min


@pytest.mark.usefixtures("init_integration")
async def test_una_lectura_realtime_rancia_no_desenchufa_el_coche(hass: HomeAssistant) -> None:
    """Basta que UNA de las dos fuentes diga «enchufado». Para darlo por desconectado tienen
    que coincidir las dos."""
    coordinator = get_coordinator(hass)
    coordinator._apply_update({"realtime": {"chargeGunState": "0"}})
    coordinator.state.seed(fields={"chargeGunState": "1"})

    assert coordinator.polling.is_plugged() is True

    coordinator.state.seed(fields={"chargeGunState": "0"})

    assert coordinator.polling.is_plugged() is False


@pytest.mark.usefixtures("init_integration")
async def test_velocidad_positiva_cuenta_como_alta_tension(hass: HomeAssistant) -> None:
    """Red de seguridad verificada en vivo (2026-06-25): 38 km/h con `engineState=0`. El coche
    está despierto en la red HV, así que el realtime es real y hay que seguirlo."""
    coordinator = get_coordinator(hass)
    coordinator._apply_update(
        {"realtime": {"engineState": "0", "hVoltageState": "0", "vehicleSpeed": "38"}})
    coordinator.state.seed(fields={})

    assert coordinator.polling.is_hv_on() is True


@pytest.mark.usefixtures("init_integration")
async def test_probe_no_reprograma_si_llego_el_stop(hass: HomeAssistant) -> None:
    """Complemento del test del sondeo huérfano, esta vez por el camino de `async_probe`."""
    coordinator = get_coordinator(hass)
    coordinator.polling.set_enabled(True)

    with (
        patch.object(coordinator, "_probe"),
        patch.object(coordinator.polling, "schedule_next") as schedule,
    ):
        coordinator.timers.close()
        await coordinator.async_probe(force=True)

    schedule.assert_not_called()


@pytest.mark.usefixtures("init_integration")
async def test_la_semilla_no_tumba_el_arranque_si_falla(hass: HomeAssistant) -> None:
    """La sonda semilla es best-effort: un fallo de red al arrancar no puede dejar la
    integración sin cargar."""
    coordinator = get_coordinator(hass)

    with patch.object(
        coordinator, "async_probe", AsyncMock(side_effect=RuntimeError("sin red"))
    ):
        await coordinator.polling._seed_cb(None)   # no debe propagar
