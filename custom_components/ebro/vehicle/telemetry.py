"""Telemetría del coche: qué campos existen y cómo se lee un mensaje MQTT.

Este módulo **no conoce Home Assistant**: es el mapa de la telemetría del vehículo y las
funciones puras que la interpretan. Vivía dentro del coordinator, donde la tabla de campos y
el parseo del payload estaban mezclados con locks, timers y creación de tareas — así que la
única forma de probar «¿un heartbeat de marcha cuenta como contacto con datos?» era levantar
un `hass` entero.

El parseo devuelve un `CarMessage`: qué tipo de mensaje es, qué campos de estado trae y si
merece refrescar Home Assistant. Las decisiones que dependen de estado vivo (locks, ráfaga,
timers) siguen en el coordinator, que es donde ese estado vive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from typing import Any

from ..const import MAX_STATUS_LEN, SERVICE_TYPE_POSITION
from ..helpers import truncate_status

_LOGGER = logging.getLogger(__name__)


# Mapa campo-del-coche → entidad.
# kind: open|onoff → binary_sensor ON si != 0 ; lock → 0=Bloqueado/1=Desbloqueado ; level → sensor 0-3
SENSORS = [
    {"key": "frontLeftDoor",  "name": "Puerta del. izquierda",  "comp": "binary_sensor", "dclass": "door",   "kind": "open"},
    {"key": "frontRightDoor", "name": "Puerta del. derecha",  "comp": "binary_sensor", "dclass": "door",   "kind": "open"},
    {"key": "backLeftDoor",   "name": "Puerta tras. izquierda", "comp": "binary_sensor", "dclass": "door",   "kind": "open"},
    {"key": "backRightDoor",  "name": "Puerta tras. derecha", "comp": "binary_sensor", "dclass": "door",   "kind": "open"},
    {"key": "trunkDoor",      "name": "Maletero",               "comp": "binary_sensor", "dclass": "opening","kind": "open"},
    {"key": "hood",           "name": "Capó",              "comp": "binary_sensor", "dclass": "opening","kind": "open"},
    {"key": "liftgateOperateState", "name": "Portón en movimiento", "comp": "binary_sensor", "dclass": "moving", "kind": "onoff"},
    {"key": "doorLock",       "name": "Cierre centralizado",           "comp": "sensor",        "dclass": None,     "kind": "lock"},
    {"key": "frontLeftWindowState",  "name": "Ventanilla del. izquierda",  "comp": "binary_sensor", "dclass": "window", "kind": "open"},
    {"key": "frontRightWindowState", "name": "Ventanilla del. derecha",  "comp": "binary_sensor", "dclass": "window", "kind": "open"},
    {"key": "backLeftWindowState",   "name": "Ventanilla tras. izquierda", "comp": "binary_sensor", "dclass": "window", "kind": "open"},
    {"key": "backRightWindowState",  "name": "Ventanilla tras. derecha", "comp": "binary_sensor", "dclass": "window", "kind": "open"},
    {"key": "sunroofState",   "name": "Techo solar",      "comp": "binary_sensor", "dclass": "window", "kind": "open"},
    {"key": "sunshadeState",  "name": "Cortina del techo",       "comp": "binary_sensor", "dclass": "window", "kind": "open", "diag": True},
    {"key": "frontHVACState", "name": "Climatización",               "comp": "binary_sensor", "dclass": "running","kind": "onoff"},
    {"key": "airPurification","name": "Purificación de aire",  "comp": "binary_sensor", "dclass": "running","kind": "onoff"},
    {"key": "frontWindshieldHeat", "name": "Desempañado del parabrisas", "comp": "binary_sensor", "dclass": "running", "kind": "onoff"},
    {"key": "fWinHeatingState","name": "Calefacción del parabrisas", "comp": "binary_sensor", "dclass": "running", "kind": "onoff", "diag": True},
    {"key": "rWinHeatingState","name": "Calefacción de la luneta",    "comp": "binary_sensor", "dclass": "running", "kind": "onoff"},
    {"key": "steerWheelHeating","name": "Calefacción del volante",   "comp": "binary_sensor", "dclass": "running", "kind": "onoff"},
    {"key": "dSeatHeatingState","name": "Calefacción asiento conductor",     "comp": "binary_sensor", "dclass": "running", "kind": "onoff"},
    {"key": "pSeatHeatingState","name": "Calefacción asiento pasajero","comp": "binary_sensor", "dclass": "running", "kind": "onoff"},
    {"key": "dSeatVentilateState","name": "Ventilación asiento conductor",      "comp": "binary_sensor", "dclass": "running", "kind": "onoff"},
    {"key": "pSeatVentilateState","name": "Ventilación asiento pasajero", "comp": "binary_sensor", "dclass": "running", "kind": "onoff"},
    {"key": "lSeatHeatingState2","name": "Calefacción asiento tras. izquierdo",      "comp": "sensor", "dclass": None, "kind": "level", "icon": "mdi:car-seat-heater"},
    {"key": "rSeatHeatingState2","name": "Calefacción asiento tras. derecho",      "comp": "sensor", "dclass": None, "kind": "level", "icon": "mdi:car-seat-heater"},
    # NB: asiento trasero CENTRAL (mSeat*State2) eliminado: este coche no lo tiene
    # (sin calefacción/ventilación en la plaza central) → sensores siempre ausentes.
    {"key": "lSeatVentilateState2","name": "Ventilación asiento tras. izquierdo",       "comp": "sensor", "dclass": None, "kind": "level", "icon": "mdi:car-seat-cooler"},
    {"key": "rSeatVentilateState2","name": "Ventilación asiento tras. derecho",       "comp": "sensor", "dclass": None, "kind": "level", "icon": "mdi:car-seat-cooler"},
    # — telemetría adicional (campos ya enviados por el coche en el 5A02) —
    {"key": "chargeGunState", "name": "Cable de carga conectado",  "comp": "binary_sensor", "dclass": "plug",    "kind": "onoff"},
    {"key": "engineState",    "name": "Motor",          "comp": "binary_sensor", "dclass": "running", "kind": "onoff", "icon": "mdi:engine"},
    # sunroofMoveState = código de FASE de movimiento del techo (valores 1/2/3/4/8, nunca 0):
    # no es un on/off limpio (no hay un valor "parado" conocido) → sensor de diagnóstico en crudo.
    {"key": "sunroofMoveState", "name": "Estado del techo", "comp": "sensor", "dclass": None, "kind": "value", "icon": "mdi:car-select", "diag": True},
    # NB campos NO mapeados a propósito (verificado en events.jsonl reales, 2026-06-21):
    #   rangeUnit / averageFuelUnit / tirePressureUnit valen SIEMPRE "1" = son FLAGS de unidad
    #   de medida, NO el valor. El coche no envía por este canal el valor real de autonomía/
    #   consumo/presión de neumáticos → mapearlos mostraría "1" fijo. Aplazados: hace falta el
    #   otro canal (realtime /asr/manager o estructura TPMS anidada).
]
META = {s["key"]: s for s in SENSORS}

# Meta-campos de los push de CONFIRMACIÓN de comando (110x/1105/1135…): NO son telemetría de
# estado del vehículo → no van entre los "fields".
CMD_CONFIRM_META = ("result", "resultTime", "seq", "reason", "hasAsy")

# [MED] Campos "geo" admitidos en la posición (push 1301 / sonda realtime). Se conserva
# SOLO la geolocalización: batería/velocidad/online viven en `data["realtime"]`.
GEO_KEYS = ("lat", "lon", "latitude", "longitude", "speed", "vehicleSpeed",
            "direction", "heading", "altitude", "gpsTime", "positionTime")

# Relojes presentes en el frame realtime. Doble uso, y por eso una sola definición:
#   * se IGNORAN al comparar dos lecturas (si no, dos respuestas con datos idénticos
#     parecerían siempre "cambiadas");
#   * son los candidatos, EN ESTE ORDEN, para fechar el primer frame tras el arranque.
# El orden importa para lo segundo, así que es una tupla y el conjunto se deriva de ella.
CLOCK_FIELDS = ("resultTime", "collectTime", "time", "updateTime")
_CLOCK_FIELDS = frozenset(CLOCK_FIELDS)

# El coche emite un push con SOLO este campo cada pocos segundos mientras circula: es un
# latido, no telemetría. Ver `CarMessage.meaningful`.
_HEARTBEAT_FIELD = "time"


def is_unit_flag(key: str) -> bool:
    """True si el campo es un FLAG DE UNIDAD DE MEDIDA, no un valor.

    Convención de la API Chery: los campos que terminan en `Unit` (`rangeUnit`,
    `averageFuelUnit`, `tirePressureUnit`, `avgHkPowerUnit`…) valen siempre 1 o 2 y dicen solo
    *en qué unidad* está expresado el campo homónimo — no contienen un dato. El
    auto-descubrimiento de los campos no mapeados (monitor de diagnóstico) los señalaba como
    "por mapear": ruido puro, que ocultaba los posibles campos REALES aún por descubrir.
    Filtrarlos por sufijo cubre también los que aparecieran en el futuro."""
    return key.endswith("Unit")


def geo_only(data: dict) -> dict:
    """Los campos de geolocalización de un frame, y solo esos.

    Se usa tanto para el push de posición (1301) como para la respuesta de la sonda realtime.
    Filtrar por `GEO_KEYS` en vez de guardar `**data` es lo que mantiene batería/velocidad
    fuera de la posición (viven en `data["realtime"]`)."""
    return {k: data[k] for k in GEO_KEYS if k in data}


def _without_clocks(obj: Any) -> Any:
    """Copia de la telemetría sin los campos-reloj, a cualquier profundidad."""
    if isinstance(obj, dict):
        return {k: _without_clocks(v) for k, v in obj.items() if k not in _CLOCK_FIELDS}
    if isinstance(obj, list):
        return [_without_clocks(v) for v in obj]
    return obj


def content_fingerprint(data: dict) -> str:
    """Huella del CONTENIDO de la telemetría: cambia solo si cambia un dato real.

    Es lo que permite fechar «datos del coche actualizados» por el contenido y no por el
    `resultTime` que manda el backend — medido el 22/07 que ese reloj se queda parado
    mientras los valores sí cambian, o sea justo lo contrario de lo que el sensor promete."""
    return hashlib.sha256(
        json.dumps(_without_clocks(data), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def format_command_result(data: dict) -> str:
    """Traduce el resultado REAL de un push de confirmación de comando en una frase para el
    usuario.

    Distinto del "aceptado" del backend (A00079, mostrado al enviar): aquí es lo que el
    COCHE reporta tras intentar ejecutar. Interpretación CONSERVADORA, basada en los datos
    reales (events.jsonl 2026-06-21) y en el significado del bean:
      - `reason` (lista) solo se rellena en los fallos → si está presente = NO conseguido;
      - `result`: 5 = operación asíncrona aún en curso (siempre con hasAsy=1);
        1/2 = ejecutado/aplicado (estado del vehículo actualizado);
      - códigos distintos → reportados en crudo, sin inventar su significado."""
    # Import local: `const` ya está importado arriba, pero estos dos nombres solo se usan aquí
    # y se leen mejor al lado de la tabla que documentan.
    from ..const import CMD_RESULT_APPLIED, CMD_RESULT_ASYNC_RUNNING

    result = str(data.get("result", "")).strip()
    reason = data.get("reason")
    if reason:  # lista de motivos de fallo señalados por el coche
        return truncate_status(f"El coche ha señalado un problema ❌ ({reason})")
    if result == CMD_RESULT_ASYNC_RUNNING:
        return "Comando en ejecución en el coche… ⏳"
    if result in CMD_RESULT_APPLIED:
        return "Comando ejecutado y confirmado por el coche ✅"
    return truncate_status(f"Confirmación recibida del coche (código {result or '?'})")


@dataclass(frozen=True)
class CarMessage:
    """Un mensaje MQTT del coche, ya interpretado.

    Separar el QUÉ dice el mensaje del QUÉ hacemos con él es lo que permite probar las reglas
    de interpretación (¿esto es una confirmación?, ¿trae datos o es un latido?) sin levantar
    Home Assistant.
    """

    service_type: str
    #: payload en crudo tal como llegó (lo consume el auto-descubrimiento del monitor).
    data: dict[str, Any] = field(default_factory=dict)
    #: campos de ESTADO del vehículo, ya como cadenas y sin los meta-campos de confirmación.
    state_fields: dict[str, str] = field(default_factory=dict)
    #: geolocalización, solo si este mensaje es un reporte de posición.
    geo: dict[str, Any] = field(default_factory=dict)

    @property
    def is_confirmation(self) -> bool:
        """True si es la respuesta del coche a un comando (110x/1105/1135…).

        Se reconoce por la presencia de `result`/`seq`: la telemetría 5A02 "pura" no los trae.
        """
        return "result" in self.data or "seq" in self.data

    @property
    def meaningful(self) -> bool:
        """True si trae datos reales; False si es el latido de marcha (solo `time`).

        Mientras circula, el coche emite un push de solo `time` cada pocos segundos. Ese
        latido no debe mover «Último contacto» ni escribir en el recorder durante todo el
        viaje — pero sí cuenta para detectar la marcha y el estado «despierto», que se miden
        sobre las marcas de tiempo de los mensajes, no sobre su contenido."""
        return any(k != _HEARTBEAT_FIELD for k in self.data)


def parse_car_message(payload: bytes | str) -> CarMessage | None:
    """Decodifica el payload MQTT del coche. `None` si no es interpretable.

    El envoltorio varía: algunos mensajes traen los datos bajo `content.data` y otros son ya
    el propio contenido. Se normaliza aquí para que el resto del componente vea siempre la
    misma forma."""
    try:
        obj = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
    except Exception as err:
        _LOGGER.debug("[auto] payload MQTT no decodificable: %s", err)
        return None

    content = obj.get("content", obj) if isinstance(obj, dict) else {}
    data = content.get("data", {}) if isinstance(content, dict) else {}
    if not isinstance(data, dict):
        data = {}
    service_type = str(content.get("serviceType")) if isinstance(content, dict) else ""

    # Los campos de ESTADO: sin el latido y sin los meta-campos de confirmación, que
    # describen el comando y no el vehículo.
    state_fields = {
        k: str(v)
        for k, v in data.items()
        if k != _HEARTBEAT_FIELD and k not in CMD_CONFIRM_META
    }

    # La posición se discrimina por el TIPO de mensaje, no por la mera presencia de lat/lon:
    # un 5A02 podría traerlas y no sería un reporte de posición.
    geo = (
        geo_only(data)
        if service_type == SERVICE_TYPE_POSITION and "lat" in data and "lon" in data
        else {}
    )
    return CarMessage(service_type=service_type, data=data, state_fields=state_fields, geo=geo)


def unknown_fields(message: CarMessage) -> list[tuple[str, Any]]:
    """Campos del 5A02 que el coche manda pero que `META` no mapea.

    Es el canal para descubrir la telemetría aún ausente (autonomía/TPMS). Se limita al 5A02
    a propósito: comparar con `META` los campos de un push de POSICIÓN señalaba `lat`/`lon`
    como "no mapeados" — falsos positivos, y peor, así una coordenada acababa registrada como
    muestra (fuga vista en campo el 2026-07-20). `GEO_KEYS` queda excluido igualmente, por si
    el coche metiera la posición dentro de un 5A02."""
    from ..const import SERVICE_TYPE_TELEMETRY

    if message.service_type != SERVICE_TYPE_TELEMETRY:
        return []
    return [
        (k, v)
        for k, v in message.data.items()
        if k != _HEARTBEAT_FIELD
        and k not in CMD_CONFIRM_META
        and k not in META
        and k not in GEO_KEYS
        and not is_unit_flag(k)
    ]


# Reexportado para que quien importe el módulo no tenga que ir a `const` a por el tope de
# longitud cuando compone mensajes de estado.
__all__ = [
    "CLOCK_FIELDS",
    "CMD_CONFIRM_META",
    "GEO_KEYS",
    "MAX_STATUS_LEN",
    "META",
    "SENSORS",
    "CarMessage",
    "content_fingerprint",
    "format_command_result",
    "geo_only",
    "is_unit_flag",
    "parse_car_message",
    "unknown_fields",
]
