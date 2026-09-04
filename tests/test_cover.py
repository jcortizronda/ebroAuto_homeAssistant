"""Tests de la plataforma `cover` (3 entidades: maletero, ventanillas, techo)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.cover import (
    DOMAIN as COVER_DOMAIN,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    CoverEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_CLOSED,
    STATE_OPEN,
    STATE_UNKNOWN,
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

MALETERO = "cover.ebro_0001_maletero"
VENTANILLAS = "cover.ebro_0001_ventanillas"
TECHO = "cover.ebro_0001_techo_solar"
VENTANAS = (
    "frontLeftWindowState",
    "frontRightWindowState",
    "backLeftWindowState",
    "backRightWindowState",
)


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.COVER]


@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_solo_abrir_y_cerrar(hass: HomeAssistant) -> None:
    """Sin STOP ni posición: el coche solo acepta abrir/cerrar completo."""
    features = hass.states.get(MALETERO).attributes[ATTR_SUPPORTED_FEATURES]
    assert features == CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE


@pytest.mark.usefixtures("init_integration")
async def test_ventanillas_agrega_las_cuatro(hass: HomeAssistant) -> None:
    """Basta UNA ventanilla abierta para que la cover agregada lea «abierta»."""
    coordinator = get_coordinator(hass)

    assert hass.states.get(VENTANILLAS).state == STATE_CLOSED

    coordinator._apply_update(
        {"fields": {**coordinator.data["fields"], "backRightWindowState": "1"}}
    )
    await hass.async_block_till_done()

    assert hass.states.get(VENTANILLAS).state == STATE_OPEN


@pytest.mark.usefixtures("init_integration")
async def test_ventanillas_una_sola_conocida_y_abierta(hass: HomeAssistant) -> None:
    """Con tres campos AUSENTES y uno abierto sigue leyendo «abierta»: la agregación ignora
    los None en vez de tratarlos como cerrado, que sería un falso negativo."""
    coordinator = get_coordinator(hass)

    campos = {k: v for k, v in coordinator.data["fields"].items() if k not in VENTANAS}
    campos["backLeftWindowState"] = "1"
    coordinator._apply_update({"fields": campos})
    await hass.async_block_till_done()

    assert hass.states.get(VENTANILLAS).state == STATE_OPEN


@pytest.mark.usefixtures("init_integration")
async def test_ventanillas_sin_ningun_campo_es_desconocido(hass: HomeAssistant) -> None:
    """Si NINGUNA de las cuatro se conoce → unknown, no «cerrada»."""
    coordinator = get_coordinator(hass)

    campos = {k: v for k, v in coordinator.data["fields"].items() if k not in VENTANAS}
    coordinator._apply_update({"fields": campos})
    await hass.async_block_till_done()

    assert hass.states.get(VENTANILLAS).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("entity_id", "servicio", "comando"),
    [
        (MALETERO, SERVICE_OPEN_COVER, "maletero_abrir"),
        (MALETERO, SERVICE_CLOSE_COVER, "maletero_cerrar"),
        (VENTANILLAS, SERVICE_OPEN_COVER, "ventanillas_abrir"),
        (VENTANILLAS, SERVICE_CLOSE_COVER, "ventanillas_cerrar"),
        (TECHO, SERVICE_OPEN_COVER, "techo_abrir"),
        (TECHO, SERVICE_CLOSE_COVER, "techo_cerrar"),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_servicios(
    hass: HomeAssistant, entity_id: str, servicio: str, comando: str
) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            COVER_DOMAIN, servicio, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    send.assert_awaited_once_with(comando, None)


@pytest.mark.usefixtures("init_integration")
async def test_optimismo_al_abrir(hass: HomeAssistant) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(coordinator, "async_send_command", AsyncMock(return_value="ok")):
        await hass.services.async_call(
            COVER_DOMAIN, SERVICE_OPEN_COVER, {ATTR_ENTITY_ID: TECHO}, blocking=True
        )

    assert coordinator.data["fields"]["sunroofState"] == "0"  # el coche aún no ha hablado
    assert hass.states.get(TECHO).state == STATE_OPEN


@pytest.mark.usefixtures("init_integration")
async def test_la_sonda_actualiza_el_maletero_con_el_coche_dormido(hass: HomeAssistant) -> None:
    """Mismo fallo que en el cierre: sin push MQTT, el maletero solo puede saberse por la
    sonda. Aquí importa además que la lectura es por CLAVE — la cover agrega varios campos y
    antes leía el bloque `fields` entero de una vez."""
    coordinator = get_coordinator(hass)

    coordinator._apply_update({"fields": {}, "awake": False, "realtime": {"trunkDoor": "0"}})
    await hass.async_block_till_done()
    assert hass.states.get(MALETERO).state == STATE_CLOSED

    coordinator._apply_update({"realtime": {"trunkDoor": "1"}})
    await hass.async_block_till_done()

    assert hass.states.get(MALETERO).state == STATE_OPEN


@pytest.mark.usefixtures("init_integration")
async def test_el_maletero_no_se_reabre_al_dormirse_el_coche(hass: HomeAssistant) -> None:
    """La secuencia REAL del historial del usuario (04/09/2026):

        12:21:50  cerrado   ← último push MQTT: cerrado de verdad
        12:26:51  despierto → off   (vence la ventana, 300 s después)
        12:26:51  ABIERTO   ← 2 ms más tarde, por la instantánea vieja de la nube

    La instantánea conservaba `trunkDoor=1` de cuando sí estaba abierto, y al dormirse el coche
    pasaba a mandar ella. Ahora manda el push siempre."""
    coordinator = get_coordinator(hass)

    coordinator._apply_update({
        "fields": {"trunkDoor": "0"},          # el coche empujó: cerrado
        "realtime": {"trunkDoor": "1"},        # la nube guarda la foto de cuando estaba abierto
        "awake": True,
    })
    await hass.async_block_till_done()
    assert hass.states.get(MALETERO).state == STATE_CLOSED

    coordinator._apply_update({"awake": False})   # vence la ventana de despierto
    await hass.async_block_till_done()

    assert hass.states.get(MALETERO).state == STATE_CLOSED
