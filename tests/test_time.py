"""Tests de la plataforma `time` (2 entidades de configuración local)."""

from __future__ import annotations

from datetime import time

from homeassistant.components import persistent_notification
from homeassistant.components.time import (
    ATTR_TIME,
    DOMAIN as TIME_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.const import CHARGE_MIN_DURATION_MIN, DOMAIN

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)

INICIO = "time.ebro_0001_hora_de_inicio_de_la_carga"
DURACION = "time.ebro_0001_duracion_de_la_carga"


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.TIME]


def _coordinator(hass: HomeAssistant):
    return get_coordinator(hass)


def _avisos(hass: HomeAssistant) -> dict:
    """Notificaciones persistentes vivas.

    Desde hace varias versiones las notificaciones ya NO son entidades de estado
    (`persistent_notification.<id>`): viven en `hass.data`. Buscarlas en `hass.states` da
    siempre None y el test pasaría sin comprobar nada.
    """
    return persistent_notification._async_get_or_create_notifications(hass)


@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_valores_por_defecto(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    assert coordinator.preferences.charge_start_minutes == 8 * 60
    assert coordinator.preferences.charge_duration_minutes == 6 * 60


@pytest.mark.usefixtures("init_integration")
async def test_set_value_guarda_minutos_desde_medianoche(hass: HomeAssistant) -> None:
    """El coche razona en minutos (verificado en vivo: startTime 465 = 07:45); el selector
    HH:MM es solo la presentación."""
    coordinator = _coordinator(hass)

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: INICIO, ATTR_TIME: time(7, 45)},
        blocking=True,
    )

    assert coordinator.preferences.charge_start_minutes == 465
    assert hass.states.get(INICIO).state == "07:45:00"


@pytest.mark.usefixtures("init_integration")
async def test_duracion_por_debajo_del_minimo_se_acota_y_avisa(
    hass: HomeAssistant,
) -> None:
    """El coche rechaza con code 89 una duración menor de 1 h. Se acota en silencio *y* se
    avisa: sin la notificación el usuario vería un valor distinto del que puso, sin
    explicación.
    """
    coordinator = _coordinator(hass)

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: DURACION, ATTR_TIME: time(0, 30)},
        blocking=True,
    )

    assert coordinator.preferences.charge_duration_minutes == CHARGE_MIN_DURATION_MIN
    assert hass.states.get(DURACION).state == "01:00:00"

    aviso = _avisos(hass).get(f"{DOMAIN}_charge_duration_min")
    assert aviso is not None
    assert "1 h" in aviso["message"]
    assert "01:00" in aviso["message"]


@pytest.mark.usefixtures("init_integration")
async def test_duracion_por_encima_del_minimo_no_avisa(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: DURACION, ATTR_TIME: time(2, 15)},
        blocking=True,
    )

    assert coordinator.preferences.charge_duration_minutes == 135
    assert f"{DOMAIN}_charge_duration_min" not in _avisos(hass)


@pytest.mark.usefixtures("init_integration")
async def test_la_hora_de_inicio_no_tiene_minimo(hass: HomeAssistant) -> None:
    """Solo la DURACIÓN está acotada; medianoche es una hora de inicio perfectamente válida."""
    coordinator = _coordinator(hass)

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: INICIO, ATTR_TIME: time(0, 0)},
        blocking=True,
    )

    assert coordinator.preferences.charge_start_minutes == 0
    assert f"{DOMAIN}_charge_duration_min" not in _avisos(hass)


@pytest.mark.usefixtures("init_integration")
async def test_los_segundos_se_descartan(hass: HomeAssistant) -> None:
    """El coche razona en minutos: los segundos no aportan nada."""
    coordinator = _coordinator(hass)

    await hass.services.async_call(
        TIME_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: INICIO, ATTR_TIME: time(7, 45, 33)},
        blocking=True,
    )

    assert coordinator.preferences.charge_start_minutes == 465
