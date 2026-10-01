"""Switch: confort (desempañadores, volante, asientos) + carga programada + alarma.

Cada interruptor fusiona el estado de solo lectura (un campo de telemetría 5A02) con los
dos comandos ON/OFF del catálogo en una sola card: ON = función activada (desempañadores/
asientos durante ~15 min con temporizador de autoapagado del coche), OFF = comando de
apagado manual. El toggle ACTÚA sobre el coche (= consentimiento explícito del usuario).

Los dos estados del asiento (calefacción / ventilación) son MUTUAMENTE EXCLUSIVOS del lado
del coche: encender la ventilación apaga el calor y viceversa (verificado en telemetría) →
lo reflejamos de inmediato también en el estado optimista, además de con los campos reales
cuando llegan.
"""
from __future__ import annotations

import ast

from homeassistant.components.switch import (
    ENTITY_ID_FORMAT,
    SwitchDeviceClass,
    SwitchEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import EbroEntity, EbroOptimisticMixin, EbroRestoreStateMixin
from .helpers import field, field_on
from .models import EbroConfigEntry

# Interruptores de confort: cada uno funde un campo de telemetría 5A02 con la pareja de
# comandos ON/OFF del catálogo. Los tres datos que cambian de uno a otro son el nombre, el
# campo y el comando — el resto era ruido: el `suffix` del unique_id SIEMPRE coincidía con el
# campo, y el comando de apagado SIEMPRE es el de encendido con el sufijo `_off`.
#
# (nombre visible, campo 5A02, comando base, icono)
_COMFORT: list[tuple[str, str, str, str]] = [
    ("Desempañado del parabrisas", "frontWindshieldHeat", "defrost_parabrisas",
     "mdi:car-defrost-front"),
    ("Calefacción de la luneta", "rWinHeatingState", "defrost_luneta", "mdi:car-defrost-rear"),
    ("Calefacción del volante", "steerWheelHeating", "volante_caliente", "mdi:steering"),
    # — asientos — la telemetría *State* se corresponde con el comando seatControl. El trasero
    #   CENTRAL queda fuera: el bean no tiene parámetro para él, así que no hay comando.
    ("Calefacción asiento conductor", "dSeatHeatingState", "asiento_conductor_caliente",
     "mdi:car-seat-heater"),
    ("Ventilación asiento conductor", "dSeatVentilateState", "asiento_conductor_ventilacion",
     "mdi:car-seat-cooler"),
    ("Calefacción asiento pasajero", "pSeatHeatingState", "asiento_pasajero_caliente",
     "mdi:car-seat-heater"),
    ("Ventilación asiento pasajero", "pSeatVentilateState", "asiento_pasajero_ventilacion",
     "mdi:car-seat-cooler"),
    ("Calefacción asiento tras. izquierdo", "lSeatHeatingState2", "asiento_tras_izq_caliente",
     "mdi:car-seat-heater"),
    ("Ventilación asiento tras. izquierdo", "lSeatVentilateState2",
     "asiento_tras_izq_ventilacion", "mdi:car-seat-cooler"),
    ("Calefacción asiento tras. derecho", "rSeatHeatingState2", "asiento_tras_der_caliente",
     "mdi:car-seat-heater"),
    ("Ventilación asiento tras. derecho", "rSeatVentilateState2",
     "asiento_tras_der_ventilacion", "mdi:car-seat-cooler"),
]

# Parejas calor/ventilación que se excluyen mutuamente: encender una apaga la otra del lado del
# coche (verificado en telemetría), así que el estado optimista debe reflejarlo ya. Se declaran
# por CAMPO, que es lo que identifica al interruptor sin depender del orden de la tabla.
_EXCLUSIVE_PAIRS = [
    ("dSeatHeatingState", "dSeatVentilateState"),
    ("pSeatHeatingState", "pSeatVentilateState"),
    ("lSeatHeatingState2", "lSeatVentilateState2"),
    ("rSeatHeatingState2", "rSeatVentilateState2"),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: EbroConfigEntry, add: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data
    # NB: el clima YA NO está aquí → es una entidad climate (climate.py) con temperatura
    # ajustable. La carga INMEDIATA tampoco: este coche responde A00084 «comando no permitido»
    # a chargeStartStopControl. La carga PROGRAMADA (chargeAppointControl) sí funciona.
    comfort = {field_name: EbroComfortSwitch(coord, name, field_name, command, icon)
               for name, field_name, command, icon in _COMFORT}
    for heat, vent in _EXCLUSIVE_PAIRS:
        comfort[heat].pair_with(comfort[vent])

    add([
        EbroScheduledChargeSwitch(coord),
        *comfort.values(),
        EbroTheftAlarmSwitch(coord),
        EbroPollingSwitch(coord),
        EbroChargeLimitSwitch(coord),
    ])


class EbroComfortSwitch(EbroOptimisticMixin, EbroRestoreStateMixin, EbroEntity, SwitchEntity,
                        RestoreEntity):
    """Interruptor de confort: ON si el campo 5A02 asociado es != 0.

    El estado real llega por MQTT solo con el coche despierto → tras un comando se muestra
    de inmediato el estado objetivo (optimista, ver EbroOptimisticMixin) y al reiniciar HA
    se restaura el último estado conocido."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coord, name: str, field_name: str, command: str, icon: str) -> None:
        # El sufijo del unique_id es el CAMPO: es estable y no depende del nombre visible.
        super().__init__(coord, f"Ebro {name}", field_name, entity_id_format=ENTITY_ID_FORMAT)
        self._field = field_name
        self._on_cmd = command
        self._off_cmd = f"{command}_off"   # convención del catálogo, sin excepciones
        self._attr_icon = icon
        self._exclusive: EbroComfortSwitch | None = None

    def pair_with(self, other: EbroComfortSwitch) -> None:
        """Declara la exclusión mutua con su gemelo, en los dos sentidos.

        Antes el `async_setup_entry` escribía `calor._exclusive = vent` desde fuera, tocando un
        atributo privado de otra entidad y teniendo que acordarse de hacerlo en ambos sentidos."""
        self._exclusive = other
        other._exclusive = self

    def _live_on(self) -> bool | None:
        return field_on(field(self.coordinator.data, self._field))

    @property
    def is_on(self) -> bool:
        # `bool(...)`: con el estado real aún desconocido (el coche no ha publicado todavía) el
        # valor por defecto es OFF, no None. Un `None` hace que HA dibuje dos botones
        # (encender/apagar) en vez del toggle único.
        return bool(self._resolved(self._live_on()))

    async def async_turn_on(self, **kwargs) -> None:
        # exclusión mutua: encender este apaga de inmediato el gemelo (ej. ventilación↔calor)
        if self._exclusive is not None:
            self._exclusive._set_optimistic(False)
        await self._run_command(self._on_cmd, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._run_command(self._off_cmd, False)


class EbroScheduledChargeSwitch(EbroOptimisticMixin, EbroRestoreStateMixin, EbroEntity,
                                SwitchEntity, RestoreEntity):
    """Carga PROGRAMADA on/off (chargeAppointControl, body con array anidado).

    Al encender, construye el plan a partir de las preferencias (entidad time "hora de inicio"
    + number "duración", todos los días) y envía mainSwitch=1 + plan activo; al apagar envía
    mainSwitch=0. startTime está en MINUTOS desde la medianoche (verificado en vivo: 465 =
    07:45). El estado real llega de la telemetría `chargeAppointPlans`."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Carga programada", "carga_programada",
                         entity_id_format=ENTITY_ID_FORMAT)

    def _live_on(self) -> bool | None:
        raw = field(self.coordinator.data, "chargeAppointPlans")
        if not raw:
            return None
        try:
            plans = ast.literal_eval(raw) if isinstance(raw, str) else raw
            if plans:
                return field_on(plans[0].get("switchStatus"))
        except (ValueError, SyntaxError, AttributeError, IndexError, TypeError):
            return None
        return None

    @property
    def is_on(self) -> bool:
        return bool(self._resolved(self._live_on()))

    async def async_turn_on(self, **kwargs) -> None:
        await self._run_command(
            "carga_prog_on", True,
            {"mainSwitch": 1, "chargeAppointPlans": [self.coordinator.build_charge_plan(1)]})

    async def async_turn_off(self, **kwargs) -> None:
        await self._run_command(
            "carga_prog_off", False,
            {"mainSwitch": 0, "chargeAppointPlans": [self.coordinator.build_charge_plan(0)]})


class EbroPollingSwitch(EbroEntity, SwitchEntity, RestoreEntity):
    """Interruptor "Actualización automática": activa/desactiva el sondeo periódico
    (despertar + lectura) sin tocar las opciones. NO es un comando al coche: actúa solo
    sobre el timer local. APAGADO por defecto —el sondeo es de solo lectura y no despierta el
    coche, pero aun así lo enciende el usuario—; el estado se restaura al reiniciar HA.

    Cuando está OFF se para el BUCLE: no hay consultas por temporizador, y los números se
    quedan en el último valor conocido (actualizables a mano con el botón "Actualizar
    ubicación"). Apagarlo apaga también "Limitar carga al %", que sin sondeo no tendría con qué
    decidir.

    **Queda UNA consulta fuera del interruptor, y es a propósito**: la del flanco
    dormido→despierto (`_on_car_message`). No la para este interruptor porque no cuesta nada que
    merezca pararse — es de solo lectura, no despierta el coche ni gasta 12 V, y ocurre en el
    único instante en que la nube tiene datos frescos. Tampoco compite con la app oficial: el
    límite de una sesión por cuenta de la nube de Chery se aplica al INICIAR SESIÓN, no por
    petición, así que quien abre la app deja fuera a Home Assistant independientemente de lo que
    esta consulte. Si llegas aquí pensando que es un olvido, no lo es."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:autorenew"

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Actualización automática", "polling_auto",
                         entity_id_format=ENTITY_ID_FORMAT)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # restaura la última elección: si estaba OFF, para el sondeo arrancado por defecto en el setup.
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.coordinator.set_poll_enabled(last.state == "on")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.poll_enabled)

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_poll_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_poll_enabled(False)
        self.async_write_ha_state()


# Texto en claro de cada motivo de `coordinator.charge_limit_blocker`. El mensaje viaja junto a
# la clave de traducción: la clave es lo que pinta la interfaz, el texto es lo que se ve en el log.
_BLOCKER_MSG = {
    "charge_limit_needs_polling":
        "El límite de carga necesita «Actualización automática» encendida: es el sondeo el que "
        "lee la batería y decide cuándo cortar. Enciéndela primero.",
    "charge_limit_needs_charging_interval":
        "El límite de carga necesita que el intervalo de «Cargando» sea mayor que 0 en las "
        "opciones de la integración: con 0 no se consulta nada mientras el coche carga, que es "
        "justo cuando habría que vigilar la batería.",
}


class EbroChargeLimitSwitch(EbroEntity, SwitchEntity, RestoreEntity):
    """Interruptor "Limitar carga al %": cuando está ON, la integración vigila la batería mientras
    carga y, al alcanzar el "Límite de carga (%)", para la carga (imponiendo una programación fuera
    del horario actual, única forma de parar en este coche). Es un límite por SOFTWARE, no un comando
    directo. OFF por defecto; se restaura al reiniciar HA. Requiere el intervalo de "Cargando" > 0.

    **Depende de que el sondeo llegue a ocurrir mientras carga.** El límite se comprueba en un
    único punto, justo después de cada lectura del canal realtime, así que sin esa lectura el
    interruptor quedaría encendido sin hacer absolutamente nada. Hay dos formas de que no ocurra
    —«Actualización automática» apagada, o el intervalo de «Cargando» a 0— y las dos las reúne
    `coordinator.charge_limit_blocker`. Encenderlo con cualquiera de las dos se RECHAZA, y si
    aparecen después el interruptor se apaga: apagar la actualización automática lo apaga en el
    momento (ver `set_poll_enabled`), y cambiar los intervalos recarga la entrada, con lo que la
    restauración de abajo vuelve a mirar la regla."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:battery-charging-80"

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Limitar carga al porcentaje", "charge_limit",
                         entity_id_format=ENTITY_ID_FORMAT)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            # Se vuelve a mirar la regla al restaurar, por dos motivos distintos:
            #
            # * las dos entidades restauran por su cuenta y HA no garantiza en qué orden. Sin
            #   esto, restaurar el límite DESPUÉS del interruptor de sondeo lo dejaría encendido
            #   sin sondeo. Con esto da igual el orden: si el sondeo restaura primero, esto lo ve;
            #   si restaura después, `set_poll_enabled(False)` lo apaga;
            # * cambiar los intervalos en las opciones RECARGA la entrada, así que este es también
            #   el punto por el que pasa un «Cargando» recién puesto a 0.
            self.coordinator.charge_limit_enabled = (
                last.state == "on" and self.coordinator.charge_limit_blocker is None)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.charge_limit_enabled)

    async def async_turn_on(self, **kwargs) -> None:
        if (motivo := self.coordinator.charge_limit_blocker) is not None:
            raise ServiceValidationError(
                _BLOCKER_MSG[motivo], translation_domain=DOMAIN, translation_key=motivo)
        self.coordinator.charge_limit_enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.charge_limit_enabled = False
        self.async_write_ha_state()


class EbroTheftAlarmSwitch(EbroOptimisticMixin, EbroRestoreStateMixin, EbroEntity, SwitchEntity,
                           RestoreEntity):
    """Alarma antirrobo del coche (theftAlarm setSwitch, endpoint /act).

    ON = el coche hace saltar la alarma y envía avisos ante movimiento no autorizado, forzado
    de puertas, rotura de lunas u otras intrusiones (descripción oficial de la app). A
    diferencia del confort, el estado NO está en la telemetría MQTT: se lee vía REST
    (querySwitch). Estrategia: valor inicial (seed) de la lectura real, luego estado optimista
    tras el toggle (setSwitch ACTÚA y quiere un taskId o el coche despierto), y restauración del
    último estado conocido al reiniciar HA."""

    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_icon = "mdi:shield-car"

    def __init__(self, coord) -> None:
        super().__init__(coord, "Ebro Alarma", "alarma", entity_id_format=ENTITY_ID_FORMAT)
        self._real: bool | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()   # restaura el último on/off conocido
        # seed del estado real desde el backend (solo lectura, best-effort: no debe romper el setup)
        try:
            v = await self.coordinator.async_query_theft()
            if v is not None:
                self._real = v != 0
                self.async_write_ha_state()
        except Exception:
            pass

    @property
    def is_on(self) -> bool:
        return bool(self._resolved(self._real))

    async def async_turn_on(self, **kwargs) -> None:
        await self._run_command("alarma_on", True)
        # el estado de la alarma NO llega por MQTT (solo REST querySwitch al arrancar): sin
        # actualizar `_real` aquí, el primer mensaje de telemetría pondría a cero el optimismo y
        # la card volvería al valor del arranque (rebote ON↔OFF). _run_command lanza en caso de
        # fallo → aquí solo se llega si el comando tuvo éxito.
        self._real = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._run_command("alarma_off", False)
        self._real = False
        self.async_write_ha_state()
