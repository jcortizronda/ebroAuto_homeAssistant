"""Tests de `diagnostics.py` — el informe de «Descargar diagnóstico».

El módulo promete en su cabecera que el archivo es «seguro de enviar». Estos tests existen
para que esa promesa siga siendo cierta: además del snapshot, hay casos adversarios con el
VIN y las coordenadas escondidos en sitios donde la ocultación POR CLAVE no llega.
"""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion
from syrupy.filters import props

from custom_components.ebro.diagnostics import TO_REDACT

from .const import FROZEN_TIME, TEST_PASSWORD, TEST_PHONE, TEST_VIN

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


@pytest.fixture
def platforms() -> list[str]:
    """Sin plataformas: el informe sale del coordinator, no de las entidades."""
    return []


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    outcome = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert outcome == snapshot(exclude=props("created_at", "modified_at"))


async def test_los_secretos_estan_ocultos(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """Credenciales de la cuenta, PIN, VIN y tUserId no pueden salir en claro.

    `password` y `phone` son las credenciales de acceso a la cuenta Ebro y se guardan EN
    CLARO en `entry.data`. `async_redact_data` no oculta nada por su cuenta —solo las claves
    que se le pasan—, así que este test es lo único que impide que vuelvan a filtrarse en el
    archivo de diagnóstico.
    """
    outcome = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    data = outcome["entry"]["data"]

    for key in ("password", "phone", "pin", "vin", "tuserid"):
        assert data[key] == REDACTED, key

    assert TEST_PASSWORD not in str(outcome)
    assert TEST_PHONE not in str(outcome)

    # el título se fuerza sin VIN (el real es "Ebro Auto (<VIN>)")
    assert outcome["entry"]["title"] == "Ebro Auto"
    assert TEST_VIN not in str(outcome)


async def test_la_posicion_nunca_sale_en_crudo(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """Dónde está el coche no se exporta: solo si HAY fix, nunca las coordenadas."""
    outcome = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    state = outcome["coordinator"]["state"]

    assert state["has_position_fix"] is True
    assert "position" not in state
    assert "40.416775" not in str(outcome)
    assert "-3.703790" not in str(outcome)


async def test_coordenadas_incrustadas_en_un_texto(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """El caso REAL encontrado en campo el 2026-07-20.

    `probe_status` es un mensaje discursivo para el usuario y llegó a contener
    «lat=40.90…, lon=14.34…»: las coordenadas acababan en claro justo en el archivo que la
    cabecera del módulo promete «seguro de enviar». La ocultación por CLAVE no lo cubre.
    """
    coordinator = init_integration.runtime_data
    coordinator._apply_update(
        {"probe_status": "🟢 Datos recibidos: lat=40.9012345, lon=14.3456789, soc=64"}
    )

    outcome = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert "40.9012345" not in str(outcome)
    assert "14.3456789" not in str(outcome)
    assert "**GEO**" in outcome["coordinator"]["state"]["probe_status"]
    # ...y el resto del mensaje se conserva: sigue siendo útil para el soporte
    assert "soc=64" in outcome["coordinator"]["state"]["probe_status"]


async def test_vin_incrustado_en_el_seq(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """`seq` vale `"<VIN>-<timestamp>"`: el VIN viaja dentro de un id compuesto.

    Está en `TO_REDACT` por eso, y además `_scrub_vin` lo caza como subcadena dondequiera
    que aparezca.
    """
    coordinator = init_integration.runtime_data
    coordinator._apply_update(
        {"realtime": {"seq": f"{TEST_VIN}-1768478400000", "dumpEnergy": "64.5"}}
    )

    outcome = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)

    assert TEST_VIN not in str(outcome)


async def test_sin_coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Entrada no cargada: el informe se queda en la parte del config entry.

    Se invoca la función de la plataforma directamente en vez de pasar por el endpoint HTTP:
    «Descargar diagnóstico» exige una entrada cargada, y lo que se quiere ejercitar aquí es
    justamente la rama en la que NO hay coordinator en `hass.data`.

    ⚠️ Esa rama hace `return` ANTES del barrido final `_scrub_geo`. Hoy no importa (no hay
    telemetría que barrer y `entry.data` ya pasó por `async_redact_data`), pero conviene
    dejarlo fijado: si mañana se añadiera un campo de texto libre a `entry.data`, este camino
    no lo barrería.
    """
    from custom_components.ebro.diagnostics import async_get_config_entry_diagnostics

    mock_config_entry.add_to_hass(hass)

    outcome = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert outcome["coordinator"] == "no inicializado (entrada no cargada)"
    assert outcome["entry"]["data"]["pin"] == REDACTED
    assert outcome == snapshot(exclude=props("created_at", "modified_at"))


async def test_los_certificados_solo_como_booleanos(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
) -> None:
    """Presencia sí, contenido nunca."""
    outcome = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    coordinator = outcome["coordinator"]

    assert isinstance(coordinator["token_present"], bool)
    assert all(isinstance(v, bool) for v in coordinator["certs_present"].values())


def test_to_redact_cubre_las_claves_sensibles_del_entry() -> None:
    """Guarda barata contra una clave sensible nueva en `entry.data` que nadie oculte.

    Se comprueba clave a clave (y no solo el resultado) porque quitar una de aquí es un
    cambio de una línea, silencioso, y sus consecuencias solo se ven en el archivo que el
    usuario acaba enviando a un tercero.
    """
    assert {"email", "phone", "password", "pin", "vin", "tuserid", "certs_src"} <= TO_REDACT
    assert {"lat", "lon", "latitude", "longitude", "position"} <= TO_REDACT


def test_el_topic_no_saca_el_tuserid_por_la_puerta_de_atras() -> None:
    """`tuserid` está en `TO_REDACT`, y el topic MQTT lo lleva incrustado. El informe se
    comparte para pedir ayuda: interesa la FORMA del topic —comodín o exacto—, no el número."""
    from custom_components.ebro.diagnostics import _scrub_user

    limpio = _scrub_user("app/4/401643347363401728/account/msgCenter/msg")

    assert "401643347363401728" not in limpio
    assert limpio == "app/4/**REDACTED**/account/msgCenter/msg"
    # la forma se conserva: distinguir el comodín del topic exacto es el objetivo del campo
    assert _scrub_user("app/4/401643347363401728/#") == "app/4/**REDACTED**/#"
    assert _scrub_user(None) is None
