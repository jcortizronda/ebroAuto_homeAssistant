"""Binary sensor: puertas/ventanillas/capó/maletero (open) + confort on/off + estado del coche.

El estado físico del coche (5A02) y la conectividad están en memoria en el coordinator →
tras un reinicio de HA vuelven a `unknown`. Los binary_sensor de estado son RestoreEntity:
restauran el último on/off conocido como respaldo hasta que llega un dato en vivo. Excepción:
«Coche despierto» NO persiste (es un flag derivado "el coche está publicando ahora" → al
arrancar debe estar off).
"""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import FIELDS_AS_RICH_ENTITY
from .entity import EbroEntity, EbroRestoreStateMixin
from .helpers import field, field_on, realtime
from .models import EbroConfigEntry
from .vehicle.telemetry import SENSORS


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    ents = [
        EbroBinarySensor(coord, s)
        for s in SENSORS
        if s["comp"] == "binary_sensor" and s["key"] not in FIELDS_AS_RICH_ENTITY
    ]
    ents.append(EbroOnline(coord))
    ents.append(EbroAwake(coord))
    ents.append(EbroSession(coord))
    # — avisos del canal realtime: neumáticos + batería baja —
    ents += [EbroRealtimeBinary(coord, spec) for spec in _RT_BINARIES]
    add(ents)


@dataclass(frozen=True)
class _RtBinarySpec:
    """Spec de un aviso del canal realtime.

    Campos con nombre en vez de una tupla de cuatro posiciones: leyendo la tabla ya no hay que
    contar cuál de las dos cadenas del medio era el nombre visible y cuál el campo del coche."""

    suffix: str        # sufijo del unique_id (estable): f"{vin}_rt_<suffix>"
    name: str          # nombre → "Ebro <name>"
    field: str         # clave en el dict realtime
    device_class: BinarySensorDeviceClass


# Avisos (warning) presentes en el canal realtime: ON = anomalía. `*TyreCall` = aviso de
# presión de neumático (device_class PROBLEM); `socLowCall` = batería de tracción baja
# (device_class BATTERY → on = "low"). Convención ON/OFF (1=aviso) por confirmar en vivo.
_RT_BINARIES = [
    _RtBinarySpec("aviso_neumatico_del_izq", "Aviso neumático del. izquierdo", "lFrontTyreCall", BinarySensorDeviceClass.PROBLEM),
    _RtBinarySpec("aviso_neumatico_del_der", "Aviso neumático del. derecho", "rFrontTyreCall", BinarySensorDeviceClass.PROBLEM),
    _RtBinarySpec("aviso_neumatico_tras_izq", "Aviso neumático tras. izquierdo", "lRearTyreCall", BinarySensorDeviceClass.PROBLEM),
    _RtBinarySpec("aviso_neumatico_tras_der", "Aviso neumático tras. derecho", "rRearTyreCall", BinarySensorDeviceClass.PROBLEM),
    _RtBinarySpec("bateria_baja", "Batería baja", "socLowCall", BinarySensorDeviceClass.BATTERY),
    # — 4 campos VERIFICADOS por la captura en vivo del payload realtime (2026-06-25, 91 campos:
    #   todos presentes, ="0" en reposo). Sustituyen a los campos del bean SDK que la captura
    #   demostró que este coche NO envía (eliminados). —
    # oilCall = aviso de combustible bajo; electricityCall = aviso de carga necesaria:
    # device_class PROBLEM (on = aviso). Polaridad 1=aviso por confirmar cuando salten.
    _RtBinarySpec("aviso_combustible_bajo", "Aviso de combustible bajo", "oilCall", BinarySensorDeviceClass.PROBLEM),
    _RtBinarySpec("aviso_carga", "Aviso de carga necesaria", "electricityCall", BinarySensorDeviceClass.PROBLEM),
    # hVoltageState = sistema de alta tensión activo. device_class RUNNING (on = en funcionamiento).
    # En reposo 0 (verificado en vivo). NB: engineState NO está aquí — ya existe el binary_sensor
    # "Motor" (de SENSORS/5A02, con historial en el recorder); el duplicado realtime "Motor
    # encendido" se eliminó en la v1.5.25 (leía el mismo campo engineState).
    _RtBinarySpec("alta_tension", "Alta tensión activa", "hVoltageState", BinarySensorDeviceClass.RUNNING),
]


class _EbroRestoreBinary(EbroRestoreStateMixin, EbroEntity, BinarySensorEntity, RestoreEntity):
    """Binary sensor que restaura el último estado on/off al reiniciar HA.

    Las subclases proporcionan `_live_is_on()` (estado actual del coordinator, o None si está
    ausente); mientras el live sea None se usa el último valor restaurado."""

    def __init__(self, coord, name: str, unique_suffix: str) -> None:
        super().__init__(coord, name, unique_suffix, entity_id_format=ENTITY_ID_FORMAT)

    def _live_is_on(self) -> bool | None:
        raise NotImplementedError

    @property
    def is_on(self) -> bool | None:
        return self._restored_or(self._live_is_on())


class EbroBinarySensor(_EbroRestoreBinary):
    """ON si el campo es != 0 (open/onoff)."""

    def __init__(self, coord, spec: dict) -> None:
        super().__init__(coord, f"Ebro {spec['name']}", spec["key"])
        self._key = spec["key"]
        dc = spec.get("dclass")
        self._attr_device_class = BinarySensorDeviceClass(dc) if dc else None
        # campos que el coche nunca envía en reposo (ej. cortina del techo, calef. parabrisas):
        # se quedan siempre "unknown" → en categoría diagnóstico, fuera de los controles principales.
        if spec.get("diag"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _live_is_on(self) -> bool | None:
        # [MED] None/"None"/"" = ausente → None (emerge el restored, no un falso off);
        # comparación numérica vía field_on (alinea "0.0" con lock/switch/cover).
        return field_on(field(self.coordinator.data, self._key))


class EbroOnline(_EbroRestoreBinary):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Conexión", "online")
        # translation_key forzado a "conexion" para coincidir con la clave en translations/*.json
        # (si no, la base la derivaría de "Conexión" → "conexion" igualmente, pero lo fijamos
        # explícito por claridad). entity_id: esquema ebro_<vin4>_<descriptor castellano>.
        vin4 = (coord.vin or "")[-4:]
        self.entity_id = ENTITY_ID_FORMAT.format(f"ebro_{vin4}_conexion")
        self._attr_translation_key = "conexion"

    def _live_is_on(self) -> bool | None:
        rt = realtime(self.coordinator.data)
        return field_on(rt["onlineStatus"]) if "onlineStatus" in rt else None


class EbroRealtimeBinary(_EbroRestoreBinary):
    """Aviso genérico sobre un campo del canal realtime (ver `_RT_BINARIES`).

    Mismo patrón que `EbroOnline` (lee de coordinator.data["realtime"]) pero en categoría
    diagnóstico. ON si el campo es != 0; ausente → restaura el último conocido."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, spec: _RtBinarySpec) -> None:
        super().__init__(coord, f"Ebro {spec.name}", f"rt_{spec.suffix}")
        self._field = spec.field
        self._attr_device_class = spec.device_class

    def _live_is_on(self) -> bool | None:
        rt = realtime(self.coordinator.data)
        return field_on(rt[self._field]) if self._field in rt else None


class EbroAwake(EbroEntity, BinarySensorEntity):
    """Flag derivado "el coche está publicando ahora" — NO persistente (off al arrancar)."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Coche despierto", "awake",
                         entity_id_format=ENTITY_ID_FORMAT)

    @property
    def is_on(self) -> bool:
        # Se pregunta al coordinator el estado REAL (tiempo transcurrido desde el último
        # mensaje) en vez de leer el flag memorizado: así el sensor es correcto también en la
        # ventana entre el vencimiento y el timer que actualiza el flag.
        return self.coordinator.is_awake


class EbroSession(_EbroRestoreBinary):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Sesión", "session")

    def _live_is_on(self) -> bool | None:
        return self.coordinator.data.get("session_ok")
