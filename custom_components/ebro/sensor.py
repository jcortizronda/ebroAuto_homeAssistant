"""Sensor: cierre (lock), niveles de asiento (level) + batería/velocidad/sesión
+ sensores de diagnóstico (resultado de comando/despertar/sonda, timestamps).

Todos los sensores son RestoreSensor: al reiniciar HA restauran el último valor conocido
como respaldo hasta que llega un dato en vivo del coche.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    ENTITY_ID_FORMAT,
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import FIELDS_AS_RICH_ENTITY
from .entity import EbroEntity
from .helpers import field, realtime, realtime_field, to_float
from .models import EbroConfigEntry
from .vehicle.telemetry import SENSORS

# ───────────────────────── sensores "realtime" ─────────────────────────
# Campos del canal REST /asr/manager/realtime (en coordinator.data["realtime"]),
# actualizados al DESPERTAR el coche (sonda de solo lectura). A diferencia del 5A02 (MQTT),
# aquí SÍ están los VALORES reales de autonomía/odómetro/neumáticos/consumos/carga
# (en el 5A02 eran solo flags de unidad "1"). Validados 1:1 contra el bean oficial
# CVRealtimeResBean del SDK de Chery. Una tabla de specs evita 20+ clases gemelas.
#
# Valores/unidades CONFIRMADOS en vivo con el coche despierto (2026-06-21, 84 campos):
#   pureElectricRange=60 km · mileageSurplus=215 km (gasolina) · cruiseRange=134 (=215 km en millas)
#   lFrontTyreKpa=292 (=42 psi) → kPa · neumáticos temp 34-35 °C · *TyreCall/socLowCall=0=ok
#   oilSurplus=23 → LITROS (215−60=155 km a gasolina /23 L ≈ 15 L/100km) · averageFuel=0.0
#   avgHkPowerKwh50km=20.6 → kWh/100km · totalVoltage=323.1 V · totalCurrent=0.0 A (¡HV reales!)
#   remainChargeTime AUSENTE con el coche sin cargar (aparecerá durante la carga).

C = UnitOfTemperature.CELSIUS
KM = UnitOfLength.KILOMETERS
BAR = UnitOfPressure.BAR
VOLT = UnitOfElectricPotential.VOLT
AMP = UnitOfElectricCurrent.AMPERE
DIST = SensorDeviceClass.DISTANCE
TEMP = SensorDeviceClass.TEMPERATURE
PRESS = SensorDeviceClass.PRESSURE
VOLTAGE = SensorDeviceClass.VOLTAGE
CURRENT = SensorDeviceClass.CURRENT
MEAS = SensorStateClass.MEASUREMENT
TOTAL = SensorStateClass.TOTAL_INCREASING


@dataclass(frozen=True)
class _RtSpec:
    """Spec de un sensor realtime. `numeric=False` → valor en crudo (estado/enum)."""

    suffix: str           # sufijo del unique_id (estable): f"{vin}_rt_<suffix>"
    name: str             # nombre → "Ebro <name>" → entity_id slugificado
    field: str            # clave en el dict realtime
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    diag: bool = False
    numeric: bool = True
    vmap: dict | None = None  # código en crudo → texto legible (campos enum)
    invalid: tuple = ()       # valores (float) = marcador "sin lectura" (HV apagada) → conserva el último conocido
    compute: Callable | None = None  # valor CALCULADO desde varios campos realtime (ignora `field`)
    scale: float | None = None  # factor multiplicativo sobre el valor en crudo (ej. kPa→bar = 0.01)
    precision: int | None = None  # decimales sugeridos para la UI
    # VOLÁTIL: campo que tiene sentido SOLO en una condición precisa (ej. remainChargeTime
    # solo existe mientras el coche carga). Ausente = la condición terminó → el valor va a
    # "desconocido", NO se conserva el último. Sin este flag «Tiempo de carga restante»
    # se quedaba en 120 min durante horas con el coche desenchufado (engañoso).
    volatile: bool = False


# ── Mapas código→texto para los campos enum de carga ──
# Los valores son enum de 3 estados ("0"/"1"/"2", confirmados por las comparaciones en el
# código de la app `rus_car_state_model.dart`). `0` = estado en reposo verificado en vivo
# (coche aparcado sin cargar). La semántica de 1/2 sigue la convención EV; cualquier código
# no previsto queda legible como "Desconocido (N)" (ver EbroRealtimeSensor._live_value) →
# ninguna información perdida, ningún valor inventado.
CHARGE_STATE_MAP = {"0": "Sin cargar", "1": "Cargando", "2": "Carga completada"}
APPT_CHARGE_STATE_MAP = {"0": "Desactivada", "1": "Activa", "2": "En ejecución"}
FAST_GUN_MAP = {"0": "Desconectada", "1": "Conectada", "2": "Conectada (carga rápida)"}


def _fmt_charge_time(rt: dict):
    """Tiempo de carga restante legible: minutos si <1 h, "Xh Ymin" si es más. `remainChargeTime`
    viene en MINUTOS y solo existe mientras carga → ausente/0 = None (queda "desconocido")."""
    minutos = to_float(rt.get("remainChargeTime"))
    if minutos is None or minutos <= 0:
        return None
    m = int(minutos)
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h} h {mm} min" if mm else f"{h} h"


def _total_range(rt: dict):
    """Autonomía total REAL = eléctrica (`pureElectricRange`) + gasolina (`mileageSurplus`).
    None si falta una parte → queda el último valor conocido (RestoreSensor).

    ⚠️ NO usar `cruiseRange` como total: el volcado de la head unit lo daba por el combinado
    del cuadro (ID_DISPLAY_MILEAGE), pero es falso. Parecía "estático" porque no seguía la
    autonomía eléctrica; el 2026-07-22 se entendió por qué — es la autonomía a GASOLINA en
    millas (182 km/1,609 = 113 y 215 km/1,609 = 134, dos muestras con un mes de diferencia).
    Por tanto no contiene en absoluto la parte eléctrica. La suma eléctrica+gasolina sigue el
    estado de batería → es la elección correcta."""
    electrica = to_float(rt.get("pureElectricRange"))
    gasolina = to_float(rt.get("mileageSurplus"))
    if electrica is None or gasolina is None:
        return None
    return electrica + gasolina


_RT_SENSORS: list[_RtSpec] = [
    # ── P1 · autonomía / kilómetros (valores km confirmados en vivo) ──
    _RtSpec("autonomia_electrica", "Autonomía eléctrica", "pureElectricRange", device_class=DIST, unit=KM,
            state_class=MEAS, icon="mdi:map-marker-distance"),
    # mileageSurplus = autonomía a GASOLINA (motor térmico), NO el total: verificado en vivo
    # 2026-06-23 que se queda en 215 km mientras la autonomía eléctrica baja (60→27 km) y el
    # combustible no cambia (oilSurplus 23 L) → sigue el depósito, no la batería.
    _RtSpec("autonomia_gasolina", "Autonomía gasolina", "mileageSurplus", device_class=DIST, unit=KM,
            state_class=MEAS, icon="mdi:gas-station"),
    # Autonomía TOTAL = eléctrica + gasolina (campo CALCULADO). Ver _total_range.
    # (cruiseRange NO se usa como total: es la autonomía a gasolina en millas — ver abajo.)
    _RtSpec("autonomia_total", "Autonomía total", "", device_class=DIST, unit=KM, state_class=MEAS,
            icon="mdi:map-marker-distance", compute=_total_range),
    # NB: `cruiseRange` NO se expone: es `mileageSurplus` (autonomía a gasolina) en MILLAS, o sea
    # el MISMO dato que "Autonomía gasolina" en otra unidad → sensor duplicado (retirado).
    _RtSpec("odometro", "Cuentakilómetros", "odometer", device_class=DIST, unit=KM, state_class=TOTAL,
            icon="mdi:counter"),
    _RtSpec("km_hibrido", "Kilometraje híbrido", "hybridMileage", device_class=DIST, unit=KM, state_class=TOTAL,
            icon="mdi:counter", diag=True),
    # ── P1 · TPMS presión (campo del coche en kPa → mostrada en BAR como en la app: ÷100) ──
    _RtSpec("neumatico_del_izq_presion", "Presión neumático del. izquierdo", "lFrontTyreKpa", device_class=PRESS,
            unit=BAR, state_class=MEAS, icon="mdi:car-tire-alert", scale=0.01, precision=2),
    _RtSpec("neumatico_del_der_presion", "Presión neumático del. derecho", "rFrontTyreKpa", device_class=PRESS,
            unit=BAR, state_class=MEAS, icon="mdi:car-tire-alert", scale=0.01, precision=2),
    _RtSpec("neumatico_tras_izq_presion", "Presión neumático tras. izquierdo", "lRearTyreKpa", device_class=PRESS,
            unit=BAR, state_class=MEAS, icon="mdi:car-tire-alert", scale=0.01, precision=2),
    _RtSpec("neumatico_tras_der_presion", "Presión neumático tras. derecho", "rRearTyreKpa", device_class=PRESS,
            unit=BAR, state_class=MEAS, icon="mdi:car-tire-alert", scale=0.01, precision=2),
    # ── P1 · TPMS temperatura (°C, diagnóstico) ──
    _RtSpec("neumatico_del_izq_temp", "Temperatura neumático del. izquierdo", "lFrontTyreTemp", device_class=TEMP,
            unit=C, state_class=MEAS, icon="mdi:thermometer", diag=True),
    _RtSpec("neumatico_del_der_temp", "Temperatura neumático del. derecho", "rFrontTyreTemp", device_class=TEMP,
            unit=C, state_class=MEAS, icon="mdi:thermometer", diag=True),
    _RtSpec("neumatico_tras_izq_temp", "Temperatura neumático tras. izquierdo", "lRearTyreTemp", device_class=TEMP,
            unit=C, state_class=MEAS, icon="mdi:thermometer", diag=True),
    _RtSpec("neumatico_tras_der_temp", "Temperatura neumático tras. derecho", "rRearTyreTemp", device_class=TEMP,
            unit=C, state_class=MEAS, icon="mdi:thermometer", diag=True),
    # ── P2 · consumos y remanentes (unidades confirmadas en vivo) ──
    _RtSpec("consumo_combustible", "Consumo medio de combustible", "averageFuel", device_class=None, unit="L/100 km",
            state_class=MEAS, icon="mdi:gas-station"),
    # avgHkPowerKwh50km=20.6 en vivo → kWh/100km (el nombre "50km" es engañoso).
    # -100 = marcador "sin dato" con el coche parado (HV apagada) → conserva el último conocido.
    _RtSpec("consumo_electrico", "Consumo medio eléctrico", "avgHkPowerKwh50km", device_class=None,
            unit="kWh/100 km", state_class=MEAS, icon="mdi:lightning-bolt", invalid=(-100.0,)),
    # oilSurplus = nivel de combustible en PORCENTAJE (0-100), no litros (confirmado en vivo
    # comparándolo con el indicador del coche).
    _RtSpec("combustible_restante", "Combustible restante", "oilSurplus", device_class=None, unit=PERCENTAGE,
            state_class=MEAS, icon="mdi:fuel"),
    # ── P2 · batería de alta tensión (válidas SOLO con el coche en marcha/carga) ──
    # Parado, el coche manda 0 V / -1000 A = marcador "HV apagada", no lecturas reales:
    # marcados como inválidos → el sensor conserva el último valor conocido en vez de ponerse a cero.
    _RtSpec("tension_hv", "Tensión batería AT", "totalVoltage", device_class=VOLTAGE, unit=VOLT, state_class=MEAS,
            icon="mdi:flash", diag=True, invalid=(0.0,)),
    _RtSpec("corriente_hv", "Corriente batería AT", "totalCurrent", device_class=CURRENT, unit=AMP, state_class=MEAS,
            icon="mdi:current-dc", diag=True, invalid=(-1000.0,)),
    # ── P2 · carga ──
    # remainChargeTime: AUSENTE con el coche sin cargar (aparecerá durante la carga). Se asume
    # en MINUTOS — pendiente de reconfirmar con el coche cargando. chargeState/
    # appointmentChargeState/fastChargingGunStatus = códigos (todos 0 = en reposo en vivo) → en crudo.
    # tiempo restante formateado ("45 min" / "1 h 30 min"): texto calculado, no numérico → sin
    # device_class/unidad. Volátil: desaparece al dejar de cargar.
    _RtSpec("tiempo_carga", "Tiempo de carga restante", "remainChargeTime", device_class=None, unit=None,
            state_class=None, icon="mdi:timer-sand", volatile=True, compute=_fmt_charge_time),
    _RtSpec("estado_carga", "Estado de carga", "chargeState", device_class=None, unit=None, state_class=None,
            icon="mdi:ev-station", diag=True, numeric=False, vmap=CHARGE_STATE_MAP),
    _RtSpec("carga_programada_estado", "Estado de carga programada", "appointmentChargeState", device_class=None,
            unit=None, state_class=None, icon="mdi:calendar-clock", diag=True, numeric=False,
            vmap=APPT_CHARGE_STATE_MAP),
    _RtSpec("toma_rapida", "Puerto de carga rápida", "fastChargingGunStatus", device_class=None, unit=None,
            state_class=None, icon="mdi:ev-plug-ccs2", diag=True, numeric=False, vmap=FAST_GUN_MAP),
    # ── P2 · clima objetivo ──
    _RtSpec("temp_fijada_izq", "Temperatura fijada izquierda", "frontSetTempLeft", device_class=TEMP, unit=C,
            state_class=MEAS, icon="mdi:thermometer", diag=True),
    _RtSpec("temp_fijada_der", "Temperatura fijada derecha", "frontSetTempRight", device_class=TEMP, unit=C,
            state_class=MEAS, icon="mdi:thermometer", diag=True),
    # NB (2026-06-25): la captura en vivo del payload realtime (91 campos) DESMINTIÓ los campos
    # del bean SDK que NO envía este coche (inCarTemperature, a/bOdoMeter, chargingPower,
    # averageSpeed, insFuelConsum, fastRemainChargeTime, tmpParkCountdown): eliminados porque
    # se quedaban `unknown` para siempre. Las novedades verificadas (oilCall/electricityCall/
    # engineState/hVoltageState) son binary_sensor → ver binary_sensor.py.
]


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    ents: list = [
        EbroFieldSensor(coord, s)
        for s in SENSORS
        if s["comp"] == "sensor" and s["key"] not in FIELDS_AS_RICH_ENTITY
    ]
    ents.append(EbroBattery(coord))
    ents.append(EbroSpeed(coord))
    # — sensores "ricos" del canal realtime: autonomía, odómetro, neumáticos,
    #   consumos, carga, clima objetivo —
    ents += [EbroRealtimeSensor(coord, s) for s in _RT_SENSORS]
    ents.append(EbroSessionStatus(coord))
    # — sensores de diagnóstico —
    ents.append(EbroTextSensor(coord, "Ebro Resultado del comando", "cmd_status", "cmd_status", "mdi:car-cog"))
    ents.append(EbroTextSensor(coord, "Ebro Resultado del despertar", "wake_status", "wake_status", "mdi:car-connected"))
    ents.append(EbroTextSensor(coord, "Ebro Resultado sonda de ubicación", "probe_status", "probe_status", "mdi:crosshairs-gps"))
    ents.append(EbroTimestampSensor(coord, "Ebro Último contacto", "lastseen", "last_seen", "mdi:car-clock"))
    ents.append(EbroTimestampSensor(coord, "Ebro Último despertar", "wake_ts", "last_wake", "mdi:car-clock"))
    ents.append(EbroTimestampSensor(coord, "Ebro Última ubicación", "pos_fix", "last_pos_fix", "mdi:map-marker-clock"))
    # [2.0] frescura del frame del coche (resultTime del realtime): cómo de viejo es el dato de
    #       batería/odómetro mostrado — útil con el coche parado para saber si es reciente o rancio.
    ents.append(EbroTimestampSensor(coord, "Ebro Datos del coche actualizados", "car_data_ts", "car_data_ts", "mdi:database-clock"))
    add(ents)


class _EbroRestoreSensor(EbroEntity, RestoreSensor):
    """Base de sensor Ebro Auto que sobrevive al reinicio de HA.

    El estado (telemetría 5A02, realtime, diagnóstico) está en memoria en el coordinator
    → tras un reinicio vuelve a `unknown`. Aquí restauramos el último valor conocido y lo
    usamos como respaldo hasta que llega un dato en vivo. Las subclases proporcionan
    `_live_value()` (valor actual del coordinator, o None si está ausente)."""

    def __init__(self, coord, name: str, unique_suffix: str) -> None:
        super().__init__(coord, name, unique_suffix, entity_id_format=ENTITY_ID_FORMAT)
        self._restored = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None:
            self._restored = last.native_value

    def _live_value(self):
        """Subclases: valor actual del coordinator, o None si está ausente."""
        raise NotImplementedError

    @property
    def native_value(self):
        live = self._live_value()
        return live if live is not None else self._restored


class EbroFieldSensor(_EbroRestoreSensor):
    """cierre (0=Bloqueado/1=Desbloqueado), nivel de asiento (Nivel N) o valor en crudo."""

    def __init__(self, coord, spec: dict) -> None:
        super().__init__(coord, f"Ebro {spec['name']}", spec["key"])
        self._key = spec["key"]
        self._kind = spec["kind"]
        if spec.get("icon"):
            self._attr_icon = spec["icon"]
        # campos técnicos (ej. código de fase del techo) → entre los detalles de diagnóstico
        if spec.get("diag"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _live_value(self):
        v = field(self.coordinator.data, self._key)
        if v is None:
            return None
        if self._kind == "lock":
            return "Bloqueada" if str(v) in ("0", "0.0") else "Desbloqueada"
        if self._kind == "level":
            return f"Nivel {v}"
        return str(v)


class EbroBattery(_EbroRestoreSensor):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Batería", "battery")

    def _live_value(self):
        soc = to_float(realtime(self.coordinator.data).get("dumpEnergy"))
        # dumpEnergy=0 = marcador "alta tensión apagada" (coche parado), NO una carga real
        # del 0%: devuelve None para conservar el último SOC conocido (como la app oficial).
        if soc is None or soc <= 0:
            return None
        return soc

    @property
    def native_value(self):
        live = self._live_value()
        if live is not None:
            return live
        # no reproponer un "0%" rancio guardado antes del fix de los marcadores: 0 no es un
        # último-valor-conocido válido para la batería → mejor "desconocido" hasta que llegue
        # una lectura real (primer viaje/carga o botón "Actualizar estado completo").
        if self._restored in (0, 0.0, "0", "0.0"):
            return None
        return self._restored


class EbroSpeed(_EbroRestoreSensor):
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:speedometer"

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Velocidad", "speed")

    def _live_value(self):
        return to_float(realtime(self.coordinator.data).get("vehicleSpeed"))


class EbroRealtimeSensor(_EbroRestoreSensor):
    """Sensor genérico sobre un campo del canal realtime (ver `_RT_SENSORS`).

    Mismo patrón que `EbroBattery`/`EbroSpeed` pero paramétrico: device_class, unidad y
    state_class vienen de la spec. `numeric=True` convierte a float (None si no es parseable
    → emerge el último valor conocido del RestoreSensor); `numeric=False` conserva el valor
    en crudo (códigos de estado de carga por decodificar)."""

    def __init__(self, coord, spec: _RtSpec) -> None:
        super().__init__(coord, f"Ebro {spec.name}", f"rt_{spec.suffix}")
        self._spec = spec
        if spec.device_class:
            self._attr_device_class = spec.device_class
        if spec.unit:
            self._attr_native_unit_of_measurement = spec.unit
        if spec.state_class:
            self._attr_state_class = spec.state_class
        if spec.icon:
            self._attr_icon = spec.icon
        if spec.precision is not None:
            self._attr_suggested_display_precision = spec.precision
        if spec.diag:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        # VOLÁTIL: sin respaldo al último valor conocido. Si el campo está ausente ahora, su
        # condición terminó (ej. carga finalizada → remainChargeTime desaparece del payload) y
        # el valor correcto es "desconocido", no el último leído. Para los demás sensores vale
        # la regla normal del RestoreSensor (conserva el último conocido).
        if self._spec.volatile:
            return self._live_value()
        return super().native_value

    def _live_value(self):
        # campo CALCULADO desde varios campos realtime (ej. autonomía total = eléctrica + gasolina)
        if self._spec.compute is not None:
            val = self._spec.compute(realtime(self.coordinator.data))
            return None if (val is None or val in self._spec.invalid) else val
        raw = realtime_field(self.coordinator.data, self._spec.field)
        if raw is None:
            return None
        if self._spec.vmap is not None:
            key = raw[:-2] if raw.endswith(".0") else raw  # "0.0" → "0"
            return self._spec.vmap.get(key, f"Desconocido ({raw})")
        if not self._spec.numeric:
            return raw
        val = to_float(raw)
        if val is None:
            return None
        # marcador "sin lectura" (ej. HV apagada) → None ⇒ queda el último valor conocido
        if val in self._spec.invalid:
            return None
        return val * self._spec.scale if self._spec.scale is not None else val


class EbroSessionStatus(_EbroRestoreSensor):
    _attr_icon = "mdi:key-chain"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Estado de la sesión", "session_detail")

    def _live_value(self):
        return self.coordinator.data.get("session_detail") or None


class EbroTextSensor(_EbroRestoreSensor):
    """Sensor textual de diagnóstico (resultado del último comando/despertar/sonda)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, name: str, suffix: str, data_key: str, icon: str) -> None:
        super().__init__(coord, name, suffix)
        self._data_key = data_key
        self._attr_icon = icon

    def _live_value(self):
        return self.coordinator.data.get(self._data_key) or None

    @property
    def native_value(self):
        # [H9] los resultados de diagnóstico (comando/despertar/sonda) NO se restauran: un
        # resultado viejo tras un reinicio sería engañoso (parecería la última acción recién
        # ejecutada). Solo el valor en vivo; en su ausencia → unknown.
        return self._live_value()


class EbroTimestampSensor(_EbroRestoreSensor):
    """Timestamp de diagnóstico (último contacto/despertar/posición)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, name: str, suffix: str, data_key: str, icon: str) -> None:
        super().__init__(coord, name, suffix)
        self._data_key = data_key
        self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # [H10] device_class TIMESTAMP exige un datetime con tz: valida el valor restaurado
        # (cadena ISO → parse), si no None, para no emitir el warning de HA "Invalid datetime"
        # ni mostrar un timestamp malformado.
        r = self._restored
        if isinstance(r, str):
            r = dt_util.parse_datetime(r)
        if not (isinstance(r, datetime) and r.tzinfo is not None):
            r = None
        self._restored = r

    def _live_value(self):
        return self.coordinator.data.get(self._data_key)
