"""Cada cuánto sondear el canal realtime, según el ESTADO del coche.

El canal MQTT (puertas, cierre, cable, motor…) llega solo y gratis. El canal realtime
(batería, autonomía, alta tensión, progreso de carga…) hay que pedirlo.

**Y pedirlo NO despierta el coche.** La sonda interroga a la nube, no al vehículo: con el coche
dormido responde igual, con `onlineStatus=0` y una instantánea de hace minutos u horas. Durante
mucho tiempo aquí ponía que sondear «consume la batería de 12 V del coche», y es falso —eso lo
hacen los comandos, que sí lo despiertan—. Lo que sí cuesta es tráfico contra la nube y la
sesión, que Chery concede **una por cuenta**: si el componente habla mucho, compite con la app
oficial del usuario.

Aun así el ritmo sigue dependiendo del estado, por dos razones que no son la batería: con el
coche dormido la nube devuelve la misma foto una y otra vez, y con el coche en marcha los datos
cambian rápido y merece la pena preguntar más.

De ahí que el ritmo no sea fijo sino función del estado. Esta decisión vivía repartida entre
`_poll_state`, `_active_interval_s`, `_burst_active` y `_note_mqtt_burst` del coordinator,
leyendo `self` en cada paso: para probar «¿enchufado + alta tensión se clasifica como carga y
no como marcha?» había que construir un coordinator entero. Aquí es una función de sus
argumentos.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import time

from ..const import MOVE_BURST_COUNT, MOVE_BURST_WINDOW_S


class PollState(StrEnum):
    """Estado del coche a efectos de sondeo, de mayor a menor prioridad."""

    CHARGING = "cargando"
    PLUGGED = "enchufado"           # cable conectado pero sin cargar aún (AT apagada)
    MOVING = "en marcha"            # alta tensión + ráfaga de MQTT = circulando
    MOVING_IDLE = "en marcha detenido"  # alta tensión sin ráfaga (semáforo, parada breve)
    PARKED = "parado"


@dataclass(frozen=True)
class PollIntervals:
    """Los cinco intervalos configurables, en MINUTOS. 0 = desactivado en ese estado."""

    parked: int
    plugged: int
    charging: int
    moving: int
    moving_idle: int

    def for_state(self, state: PollState) -> int:
        return {
            PollState.CHARGING: self.charging,
            PollState.PLUGGED: self.plugged,
            PollState.MOVING: self.moving,
            PollState.MOVING_IDLE: self.moving_idle,
            PollState.PARKED: self.parked,
        }[state]


@dataclass(frozen=True)
class CarConditions:
    """Lo que se sabe del coche en el instante de decidir.

    Se pasa como dato y no se lee de `self` a propósito: es lo que hace la decisión
    reproducible en un test sin `hass` ni MQTT.
    """

    plugged: bool
    charging: bool
    hv_on: bool
    burst_active: bool
    #: el cable lleva conectado más del tope sin llegar a cargar nunca
    plugged_timed_out: bool = False


def classify(conditions: CarConditions) -> PollState:
    """Condiciones del coche → estado de sondeo. Prioridad: cargando > enchufado > en marcha >
    en marcha detenido > parado.

    Claves: (1) cargar también enciende la alta tensión → enchufado + AT = CARGANDO (no
    marcha). (2) La MARCHA se detecta por la RÁFAGA de MQTT (el coche late «Último contacto»
    al circular) + AT encendida y cable desconectado. (3) Con AT encendida pero SIN ráfaga
    (semáforo, o te has bajado con el coche en marcha) → «en marcha detenido», más relajado.
    (4) AT apagada → parado.

    **Cargar EXIGE el cable.** `chargeState=1` sin cable conectado no es una recarga: es un
    híbrido recuperando energía en marcha —freno regenerativo o motor térmico— y pasa
    continuamente mientras se conduce. Registrado en campo el 2026-08-20/25 decenas de veces,
    siempre con `plugged=false` y alternando con «en marcha» cada pocos minutos:

        13:06:19  cargando   cada 900 s     ← conduciendo
        13:09:27  en marcha  cada 180 s
        13:13:12  cargando   cada 900 s     ← conduciendo
        13:14:13  en marcha  cada 180 s

    El efecto es que el sondeo se relajaba de 3 a 15 minutos JUSTO al circular, que es cuando
    más rápido cambia todo. Sin cable, la recarga es un detalle del tren motriz, no un estado
    de sondeo."""
    if conditions.plugged and (conditions.charging or conditions.hv_on):
        return PollState.CHARGING
    if conditions.plugged and not conditions.plugged_timed_out:
        return PollState.PLUGGED
    if conditions.hv_on and not conditions.plugged:
        return PollState.MOVING if conditions.burst_active else PollState.MOVING_IDLE
    return PollState.PARKED


def interval_seconds(minutes: int) -> int | None:
    """Minutos configurados → segundos, o `None` si ese estado tiene el sondeo desactivado.

    `None` NO es un error: es «no preguntes nada hasta el próximo evento MQTT gratis», que es el
    valor por defecto con el coche parado."""
    return minutes * 60 if minutes and minutes > 0 else None


class BurstTracker:
    """Detector de MARCHA por ráfaga de mensajes MQTT.

    Al circular, el coche emite «Último contacto» seguido; parado no manda nada. ≥
    `MOVE_BURST_COUNT` mensajes en los últimos `MOVE_BURST_WINDOW_S` segundos = en movimiento.

    Thread-safe por diseño de uso: lo consultan el bucle de eventos de HA y el hilo de paho,
    pero quien lo posee (el coordinator) lo protege con su propio lock. La poda de marcas
    viejas ocurre en cada consulta, así que no hace falta ningún timer que lo limpie.
    """

    def __init__(self, count: int = MOVE_BURST_COUNT, window_s: int = MOVE_BURST_WINDOW_S) -> None:
        self._count = count
        self._window_s = window_s
        self._timestamps: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def record(self, now: float | None = None) -> bool:
        """Registra la marca de tiempo de un mensaje y devuelve si AHORA hay ráfaga."""
        now = time.time() if now is None else now
        self._timestamps.append(now)
        self._prune(now)
        return len(self._timestamps) >= self._count

    def is_active(self, now: float | None = None) -> bool:
        """¿Hay ráfaga en este momento? Poda las marcas viejas."""
        now = time.time() if now is None else now
        self._prune(now)
        return len(self._timestamps) >= self._count
