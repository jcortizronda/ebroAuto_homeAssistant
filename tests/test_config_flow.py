"""Tests de `config_flow.py` — alta, selección de vehículo, reconfiguración, reauth y opciones.

Parcheando solo `_password_login` y `_discover` se evita TODA la red y todo el sistema de
ficheros del flujo: son las dos funciones que el propio módulo aísla para eso.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ebro.const import (
    CONF_AREA_CODE,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PIN,
    CONF_POLL_PARKED,
    CONF_TUSERID,
    CONF_VEHICLE_NAME,
    CONF_VIN,
    DOMAIN,
)

from .const import ENTRY_DATA, ENTRY_OPTIONS, TEST_PHONE, TEST_PIN, TEST_TUSERID, TEST_VIN

OTRO_VIN = "LSJA0000000000002"

USER_INPUT = {
    CONF_PHONE: TEST_PHONE,
    CONF_PASSWORD: "contrasena",
    CONF_PIN: TEST_PIN,
    CONF_AREA_CODE: "34",
}

POLL_INPUT = {
    CONF_POLL_PARKED: 0,
    "poll_plugged_min": 30,
    "poll_charging_min": 15,
    "poll_moving_min": 3,
    "poll_moving_idle_min": 5,
    "plugged_wait_max_min": 0,
}


@pytest.fixture
def mock_flow_backend():
    """Corta las dos únicas costuras del flujo hacia red/disco."""
    with (
        patch(
            "custom_components.ebro.config_flow._password_login",
            return_value=(True, "ok"),
        ) as login,
        patch(
            "custom_components.ebro.config_flow._discover",
            return_value=(True, TEST_TUSERID, [TEST_VIN], "ok"),
        ) as discover,
        patch(
            "custom_components.ebro.config_flow._finalize_token", return_value=True
        ) as finalize,
        patch("custom_components.ebro.config_flow._cleanup_pending") as cleanup,
        patch("custom_components.ebro.async_setup_entry", return_value=True),
    ):
        yield {
            "login": login,
            "discover": discover,
            "finalize": finalize,
            "cleanup": cleanup,
        }


# ───────────────────────── alta ─────────────────────────


@pytest.mark.usefixtures("mock_flow_backend")
async def test_alta_con_un_vehiculo(hass: HomeAssistant) -> None:
    """Camino feliz: con un solo VIN se salta la selección y se va directo a los intervalos."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "poll"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=POLL_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Ebro Auto ({TEST_VIN})"

    entry = result["result"]
    assert entry.unique_id == TEST_VIN
    assert entry.data[CONF_VIN] == TEST_VIN
    assert entry.data[CONF_TUSERID] == TEST_TUSERID
    assert entry.options == POLL_INPUT


async def test_alta_con_varios_vehiculos(
    hass: HomeAssistant, mock_flow_backend
) -> None:
    """Con más de un VIN aparece el paso de selección."""
    mock_flow_backend["discover"].return_value = (
        True,
        TEST_TUSERID,
        [TEST_VIN, OTRO_VIN],
        "ok",
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select_vehicle"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_VIN: OTRO_VIN}
    )
    assert result["step_id"] == "poll"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=POLL_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == OTRO_VIN


@pytest.mark.parametrize(
    ("failed", "error"),
    [
        ("login", "login_failed"),
        ("discover", "no_vehicle"),
    ],
)
async def test_errores_y_recuperacion(
    hass: HomeAssistant, mock_flow_backend, failed: str, error: str
) -> None:
    """Cada error debe mostrar el formulario otra vez Y permitir completar el alta después.

    La recuperación es tan importante como el error: un flujo que quede en un callejón sin
    salida obliga al usuario a empezar de cero.
    """
    if failed == "login":
        mock_flow_backend["login"].return_value = (False, "credenciales incorrectas")
    else:
        mock_flow_backend["discover"].return_value = (False, "", [], "cuenta sin coches")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    # el motivo real del backend se le enseña al usuario (placeholder de strings.json)
    assert result["description_placeholders"]["reason"]
    # y el token a medias se borra: si no, un reintento partiría de una credencial inservible
    mock_flow_backend["cleanup"].assert_called()

    # recuperación
    mock_flow_backend["login"].return_value = (True, "ok")
    mock_flow_backend["discover"].return_value = (True, TEST_TUSERID, [TEST_VIN], "ok")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=USER_INPUT
    )
    assert result["step_id"] == "poll"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=POLL_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_descubrimiento_sin_vins_es_no_vehicle(
    hass: HomeAssistant, mock_flow_backend
) -> None:
    """`d_ok=True` pero lista vacía cuenta igualmente como «ningún vehículo»."""
    mock_flow_backend["discover"].return_value = (True, TEST_TUSERID, [], "sin coches")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )

    assert result["errors"] == {"base": "no_vehicle"}


@pytest.mark.usefixtures("mock_flow_backend")
async def test_ya_configurado(hass: HomeAssistant) -> None:
    """El duplicado se detecta ANTES de pedir los intervalos, para no hacer rellenar en balde."""
    MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_token_no_movible_aborta(hass: HomeAssistant, mock_flow_backend) -> None:
    """Sin el token en su ruta definitiva la integración no podría hablar con el coche: mejor
    abortar que crear una entrada rota."""
    mock_flow_backend["finalize"].return_value = False

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=POLL_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "token_move_failed"


@pytest.mark.usefixtures("mock_flow_backend")
async def test_intervalos_fuera_de_rango(hass: HomeAssistant) -> None:
    """0–1440 minutos: un intervalo de 2 días no tendría sentido y el backend lo sufriría."""
    import voluptuous as vol

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=USER_INPUT
    )

    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={**POLL_INPUT, CONF_POLL_PARKED: 5000}
        )


# ───────────────────────── reconfiguración del PIN ─────────────────────────


async def test_reconfigure_cambia_el_pin_y_limpia_el_bloqueo(
    hass: HomeAssistant, mock_core, issue_registry: ir.IssueRegistry
) -> None:
    """Reconfigurar es el gesto explícito de remedio: borra el Repair y pone a cero el
    anti-bloqueo, AUNQUE el usuario reintroduzca el mismo PIN.

    Sin el reseteo se quedaría parado —sin ninguna señal— hasta que venciera la ventana.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    coordinator.session.raise_pin_issue("PIN rechazado")
    assert issue_registry.async_get_issue(DOMAIN, f"pin_wrong_{entry.entry_id}")

    with patch.object(coordinator.ctx, "reset_pin_lockout") as reset:
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PIN: "9876"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PIN] == "9876"
    reset.assert_called_once()
    assert issue_registry.async_get_issue(DOMAIN, f"pin_wrong_{entry.entry_id}") is None


@pytest.mark.usefixtures("mock_core")
async def test_reconfigure_pin_vacio(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PIN: "   "}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "pin_required"}


# ───────────────────────── reautenticación ─────────────────────────


async def test_reauth_ok(hass: HomeAssistant, mock_core, mock_flow_backend) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    # el formulario recuerda al usuario CON QUÉ cuenta está reautenticando
    assert result["description_placeholders"]["phone"] == TEST_PHONE

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "contrasena-nueva"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "contrasena-nueva"


async def test_reauth_login_fallido_y_recuperacion(
    hass: HomeAssistant, mock_core, mock_flow_backend
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    )
    entry.add_to_hass(hass)
    mock_flow_backend["login"].return_value = (False, "contraseña incorrecta")

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "mala"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "login_failed"}

    mock_flow_backend["login"].return_value = (True, "ok")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "buena"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


async def test_reauth_token_no_movible(
    hass: HomeAssistant, mock_core, mock_flow_backend
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    )
    entry.add_to_hass(hass)
    mock_flow_backend["finalize"].return_value = False

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "nueva"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "token_move_failed"}


# ───────────────────────── options flow ─────────────────────────


@pytest.mark.usefixtures("mock_core")
async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS, unique_id=TEST_VIN
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    nuevas = {**POLL_INPUT, CONF_POLL_PARKED: 45, CONF_VEHICLE_NAME: "Mi Ebro"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=nuevas
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_POLL_PARKED] == 45
    assert entry.options[CONF_VEHICLE_NAME] == "Mi Ebro"


@pytest.mark.usefixtures("mock_core")
async def test_el_override_del_nombre_llega_al_dispositivo(hass: HomeAssistant) -> None:
    """El apodo de las opciones gana sobre el de `entry.data`; el dispositivo se identifica
    por VIN, así que renombrarlo no toca ni entity_id ni histórico."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options={**ENTRY_OPTIONS, CONF_VEHICLE_NAME: "Mi coche"},
        unique_id=TEST_VIN,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.vehicle_name == "Mi coche"
