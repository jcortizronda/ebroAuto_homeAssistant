"""Cover: maletero, ventanillas, techo (estado de campos 5A02 + comandos abrir/cerrar).

Fusiona los estados de solo lectura (maletero trunkDoor, ventanillas, techo sunroofState)
con sus botones abrir/cerrar en entidades "persiana" nativas, con estado + acción en una
sola card. NB: las 4 ventanillas siguen también como binary_sensor sueltos (detalle "qué
ventanilla"); el cover "Ventanillas" es el comando agregado. La ventilación de ventanillas
queda como botón aparte (no mapeable en abrir/cerrar). Cada abrir/cerrar ACTÚA sobre el
coche (= consentimiento explícito del usuario).
"""
from __future__ import annotations

from homeassistant.components.cover import (
    ENTITY_ID_FORMAT,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import EbroEntity, EbroOptimisticMixin, EbroRestoreStateMixin
from .helpers import field, field_on
from .models import EbroConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    add([
        EbroCover(coord, "Ebro Maletero", "maletero", ["trunkDoor"],
                    "maletero_abrir", "maletero_cerrar", CoverDeviceClass.DOOR, "mdi:car-back"),
        EbroCover(coord, "Ebro Ventanillas", "ventanillas",
                    ["frontLeftWindowState", "frontRightWindowState",
                     "backLeftWindowState", "backRightWindowState"],
                    "ventanillas_abrir", "ventanillas_cerrar", CoverDeviceClass.WINDOW, "mdi:car-door"),
        EbroCover(coord, "Ebro Techo solar", "techo", ["sunroofState"],
                    "techo_abrir", "techo_cerrar", CoverDeviceClass.SHADE, "mdi:car-select"),
    ])


class EbroCover(EbroOptimisticMixin, EbroRestoreStateMixin, EbroEntity, CoverEntity, RestoreEntity):
    """Apertura motorizada: ABIERTA si al menos uno de los campos asociados es != 0.

    El estado real llega por MQTT solo con el coche despierto → tras un comando se muestra
    de inmediato el estado objetivo (optimista) y al reiniciar HA se restaura el último conocido."""

    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
    # `_restored` True = CERRADA (es lo que devuelve `is_closed`).
    _restore_states = ("closed", "open")

    def __init__(self, coord, name, suffix, keys, open_cmd, close_cmd, dclass, icon) -> None:
        super().__init__(coord, name, suffix, entity_id_format=ENTITY_ID_FORMAT)
        self._keys = keys
        self._open_cmd = open_cmd
        self._close_cmd = close_cmd
        self._attr_device_class = dclass
        self._attr_icon = icon

    def _live_closed(self) -> bool | None:
        # field_on por cada campo: None=ausente, True=abierto. Alinea "0.0" con el resto.
        # `field` mira MQTT y, si ahí no está, la sonda realtime: con el coche dormido esa es
        # la única fuente, y sin ella el maletero se quedaba congelado.
        states = [field_on(field(self.coordinator.data, k)) for k in self._keys]
        if all(s is None for s in states):
            return None  # ningún campo conocido → emerge restored/unknown
        return not any(states)  # al menos uno abierto → cover abierta

    @property
    def is_closed(self) -> bool | None:
        return self._resolved(self._live_closed())

    async def async_open_cover(self, **kwargs) -> None:
        await self._run_command(self._open_cmd, False)  # no cerrado = abierto

    async def async_close_cover(self, **kwargs) -> None:
        await self._run_command(self._close_cmd, True)
