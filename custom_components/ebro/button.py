"""Button: los comandos del coche (catálogo core/commands) + despertar + actualizar posición."""
from __future__ import annotations

import functools
from typing import ClassVar

from homeassistant.components.button import ENTITY_ID_FORMAT, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COMMANDS_AS_RICH_ENTITY, DOMAIN
from .entity import EbroEntity
from .models import EbroConfigEntry


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
    # Parar la carga EN CURSO. No existía forma de hacerlo: apagar el interruptor de carga
    # programada solo desactiva la programación, no corta una carga ya empezada. Lo que sí la
    # corta vivía dentro del código y solo lo usaba el límite por software.
    #
    # Es un botón y no un servicio porque no lleva parámetros: así se pone en una tarjeta, sale
    # en la página del dispositivo, y una automatización lo pulsa igual con `button.press`.
    ents.append(EbroActionButton(coord, "Ebro Parar carga", "stop_charge",
                                    coord.async_stop_charge_via_schedule))
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
        """Manda el comando, y si falla lo DICE.

        Antes se tragaba la excepción y solo la escribía en el log, con el argumento de que el
        detalle ya salía en «Resultado del comando». Pero eso obliga a tener ese sensor a la
        vista para enterarse de que un botón no ha hecho nada, y deja fuera al resto de la
        integración: el candado, las persianas y el clima sí avisan (`entity._run_command`).
        Peor aún, el mensaje de la cola ocupada —escrito expresamente para el usuario— se
        genera ANTES de cualquier publicación, así que desde un botón no llegaba a ninguna parte.

        Un `HomeAssistantError` ya trae un texto pensado para leerse: se reenvía tal cual, porque
        envolverlo daría «Comando «cerrar» fallido: El coche sigue ocupado…». El resto se envuelve
        con el nombre del comando."""
        try:
            await self.coordinator.async_send_command(self._key)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise self._command_error(self._key, err) from err


class EbroActionButton(EbroEntity, ButtonEntity):
    """Botón para una acción del coordinator (despertar/sonda)."""

    _ICONS: ClassVar[dict[str, str]] = {
        "wake": "mdi:car-connected",
        "refresh_pos": "mdi:crosshairs-gps",
        "refresh_full": "mdi:car-info",
        "apply_charge_plan": "mdi:calendar-check",
        "stop_charge": "mdi:stop-circle-outline",
    }

    def __init__(self, coord, name: str, suffix: str, action, category=None) -> None:
        super().__init__(coord, name, suffix, entity_id_format=ENTITY_ID_FORMAT)
        self._action = action
        self._attr_icon = self._ICONS.get(suffix, "mdi:gesture-tap-button")
        if category is not None:
            self._attr_entity_category = category

    async def async_press(self) -> None:
        """Igual que `EbroCommandButton`, pero el mensaje habla del NOMBRE del botón.

        Estos botones no ejecutan un comando del catálogo sino una acción del coordinator, así
        que «Comando «wake» fallido» no le diría nada a nadie: el usuario ha pulsado «Despertar
        coche»."""
        try:
            await self._action()
        except HomeAssistantError:
            raise
        except Exception as err:
            nombre = self._raw_name.removeprefix("Ebro ")
            raise HomeAssistantError(
                f"«{nombre}» no se ha podido completar: {err}",
                translation_domain=DOMAIN,
                translation_key="action_failed",
                translation_placeholders={"name": nombre, "error": str(err)},
            ) from err
