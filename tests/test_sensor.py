"""Tests de la plataforma `sensor` (35 entidades)."""

from __future__ import annotations

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.const import DOMAIN

from .conftest import get_coordinator
from .const import FROZEN_TIME, TEST_VIN

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.SENSOR]


@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Snapshot de las entidades sensor con el escenario «cargando»."""
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)

    # todas las entidades cuelgan del único dispositivo (el vehículo, identificado por VIN)
    device_entry = device_registry.async_get_device(identifiers={(DOMAIN, TEST_VIN)})
    assert device_entry
    for entity_entry in er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    ):
        assert entity_entry.device_id == device_entry.id


async def test_entities_arranque_en_frio(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
    telemetry_empty: dict,
    mock_core,
    platforms: list[str],
) -> None:
    """Snapshot sin ningún dato del coche.

    Cada consumidor hace `data.get("realtime") or {}`; esa rama no se ejercitaría nunca con
    el escenario poblado, y es exactamente el estado en el que arranca una instalación nueva.
    """
    from unittest.mock import patch

    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.ebro.PLATFORMS", platforms):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_numero_de_entidades(
    entity_registry: er.EntityRegistry, mock_config_entry: MockConfigEntry
) -> None:
    """Guarda contra una tabla que pierda (o duplique) filas sin que nadie lo note."""
    entidades = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert len(entidades) == 36


@pytest.mark.usefixtures("init_integration")
async def test_autonomia_total_es_electrica_mas_gasolina(hass: HomeAssistant) -> None:
    """Campo CALCULADO, no leído: 60 km eléctricos + 215 km de gasolina = 275 km."""
    assert hass.states.get("sensor.ebro_0001_autonomia_total").state == "275.0"


@pytest.mark.usefixtures("init_integration")
async def test_presion_de_neumaticos_convertida_a_bar(hass: HomeAssistant) -> None:
    """El coche manda kPa; la app (y estos sensores) muestran bar → `scale=0.01`."""
    assert hass.states.get("sensor.ebro_0001_presion_neumatico_del_izquierdo").state == "2.92"


@pytest.mark.usefixtures("init_integration")
async def test_tiempo_de_carga_formateado(hass: HomeAssistant) -> None:
    """`remainChargeTime` llega en minutos: 90 → «1 h 30 min»."""
    assert hass.states.get("sensor.ebro_0001_tiempo_de_carga_restante").state == "1 h 30 min"


@pytest.mark.usefixtures("init_integration")
async def test_estado_de_carga_traducido(hass: HomeAssistant) -> None:
    """Los campos enum pasan por su `vmap`."""
    assert hass.states.get("sensor.ebro_0001_estado_de_carga").state == "Cargando"


@pytest.mark.usefixtures("init_integration")
async def test_solo_queda_un_sensor_de_campo_5a02(hass: HomeAssistant) -> None:
    """`SENSORS` tiene 6 filas con `comp == "sensor"`, pero 5 están en
    `FIELDS_AS_RICH_ENTITY` (el cierre es un `lock`, los asientos traseros son `switch`) →
    el único `EbroFieldSensor` que sobrevive es el estado del techo.

    Es una consecuencia sutil de dos tablas que hay que mantener alineadas a mano: si alguien
    saca una clave de `FIELDS_AS_RICH_ENTITY` sin quitarla de `SENSORS`, aparece una entidad
    duplicada del mismo campo.
    """
    from custom_components.ebro.const import FIELDS_AS_RICH_ENTITY
    from custom_components.ebro.vehicle.telemetry import SENSORS

    de_campo = [
        s for s in SENSORS if s["comp"] == "sensor" and s["key"] not in FIELDS_AS_RICH_ENTITY
    ]
    assert [s["key"] for s in de_campo] == ["sunroofMoveState"]

    # `kind == "value"` → valor en crudo, sin traducir
    assert hass.states.get("sensor.ebro_0001_estado_del_techo").state == "8"


@pytest.mark.usefixtures("init_integration")
async def test_vmap_desconocido_no_inventa_valor(hass: HomeAssistant) -> None:
    """Un código fuera del mapa queda legible como «Desconocido (N)»: ninguna información
    perdida, ningún valor inventado."""
    coordinator = get_coordinator(hass)
    coordinator._apply_update(
        {"realtime": {**coordinator.data["realtime"], "chargeState": "7"}}
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.ebro_0001_estado_de_carga").state == "Desconocido (7)"


@pytest.mark.usefixtures("init_integration")
async def test_marcadores_invalidos_dejan_el_sensor_en_desconocido(
    hass: HomeAssistant,
) -> None:
    """Con la alta tensión apagada el coche manda 0 V / −1000 A: son marcadores «sin
    lectura», no medidas reales. Devolver None deja emerger el último valor conocido en vez
    de dibujar una caída a cero en el histórico.
    """
    coordinator = get_coordinator(hass)
    coordinator._apply_update(
        {
            "realtime": {
                **coordinator.data["realtime"],
                "totalVoltage": "0.0",
                "totalCurrent": "-1000.0",
                "avgHkPowerKwh50km": "-100.0",
            }
        }
    )
    await hass.async_block_till_done()

    # sin valor restaurado previo → unknown (nunca 0 ni −1000)
    for entity_id in (
        "sensor.ebro_0001_tension_bateria_at",
        "sensor.ebro_0001_corriente_bateria_at",
        "sensor.ebro_0001_consumo_medio_electrico",
    ):
        assert hass.states.get(entity_id).state == "unknown", entity_id


@pytest.mark.usefixtures("init_integration")
async def test_bateria_a_cero_es_marcador_no_carga_real(hass: HomeAssistant) -> None:
    """`dumpEnergy = 0` es «alta tensión apagada», no un 0 % real: la app oficial hace lo
    mismo y conserva el último SOC conocido."""
    coordinator = get_coordinator(hass)

    assert hass.states.get("sensor.ebro_0001_bateria").state == "64.5"

    coordinator._apply_update(
        {"realtime": {**coordinator.data["realtime"], "dumpEnergy": "0"}}
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.ebro_0001_bateria").state == "unknown"


@pytest.mark.usefixtures("init_integration")
async def test_los_sensores_de_diagnostico_no_se_restauran(hass: HomeAssistant) -> None:
    """Un resultado viejo tras un reinicio parecería la última acción recién ejecutada."""
    coordinator = get_coordinator(hass)

    assert hass.states.get("sensor.ebro_0001_resultado_del_comando").state == "confirmado ✅"

    coordinator._apply_update({"cmd_status": None})
    await hass.async_block_till_done()

    assert hass.states.get("sensor.ebro_0001_resultado_del_comando").state == "unknown"


@pytest.mark.usefixtures("init_integration")
async def test_la_programacion_del_coche_se_ensena_tal_cual(hass: HomeAssistant) -> None:
    """El caso que lo motivó: cambias la programación desde la app o desde el coche, y en Home
    Assistant no había forma de enterarse — las entidades de hora y duración son la preferencia
    de lo que se ENVIARÁ, no lo que el vehículo tiene puesto."""
    from custom_components.ebro.vehicle.charging import ChargeSchedule

    coordinator = get_coordinator(hass)
    coordinator._apply_update(
        {"charge_schedule": ChargeSchedule(
            enabled=True, start_minutes=465, duration_minutes=540, days=(1, 2, 3))}
    )
    await hass.async_block_till_done()

    estado = hass.states.get("sensor.ebro_0001_carga_programada_en_el_coche")

    assert estado.state == "07:45"
    assert estado.attributes["duracion_min"] == 540
    assert estado.attributes["dias"] == [1, 2, 3]


@pytest.mark.usefixtures("init_integration")
async def test_una_programacion_apagada_lo_dice(hass: HomeAssistant) -> None:
    from custom_components.ebro.vehicle.charging import ChargeSchedule

    coordinator = get_coordinator(hass)
    coordinator._apply_update(
        {"charge_schedule": ChargeSchedule(
            enabled=False, start_minutes=465, duration_minutes=540)}
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.ebro_0001_carga_programada_en_el_coche").state == "Desactivada"


def _sensor_frescura(hass: HomeAssistant):
    return hass.data["entity_components"][SENSOR_DOMAIN].get_entity(
        "sensor.ebro_0001_datos_del_coche_actualizados"
    )


@pytest.mark.usefixtures("init_integration")
async def test_un_timestamp_no_retrocede_tras_recargar(hass: HomeAssistant) -> None:
    """La secuencia REAL del historial del usuario (4 de septiembre de 2026).

    Al recargar la integración, el primer frame se fecha con la marca que declara el coche. Si
    esa marca es más vieja que lo ya registrado —y lo era, porque venía de `resultTime`, que se
    queda congelado durante días— el sensor daba un salto atrás."""
    from datetime import UTC, datetime

    entidad = _sensor_frescura(hass)
    entidad._restored = entidad._max_seen = datetime(2026, 9, 4, 5, 41, 52, tzinfo=UTC)
    coordinator = get_coordinator(hass)

    coordinator._apply_update({"car_data_ts": datetime(2026, 9, 4, 4, 0, 44, tzinfo=UTC)})
    await hass.async_block_till_done()

    assert entidad.native_value == datetime(2026, 9, 4, 5, 41, 52, tzinfo=UTC)

    # ...pero un valor MÁS NUEVO sí manda: es un trinquete, no un congelador
    coordinator._apply_update({"car_data_ts": datetime(2026, 9, 4, 6, 15, tzinfo=UTC)})
    await hass.async_block_till_done()

    assert entidad.native_value == datetime(2026, 9, 4, 6, 15, tzinfo=UTC)


@pytest.mark.usefixtures("init_integration")
async def test_el_trinquete_no_depende_del_valor_restaurado(hass: HomeAssistant) -> None:
    """Sin nada restaurado —instalación nueva— la garantía tiene que seguir en pie: se guarda
    el máximo VISTO, no solo el de arranque."""
    from datetime import UTC, datetime

    entidad = _sensor_frescura(hass)
    entidad._restored = entidad._max_seen = None
    coordinator = get_coordinator(hass)

    coordinator._apply_update({"car_data_ts": datetime(2026, 9, 4, 6, 15, tzinfo=UTC)})
    await hass.async_block_till_done()
    assert entidad.native_value == datetime(2026, 9, 4, 6, 15, tzinfo=UTC)

    coordinator._apply_update({"car_data_ts": datetime(2026, 9, 1, 19, 34, tzinfo=UTC)})
    await hass.async_block_till_done()

    assert entidad.native_value == datetime(2026, 9, 4, 6, 15, tzinfo=UTC)
