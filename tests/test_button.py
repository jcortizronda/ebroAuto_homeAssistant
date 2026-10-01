"""Tests de la plataforma `button` (7 entidades: 3 de comando + 4 de acción)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.const import COMMANDS_AS_RICH_ENTITY, DOMAIN

from .conftest import get_coordinator
from .const import FROZEN_TIME

pytestmark = pytest.mark.freeze_time(FROZEN_TIME)


@pytest.fixture
def platforms() -> list[str]:
    return [Platform.BUTTON]


def _coordinator(hass: HomeAssistant):
    return get_coordinator(hass)


@pytest.mark.usefixtures("init_integration")
async def test_entities(
    hass: HomeAssistant,
    snapshot: SnapshotAssertion,
    entity_registry: er.EntityRegistry,
    mock_config_entry: MockConfigEntry,
) -> None:
    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


@pytest.mark.usefixtures("init_integration")
async def test_solo_quedan_tres_botones_de_comando(
    entity_registry: er.EntityRegistry, mock_config_entry: MockConfigEntry
) -> None:
    """Del catálogo de 41 comandos, 38 los reclama un lock/switch/cover/climate.

    Las dos tablas (`COMMANDS` y `COMMANDS_AS_RICH_ENTITY`) se mantienen alineadas a mano:
    sacar una clave de la segunda sin darse cuenta haría reaparecer un botón suelto que
    duplica un control existente.
    """
    from custom_components.ebro.core import catalog

    sueltos = [k for k, _ in catalog.COMMANDS if k not in COMMANDS_AS_RICH_ENTITY]
    assert sueltos == [
        "ventilar_ventanillas",
        "encontrar_coche_luces",
        "localizar_coche_gps",
    ]

    entidades = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    # + wake/refresh_pos/refresh_full/charge_plan/stop_charge
    assert len(entidades) == len(sueltos) + 5


@pytest.mark.parametrize(
    ("entity_id", "comando"),
    [
        ("button.ebro_0001_ventilar_ventanillas", "ventilar_ventanillas"),
        ("button.ebro_0001_encontrar_coche_luces", "encontrar_coche_luces"),
        ("button.ebro_0001_localizar_coche_gps", "localizar_coche_gps"),
    ],
)
@pytest.mark.usefixtures("init_integration")
async def test_botones_de_comando(
    hass: HomeAssistant, entity_id: str, comando: str
) -> None:
    coordinator = _coordinator(hass)

    with patch.object(
        coordinator, "async_send_command", AsyncMock(return_value="ok")
    ) as send:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    send.assert_awaited_once_with(comando)


@pytest.mark.parametrize(
    ("entity_id", "metodo", "kwargs"),
    [
        ("button.ebro_0001_despertar_coche", "async_wake", {}),
        # `force=True`: el cooldown de la sonda frena el bucle automático, no a quien pulsa.
        # Sin forzar, pulsar dentro de los 2 min siguientes a la lectura anterior no hacía
        # nada y encima sin publicar un mensaje — el botón parecía roto.
        ("button.ebro_0001_actualizar_ubicacion", "async_probe", {"force": True}),
        ("button.ebro_0001_actualizar_estado_completo", "async_refresh_full_status", {}),
        ("button.ebro_0001_aplicar_carga_programada", "async_apply_scheduled_charge", {}),
    ],
)
async def test_botones_de_accion(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_core,
    platforms: list[str],
    telemetry: dict,
    entity_id: str,
    metodo: str,
    kwargs: dict,
) -> None:
    """`EbroActionButton` liga el método del coordinator EN LA CONSTRUCCIÓN.

    Por eso el parcheo tiene que ocurrir ANTES del setup: parchear la clase después no lo
    vería, porque el botón guarda ya el objeto-método enlazado.
    """
    from custom_components.ebro.vehicle.coordinator import EbroCoordinator

    mock_config_entry.add_to_hass(hass)
    with (
        patch("custom_components.ebro.PLATFORMS", platforms),
        patch.object(EbroCoordinator, metodo, AsyncMock(return_value=None)) as action,
    ):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        action.reset_mock()  # el setup puede haber llamado a alguno por su cuenta
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    action.assert_awaited_once_with(**kwargs)


def _religa_wake(hass: HomeAssistant, coordinator) -> None:
    """Vuelve a ligar el método parcheado al botón.

    `EbroActionButton` recibe `coord.async_wake` YA LIGADO en el constructor, así que parchear
    el coordinator después no le llega. Es una peculiaridad de cómo se construyen estos botones,
    no del comportamiento que se está probando."""
    hass.data["entity_components"][BUTTON_DOMAIN].get_entity(
        "button.ebro_0001_despertar_coche"
    )._action = coordinator.async_wake


@pytest.mark.usefixtures("init_integration")
async def test_un_comando_fallido_avisa_al_usuario(hass: HomeAssistant) -> None:
    """Antes se tragaba la excepción y solo la escribía en el log: pulsabas el botón, no pasaba
    nada, y no había forma de saberlo sin ir a mirar los registros. El resto de la integración
    (candado, persianas, clima) sí avisa, así que los botones eran la excepción."""
    coordinator = _coordinator(hass)

    with (
        patch.object(
            coordinator,
            "async_send_command",
            AsyncMock(side_effect=RuntimeError("A00082 coche ocupado")),
        ) as send,
        pytest.raises(HomeAssistantError, match="localizar_coche_gps"),
    ):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ebro_0001_localizar_coche_gps"},
            blocking=True,
        )

    send.assert_awaited_once()


@pytest.mark.usefixtures("init_integration")
async def test_un_mensaje_ya_pensado_para_el_usuario_no_se_envuelve(hass: HomeAssistant) -> None:
    """El de la cola ocupada es el caso que destapó todo esto: `async_send_command` lo genera
    ANTES de publicar nada, así que desde un botón no llegaba a ninguna parte — ni al aviso ni
    al sensor de «Resultado del comando». Y al propagarlo hay que reenviarlo TAL CUAL: envolverlo
    daría «El comando «localizar_coche_gps» ha fallado: El coche sigue ocupado…»."""
    coordinator = _coordinator(hass)
    ocupado = HomeAssistantError("El coche sigue ocupado con los comandos anteriores.")

    with (
        patch.object(coordinator, "async_send_command", AsyncMock(side_effect=ocupado)),
        pytest.raises(HomeAssistantError) as capt,
    ):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ebro_0001_localizar_coche_gps"},
            blocking=True,
        )

    assert str(capt.value) == "El coche sigue ocupado con los comandos anteriores."


@pytest.mark.usefixtures("init_integration")
async def test_una_accion_fallida_habla_del_nombre_del_boton(hass: HomeAssistant) -> None:
    """«Despertar coche», no «Comando «wake» fallido»: el usuario ha pulsado un botón con un
    nombre, no ha invocado una clave interna."""
    coordinator = _coordinator(hass)

    with (
        patch.object(coordinator, "async_wake", AsyncMock(side_effect=RuntimeError("sin red"))),
        pytest.raises(HomeAssistantError, match="Despertar coche"),
    ):
        _religa_wake(hass, coordinator)
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ebro_0001_despertar_coche"},
            blocking=True,
        )


@pytest.mark.usefixtures("init_integration")
async def test_los_errores_de_los_botones_son_traducibles(hass: HomeAssistant) -> None:
    """Sin `translation_key` la interfaz pinta «No se pudo realizar la acción button.press» con
    nuestro texto pegado detrás; con ella pinta solo nuestro texto. El mensaje en claro se
    conserva igualmente, porque es lo que se ve en el log."""
    coordinator = _coordinator(hass)

    with (
        patch.object(coordinator, "async_wake", AsyncMock(side_effect=RuntimeError("sin red"))),
        pytest.raises(HomeAssistantError) as capt,
    ):
        _religa_wake(hass, coordinator)
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: "button.ebro_0001_despertar_coche"},
            blocking=True,
        )

    assert capt.value.translation_domain == DOMAIN
    assert capt.value.translation_key == "action_failed"
    assert capt.value.translation_placeholders["name"] == "Despertar coche"


@pytest.mark.usefixtures("init_integration")
async def test_una_accion_fallida_avisa_al_usuario(hass: HomeAssistant) -> None:
    """Igual que con los comandos: antes solo iba al log."""
    coordinator = _coordinator(hass)

    with patch.object(coordinator, "async_wake", AsyncMock(side_effect=OSError("red"))):
        # el botón ya tiene ligado el método original, así que se parchea el objeto ligado
        entidad = hass.data["entity_components"][BUTTON_DOMAIN].get_entity(
            "button.ebro_0001_despertar_coche"
        )
        entidad._action = coordinator.async_wake
        with pytest.raises(HomeAssistantError, match="Despertar coche"):
            await hass.services.async_call(
                BUTTON_DOMAIN,
                SERVICE_PRESS,
                {ATTR_ENTITY_ID: "button.ebro_0001_despertar_coche"},
                blocking=True,
            )
