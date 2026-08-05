"""Time: horas de configuración locales (no comandos directos al coche).

Como los number de configuración, estas entidades NO envían nada al coche por sí solas:
guardan una preferencia que usan los demás controles en el momento del envío.
  - Carga programada · hora de inicio: la hora (HH:MM) a la que arrancar la carga.
  - Carga programada · duración: cuánto durar (HH:MM = horas y minutos, p. ej. 02:15).
El interruptor "Carga programada" (y el botón "Aplicar carga programada") componen el plan
`chargeAppointControl` usando ambos valores.

Por qué una entidad `time` y no un number: el coche acepta la hora/duración en MINUTOS (verificado
en vivo: startTime 465 = 07:45) → con un selector HH:MM se eligen horas Y minutos, mucho más claro
que un deslizador de minutos sueltos.

Es RestoreEntity → al reiniciar HA restaura la última hora fijada y la reescribe en el
coordinator (de donde el interruptor la lee como `charge_start_minutes`).
"""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import ENTITY_ID_FORMAT, TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CHARGE_MIN_DURATION_MIN,
    DEFAULT_CHARGE_DURATION_MIN,
    DEFAULT_CHARGE_START_MIN,
    DOMAIN,
)
from .entity import EbroEntity
from .models import EbroConfigEntry, preference_target

# (nombre, suffix, atributo-minutos en el coordinator, HH por defecto, MM por defecto, icono, min_min)
# Ambas se guardan como minutos-desde-medianoche (HH*60+MM). La HORA DE INICIO se convierte a UTC
# al enviar el plan (el backend la interpreta en UTC); la DURACIÓN es un delta (HH:MM = horas y
# minutos), NO se convierte. Selector HH:MM para poder poner p. ej. 02:15 = 135 min. `min_min` = mínimo
# aceptado (la duración de carga tiene mínimo 1 h: el coche rechaza menos con code 89).
TIMES = [
    ("Ebro Hora de inicio de la carga", "carga_hora_inicio",
     "charge_start_minutes", *divmod(DEFAULT_CHARGE_START_MIN, 60), "mdi:clock-start", 0),
    ("Ebro Duración de la carga", "carga_duracion",
     "charge_duration_minutes", *divmod(DEFAULT_CHARGE_DURATION_MIN, 60),
     "mdi:battery-clock", CHARGE_MIN_DURATION_MIN),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    add([EbroConfigTime(coord, *spec) for spec in TIMES])


class EbroConfigTime(EbroEntity, TimeEntity, RestoreEntity):
    """Selector de hora de configuración local: publica los minutos-desde-medianoche en el coordinator."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coord, name, suffix, attr, def_h, def_m, icon, min_min=0) -> None:
        super().__init__(coord, name, suffix, entity_id_format=ENTITY_ID_FORMAT)
        self._attr = attr
        self._target = preference_target(coord, attr)
        self._min_min = min_min
        self._attr_icon = icon
        self._value = self._as_time(self._clamp(def_h * 60 + def_m))
        self._push()   # el valor por defecto queda disponible antes de que HA añada la entidad

    def _clamp(self, total: int) -> int:
        """Fuerza el mínimo aceptado (p. ej. la duración de carga: mínimo 1 h)."""
        return max(self._min_min, int(total)) if self._min_min else int(total)

    @staticmethod
    def _as_time(total: int) -> time:
        return time(hour=(total // 60) % 24, minute=total % 60)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "", "unknown", "unavailable"):
            try:
                hh, mm, *_ = (int(p) for p in last.state.split(":"))
                self._value = self._as_time(self._clamp(hh * 60 + mm))
            except (ValueError, TypeError):
                pass
        self._push()

    def _push(self) -> None:
        setattr(self._target, self._attr, self._value.hour * 60 + self._value.minute)

    @property
    def native_value(self) -> time:
        return self._value

    async def async_set_value(self, value: time) -> None:
        # los segundos no hacen falta (el coche razona en minutos); se fuerza el mínimo si aplica
        requested = value.hour * 60 + value.minute
        clamped = self._clamp(requested)
        self._value = self._as_time(clamped)
        self._push()
        self.async_write_ha_state()
        # si el usuario intentó poner menos del mínimo, se ajustó solo → avísale para que lo note
        if self._min_min and requested < self._min_min:
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                self.hass,
                f"La duración mínima de carga que acepta el coche es de {self._min_min // 60} h. "
                f"Se ha ajustado automáticamente a {clamped // 60:02d}:{clamped % 60:02d}.",
                title="Ebro Auto — duración de carga",
                notification_id=f"{DOMAIN}_charge_duration_min")
