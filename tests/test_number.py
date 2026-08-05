"""Tests de la plataforma `number` (2 entidades de configuración local)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.const import DEFAULT_CHARGE_LIMIT_SOC

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)

CLIMA = "number.ebro_0001_duracion_de_la_climatizacion"
LIMITE = "number.ebro_0001_limite_de_carga"


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.NUMBER]


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
async def test_valores_por_defecto_disponibles_enseguida(hass: HomeAssistant) -> None:
    """`__init__` escribe ya el valor por defecto en el coordinator, antes de
    `async_added_to_hass`: si no, un comando de clima disparado en el primer segundo tras el
    arranque no encontraría `clima_duration`."""
    coordinator = _coordinator(hass)

    assert coordinator.preferences.clima_duration == 15
    assert coordinator.charge_limit_soc == DEFAULT_CHARGE_LIMIT_SOC


@pytest.mark.parametrize(
    ("entity_id", "atributo", "es_preferencia", "valor", "esperado"),
    [
        # `clima_duration` es una preferencia pura (solo se guarda hasta que el clima la usa);
        # `charge_limit_soc` es una propiedad del coordinator, con el ChargeLimiter detrás.
        (CLIMA, "clima_duration", True, 5, 5),
        (CLIMA, "clima_duration", True, 10, 10),
        (LIMITE, "charge_limit_soc", False, 90, 90),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_set_value_escribe_en_el_coordinator(
    hass: HomeAssistant, entity_id: str, atributo: str, es_preferencia: bool,
    valor: float, esperado
) -> None:
    """Estos deslizadores NO mandan nada al coche: guardan la preferencia que los demás
    controles leen en el momento del envío. Se afirma sobre el ATRIBUTO, no sobre un mock."""
    coordinator = _coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: valor},
            blocking=True,
        )

    send.assert_not_awaited()
    destino = coordinator.preferences if es_preferencia else coordinator
    assert getattr(destino, atributo) == esperado


@pytest.mark.usefixtures("init_integration")
async def test_los_enteros_se_guardan_como_int(hass: HomeAssistant) -> None:
    """`_push()` existe para que los body de comando no acaben con `"8.0"` donde la app usa
    enteros — el backend rechaza el formato decimal."""
    coordinator = _coordinator(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: CLIMA, ATTR_VALUE: 10.0},
        blocking=True,
    )

    assert coordinator.preferences.clima_duration == 10
    assert isinstance(coordinator.preferences.clima_duration, int)


@pytest.mark.usefixtures("init_integration")
async def test_el_clima_usa_la_duracion_recien_puesta(hass: HomeAssistant) -> None:
    """Comprobación de extremo a extremo del acoplamiento number → coordinator → climate."""
    coordinator = _coordinator(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: CLIMA, ATTR_VALUE: 5},
        blocking=True,
    )

    assert str(int(coordinator.preferences.clima_duration)) == "5"


@pytest.mark.usefixtures("init_integration")
async def test_rangos(hass: HomeAssistant) -> None:
    clima = hass.states.get(CLIMA).attributes
    assert (clima["min"], clima["max"], clima["step"]) == (5, 15, 5)

    limite = hass.states.get(LIMITE).attributes
    assert (limite["min"], limite["max"], limite["step"]) == (50, 100, 5)
