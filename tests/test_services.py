"""Tests del servicio `ebro.programar_carga`.

Existe para que una automatización pueda programar la carga en UNA llamada: con las entidades
hay que fijar la hora, fijar la duración y pulsar «Aplicar», y confiar en que ese orden se
respete. Ver el docstring de `services.py` para por qué las entidades de hora no mandan solas.
"""
from __future__ import annotations

from datetime import time
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ebro.const import CHARGE_MIN_DURATION_MIN, DOMAIN

from .conftest import get_coordinator

SERVICIO = "programar_carga"
DESTINO = "switch.ebro_0001_carga_programada"


@pytest.fixture
def platforms() -> list[str]:
    from homeassistant.const import Platform

    return [Platform.SWITCH]


@pytest.mark.usefixtures("init_integration")
async def test_programa_la_carga_en_una_sola_llamada(hass: HomeAssistant) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(coordinator, "_send_charge_plan", AsyncMock()) as enviar:
        await hass.services.async_call(
            DOMAIN, SERVICIO,
            {"entity_id": DESTINO, "hora_inicio": time(2, 30), "duracion": 240},
            blocking=True,
        )

    assert coordinator.preferences.charge_start_minutes == 150   # 02:30
    assert coordinator.preferences.charge_duration_minutes == 240
    plan = enviar.await_args[0][0]
    assert plan["timeConsuming"] == 240
    assert plan["switchStatus"] == 1


@pytest.mark.usefixtures("init_integration")
async def test_acepta_la_hora_como_texto(hass: HomeAssistant) -> None:
    """Desde YAML la hora llega como cadena y desde la interfaz como objeto. Las dos valen: el
    llamador no tiene por qué conocer esa diferencia."""
    coordinator = get_coordinator(hass)

    with patch.object(coordinator, "_send_charge_plan", AsyncMock()):
        await hass.services.async_call(
            DOMAIN, SERVICIO,
            {"entity_id": DESTINO, "hora_inicio": "07:45:00", "duracion": 90},
            blocking=True,
        )

    assert coordinator.preferences.charge_start_minutes == 465   # 07:45


@pytest.mark.usefixtures("init_integration")
async def test_activar_false_guarda_el_horario_sin_encenderlo(hass: HomeAssistant) -> None:
    coordinator = get_coordinator(hass)

    with patch.object(coordinator, "_send_charge_plan", AsyncMock()) as enviar:
        await hass.services.async_call(
            DOMAIN, SERVICIO,
            {"entity_id": DESTINO, "hora_inicio": time(3, 0), "duracion": 120,
             "activar": False},
            blocking=True,
        )

    assert enviar.await_args[0][0]["switchStatus"] == 0


@pytest.mark.usefixtures("init_integration")
async def test_una_duracion_demasiado_corta_se_rechaza(hass: HomeAssistant) -> None:
    """El coche rechaza menos de una hora con un código críptico. `build_plan` la sube en
    silencio —bien para la interfaz, donde el control ya no deja bajar— pero una llamada escrita
    a mano merece un error en vez de ejecutarse con otra duración distinta de la pedida."""
    coordinator = get_coordinator(hass)

    with (
        patch.object(coordinator, "_send_charge_plan", AsyncMock()) as enviar,
        pytest.raises(ServiceValidationError, match=str(CHARGE_MIN_DURATION_MIN)),
    ):
        await hass.services.async_call(
            DOMAIN, SERVICIO,
            {"entity_id": DESTINO, "hora_inicio": time(2, 0), "duracion": 30},
            blocking=True,
        )

    enviar.assert_not_awaited()


@pytest.mark.usefixtures("init_integration")
async def test_un_destino_que_no_es_de_ebro_se_rechaza(hass: HomeAssistant) -> None:
    """Sin esto, una llamada mal apuntada no haría nada y parecería que funcionó."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICIO,
            {"entity_id": "switch.de_otra_integracion", "hora_inicio": time(2, 0),
             "duracion": 120},
            blocking=True,
        )


async def test_el_servicio_se_registra_una_sola_vez(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_core
) -> None:
    """Se registra en `async_setup` (componente) y no en `async_setup_entry` (por entrada): con
    dos coches configurados, lo segundo lo daría de alta dos veces."""
    from custom_components.ebro import async_setup

    await async_setup(hass, {})
    await async_setup(hass, {})

    assert hass.services.has_service(DOMAIN, SERVICIO)
