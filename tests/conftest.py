"""Arnés de test compartido de Ebro Auto.

Este conftest es más denso que el de una integración típica, y las razones son concretas:

* **Todos los imports del componente son perezosos** (`from .core import commands` DENTRO de
  la función). Eso obliga a parchear siempre el **módulo de origen**
  (`custom_components.ebro.core.commands.send`) y nunca un nombre reexportado en el consumidor:
  el consumidor no liga el nombre en tiempo de import, así que parchearlo ahí no tendría efecto.
* **El coordinator es push-only**: `update_interval=None` y no existe `_async_update_data`.
  Llamar a `async_refresh()` reventaría con `NotImplementedError`; los datos se inyectan por el
  mismo camino que un mensaje MQTT, con `_apply_update()`.
* **El detector de generaciones de taskId solapadas vive en el vehículo**, no en un global de
  módulo: por eso ya no hace falta resetearlo entre tests (antes sí, y un test que lo dejara a
  medias hacía fallar a otro sin relación aparente).
* **El `entity_id` no lo deriva HA**: lo fija `entity.py` con el esquema
  `<plataforma>.ebro_<4 cifras del VIN>_<translation_key>`, para que no dependa del nombre del
  dispositivo (que es dinámico: cambia al descubrirse el modelo del coche).
"""

from __future__ import annotations

from collections.abc import Generator
import copy
from datetime import UTC, datetime
import json
import pathlib
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.const import DOMAIN, PLATFORMS

from .const import ENTRY_DATA, ENTRY_OPTIONS, TEST_VIN

_FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def get_coordinator(hass: HomeAssistant):
    """El coordinator del entry cargado.

    Vive en `entry.runtime_data`. Antes los tests lo sacaban de
    `hass.data[DOMAIN][next(iter(hass.data[DOMAIN]))]`, una expresión que aparecía copiada en
    diecinueve sitios y que dependía de que el componente usara `hass.data`.
    """
    return hass.config_entries.async_entries(DOMAIN)[0].runtime_data


def load_json_fixture(name: str) -> dict:
    """Carga un payload JSON de `tests/fixtures/`.

    Se resuelve a mano en vez de con `load_json_object_fixture` de phcc: aquel busca dentro de
    `tests/components/<dominio>/fixtures` del core de HA, que no es donde vive un componente
    custom.
    """
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


# Cargados en tiempo de IMPORT (fase de colección de pytest, fuera del bucle de eventos): así
# ninguna lectura de fichero ocurre dentro del loop, que HA marcaría como blocking call.
MQTT_FIELDS: dict = load_json_fixture("mqtt_5a02.json")
REALTIME: dict = load_json_fixture("realtime.json")
POSITION: dict = load_json_fixture("position.json")
QUERY_LIST: dict = load_json_fixture("query_list.json")

#: Instantes fijos que se inyectan en `coordinator.data`. Congelados para que los sensores de
#: tipo TIMESTAMP tengan un valor estable en los snapshots.
LAST_SEEN = datetime(2026, 1, 15, 11, 58, 0, tzinfo=UTC)
LAST_WAKE = datetime(2026, 1, 15, 11, 50, 0, tzinfo=UTC)
LAST_POS_FIX = datetime(2026, 1, 15, 11, 57, 0, tzinfo=UTC)
CAR_DATA_TS = datetime(2026, 1, 15, 11, 59, 0, tzinfo=UTC)


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Aplica la extensión syrupy de Home Assistant.

    Hace dos cosas imprescindibles: sustituye por `<ANY>` los campos volátiles
    (`config_entry_id`, `device_id`, `id`, `context`…), sin lo cual ningún snapshot de
    entidad sería reproducible entre ejecuciones; y guarda los `.ambr` en `snapshots/` en
    vez del `__snapshots__/` por defecto de syrupy, igual que el core de HA.

    phcc ya define esta misma fixture, pero el plugin de syrupy se registra después y la
    tapa. Redefinirla en un conftest gana sobre ambos plugins — es exactamente lo que hace
    `tests/conftest.py` del core.
    """
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Deja que HA descubra `custom_components/ebro`.

    HA localiza los componentes custom importando el paquete `custom_components` desde
    `sys.path`; como `tests/` es un paquete, pytest inserta la raíz del repo y el import
    funciona sin symlinks ni copias.
    """


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Config entry por defecto (un vehículo, opciones de sondeo explícitas)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Ebro Auto ({TEST_VIN})",
        data=ENTRY_DATA,
        options=ENTRY_OPTIONS,
        unique_id=TEST_VIN,
    )


@pytest.fixture
def mock_core() -> Generator[dict[str, MagicMock]]:
    """Corta TODAS las costuras de red, MQTT y certificados del componente.

    Devuelve un dict con los mocks para que cada test pueda ajustar `return_value` /
    `side_effect`. Ojo: son mocks **síncronos** (`MagicMock`, no `AsyncMock`) porque el
    componente los ejecuta siempre vía `hass.async_add_executor_job`.
    """
    with (
        patch(
            "custom_components.ebro.vehicle.coordinator.EbroCoordinator._provision_certs",
            return_value=(True, "certificados presentes"),
        ) as provision_certs,
        patch(
            "custom_components.ebro.vehicle.coordinator.EbroCoordinator._connect_car",
            return_value=None,
        ) as connect_car,
        patch(
            "custom_components.ebro.vehicle.coordinator.EbroCoordinator._fetch_vehicle_identity",
            return_value=None,
        ) as fetch_identity,
        patch(
            "custom_components.ebro.core.session.check",
            return_value=(True, "sesión válida", "OK"),
        ) as session_check,
        patch(
            "custom_components.ebro.core.session.refresh_if_expiring",
            return_value=(True, ""),
        ) as session_refresh,
        patch(
            "custom_components.ebro.core.commands.send",
            return_value="confirmado ✅",
        ) as commands_send,
        patch(
            "custom_components.ebro.core.commands.query_theft_switch",
            return_value=1,
        ) as query_theft,
        patch(
            "custom_components.ebro.core.wake.do_wake",
            return_value={"ok": True, "online": True, "code": "000000"},
        ) as do_wake,
        patch(
            "custom_components.ebro.core.probe.probe_once",
            return_value={
                "ok": True,
                "online": True,
                "got_data": True,
                "codes": ["000000"],
                "rich": {},
            },
        ) as probe_once,
    ):
        yield {
            "provision_certs": provision_certs,
            "connect_car": connect_car,
            "fetch_identity": fetch_identity,
            "session_check": session_check,
            "session_refresh": session_refresh,
            "commands_send": commands_send,
            "query_theft": query_theft,
            "do_wake": do_wake,
            "probe_once": probe_once,
        }


@pytest.fixture
def telemetry() -> dict:
    """Escenario por defecto: coche despierto, enchufado y CARGANDO.

    Se elige "cargando" porque es el estado que deja más sensores con un valor real: aparece
    `remainChargeTime`, la alta tensión está encendida (tensión/corriente dejan de ser los
    marcadores −1000/0) y la carga programada tiene un plan activo.
    """
    return {
        "fields": copy.deepcopy(MQTT_FIELDS),
        "realtime": copy.deepcopy(REALTIME),
        "position": copy.deepcopy(POSITION),
        "awake": True,
        "car_connected": True,
        "session_ok": True,
        "session_detail": "sesión válida",
        "cmd_status": "confirmado ✅",
        "wake_status": "coche accesible",
        "probe_status": "sonda ok",
        "last_seen": LAST_SEEN,
        "last_wake": LAST_WAKE,
        "last_pos_fix": LAST_POS_FIX,
        "car_data_ts": CAR_DATA_TS,
    }


@pytest.fixture
def telemetry_empty() -> dict:
    """Escenario "arranque en frío": el coche todavía no ha publicado nada.

    Vale la pena tenerlo porque cada consumidor hace `data.get("realtime") or {}` y esa rama
    no se ejercitaría nunca con el escenario poblado.
    """
    return {}


@pytest.fixture
def platforms() -> list[str]:
    """Plataformas a cargar. Cada `test_<plataforma>.py` la sobreescribe con la suya.

    `snapshot_platform` exige que haya UNA sola plataforma cargada (comprueba que todas las
    entradas del registro comparten dominio), de ahí el parcheo de `PLATFORMS`.
    """
    return PLATFORMS


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_core: dict[str, MagicMock],
    platforms: list[str],
    telemetry: dict,
) -> MockConfigEntry:
    """Da de alta el entry con las plataformas pedidas y siembra la telemetría."""
    mock_config_entry.add_to_hass(hass)

    with patch("custom_components.ebro.PLATFORMS", platforms):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        if telemetry:
            coordinator = mock_config_entry.runtime_data
            seed_telemetry(coordinator, telemetry)
            await hass.async_block_till_done()

    return mock_config_entry


def seed_telemetry(coordinator, telemetry: dict) -> None:
    """Inyecta datos como lo haría un push MQTT del coche.

    `_apply_update` es el mismo camino que usa `_on_car_message` (vía `_update`), así que las
    entidades reciben la notificación real del coordinator. Además se siembra
    el instante del último mensaje, que `coordinator.is_awake` compara contra el reloj — es lo que decide el
    estado de «Coche despierto», el único binary_sensor que no sale de `coordinator.data`.
    """
    import time

    coordinator.state.seed(
        fields=dict(telemetry.get("fields") or {}),
        position=telemetry.get("position"),
        last_msg_ts=time.time() if telemetry.get("awake") else 0.0,
    )
    coordinator._apply_update(telemetry)
