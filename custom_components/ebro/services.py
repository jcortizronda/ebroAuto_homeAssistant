"""El servicio `ebro.programar_carga`.

**Por qué un servicio y no más entidades.** Programar la carga son hoy tres pasos: fijar la
hora, fijar la duración y pulsar «Aplicar». Las dos entidades de hora guardan una preferencia y
NO mandan nada por su cuenta, y eso es deliberado — en esta integración los números y las horas
guardan, y solo los botones e interruptores actúan, para que mover un control por accidente no
le dé órdenes al coche. Ver el docstring de `time.py`.

Esa separación está bien para la interfaz y es incómoda para una automatización, que tendría que
encadenar tres llamadas y confiar en el orden. Un servicio es un acto explícito igual que un
botón, así que encaja con el mismo principio y resuelve el caso de uso: «carga a las 2:00 durante
4 horas cuando la luz esté barata», en una sola llamada atómica.

Parar la carga NO está aquí: no lleva parámetros, así que es un botón (`button.py`). Un servicio
sin campos no se puede poner en una tarjeta ni sale en la página del dispositivo.
"""
from __future__ import annotations

from datetime import time as dt_time
from typing import TYPE_CHECKING

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import voluptuous as vol

from .const import CHARGE_MIN_DURATION_MIN, DOMAIN

if TYPE_CHECKING:
    from .vehicle.coordinator import EbroCoordinator

SERVICE_SCHEDULE_CHARGE = "programar_carga"

ATTR_START = "hora_inicio"
ATTR_DURATION = "duracion"
ATTR_ENABLE = "activar"

#: `cv.time` no: el servicio puede llamarse desde YAML (cadena "02:00:00") o desde la interfaz
#: (objeto `time`), y vol.Any acepta las dos sin obligar al llamador a conocer la diferencia.
_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): vol.Any(str, [str]),
    vol.Required(ATTR_START): vol.Any(dt_time, str),
    vol.Required(ATTR_DURATION): vol.Coerce(int),
    vol.Optional(ATTR_ENABLE, default=True): vol.Coerce(bool),
})


def _minutos(valor) -> int:
    """Hora (objeto o "HH:MM[:SS]") → minutos desde medianoche."""
    if isinstance(valor, dt_time):
        return valor.hour * 60 + valor.minute
    partes = str(valor).split(":")
    return int(partes[0]) * 60 + int(partes[1] if len(partes) > 1 else 0)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Registra los servicios del dominio. Se llama UNA vez, al cargar el componente."""

    async def _programar_carga(call: ServiceCall) -> None:
        duracion = call.data[ATTR_DURATION]
        # El coche rechaza menos de una hora con un código críptico (89). `charging.build_plan`
        # lo sube en silencio, que está bien para la interfaz —donde el deslizador ya no deja
        # bajar— pero no para una llamada escrita a mano: ahí es mejor decir que no.
        if duracion < CHARGE_MIN_DURATION_MIN:
            raise ServiceValidationError(
                f"La duración mínima de carga que acepta el coche es de "
                f"{CHARGE_MIN_DURATION_MIN} minutos; se pidieron {duracion}.",
                translation_domain=DOMAIN,
                translation_key="charge_duration_too_short",
                translation_placeholders={"minimo": str(CHARGE_MIN_DURATION_MIN),
                                          "pedido": str(duracion)})

        for coordinator in _coordinadores(hass, call):
            coordinator.preferences.charge_start_minutes = _minutos(call.data[ATTR_START])
            coordinator.preferences.charge_duration_minutes = duracion
            await coordinator.async_apply_charge_schedule(bool(call.data[ATTR_ENABLE]))

    hass.services.async_register(
        DOMAIN, SERVICE_SCHEDULE_CHARGE, _programar_carga, schema=_SCHEMA)


def _coordinadores(hass: HomeAssistant, call: ServiceCall) -> list[EbroCoordinator]:
    """Los coordinators de las entidades apuntadas por `target`.

    El servicio apunta a una ENTIDAD y no al dominio entero: con dos coches configurados, una
    llamada sin destino los programaría los dos. Home Assistant ya sabe resolver el destino —
    aquí solo se traduce de entidad a su entrada de configuración."""
    registro = er.async_get(hass)
    ids = call.data[ATTR_ENTITY_ID]
    encontrados: list[EbroCoordinator] = []
    vistos: set[str] = set()
    for entity_id in ([ids] if isinstance(ids, str) else ids):
        entrada = registro.async_get(entity_id)
        if entrada is None or entrada.platform != DOMAIN or entrada.config_entry_id in vistos:
            continue
        config_entry = hass.config_entries.async_get_entry(entrada.config_entry_id)
        coordinator = getattr(config_entry, "runtime_data", None) if config_entry else None
        if coordinator is not None:
            vistos.add(entrada.config_entry_id)
            encontrados.append(coordinator)
    if not encontrados:
        raise ServiceValidationError(
            "El destino no corresponde a ningún vehículo Ebro cargado.",
            translation_domain=DOMAIN, translation_key="no_vehicle_target")
    return encontrados
