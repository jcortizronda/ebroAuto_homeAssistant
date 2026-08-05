"""Climate: clima avanzado de Ebro Auto (preclimatización con temperatura ajustable).

Sustituye al viejo interruptor de clima a 21° fijo: ahora se fija la temperatura deseada
(16–30 °C) y el coche la aplica (calienta o enfría hasta el setpoint). Usa el comando
`airControl` (el mismo, verificado en vivo, que arrancaba el clima fijo), variando
`temperature` y la duración `times` (de number.ebro_clima_durata).

Modelo HA: una única entidad climate con modos OFF / HEAT_COOL (= el coche lleva el
habitáculo al setpoint calentando o enfriando) + un solo deslizador de temperatura. El
estado encendido/apagado llega de la telemetría `frontHVACState`; tras un comando se muestra
de inmediato el estado objetivo (optimista) hasta que llega un nuevo dato del coche.

Los asientos calefactados/ventilados y los desempañadores siguen siendo interruptores
separados (switch.py): así encender el clima NO toca el estado de los asientos.
"""
from __future__ import annotations

from typing import ClassVar

from homeassistant.components.climate import (
    ENTITY_ID_FORMAT,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DEFAULT_CLIMA_DURATION_MIN
from .entity import EbroEntity, EbroOptimisticMixin
from .helpers import field, field_on
from .models import EbroConfigEntry

MIN_TEMP = 16.0
MAX_TEMP = 30.0
DEFAULT_TEMP = 21.0


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    add([EbroClimate(coord)])


class EbroClimate(EbroOptimisticMixin, EbroEntity, ClimateEntity, RestoreEntity):
    """Clima del coche: ON (HEAT_COOL) al setpoint elegido / OFF, vía airControl."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = 1.0
    _attr_icon = "mdi:air-conditioner"
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(self, coord) -> None:
        # entity_id FIJADO por la clase base (esquema ebro_<vin4>_climatizacion); sin ello HA
        # lo derivaría "sucio" con el nombre del dispositivo.
        # unique_id distinto de cualquier viejo switch (suffix "climate") → entidad nueva.
        super().__init__(coord, "Ebro Climatización", "climate", entity_id_format=ENTITY_ID_FORMAT)
        self._target = DEFAULT_TEMP

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            t = last.attributes.get(ATTR_TEMPERATURE)
            try:
                if t is not None:
                    self._target = min(MAX_TEMP, max(MIN_TEMP, float(t)))
            except (TypeError, ValueError):
                pass

    # ── estado ──
    def _live_on(self) -> bool | None:
        return field_on(field(self.coordinator.data, "frontHVACState"))

    @property
    def target_temperature(self) -> float:
        return self._target

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.HEAT_COOL if self._optimistic_or(self._live_on()) else HVACMode.OFF

    # ── comandos ──
    def _params(self) -> dict:
        dur = int(self.coordinator.preferences.clima_duration or DEFAULT_CLIMA_DURATION_MIN)
        return {"temperature": f"{self._target:.1f}", "times": str(dur)}

    def _command_error(self, key: str, err: Exception) -> HomeAssistantError:
        """Mensaje propio: al usuario le dice algo «el clima», no la clave interna del comando."""
        return HomeAssistantError(f"Comando de clima fallido: {err}")

    async def _send(self, key: str, on: bool) -> None:
        await self._run_command(key, on, self._params())

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._send("clima_off", False)
        else:
            await self._send("clima_on", True)

    async def async_turn_on(self) -> None:
        await self._send("clima_on", True)

    async def async_turn_off(self) -> None:
        await self._send("clima_off", False)

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._target = min(MAX_TEMP, max(MIN_TEMP, float(temp)))
        # si el clima ya está encendido, reaplica de inmediato el nuevo setpoint; si no,
        # solo lo memoriza (se usará en el próximo encendido).
        if self.hvac_mode != HVACMode.OFF:
            await self._send("clima_on", True)
        else:
            self.async_write_ha_state()
