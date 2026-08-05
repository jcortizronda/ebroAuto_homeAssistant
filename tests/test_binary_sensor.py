"""Tests de la plataforma `binary_sensor` (26 entidades)."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.BINARY_SENSOR]


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
    assert len(entidades) == 26


@pytest.mark.usefixtures("init_integration")
async def test_coche_despierto_no_sale_de_coordinator_data(hass: HomeAssistant) -> None:
    """«Coche despierto» es el único binary_sensor que NO lee `coordinator.data`.

    Lee `coordinator.is_awake`, que compara el reloj contra el último mensaje recibido.
    Se pregunta el estado REAL en vez del flag memorizado para que el sensor sea correcto
    también en la ventana entre el vencimiento y el timer que refresca el flag — y por eso
    su snapshot exige reloj congelado.
    """
    coordinator = get_coordinator(hass)

    assert hass.states.get("binary_sensor.ebro_0001_coche_despierto").state == STATE_ON

    # sin mensajes recientes vence, aunque `data["awake"]` siga diciendo True
    coordinator.state.seed(last_msg_ts=0.0)
    coordinator._apply_update({})
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.ebro_0001_coche_despierto").state == STATE_OFF


@pytest.mark.usefixtures("init_integration")
async def test_conexion_fija_su_entity_id_a_mano(
    entity_registry: er.EntityRegistry,
) -> None:
    """`EbroOnline` se salta la derivación de la clase base y escribe su `entity_id` directo:
    conviene dejarlo fijado, porque es el único que no sigue el mismo camino que sus 25
    hermanos y un cambio ahí pasaría desapercibido."""
    entry = entity_registry.async_get("binary_sensor.ebro_0001_conexion")
    assert entry is not None
    assert entry.translation_key == "conexion"


@pytest.mark.usefixtures("init_integration")
async def test_campo_ausente_queda_en_desconocido(hass: HomeAssistant) -> None:
    """`field_on` devuelve None para un campo ausente → emerge el valor restaurado (aquí
    ninguno) en vez de un falso «off», que parecería una puerta cerrada de verdad."""
    coordinator = get_coordinator(hass)

    campos = dict(coordinator.data["fields"])
    del campos["frontLeftDoor"]
    coordinator._apply_update({"fields": campos})
    await hass.async_block_till_done()

    assert (
        hass.states.get("binary_sensor.ebro_0001_puerta_del_izquierda").state
        == STATE_UNKNOWN
    )


@pytest.mark.usefixtures("init_integration")
async def test_cero_con_decimales_es_off(hass: HomeAssistant) -> None:
    """`"0.0"` debe leerse numéricamente: textualmente sería una cadena no vacía → «on»."""
    coordinator = get_coordinator(hass)

    coordinator._apply_update(
        {"fields": {**coordinator.data["fields"], "frontLeftDoor": "0.0"}}
    )
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.ebro_0001_puerta_del_izquierda").state == STATE_OFF


@pytest.mark.usefixtures("init_integration")
async def test_avisos_realtime(hass: HomeAssistant) -> None:
    """Los `*Call` del canal realtime: 0 = todo correcto."""
    assert (
        hass.states.get("binary_sensor.ebro_0001_aviso_neumatico_del_izquierdo").state
        == STATE_OFF
    )
    assert hass.states.get("binary_sensor.ebro_0001_alta_tension_activa").state == STATE_ON


@pytest.mark.usefixtures("init_integration")
async def test_sesion(hass: HomeAssistant) -> None:
    assert hass.states.get("binary_sensor.ebro_0001_sesion").state == STATE_ON
