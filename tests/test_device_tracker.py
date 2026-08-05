"""Tests de la plataforma `device_tracker` (1 entidad)."""

from __future__ import annotations

from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)

ENTITY_ID = "device_tracker.ebro_0001_ubicacion"


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.DEVICE_TRACKER]


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
async def test_posicion_desde_el_push_1301(hass: HomeAssistant) -> None:
    atributos = hass.states.get(ENTITY_ID).attributes
    assert atributos[ATTR_LATITUDE] == 40.416775
    assert atributos[ATTR_LONGITUDE] == -3.703790


@pytest.mark.usefixtures("init_integration")
async def test_acepta_los_nombres_largos(hass: HomeAssistant) -> None:
    """Según el endpoint, el backend manda `lat`/`lon` o `latitude`/`longitude`."""
    coordinator = _coordinator(hass)
    coordinator._apply_update({"position": {"latitude": "41.0", "longitude": "2.0"}})
    await hass.async_block_till_done()

    atributos = hass.states.get(ENTITY_ID).attributes
    assert (atributos[ATTR_LATITUDE], atributos[ATTR_LONGITUDE]) == (41.0, 2.0)


@pytest.mark.usefixtures("init_integration")
async def test_sin_posicion_no_hay_coordenadas(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._apply_update({"position": None})
    await hass.async_block_till_done()

    atributos = hass.states.get(ENTITY_ID).attributes
    assert ATTR_LATITUDE not in atributos


@pytest.mark.usefixtures("init_integration")
async def test_valores_ilegibles_se_ignoran(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._apply_update({"position": {"lat": "no-un-numero", "lon": None}})
    await hass.async_block_till_done()

    atributos = hass.states.get(ENTITY_ID).attributes
    assert ATTR_LATITUDE not in atributos


@pytest.mark.usefixtures("init_integration")
async def test_lat_cero_cae_en_el_alias(hass: HomeAssistant) -> None:
    """`pos.get("lat") or pos.get("latitude")` usa `or`, no una comprobación de None: un
    `lat` de 0.0 es falsy y cae al alias.

    Sin importancia práctica para un coche en España, pero es un comportamiento real del
    código y conviene tenerlo documentado por si alguien lo lee como si fuera un `is None`.
    """
    coordinator = _coordinator(hass)
    coordinator._apply_update({"position": {"lat": 0.0, "latitude": "41.0", "lon": "2.0"}})
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).attributes[ATTR_LATITUDE] == 41.0


@pytest.mark.usefixtures("init_integration")
async def test_source_type_es_gps(hass: HomeAssistant) -> None:
    assert hass.states.get(ENTITY_ID).attributes["source_type"] == "gps"
