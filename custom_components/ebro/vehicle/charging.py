"""Carga programada y límite de batería por software.

Este coche no tiene tope de SOC nativo: `chargeStartStopControl` responde A00084 («comando no
permitido»). La única forma de parar una carga es imponer una **programación cuya ventana ya
terminó** — un truco que hay que construir bien o no funciona, y que estaba escrito en dos
sitios distintos del coordinator junto con el literal del plan.

Aquí viven las tres cosas que componen esa función y nada más:

* cómo se construye un `chargeAppointPlans` (el literal, una sola vez);
* la conversión de la hora a UTC, que es de donde venía un bug real de dos horas;
* la decisión de cortar la carga al alcanzar el objetivo.

La decisión (`should_stop`) es pura: recibe el SOC y si está cargando, y responde. Quién
manda el comando y cuándo sigue siendo el coordinator.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.util import dt as dt_util

from ..const import (
    CHARGE_MIN_DURATION_MIN,
    CHARGE_STOP_DURATION_MIN,
    CHARGE_STOP_START_BACK_MIN,
    DEFAULT_CHARGE_LIMIT_SOC,
    MINUTES_PER_DAY,
)

_LOGGER = logging.getLogger(__name__)

# Todos los días de la semana: el plan de carga se aplica siempre, y el interruptor de la
# entidad es quien decide si está activo.
ALL_WEEKDAYS = [1, 2, 3, 4, 5, 6, 7]


def local_minutes_to_utc(local_mins: int) -> int:
    """Minutos-desde-medianoche en HORA LOCAL de HA → minutos-desde-medianoche en UTC.

    El backend de Ebro interpreta el `startTime` de la carga programada en UTC (verificado en
    campo: con zona horaria local UTC+2 una carga puesta a las 03:00 arrancaba a las 05:00). HA
    guarda la hora tal como la elige el usuario (reloj de pared local), así que hay que pasarla
    a UTC antes de enviarla. Se usa la fecha de HOY para que el desfase respete el horario de
    verano/invierno vigente."""
    local_mins = int(local_mins) % MINUTES_PER_DAY
    hh, mm = divmod(local_mins, 60)
    local_dt = dt_util.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    utc_dt = dt_util.as_utc(local_dt)
    return utc_dt.hour * 60 + utc_dt.minute


def utc_minutes_to_local(utc_mins: int) -> int:
    """La inversa de `local_minutes_to_utc`: lo que guarda el coche → reloj de pared.

    Hace falta para ENSEÑAR la programación que tiene el coche. Sin esto, una carga puesta a
    las 03:00 desde la app se mostraría como 01:00 en España, y el usuario pensaría que la
    integración se ha equivocado — cuando el desfase es solo el formato del backend."""
    utc_mins = int(utc_mins) % MINUTES_PER_DAY
    hh, mm = divmod(utc_mins, 60)
    utc_dt = dt_util.utcnow().replace(hour=hh, minute=mm, second=0, microsecond=0)
    local_dt = dt_util.as_local(utc_dt)
    return local_dt.hour * 60 + local_dt.minute


@dataclass(frozen=True)
class ChargeSchedule:
    """La programación de carga TAL COMO LA TIENE EL COCHE.

    Distinta de las entidades de hora y duración, que son la preferencia del usuario — lo que
    se enviará al pulsar. Si la programación se cambia desde la app oficial o desde el propio
    coche, esas preferencias no se enteran; esto sí.
    """

    enabled: bool
    start_minutes: int | None      # reloj de pared local
    duration_minutes: int | None
    days: tuple[int, ...] = ()


def parse_schedule(payload: dict | None) -> ChargeSchedule | None:
    """Respuesta de `chargeAppointQuery` → `ChargeSchedule`, o `None` si no es interpretable.

    El interruptor general (`mainSwitch`) y el del plan (`switchStatus`) son cosas distintas y
    ambos tienen que estar activos para que el coche cargue solo. El coche devuelve una LISTA
    de planes; se toma el primero, que es el único que esta integración sabe escribir.
    """
    if not isinstance(payload, dict):
        return None
    planes = payload.get("chargeAppointPlans")
    plan = planes[0] if isinstance(planes, list) and planes and isinstance(planes[0], dict) else {}
    encendido = _as_int(payload.get("mainSwitch")) == 1 and _as_int(plan.get("switchStatus")) == 1
    inicio = _as_int(plan.get("startTime"))
    duracion = _as_int(plan.get("timeConsuming"))
    dias = plan.get("cycleData")
    return ChargeSchedule(
        enabled=encendido,
        start_minutes=None if inicio is None else utc_minutes_to_local(inicio),
        duration_minutes=duracion,
        days=tuple(int(d) for d in dias) if isinstance(dias, list) else (),
    )


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_plan(start_minutes: int, duration_minutes: int, switch_status: int) -> dict:
    """Un plan de `chargeAppointPlans`.

    `startTime` va en UTC (el backend lo interpreta así); `timeConsuming` es una DURACIÓN, y
    por tanto NO se convierte. El coche exige un mínimo de 1 h: una duración menor la rechaza
    con code 89, así que se fuerza aquí en vez de dejar que falle el envío."""
    return {
        "cycleData": ALL_WEEKDAYS,
        "startTime": local_minutes_to_utc(int(start_minutes)),
        "switchStatus": switch_status,
        "timeConsuming": max(CHARGE_MIN_DURATION_MIN, int(duration_minutes)),
    }


def build_stop_plan() -> dict:
    """Un plan cuya ventana YA terminó: la única forma de parar la carga en este coche.

    `startTime` = ahora − 90 min con 60 min de duración → ventana [ahora−90, ahora−30], cerrada
    hace media hora → «ahora» queda fuera → el coche deja de cargar. La duración no puede ser
    menor: el backend rechaza con code 89 cualquier cosa por debajo de la hora, así que el
    truco se apoya en desplazar el INICIO, no en acortar la ventana.

    NB: aquí `startTime` NO pasa por `local_minutes_to_utc`, porque se calcula ya en UTC."""
    now_utc = dt_util.utcnow()
    start = (now_utc.hour * 60 + now_utc.minute - CHARGE_STOP_START_BACK_MIN) % MINUTES_PER_DAY
    return {
        "cycleData": ALL_WEEKDAYS,
        "startTime": int(start),
        "switchStatus": 1,
        "timeConsuming": CHARGE_STOP_DURATION_MIN,
    }


@dataclass
class ChargeLimiter:
    """Límite de carga por SOFTWARE: corta la carga al alcanzar el porcentaje objetivo.

    El estado que guarda es uno solo y tiene una razón: `_stopped` evita reenviar el corte en
    cada lectura mientras el coche tarda en obedecer. Se rearma solo al dejar de cargar, para
    que la siguiente sesión vuelva a poder cortar.
    """

    enabled: bool = False
    target_soc: int = DEFAULT_CHARGE_LIMIT_SOC
    _stopped: bool = False

    def should_stop(self, *, charging: bool, soc: float | None) -> bool:
        """¿Hay que cortar la carga AHORA? Actualiza el estado interno.

        Devuelve True una sola vez por sesión de carga. `soc=None` (alta tensión apagada, el
        coche manda 0 como marcador) no cuenta como «objetivo alcanzado»: no se corta a ciegas.
        """
        if not (self.enabled and charging):
            self._stopped = False   # no cargando → listo para la próxima sesión
            return False
        if soc is None or soc < self.target_soc or self._stopped:
            return False
        self._stopped = True
        _LOGGER.info("[carga] límite %s%% alcanzado (%.0f%%) → parando la carga",
                     self.target_soc, soc)
        return True

    def near_target(self, soc: float | None, margin: int) -> bool:
        """True si falta `margin` % o menos para el objetivo (→ sondear más fino para clavar
        el corte)."""
        if not self.enabled or soc is None:
            return False
        return 0 <= (self.target_soc - soc) <= margin
