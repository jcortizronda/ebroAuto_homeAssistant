"""Tipos compartidos del componente.

`EbroConfigEntry` es un `ConfigEntry` que sabe qué lleva dentro. Antes el coordinator vivía en
`hass.data[DOMAIN][entry.entry_id]`, lo que obligaba a diecinueve accesos con `.get()`
defensivo repartidos por las diez plataformas, `diagnostics`, `repairs` y el config flow — y
ninguno de ellos tenía tipo: el editor no sabía que aquello era un `EbroCoordinator`.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from .const import (
    DEFAULT_CHARGE_DURATION_MIN,
    DEFAULT_CHARGE_START_MIN,
    DEFAULT_CLIMA_DURATION_MIN,
)

if TYPE_CHECKING:
    from .vehicle.coordinator import EbroCoordinator

#: El config entry de Ebro Auto, con su coordinator en `entry.runtime_data`.
type EbroConfigEntry = ConfigEntry["EbroCoordinator"]


@dataclass
class ChargePreferences:
    """Preferencias locales que fijan las entidades `time` y `number`.

    Estas entidades NO mandan nada al coche por sí solas: guardan lo que otros controles usan
    en el momento del envío. El contrato era implícito y frágil — cada spec llevaba el nombre
    del atributo como CADENA y la entidad hacía `setattr(coordinator, ese_nombre, valor)`, así
    que una errata creaba un atributo nuevo en silencio y el valor no llegaba a su destino
    nunca. Aquí los nombres son campos de verdad, y `field_names()` permite validarlos al
    construir la entidad en vez de descubrirlo en producción.
    """

    #: duración `times` del comando airControl (entidad climate)
    clima_duration: int = DEFAULT_CLIMA_DURATION_MIN
    #: hora de inicio de la carga programada, en minutos desde medianoche (hora local)
    charge_start_minutes: int = DEFAULT_CHARGE_START_MIN
    #: duración de la carga programada, en minutos
    charge_duration_minutes: int = DEFAULT_CHARGE_DURATION_MIN

    @classmethod
    def field_names(cls) -> frozenset[str]:
        return frozenset(f.name for f in fields(cls))


def preference_target(coordinator, name: str):
    """El objeto que POSEE la preferencia `name`. Falla al construir la entidad si no existe.

    Hay dos destinos legítimos y conviene no confundirlos:

    * `coordinator.preferences` — valores que solo se guardan hasta que otro control los usa
      (hora y duración de la carga, duración del clima);
    * el propio `coordinator` — valores que además tienen lógica detrás, como
      `charge_limit_soc`, que es una propiedad del `ChargeLimiter`.

    El nombre viaja como cadena en las tablas de specs de `number.py` y `time.py`. Sin esta
    comprobación, una errata creaba un atributo nuevo en silencio y el valor que el usuario
    fija no llegaba nunca a su destino — un fallo mudo, que es el peor tipo.

    Vive aquí y no en las dos plataformas porque estaba COPIADA en ambas, docstring incluido,
    y porque lo que valida es el dataclass de arriba.
    """
    if name in ChargePreferences.field_names():
        return coordinator.preferences
    if hasattr(coordinator, name):
        return coordinator
    raise ValueError(
        f"«{name}» no es ni una preferencia ({sorted(ChargePreferences.field_names())}) "
        f"ni un atributo del coordinator")
