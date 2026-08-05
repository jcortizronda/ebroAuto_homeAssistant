"""Tests de la plataforma `button` (7 entidades: 3 de comando + 4 de acción)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.const import COMMANDS_AS_RICH_ENTITY

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.BUTTON]


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
async def test_solo_quedan_tres_botones_de_comando(
    entity_registry: er.EntityRegistry, mock_config_entry: MockConfigEntry
) -> None:
    """Del catálogo de 41 comandos, 38 los reclama un lock/switch/cover/climate.

    Las dos tablas (`COMMANDS` y `COMMANDS_AS_RICH_ENTITY`) se mantienen alineadas a mano:
    sacar una clave de la segunda sin darse cuenta haría reaparecer un botón suelto que
    duplica un control existente.
    """
    from custom_components.ebro.core import catalog

    sueltos = [k for k, _ in catalog.COMMANDS if k not in COMMANDS_AS_RICH_ENTITY]
    assert sueltos == [
        "ventilar_ventanillas",
        "encontrar_coche_luces",
        "localizar_coche_gps",
    ]

    entidades = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert len(entidades) == len(sueltos) + 4  # + wake/refresh_pos/refresh_full/charge_plan


@pytest.mark.parametrize(
    ("entity_id", "comando"),
    [
        ("button.ebro_0001_ventilar_ventanillas", "ventilar_ventanillas"),
        ("button.ebro_0001_encontrar_coche_luces", "encontrar_coche_luces"),
        ("button.ebro_0001_localizar_coche_gps", "localizar_coche_gps"),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_botones_de_comando(
    hass: HomeAssistant, entity_id: str, comando: str
) -> None:
    coordinator = _coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    send.assert_awaited_once_with(comando)


@pytest.mark.parametrize(
    ("entity_id", "metodo"),
    [
        ("button.ebro_0001_despertar_coche", "async_wake"),
        ("button.ebro_0001_actualizar_ubicacion", "async_probe"),
        ("button.ebro_0001_actualizar_estado_completo", "async_refresh_full_status"),
        ("button.ebro_0001_aplicar_carga_programada", "async_apply_scheduled_charge"),
    ],
)
async def test_botones_de_accion(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_core,
    platforms: list[str],
    telemetry: dict,
    entity_id: str,
    metodo: str,
) -> None:
    """`EbroActionButton` liga el método del coordinator EN LA CONSTRUCCIÓN.

    Por eso el parcheo tiene que ocurrir ANTES del setup: parchear la clase después no lo
    vería, porque el botón guarda ya el objeto-método enlazado.
    """
    from custom_components.ebro.vehicle.coordinator import EbroCoordinator

    mock_config_entry.add_to_hass(hass)
    with (
        patch("custom_components.ebro.PLATFORMS", platforms),
        patch.object(EbroCoordinator, metodo, AsyncMock(return_value=None)) as action,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        action.reset_mock()  # el setup puede haber llamado a alguno por su cuenta
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    action.assert_awaited_once_with()


@pytest.mark.usefixtures("init_integration")
async def test_un_comando_fallido_no_propaga(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """`async_press` se traga las excepciones a propósito: el resultado (también de error) ya
    lo publica el coordinator en `sensor.…_resultado_del_comando`.

    Consecuencia para quien escriba tests: hay que afirmar sobre el mock y el log, NUNCA con
    `pytest.raises`.
    """
    coordinator = _coordinator(hass)

    with (
        caplog.at_level(logging.ERROR),
        patch.object(
            coordinator,
            "async_send_command",
            AsyncMock(side_effect=RuntimeError("A00082 coche ocupado")),
        ) as send,
    ):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ebro_0001_localizar_coche_gps"},
            blocking=True,
        )

    send.assert_awaited_once()
    assert "localizar_coche_gps" in caplog.text


@pytest.mark.usefixtures("init_integration")
async def test_una_accion_fallida_no_propaga(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _coordinator(hass)

    with (
        caplog.at_level(logging.ERROR),
        patch.object(coordinator, "async_wake", AsyncMock(side_effect=OSError("red"))),
    ):
        # el botón ya tiene ligado el método original, así que se parchea el objeto ligado
        entidad = hass.data["entity_components"][BUTTON_DOMAIN].get_entity(
            "button.ebro_0001_despertar_coche"
        )
        entidad._action = coordinator.async_wake
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ebro_0001_despertar_coche"},
            blocking=True,
        )

    assert "Despertar coche" in caplog.text
