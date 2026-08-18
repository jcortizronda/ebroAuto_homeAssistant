"""Tests de la plataforma `lock` (1 entidad)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.lock import (
    DOMAIN as LOCK_DOMAIN,
    SERVICE_LOCK,
    SERVICE_UNLOCK,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)

ENTITY_ID = "lock.ebro_0001_cierre_centralizado"


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.LOCK]


@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_polaridad_invertida(hass: HomeAssistant) -> None:
    """`doorLock == 0` = BLOQUEADO. Es la única polaridad invertida de la integración, y la
    consecuencia de equivocarla sería dejar un coche abierto creyéndolo cerrado."""
    coordinator = get_coordinator(hass)

    assert hass.states.get(ENTITY_ID).state == "locked"

    coordinator._apply_update({"fields": {**coordinator.data["fields"], "doorLock": "1"}})
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "unlocked"


@pytest.mark.usefixtures("init_integration")
async def test_campo_ausente_queda_en_desconocido(hass: HomeAssistant) -> None:
    coordinator = get_coordinator(hass)

    campos = dict(coordinator.data["fields"])
    del campos["doorLock"]
    coordinator._apply_update({"fields": campos})
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_UNKNOWN


@pytest.mark.parametrize(
    ("servicio", "comando"),
    [(SERVICE_LOCK, "bloquear"), (SERVICE_UNLOCK, "desbloquear")],
)
@pytest.mark.usefixtures("init_integration")
async def test_servicios(hass: HomeAssistant, servicio: str, comando: str) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            LOCK_DOMAIN, servicio, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )

    send.assert_awaited_once_with(comando, None)


@pytest.mark.usefixtures("init_integration")
async def test_estado_optimista_inmediato(hass: HomeAssistant) -> None:
    """Un comando ACTÚA ya sobre el coche, pero el estado real solo vuelve por MQTT con el
    coche despierto — puede tardar horas. Mientras tanto se muestra el objetivo."""
    coordinator = get_coordinator(hass)

    with patch.object(coordinator, "async_send_command", AsyncMock(return_value="ok")):
        await hass.services.async_call(
            LOCK_DOMAIN, SERVICE_UNLOCK, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )

    # el campo real sigue diciendo "0" (bloqueado), pero la card ya muestra el objetivo
    assert coordinator.data["fields"]["doorLock"] == "0"
    assert hass.states.get(ENTITY_ID).state == "unlocked"


@pytest.mark.usefixtures("init_integration")
async def test_un_comando_fallido_anula_el_optimismo(hass: HomeAssistant) -> None:
    """Sin esto la card se quedaría bloqueada en un objetivo que nunca se ejecutó."""
    coordinator = get_coordinator(hass)

    with (
        patch.object(
            coordinator, "async_send_command", AsyncMock(side_effect=RuntimeError("A00084"))
        ),
        pytest.raises(HomeAssistantError, match="Comando «desbloquear» fallido"),
    ):
        await hass.services.async_call(
            LOCK_DOMAIN, SERVICE_UNLOCK, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )

    assert hass.states.get(ENTITY_ID).state == "locked"


@pytest.mark.usefixtures("init_integration")
async def test_la_sonda_actualiza_el_cierre_con_el_coche_dormido(hass: HomeAssistant) -> None:
    """Reproduce el fallo reportado en vivo: MQTT sin entregar un solo mensaje (`fields`
    vacío, `last_seen` nulo) mientras la sonda realtime traía `doorLock` en cada lectura. El
    cierre se quedaba en el último valor conocido y la app oficial sí mostraba el cambio.

    Que MQTT no entregue nada no es raro: la nube de Chery admite UNA sesión por cuenta, así
    que con la app oficial abierta el canal push de la integración se queda seco."""
    coordinator = get_coordinator(hass)

    coordinator._apply_update({"fields": {}, "awake": False, "realtime": {"doorLock": "0"}})
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == "locked"

    # el usuario abre el coche con la llave → la siguiente sonda lo trae
    coordinator._apply_update({"realtime": {"doorLock": "1"}})
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == "unlocked"
