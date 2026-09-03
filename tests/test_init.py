"""Tests de `__init__.py` — arranque, descarga y recarga por opciones."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.const import CONF_POLL_PARKED, CONF_VEHICLE_NAME, DOMAIN

from .const import FROZEN_TIME, TEST_VIN

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


@pytest.mark.usefixtures("init_integration")
async def test_setup_ok(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_config_entry.runtime_data is not None


@pytest.mark.usefixtures("init_integration")
async def test_todas_las_entidades(
    entity_registry: er.EntityRegistry, mock_config_entry: MockConfigEntry
) -> None:
    """Con las 10 plataformas cargadas deben nacer 94 entidades.

    Es el guardarraíl más barato contra una tabla que pierda filas: cada plataforma tiene su
    propio recuento, pero este total pilla también una plataforma que no llegue a cargar.
    """
    entidades = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert len(entidades) == 94


@pytest.mark.usefixtures("init_integration")
async def test_dispositivo(
    device_registry: dr.DeviceRegistry, snapshot: SnapshotAssertion
) -> None:
    """Un config entry = un vehículo = un dispositivo, identificado por VIN.

    El VIN como identificador es lo que permite renombrar el dispositivo sin tocar ni los
    entity_id ni el histórico.
    """
    device_entry = device_registry.async_get_device(identifiers={(DOMAIN, TEST_VIN)})
    assert device_entry
    assert device_entry == snapshot


async def test_certificados_ausentes_es_not_ready(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_core
) -> None:
    """Sin los certificados mutual-TLS no se puede conectar el MQTT: HA debe reintentar, no
    dar la entrada por rota."""
    mock_core["provision_certs"].return_value = (False, "faltan ca.pem, client.pem")
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert getattr(mock_config_entry, "runtime_data", None) is None


async def test_fallo_en_el_arranque_limpia_los_recursos(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_core
) -> None:
    """Si CUALQUIER paso posterior falla hay que dejar el sistema como estaba.

    Sin esta limpieza quedarían el hilo de paho y los timers de keepalive/sondeo huérfanos,
    contactando con la nube de una integración que ni siquiera está cargada.
    """
    from custom_components.ebro.vehicle.coordinator import EbroCoordinator

    mock_config_entry.add_to_hass(hass)

    with (
        patch.object(
            EbroCoordinator, "async_start", AsyncMock(side_effect=RuntimeError("mqtt ko"))
        ),
        patch.object(EbroCoordinator, "async_stop") as stop,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is not ConfigEntryState.LOADED
    stop.assert_called_once()


@pytest.mark.usefixtures("init_integration")
async def test_unload(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    coordinator = mock_config_entry.runtime_data

    # `wraps`, no un mock a secas: `async_stop` es quien cierra el TimerRegistry, y
    # sustituirlo dejaría el keepalive de 900 s vivo (pytest lo detecta como timer huérfano).
    with patch.object(coordinator, "async_stop", wraps=coordinator.async_stop) as stop:
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    # `runtime_data` lo limpia Home Assistant al descargar: el componente ya no hace `pop`.
    assert getattr(mock_config_entry, "runtime_data", None) is None
    stop.assert_called_once()
    assert coordinator._timers.armed() == set()
    assert coordinator._timers.closing is True


@pytest.mark.usefixtures("init_integration")
async def test_unload_rechazado_conserva_el_coordinator(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Si una plataforma rechaza la descarga, HA sigue considerando la entrada cargada: no se
    puede destruir el coordinator bajo entidades todavía vivas."""
    coordinator = mock_config_entry.runtime_data

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(return_value=False),
        ),
        patch.object(coordinator, "async_stop") as stop,
    ):
        assert not await hass.config_entries.async_unload(mock_config_entry.entry_id)

    assert mock_config_entry.runtime_data is coordinator
    stop.assert_not_called()
    # ...y los timers siguen armados, que es justo el punto: la entrada sigue viva
    assert coordinator._timers.closing is False

    # Teardown a mano: una descarga rechazada deja el entry en FAILED_UNLOAD, que HA
    # considera NO recuperable → no se puede reintentar `async_unload`. Se para el
    # coordinator directamente para no dejar el keepalive de 900 s huérfano.
    await hass.async_add_executor_job(coordinator.async_stop)


# ───────────────────────── listener de opciones ─────────────────────────


@pytest.mark.usefixtures("init_integration")
async def test_cambiar_solo_entry_data_no_recarga(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """`add_update_listener` salta también cuando cambia `entry.data`, no solo las opciones.

    Es un caso REAL, no teórico: la tarea de relleno de identidad del vehículo escribe en
    `entry.data` justo tras el arranque → la entrada recién iniciada se recargaba sola, HA la
    cargaba dos veces, y un apagado durante la recarga la dejaba en UNLOAD_IN_PROGRESS.
    """
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(
            mock_config_entry,
            data={**mock_config_entry.data, CONF_VEHICLE_NAME: "Otro nombre"},
        )
        await hass.async_block_till_done()

    reload.assert_not_called()


@pytest.mark.usefixtures("init_integration")
async def test_cambiar_las_opciones_si_recarga(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """El options flow (intervalos de sondeo, override del nombre) no recarga por su cuenta."""
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(
            mock_config_entry,
            options={**mock_config_entry.options, CONF_POLL_PARKED: 60},
        )
        await hass.async_block_till_done()

    reload.assert_called_once_with(mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_sin_coordinator_recarga_por_prudencia(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Sin coordinator vivo no hay con qué comparar → se recarga, que es lo conservador."""
    coordinator = mock_config_entry.runtime_data
    del mock_config_entry.runtime_data

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(
            mock_config_entry,
            data={**mock_config_entry.data, CONF_VEHICLE_NAME: "Otro"},
        )
        await hass.async_block_till_done()

    reload.assert_called_once_with(mock_config_entry.entry_id)
    mock_config_entry.runtime_data = coordinator  # para el teardown
