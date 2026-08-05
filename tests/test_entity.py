"""Tests de `entity.py` — la base común de las 93 entidades.

Aquí viven tres cosas que, si se rompen, se rompen a la vez en todas las plataformas: la
coerción tri-estado `field_on`, la derivación del `entity_id` y el estado optimista.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN, SERVICE_UNLOCK
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ebro.const import DEFAULT_VEHICLE_NAME, DOMAIN
from custom_components.ebro.entity import field_on

from .conftest import get_coordinator
from .const import ENTRY_DATA, ENTRY_OPTIONS, FROZEN_TIME, TEST_VIN, TEST_VIN4

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


def _entidad_de_prueba(name: str, unique_suffix: str, entity_id_format: str):
    """Una `EbroEntity` suelta, sin `hass` ni plataforma.

    `EbroEntity.__init__` solo necesita el VIN del coordinator para componer el `entity_id`, así
    que un doble mínimo basta y el test queda en microsegundos."""
    from unittest.mock import MagicMock

    from custom_components.ebro.entity import EbroEntity

    coordinator = MagicMock()
    coordinator.vin = TEST_VIN
    return EbroEntity(coordinator, name, unique_suffix, entity_id_format=entity_id_format)


# ───────────────────────── field_on ─────────────────────────


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        # AUSENTE → None, para que emerja el valor restaurado en vez de un falso «off».
        # Un falso False en una puerta se leería como «cerrada», que es lo peor que puede pasar.
        (None, None),
        ("None", None),
        ("", None),
        ("   ", None),
        # comparación NUMÉRICA: alinea "0.0" entre binary_sensor / lock / switch / cover
        ("0", False),
        ("0.0", False),
        (0, False),
        (0.0, False),
        ("1", True),
        ("1.0", True),
        ("2", True),
        (1, True),
        ("-1", True),
        # respaldo TEXTUAL para los booleanos
        ("false", False),
        ("FALSE", False),
        ("off", False),
        ("no", False),
        ("true", True),
        ("on", True),
        ("cualquier-cosa", True),
    ],
)
def test_field_on(valor, esperado) -> None:
    """La coerción tri-estado compartida por binary_sensor, switch, cover, lock y climate: un
    solo test cubre la polaridad de las cinco plataformas."""
    assert field_on(valor) is esperado


# ───────────────────────── identidad de la entidad ─────────────────────────


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.LOCK]


@pytest.mark.usefixtures("init_integration")
async def test_unique_id_lleva_el_vin(entity_registry: er.EntityRegistry) -> None:
    """El unique_id es `<VIN>_<sufijo>`: es lo que ata la entidad a su histórico, así que
    cambiarlo se lo cargaría."""
    entry = entity_registry.async_get("lock.ebro_0001_cierre_centralizado")

    assert entry.unique_id == f"{TEST_VIN}_lock"


@pytest.mark.usefixtures("init_integration")
async def test_entity_id_derivado_del_nombre_castellano(
    entity_registry: er.EntityRegistry,
) -> None:
    """El `entity_id` NO lo deriva HA: lo fija `entity.py` como
    `<plataforma>.ebro_<4 últimas del VIN>_<descriptor en castellano de es.json>`.

    Dos consecuencias que conviene tener por escrito: las 4 cifras del VIN hacen el id único
    con varios vehículos en el mismo HA, y **editar un nombre en `translations/es.json`
    renombra entity_ids ya en uso en los dashboards de los usuarios** (ver
    `examples/vehicle-status-card.yaml`, que los referencia por nombre).
    """
    entry = entity_registry.async_get("lock.ebro_0001_cierre_centralizado")

    assert entry is not None
    assert entry.entity_id.startswith(f"lock.{DOMAIN}_{TEST_VIN4}_")
    assert entry.translation_key == "cierre_centralizado"


@pytest.mark.usefixtures("init_integration")
async def test_dispositivo(device_registry: dr.DeviceRegistry) -> None:
    device = device_registry.async_get_device(identifiers={(DOMAIN, TEST_VIN)})

    assert device.name == "Ebro S900"
    assert device.manufacturer == "Ebro"
    assert device.model == "S900"


async def test_dispositivo_sin_identidad_conocida(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    mock_core,
    platforms: list[str],
) -> None:
    """Antes de que el relleno de identidad responda, el dispositivo usa el nombre genérico —
    y se identifica igualmente por VIN, así que renombrarlo después no toca el histórico."""
    datos = {
        k: v
        for k, v in ENTRY_DATA.items()
        if k not in ("vehicle_name", "vehicle_model", "vehicle_brand")
    }
    entry = MockConfigEntry(
        domain=DOMAIN, data=datos, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    )
    entry.add_to_hass(hass)

    with patch("custom_components.ebro.PLATFORMS", platforms):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    device = device_registry.async_get_device(identifiers={(DOMAIN, TEST_VIN)})

    assert device.name == DEFAULT_VEHICLE_NAME
    assert device.manufacturer == "Ebro"
    assert device.model is None


def test_el_entity_id_no_depende_de_las_traducciones() -> None:
    """El descriptor del `entity_id` es la `translation_key`, no el nombre traducido.

    Antes se leía `translations/es.json` en runtime para sacar de ahí el descriptor. Era un
    rodeo: `name` es literalmente "Ebro " + el nombre castellano, así que `slugify(nombre_es)`
    y la `translation_key` daban siempre lo mismo — verificado contra las 93 entidades de los
    snapshots. Quitarlo elimina una lectura de fichero en el arranque, un `lru_cache` global de
    proceso y la fixture que había que acordarse de vaciar entre tests.

    Este test fija la propiedad: un `es.json` inservible no puede mover ningún `entity_id`.
    """
    from unittest.mock import patch as _patch

    with _patch("builtins.open", side_effect=OSError("es.json ilegible")):
        entidad = _entidad_de_prueba("Ebro Batería", "battery", "sensor.{}")

    assert entidad.entity_id == "sensor.ebro_0001_bateria"
    assert entidad._attr_translation_key == "bateria"


# ───────────────────────── estado optimista ─────────────────────────


@pytest.mark.usefixtures("init_integration")
async def test_el_optimismo_se_ancla_en_last_seen(hass: HomeAssistant) -> None:
    """Un comando ACTÚA ya sobre el coche, pero el estado real solo vuelve por MQTT y con el
    coche despierto — puede quedarse quieto durante horas. El objetivo se muestra de
    inmediato y se mantiene hasta que llega un mensaje NUEVO del coche.
    """
    coordinator = get_coordinator(hass)
    entidad = hass.data["entity_components"][LOCK_DOMAIN].get_entity(
        "lock.ebro_0001_cierre_centralizado"
    )

    with patch.object(coordinator, "async_send_command", AsyncMock(return_value="ok")):
        await hass.services.async_call(
            LOCK_DOMAIN,
            SERVICE_UNLOCK,
            {ATTR_ENTITY_ID: "lock.ebro_0001_cierre_centralizado"},
            blocking=True,
        )

    assert entidad._opt_value is False
    assert entidad._opt_anchor == coordinator.data["last_seen"]

    # telemetría nueva (last_seen avanza) → la verdad vuelve a ser el campo real
    coordinator._apply_update(
        {"last_seen": coordinator.data["last_seen"].replace(second=42)}
    )
    await hass.async_block_till_done()

    assert entidad._opt_value is None
    assert hass.states.get("lock.ebro_0001_cierre_centralizado").state == "locked"


@pytest.mark.usefixtures("init_integration")
async def test_una_actualizacion_sin_datos_nuevos_conserva_el_optimismo(
    hass: HomeAssistant,
) -> None:
    """El coordinator notifica también por cosas que no son telemetría del coche (estado de
    sesión, resultado de sonda). Si eso anulara el optimismo, la card volvería al estado
    viejo a los pocos segundos de cada comando."""
    coordinator = get_coordinator(hass)
    entidad = hass.data["entity_components"][LOCK_DOMAIN].get_entity(
        "lock.ebro_0001_cierre_centralizado"
    )

    with patch.object(coordinator, "async_send_command", AsyncMock(return_value="ok")):
        await hass.services.async_call(
            LOCK_DOMAIN,
            SERVICE_UNLOCK,
            {ATTR_ENTITY_ID: "lock.ebro_0001_cierre_centralizado"},
            blocking=True,
        )

    coordinator._apply_update({"session_detail": "otra cosa"})  # last_seen NO se mueve
    await hass.async_block_till_done()

    assert entidad._opt_value is False
    assert hass.states.get("lock.ebro_0001_cierre_centralizado").state == "unlocked"
