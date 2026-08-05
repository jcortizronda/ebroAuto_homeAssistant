"""Tests de la plataforma `climate` (1 entidad)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)

ENTITY_ID = "climate.ebro_0001_climatizacion"


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.CLIMATE]


@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_estado_desde_la_telemetria(hass: HomeAssistant) -> None:
    """`frontHVACState` manda; `hvac_mode` nunca devuelve None."""
    coordinator = get_coordinator(hass)

    assert hass.states.get(ENTITY_ID).state == HVACMode.HEAT_COOL

    coordinator._apply_update(
        {"fields": {**coordinator.data["fields"], "frontHVACState": "0"}}
    )
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == HVACMode.OFF


@pytest.mark.parametrize(
    ("servicio", "datos", "comando"),
    [
        (SERVICE_TURN_ON, {}, "clima_on"),
        (SERVICE_TURN_OFF, {}, "clima_off"),
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.HEAT_COOL}, "clima_on"),
        (SERVICE_SET_HVAC_MODE, {ATTR_HVAC_MODE: HVACMode.OFF}, "clima_off"),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_servicios(
    hass: HomeAssistant, servicio: str, datos: dict, comando: str
) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            CLIMATE_DOMAIN, servicio, {ATTR_ENTITY_ID: ENTITY_ID, **datos}, blocking=True
        )

    # temperatura por defecto 21.0 y duración tomada del number «clima_duration»
    send.assert_awaited_once_with(comando, {"temperature": "21.0", "times": "15"})


@pytest.mark.usefixtures("init_integration")
async def test_la_duracion_viene_del_number(hass: HomeAssistant) -> None:
    """`clima_duration` lo escribe `number.py` en el coordinator; el clima solo lo lee."""
    coordinator = get_coordinator(hass)
    coordinator.preferences.clima_duration = 5

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )

    assert send.await_args[0][1]["times"] == "5"


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature_con_el_clima_encendido_reaplica(hass: HomeAssistant) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 24},
            blocking=True,
        )

    send.assert_awaited_once_with("clima_on", {"temperature": "24.0", "times": "15"})


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature_con_el_clima_apagado_solo_memoriza(
    hass: HomeAssistant,
) -> None:
    """Con el clima apagado NO se manda nada al coche: el setpoint queda guardado para el
    próximo encendido. Mandar `clima_on` aquí encendería el clima sin que nadie lo pidiera."""
    coordinator = get_coordinator(hass)
    coordinator._apply_update(
        {"fields": {**coordinator.data["fields"], "frontHVACState": "0"}}
    )
    await hass.async_block_till_done()

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 24},
            blocking=True,
        )

    send.assert_not_awaited()
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 24.0
    assert hass.states.get(ENTITY_ID).state == HVACMode.OFF


@pytest.mark.parametrize(("pedida", "aplicada"), [(10, 16.0), (40, 30.0), (22, 22.0)])
@pytest.mark.usefixtures("init_integration")
async def test_temperatura_acotada_a_16_30(
    hass: HomeAssistant, pedida: float, aplicada: float
) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        # se llama al método directamente: el esquema del servicio ya rechazaría 10 y 40
        entidad = hass.data["entity_components"][CLIMATE_DOMAIN].get_entity(ENTITY_ID)
        await entidad.async_set_temperature(**{ATTR_TEMPERATURE: pedida})

    assert send.await_args[0][1]["temperature"] == f"{aplicada:.1f}"


@pytest.mark.usefixtures("init_integration")
async def test_set_temperature_sin_temperatura_no_hace_nada(hass: HomeAssistant) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        entidad = hass.data["entity_components"][CLIMATE_DOMAIN].get_entity(ENTITY_ID)
        await entidad.async_set_temperature()

    send.assert_not_awaited()


@pytest.mark.usefixtures("init_integration")
async def test_error_con_mensaje_propio(hass: HomeAssistant) -> None:
    """`EbroClimate` usa `EbroOptimisticMixin` pero redefine `_command_error`: al usuario le
    habla del clima, no de la clave interna del comando («Comando «clima_off» fallido»)."""
    coordinator = get_coordinator(hass)

    with (
        patch.object(
            coordinator, "async_send_command", AsyncMock(side_effect=RuntimeError("A00084"))
        ),
        pytest.raises(HomeAssistantError, match="Comando de clima fallido"),
    ):
        await hass.services.async_call(
            CLIMATE_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )

    # el optimismo se anula → vuelve al estado real (encendido)
    assert hass.states.get(ENTITY_ID).state == HVACMode.HEAT_COOL
