"""Button: los comandos del coche (catálogo core/commands) + despertar + actualizar posición."""
from __future__ import annotations

import functools
import logging
from typing import ClassVar

from homeassistant.components.button import ENTITY_ID_FORMAT, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COMMANDS_AS_RICH_ENTITY
from .entity import EbroEntity
from .models import EbroConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    from .core import catalog

    ents: list[ButtonEntity] = []
    for key, spec in catalog.COMMANDS:
        # los comandos que ahora tienen un lock/switch/cover dedicado NO se vuelven botones
        if key in COMMANDS_AS_RICH_ENTITY:
            continue
        ents.append(EbroCommandButton(coord, key, spec))
    ents.append(EbroActionButton(coord, "Ebro Despertar coche", "wake", coord.async_wake))
    # `force=True`: el cooldown de la sonda existe para frenar el BUCLE automático, no al
    # usuario. Sin esto, pulsar el botón dentro de los 2 minutos siguientes a la lectura
    # anterior no hacía absolutamente nada — y encima en silencio, sin publicar un solo
    # mensaje, así que parecía que el botón estuviera roto. Es además el único llamador que
    # no forzaba, cuando es el único que responde a una petición explícita de una persona.
    ents.append(EbroActionButton(coord, "Ebro Actualizar ubicación", "refresh_pos",
                                    functools.partial(coord.async_probe, force=True)))
    # Actualizar estado completo: fuerza odómetro/batería/tensión REALES encendiendo brevemente
    # el clima (única forma de encender la alta tensión, de la que dependen los datos frescos).
    ents.append(EbroActionButton(coord, "Ebro Actualizar estado completo", "refresh_full",
                                    coord.async_refresh_full_status))
    # Reenvía el plan de carga programada con la hora/duración actuales (tras cambiarlas hay que
    # reenviarlo: las entidades de hora y duración solo guardan la preferencia, no la mandan solas).
    ents.append(EbroActionButton(coord, "Ebro Aplicar carga programada", "apply_charge_plan",
                                    coord.async_apply_scheduled_charge))
    add(ents)


class EbroCommandButton(EbroEntity, ButtonEntity):
    """Un botón por comando del catálogo. El toque = consentimiento explícito de la ejecución."""

    def __init__(self, coord, key: str, spec: dict) -> None:
        # entity_id = button.ebro_<key>, NO derivado del nombre largo.
        super().__init__(coord, f"Ebro {spec['name']}", f"cmd_{key}",
                         object_id=f"ebro_{key}", entity_id_format=ENTITY_ID_FORMAT)
        self._key = key
        if spec.get("icon"):
            self._attr_icon = spec["icon"]

    async def async_press(self) -> None:
        # [LOW] el resultado (también de error) ya lo publica el coordinator en los sensores de
        # diagnóstico (cmd_status): aquí registramos sin propagar una excepción en crudo.
        try:
            await self.coordinator.async_send_command(self._key)
        except Exception:
            _LOGGER.exception("Ebro: comando «%s» fallido", self._key)


class EbroActionButton(EbroEntity, ButtonEntity):
    """Botón para una acción del coordinator (despertar/sonda)."""

    _ICONS: ClassVar[dict[str, str]] = {
        "wake": "mdi:car-connected",
        "refresh_pos": "mdi:crosshairs-gps",
        "refresh_full": "mdi:car-info",
        "apply_charge_plan": "mdi:calendar-check",
    }

    def __init__(self, coord, name: str, suffix: str, action, category=None) -> None:
        super().__init__(coord, name, suffix, entity_id_format=ENTITY_ID_FORMAT)
        self._action = action
        self._attr_icon = self._ICONS.get(suffix, "mdi:gesture-tap-button")
        if category is not None:
            self._attr_entity_category = category

    async def async_press(self) -> None:
        # [LOW] despertar/sonda: el resultado va a los sensores de diagnóstico; registra y no
        # propaga una excepción en crudo a la UI.
        try:
            await self._action()
        except Exception:
            _LOGGER.exception("Ebro: acción «%s» fallida", self._raw_name)
