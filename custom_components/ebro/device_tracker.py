"""Device tracker: posición GPS del coche (push 1301 / sonda realtime)."""
from __future__ import annotations

from homeassistant.components.device_tracker import (
    ENTITY_ID_FORMAT,
    SourceType,
    TrackerEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import EbroEntity
from .helpers import to_float
from .models import EbroConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    add([EbroTracker(coord)])


class EbroTracker(EbroEntity, TrackerEntity, RestoreEntity):
    """Posición GPS. La posición en vivo está en memoria en el coordinator (push 1301 /
    sonda realtime) → tras un reinicio de HA queda `unknown` hasta que se pulsa
    «Localizar»/«Actualizar ubicación». Para no perder la posición en el mapa, al arrancar
    se restaura el último fix conocido y se usa como respaldo hasta que llega un dato en vivo."""

    _attr_icon = "mdi:car"

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Ubicación", "position",
                         entity_id_format=ENTITY_ID_FORMAT)
        self._restored_lat: float | None = None
        self._restored_lon: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._restored_lat = to_float(last.attributes.get("latitude"))
            self._restored_lon = to_float(last.attributes.get("longitude"))

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        pos = self.coordinator.data.get("position") or {}
        live = to_float(pos.get("lat") or pos.get("latitude"))
        return live if live is not None else self._restored_lat

    @property
    def longitude(self) -> float | None:
        pos = self.coordinator.data.get("position") or {}
        live = to_float(pos.get("lon") or pos.get("longitude"))
        return live if live is not None else self._restored_lon
