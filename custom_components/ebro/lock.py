"""Lock: cierre de puertas (estado del campo doorLock + comandos bloquear/desbloquear).

Fusiona en una sola entidad nativa lo que antes eran dos cosas separadas: el sensor
"Cierre" (solo lectura) y los dos botones Bloquear/Desbloquear. Pulsar bloquear/desbloquear
ACTÚA sobre el coche (= consentimiento explícito del usuario), como los antiguos botones.
"""
from __future__ import annotations

from homeassistant.components.lock import ENTITY_ID_FORMAT, LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import EbroEntity, EbroOptimisticMixin, EbroRestoreStateMixin
from .helpers import field, field_on
from .models import EbroConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    add([EbroLock(entry.runtime_data)])


class EbroLock(EbroOptimisticMixin, EbroRestoreStateMixin, EbroEntity, LockEntity, RestoreEntity):
    """Cierre del coche: 0=Bloqueado, 1=Desbloqueado (campo doorLock).

    El estado real llega por MQTT solo con el coche despierto → tras un comando se muestra
    de inmediato el estado objetivo (optimista, ver EbroOptimisticMixin) y al reiniciar HA
    se restaura el último estado conocido."""

    _attr_icon = "mdi:car-door-lock"
    _restore_states = ("locked", "unlocked")

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Cierre centralizado", "lock", entity_id_format=ENTITY_ID_FORMAT)

    def _live_locked(self) -> bool | None:
        # doorLock: 0 = Bloqueado, !=0 = Desbloqueado → locked = NOT field_on (alineado
        # con binary/switch/cover, "0.0" incluido). field_on None = campo ausente.
        on = field_on(field(self.coordinator.data, "doorLock"))
        return None if on is None else not on

    @property
    def is_locked(self) -> bool | None:
        return self._resolved(self._live_locked())

    async def async_lock(self, **kwargs) -> None:
        await self._run_command("bloquear", True)

    async def async_unlock(self, **kwargs) -> None:
        await self._run_command("desbloquear", False)
