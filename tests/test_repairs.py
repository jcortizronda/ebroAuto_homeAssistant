"""Tests de `repairs.py` — el único Repair de la integración: «PIN de comandos erróneo»."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from syrupy.assertion import SnapshotAssertion
from syrupy.filters import props

from custom_components.ebro.const import CONF_PIN, DOMAIN

from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


@pytest.fixture
def platforms() -> list[str]:
    return []


def _issue_id(entry: MockConfigEntry) -> str:
    return f"pin_wrong_{entry.entry_id}"


async def test_se_crea_la_issue(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    coordinator = init_integration.runtime_data
    coordinator.session.raise_pin_issue("code=A00285 'wrong password'")

    issue = issue_registry.async_get_issue(DOMAIN, _issue_id(init_integration))
    assert issue is not None
    assert issue.translation_key == "pin_wrong"
    assert issue.is_fixable is True
    assert issue.severity is ir.IssueSeverity.ERROR
    # el entry_id viaja DENTRO de `data` y del propio `issue_id`: es lo que permite al fix
    # flow saber a qué entrada aplicar el PIN nuevo, y con varios coches configurados es lo
    # único que distingue un aviso del otro.
    assert issue.data == {"entry_id": init_integration.entry_id}
    assert issue.issue_id == f"pin_wrong_{init_integration.entry_id}"

    # `entry_id` es un ULID aleatorio en cada ejecución y el serializador de HA no lo
    # sustituye por <ANY> dentro de `data`/`issue_id` (solo lo hace en los campos volátiles
    # que conoce) → se excluye del snapshot; los dos asserts de arriba ya lo cubren exacto.
    assert issue == snapshot(exclude=props("entry_id", "issue_id"))


async def test_un_comando_correcto_borra_la_issue(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """El aviso deja de tener razón de ser en cuanto un comando sale bien."""
    coordinator = init_integration.runtime_data
    coordinator.session.raise_pin_issue("PIN rechazado")
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(init_integration))

    with (
        patch.object(coordinator, "_send_command", return_value="ok"),
        patch("custom_components.ebro.vehicle.coordinator.COMMAND_SETTLE_S", 0),
    ):
        await coordinator.async_send_command("bloquear")
        await hass.async_block_till_done()

    assert issue_registry.async_get_issue(DOMAIN, _issue_id(init_integration)) is None


async def test_el_enrutado_decide_cuando_abrir_el_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    """El Repair solo se abre cuando la tabla de enrutado dice «es el PIN».

    Antes esto estaba repartido y el respaldo del despertar clasificaba como «PIN erróneo»
    rechazos que eran de permisos o de sesión: se le proponía al usuario el remedio
    equivocado y —peor— se le acercaba al bloqueo real de la cuenta.
    """
    from custom_components.ebro.core.commands import CommandError

    coordinator = init_integration.runtime_data

    # un error de permisos NO abre el Repair del PIN
    coordinator.session.route_remedy(CommandError("sin permiso", code="A00374", reason="config"))
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(init_integration)) is None

    # un rechazo de PIN sí
    coordinator.session.route_remedy(CommandError("PIN erróneo", code="A00285", reason="pin"))
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(init_integration))


async def test_una_sesion_muerta_pide_reauth_no_el_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    from custom_components.ebro.core.commands import CommandError

    coordinator = init_integration.runtime_data

    with patch.object(coordinator.entry, "async_start_reauth") as reauth:
        coordinator.session.route_remedy(
            CommandError("token caducado", code="A00000", reason="reauth")
        )

    reauth.assert_called_once()
    assert issue_registry.async_get_issue(DOMAIN, _issue_id(init_integration)) is None


# ───────────────────────── el flujo de reparación ─────────────────────────


async def _abre_el_fix_flow(hass: HomeAssistant, issue_id: str) -> dict:
    from custom_components.ebro.repairs import async_create_fix_flow

    flow = await async_create_fix_flow(hass, issue_id, {"entry_id": issue_id.split("_", 2)[2]})
    flow.hass = hass
    flow.issue_id = issue_id
    return flow


async def test_fix_flow_camino_feliz(
    hass: HomeAssistant, init_integration: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    """Introducir el PIN nuevo lo guarda, recarga la entrada y suelta el anti-bloqueo."""
    coordinator = init_integration.runtime_data
    coordinator.session.raise_pin_issue("PIN rechazado")

    flow = await _abre_el_fix_flow(hass, _issue_id(init_integration))

    outcome = await flow.async_step_init()
    assert outcome["type"] is FlowResultType.FORM
    assert outcome["step_id"] == "pin"

    outcome = await flow.async_step_pin({CONF_PIN: "4321"})
    await hass.async_block_till_done()

    assert outcome["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.data[CONF_PIN] == "4321"


async def test_fix_flow_pin_vacio(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    flow = await _abre_el_fix_flow(hass, _issue_id(init_integration))

    outcome = await flow.async_step_pin({CONF_PIN: "   "})

    assert outcome["type"] is FlowResultType.FORM
    assert outcome["errors"] == {"base": "pin_required"}


async def test_fix_flow_sin_entrada(hass: HomeAssistant) -> None:
    """La entrada puede haber desaparecido entre que salta el aviso y el usuario lo abre."""
    from custom_components.ebro.repairs import EbroPinRepairFlow

    flow = EbroPinRepairFlow({"entry_id": "una-entrada-que-ya-no-existe"})
    flow.hass = hass

    outcome = await flow.async_step_pin({CONF_PIN: "1234"})

    assert outcome["type"] is FlowResultType.ABORT
    assert outcome["reason"] == "entry_not_found"


async def test_el_lockout_se_limpia_sobre_el_coordinator_recargado(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """`_clear_pin_lockout` corre DESPUÉS de `async_reload`, así que actúa sobre la instancia
    NUEVA del coordinator: el estado del anti-bloqueo vive en memoria y no sobrevive a la
    recarga, pero el reseteo explícito sigue haciendo falta si la recarga fallara."""
    flow = await _abre_el_fix_flow(hass, _issue_id(init_integration))

    await flow.async_step_pin({CONF_PIN: "4321"})
    await hass.async_block_till_done()

    nuevo = init_integration.runtime_data
    assert nuevo.ctx.lockout.failed_attempts == 0
    assert nuevo.pin == "4321"


async def test_clear_pin_lockout_sin_coordinator_no_revienta(hass: HomeAssistant) -> None:
    """Si el entry no existe (o aún no cargó), no hay `runtime_data`: no-op silencioso."""
    from custom_components.ebro.repairs import _clear_pin_lockout

    _clear_pin_lockout(hass, "entrada-inexistente")


@pytest.mark.parametrize("fichero", ["strings.json", "translations/es.json"])
def test_las_traducciones_de_la_issue_existen(fichero: str) -> None:
    """El Repair se presenta con `translation_key`: sin las cadenas el usuario vería una
    tarjeta con el identificador crudo y un formulario sin etiquetas.

    Se comprueban todas las claves que el flujo puede llegar a devolver, incluidas las de
    error y abort, que son justo las que se olvidan porque solo aparecen en el camino malo.
    """
    import json
    import pathlib

    ruta = pathlib.Path(__file__).parent.parent / "custom_components" / "ebro" / fichero
    datos = json.loads(ruta.read_text(encoding="utf-8"))

    pin_wrong = datos["issues"]["pin_wrong"]
    assert pin_wrong["title"]
    assert pin_wrong["fix_flow"]["step"]["pin"]
    assert pin_wrong["fix_flow"]["error"]["pin_required"]
    assert pin_wrong["fix_flow"]["abort"]["entry_not_found"]
