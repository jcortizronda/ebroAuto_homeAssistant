"""Number: parámetros de configuración locales (no comandos directos al coche).

Estos deslizadores NO envían nada al coche por sí solos: guardan las preferencias que usan
los demás controles en el momento del envío.
  - Duración del clima (min): duración `times` del comando airControl de la entidad climate.

La duración de la carga programada ya NO está aquí: es una entidad `time` (HH:MM, ver time.py),
para poder ponerla en horas y minutos (p. ej. 02:15).

Son RestoreNumber → al reiniciar HA restauran el último valor fijado y lo reescriben en el
coordinator (de donde climate/switch lo leen).
"""
from __future__ import annotations

from homeassistant.components.number import (
    ENTITY_ID_FORMAT,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_CHARGE_LIMIT_SOC, DEFAULT_CLIMA_DURATION_MIN
from .entity import EbroEntity
from .models import EbroConfigEntry, preference_target

# (nombre, suffix, atributo en el coordinator, min, max, step, default, unidad, icono)
NUMBERS = [
    ("Ebro Duración de la climatización", "clima_duracion", "clima_duration",
     5, 15, 5, DEFAULT_CLIMA_DURATION_MIN, UnitOfTime.MINUTES, "mdi:timer-cog"),
    # Límite de carga por software (lo aplica el switch "Limitar carga al %" + el coordinator).
    ("Ebro Límite de carga", "carga_limite", "charge_limit_soc",
     50, 100, 5, DEFAULT_CHARGE_LIMIT_SOC, PERCENTAGE, "mdi:battery-charging-80"),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    add([EbroConfigNumber(coord, *spec) for spec in NUMBERS])


class EbroConfigNumber(EbroEntity, RestoreNumber):
    """Deslizador de configuración local: escribe su propio valor en el coordinator."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, name, suffix, attr, vmin, vmax, step, default, unit, icon) -> None:
        super().__init__(coord, name, suffix, entity_id_format=ENTITY_ID_FORMAT)
        self._attr = attr
        self._target = preference_target(coord, attr)
        self._attr_native_min_value = vmin
        self._attr_native_max_value = vmax
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._value = float(default)
        self._push()   # el valor por defecto queda disponible antes de que HA añada la entidad

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._value = float(last.native_value)
        self._push()

    def _push(self) -> None:
        # mantén el valor como int cuando es entero (horas/días), para que los body de comando
        # no acaben con "8.0" donde la app usa enteros.
        v = int(self._value) if float(self._value).is_integer() else self._value
        setattr(self._target, self._attr, v)

    @property
    def native_value(self) -> float:
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        self._value = float(value)
        self._push()
        self.async_write_ha_state()
