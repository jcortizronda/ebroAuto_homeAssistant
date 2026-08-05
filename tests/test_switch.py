"""Tests de la plataforma `switch` (15 entidades).

Es la plataforma más rica: cinco clases distintas con semánticas muy diferentes — unas mandan
comandos al coche, otras solo escriben un atributo local del coordinator.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)

CARGA_PROG = "switch.ebro_0001_carga_programada"
ASIENTO_CALOR = "switch.ebro_0001_calefaccion_asiento_conductor"
ASIENTO_VENT = "switch.ebro_0001_ventilacion_asiento_conductor"
VOLANTE = "switch.ebro_0001_calefaccion_del_volante"
ALARMA = "switch.ebro_0001_alarma"
POLLING = "switch.ebro_0001_actualizacion_automatica"
LIMITE = "switch.ebro_0001_limitar_carga_al_porcentaje"


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.SWITCH]


def _coordinator(hass: HomeAssistant):
    return get_coordinator(hass)


@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_numero_de_entidades(
    entity_registry: er.EntityRegistry, mock_config_entry: MockConfigEntry
) -> None:
    entidades = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert len(entidades) == 15


# ───────────────────────── confort ─────────────────────────


@pytest.mark.parametrize(
    ("servicio", "comando"),
    [
        (SERVICE_TURN_ON, "volante_caliente"),
        (SERVICE_TURN_OFF, "volante_caliente_off"),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_confort_manda_comando(
    hass: HomeAssistant, servicio: str, comando: str
) -> None:
    coordinator = _coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            SWITCH_DOMAIN, servicio, {ATTR_ENTITY_ID: VOLANTE}, blocking=True
        )

    send.assert_awaited_once_with(comando, None)


@pytest.mark.usefixtures("init_integration")
async def test_confort_lee_el_campo_5a02(hass: HomeAssistant) -> None:
    assert hass.states.get(VOLANTE).state == STATE_ON
    assert hass.states.get(ASIENTO_VENT).state == STATE_OFF


@pytest.mark.usefixtures("init_integration")
async def test_calor_y_ventilacion_se_excluyen(hass: HomeAssistant) -> None:
    """El coche los excluye de verdad (verificado en telemetría): encender la ventilación
    apaga el calor. Se refleja de inmediato en el optimismo para que la card no muestre los
    dos encendidos durante los minutos que tarda en llegar la telemetría real.
    """
    coordinator = _coordinator(hass)
    assert hass.states.get(ASIENTO_CALOR).state == STATE_ON

    with patch.object(coordinator, "async_send_command", AsyncMock(return_value="ok")):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ASIENTO_VENT}, blocking=True
        )

    assert hass.states.get(ASIENTO_VENT).state == STATE_ON
    # ...y el gemelo se apaga aunque el campo 5A02 siga diciendo "1"
    assert coordinator.data["fields"]["dSeatHeatingState"] == "1"
    assert hass.states.get(ASIENTO_CALOR).state == STATE_OFF


@pytest.mark.usefixtures("init_integration")
async def test_el_volante_no_tiene_gemelo(hass: HomeAssistant) -> None:
    """Solo los cuatro asientos están emparejados; parabrisas/luneta/volante no."""
    coordinator = _coordinator(hass)

    with patch.object(coordinator, "async_send_command", AsyncMock(return_value="ok")):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: VOLANTE}, blocking=True
        )

    assert hass.states.get(ASIENTO_CALOR).state == STATE_ON  # intacto


# ───────────────────────── carga programada ─────────────────────────


@pytest.mark.usefixtures("init_integration")
async def test_carga_programada_lee_el_plan_como_cadena(hass: HomeAssistant) -> None:
    """El coche manda `chargeAppointPlans` como el repr de una lista de dicts (una cadena),
    que hay que pasar por `ast.literal_eval`."""
    assert hass.states.get(CARGA_PROG).state == STATE_ON


@pytest.mark.usefixtures("init_integration")
async def test_carga_programada_acepta_lista_nativa(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._apply_update(
        {
            "fields": {
                **coordinator.data["fields"],
                "chargeAppointPlans": [{"switchStatus": 0}],
            }
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get(CARGA_PROG).state == STATE_OFF


@pytest.mark.parametrize(
    "malformado", ["no-es-python", "[{", "[]", "[{'otraClave': 1}]", ""]
)
@pytest.mark.usefixtures("init_integration")
async def test_carga_programada_plan_malformado_no_revienta(
    hass: HomeAssistant, malformado
) -> None:
    """Ante un plan ilegible cae al último estado conocido, sin lanzar."""
    coordinator = _coordinator(hass)
    coordinator._apply_update(
        {"fields": {**coordinator.data["fields"], "chargeAppointPlans": malformado}}
    )
    await hass.async_block_till_done()

    assert hass.states.get(CARGA_PROG).state in (STATE_ON, STATE_OFF)


@pytest.mark.usefixtures("init_integration")
async def test_carga_programada_envia_el_plan_construido(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: CARGA_PROG}, blocking=True
        )

    comando, params = send.await_args[0]
    assert comando == "carga_prog_off"
    assert params["mainSwitch"] == 0
    assert params["chargeAppointPlans"] == [coordinator.build_charge_plan(0)]


# ───────────────────────── alarma antirrobo ─────────────────────────


@pytest.mark.usefixtures("init_integration")
async def test_alarma_se_siembra_por_rest(hass: HomeAssistant, mock_core) -> None:
    """El estado de la alarma NO llega por MQTT: se lee vía REST al añadir la entidad.

    Sin mockear `query_theft_switch` este estado sería no determinista y el snapshot
    fallaría de forma intermitente.
    """
    mock_core["query_theft"].assert_called()
    assert hass.states.get(ALARMA).state == STATE_ON


@pytest.mark.parametrize(
    ("servicio", "comando", "state"),
    [
        (SERVICE_TURN_ON, "alarma_on", STATE_ON),
        (SERVICE_TURN_OFF, "alarma_off", STATE_OFF),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_alarma_servicios(
    hass: HomeAssistant, servicio: str, comando: str, state: str
) -> None:
    coordinator = _coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            SWITCH_DOMAIN, servicio, {ATTR_ENTITY_ID: ALARMA}, blocking=True
        )

    send.assert_awaited_once_with(comando, None)
    assert hass.states.get(ALARMA).state == state


@pytest.mark.usefixtures("init_integration")
async def test_alarma_no_rebota_con_la_telemetria(hass: HomeAssistant) -> None:
    """Sin actualizar `_real` tras el comando, el primer mensaje MQTT anularía el optimismo y
    la card volvería al valor de arranque: un rebote ON↔OFF visible para el usuario."""
    coordinator = _coordinator(hass)

    with patch.object(coordinator, "async_send_command", AsyncMock(return_value="ok")):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ALARMA}, blocking=True
        )

    # llega telemetría nueva (avanza last_seen) → se anula el optimismo
    coordinator._apply_update({"last_seen": coordinator.data["last_seen"].replace(second=30)})
    await hass.async_block_till_done()

    assert hass.states.get(ALARMA).state == STATE_OFF


# ───────────────────────── interruptores puramente locales ─────────────────────────


@pytest.mark.usefixtures("init_integration")
async def test_polling_no_manda_ningun_comando(hass: HomeAssistant) -> None:
    """«Actualización automática» actúa solo sobre los timers locales: no toca el coche."""
    coordinator = _coordinator(hass)
    assert hass.states.get(POLLING).state == STATE_OFF

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: POLLING}, blocking=True
        )

    send.assert_not_awaited()
    assert coordinator.poll_enabled is True
    assert hass.states.get(POLLING).state == STATE_ON


@pytest.mark.usefixtures("init_integration")
async def test_limite_de_carga_es_solo_un_atributo(hass: HomeAssistant) -> None:
    """Límite por SOFTWARE: el switch solo levanta la bandera que vigila el coordinator."""
    coordinator = _coordinator(hass)
    assert coordinator.charge_limit_enabled is False

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: LIMITE}, blocking=True
        )

    send.assert_not_awaited()
    assert coordinator.charge_limit_enabled is True
    assert hass.states.get(LIMITE).state == STATE_ON
