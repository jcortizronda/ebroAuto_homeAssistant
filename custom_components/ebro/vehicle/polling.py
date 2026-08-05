"""El bucle de sondeo: cuándo se le pregunta al coche.

Esta es la decisión más cara de la integración cuando se equivoca. El canal MQTT (puertas,
cierre, cable, motor) llega solo y gratis, pero el canal realtime — batería, autonomía, alta
tensión, progreso de carga — hay que **pedirlo**, y pedirlo de más tiene dos costes medidos en
campo: consume la batería de 12 V del coche, y desconecta la app oficial del usuario, porque la
nube de Chery admite una sola sesión por cuenta.

De ahí que no haya un intervalo fijo. El ritmo lo decide el ESTADO del coche, y ese estado se
deduce de dos fuentes que llegan gratis (la telemetría MQTT y la última lectura realtime).

**El reparto con `poll_policy`.** Allí está la decisión pura — dadas unas condiciones, qué
estado es y cada cuántos minutos toca. Aquí está el *conductor*: leer esas condiciones del
coche, aplicar el único ajuste que depende de la configuración del usuario (afinar cerca del
límite de carga), programar el timer y reprogramarlo tras cada lectura.

**Por qué el bucle se auto-reprograma.** No hay un timer periódico: cada lectura programa la
siguiente según el estado que acaba de leer. Eso tiene una consecuencia que hay que respetar —
`schedule_next()` es el ÚNICO punto donde el bucle puede pararse, y si una lectura ya en vuelo
regresa después de descargar la integración, el `TimerRegistry` cerrado es lo que impide que
rearme un sondeo huérfano. Eso pasó de verdad: el ciclo siguió interrogando a la nube durante
horas con la integración apagada, sin un solo error en el log.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from ..const import CHARGE_LIMIT_NEAR_MIN, CHARGE_LIMIT_NEAR_SOC, SEED_PROBE_DELAY_S
from ..helpers import field_on, is_code, realtime, to_float
from .poll_policy import (
    BurstTracker,
    CarConditions,
    PollIntervals,
    PollState,
    classify,
    interval_seconds,
)
from .timers import HV_POLL, POLL_GROUP, STARTUP_PROBE

if TYPE_CHECKING:
    from .coordinator import EbroCoordinator

_LOGGER = logging.getLogger(__name__)

# Campos de los que se deduce que la ALTA TENSIÓN está encendida. Es el único estado en que
# `/asr/manager/realtime` reporta odómetro/SOC/tensión REALES: con la HV apagada son valores
# rancios o marcadores (dumpEnergy=0, totalVoltage=0, totalCurrent=-1000).
_HV_FIELDS = ("hVoltageState", "engineState")


class PollController:
    """Decide cuándo se sondea el canal realtime, y lo programa.

    Recibe el coordinator una sola vez: necesita su estado (telemetría y realtime), su registro
    de timers y su capacidad de ejecutar la sonda en un executor. La alternativa —seis callbacks
    en el constructor— haría el acoplamiento más difícil de leer, no menos.
    """

    def __init__(self, coordinator: EbroCoordinator, intervals: PollIntervals) -> None:
        self._coordinator = coordinator
        self._intervals = intervals
        self._burst = BurstTracker()
        self._burst_was_active = False
        #: cuándo se conectó el cable (para el tope de «enchufado sin llegar a cargar»)
        self._plugged_since = 0.0
        #: interruptor «Actualización automática». APAGADO por defecto: el sondeo es de solo
        #: lectura y nunca despierta el coche, pero aun así lo activa el usuario.
        self.enabled = False

    # ───────────────────── condiciones leídas del coche ─────────────────────
    @property
    def _data(self) -> dict:
        return self._coordinator.data

    def is_plugged(self) -> bool:
        """El cable está conectado (`chargeGunState != 0`).

        Basta que lo diga UNA de las dos fuentes. Una lectura realtime rancia con
        `chargeGunState=0` no debe hacer que una carga en curso se confunda con «marcha»; para
        darlo por desenchufado tienen que coincidir las dos."""
        desde_realtime = realtime(self._data).get("chargeGunState")
        desde_mqtt = self._coordinator.state.field("chargeGunState")
        return bool(field_on(desde_realtime)) or bool(field_on(desde_mqtt))

    def is_charging(self) -> bool:
        """El coche está CARGANDO de verdad (`chargeState=1`, canal realtime).

        Distinto de `is_plugged`: el cable puede estar conectado sin cargar — carga programada
        esperando su hora, carga ya completada."""
        return is_code(realtime(self._data).get("chargeState"), 1)

    def is_hv_on(self) -> bool:
        """La alta tensión está encendida (marcha, carga o clima).

        Lee el realtime más fresco con respaldo en el último 5A02 recibido por MQTT."""
        rt = realtime(self._data)
        fields = self._coordinator.state.fields()
        for key in _HV_FIELDS:
            value = rt.get(key)
            if value is None:
                value = fields.get(key)
            if is_code(value, 1):
                return True
        # Red de seguridad: en marcha ocurre `engineState=0` con velocidad > 0 (verificado en
        # vivo el 2026-06-25: 38 km/h con engineState=0 y hVoltageState=1). El coche está
        # despierto en la red HV, así que el realtime es real y hay que seguirlo.
        speed = rt.get("vehicleSpeed")
        if speed is None:
            speed = fields.get("vehicleSpeed")
        speed = to_float(speed)
        return speed is not None and speed > 0

    def plugged_timed_out(self) -> bool:
        """El cable lleva conectado más del tope SIN llegar a cargar (0 = sin límite).

        Evita sondear indefinidamente un coche enchufado que nunca carga."""
        limit = self._coordinator.plugged_wait_max_s
        if not limit or not self._plugged_since:
            return False
        return (time.time() - self._plugged_since) > limit

    def conditions(self) -> CarConditions:
        """Fotografía de las condiciones del coche, leída UNA vez.

        Cada una toma el lock del estado o recorre el dict realtime: `schedule_next` las
        necesita para clasificar, para el log y para el registro de diagnóstico, y antes las
        calculaba tres veces por vuelta."""
        return CarConditions(
            plugged=self.is_plugged(),
            charging=self.is_charging(),
            hv_on=self.is_hv_on(),
            burst_active=self._burst.is_active(),
            plugged_timed_out=self.plugged_timed_out(),
        )

    # ───────────────────────── la decisión ─────────────────────────
    def state_and_minutes(self, conditions: CarConditions | None = None) -> tuple[str, int]:
        """(etiqueta, minutos) del estado de sondeo actual.

        La clasificación vive en `poll_policy.classify`, que es pura. Aquí solo se aplica el
        único ajuste que depende de la configuración del usuario: cerca del límite de carga se
        sondea más fino, para clavar el corte."""
        state = classify(conditions if conditions is not None else self.conditions())
        minutes = self._intervals.for_state(state)
        limiter = self._coordinator.charge_limiter
        if state is PollState.CHARGING and limiter.near_target(
                self._coordinator.current_soc(), CHARGE_LIMIT_NEAR_SOC):
            minutes = CHARGE_LIMIT_NEAR_MIN
        return str(state), minutes

    # ───────────────────────── ráfaga de MQTT ─────────────────────────
    def note_message(self, now: float) -> bool:
        """Registra un mensaje MQTT y devuelve si acaba de EMPEZAR una ráfaga.

        El flanco es lo que interesa: al pasar de «sin ráfaga» a «hay ráfaga» el coche se ha
        puesto en marcha, y una sonda fresca leerá la alta tensión y reprogramará el bucle al
        ritmo adecuado. Dentro de la ráfaga no se dispara nada más."""
        active = self._burst.record(now)
        edge = active and not self._burst_was_active
        self._burst_was_active = active
        return edge

    def note_plug_change(self, plugged: bool, now: float) -> None:
        """Anota que el cable se ha conectado o desconectado (arranca o reinicia el tope)."""
        if plugged:
            if not self._plugged_since:
                self._plugged_since = now
        else:
            self._plugged_since = 0.0

    # ───────────────────────── programación ─────────────────────────
    def start(self) -> None:
        """Siembra el bucle con UNA sonda, si el interruptor está encendido.

        Se espera `SEED_PROBE_DELAY_S` para dar tiempo a que MQTT conecte: así la primera
        lectura ya clasifica bien el estado (cargando / en marcha / parado) en vez de decidir
        con el dict vacío."""
        if not self.enabled:
            _LOGGER.debug("[poll] desactivado por el interruptor")
            return
        timers = self._coordinator.timers
        if not timers.is_armed(STARTUP_PROBE):
            timers.arm(STARTUP_PROBE, lambda: async_call_later(
                self._coordinator.hass, SEED_PROBE_DELAY_S, self._seed_cb))

    async def _seed_cb(self, _now) -> None:
        self._coordinator.timers.cancel(STARTUP_PROBE)
        try:
            await self._coordinator.async_probe(force=True)
        except Exception as err:
            _LOGGER.debug("[poll] sondeo inicial (seed) fallido: %s", err)

    @callback
    def set_enabled(self, on: bool) -> None:
        """Enciende o apaga el sondeo en runtime (interruptor «Actualización automática»)."""
        self.enabled = on
        if on:
            self.start()
        else:
            # apaga TODO el grupo de una vez. El keep-alive de sesión NO está en el grupo a
            # propósito: mantiene vivo el token sin contactar nunca con el coche.
            self._coordinator.timers.cancel_many(POLL_GROUP)

    @callback
    def schedule_next(self) -> None:
        """Programa la siguiente lectura según el estado actual, o para el bucle.

        Es el ÚNICO punto donde el bucle puede detenerse, porque es el único que lo rearma.
        Con «parado» e intervalo 0 no se programa nada: no se toca el coche hasta el próximo
        evento MQTT gratis."""
        timers = self._coordinator.timers
        timers.cancel(HV_POLL)
        diag = self._coordinator.diag_recorder
        if timers.closing or not self.enabled:
            if diag is not None:
                diag.record("hv_followup", orphan=True, closing=timers.closing,
                            poll_enabled=self.enabled)
            return
        # con datos frescos, corta la carga si se alcanzó el límite por software
        self._coordinator.check_charge_limit()

        conditions = self.conditions()
        label, minutes = self.state_and_minutes(conditions)
        every = interval_seconds(minutes)
        _LOGGER.debug(
            "[poll] estado=%s (cable=%s carga=%s AT=%s despierto=%s) → %s",
            label, conditions.plugged, conditions.charging, conditions.hv_on,
            self._coordinator.is_awake,
            f"sondeo cada {minutes} min" if every else "detenido (no se toca el coche)")
        if every is None:
            return   # parado con intervalo 0 → hasta el próximo disparador MQTT
        if diag is not None:
            diag.record("hv_followup", orphan=False, plugged=conditions.plugged,
                        charging=conditions.charging, state=label, every_s=every)
        timers.arm(HV_POLL, lambda: async_call_later(
            self._coordinator.hass, every, self._followup_cb))

    async def _followup_cb(self, _now) -> None:
        self._coordinator.timers.cancel(HV_POLL)
        await self._coordinator.async_probe(force=True)
