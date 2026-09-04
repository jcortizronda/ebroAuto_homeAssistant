"""Registro único de los timers del coordinator.

**El problema que resuelve.** El coordinator programa cinco timers recurrentes (keep-alive
de sesión, sondeo de telemetría, seguimiento de alta tensión/carga, sonda inicial, latido de
detección de marcha). Eran cinco atributos `_*_unsub` cancelados en tres sitios distintos y
rearmados en otros dos. La consecuencia no era teórica:

* una lectura ya "en vuelo" cuando la integración se descargaba volvía DESPUÉS del stop y
  rearmaba el seguimiento → el ciclo seguía interrogando a la nube durante horas, con la
  integración apagada;
* apagando «Actualización automática» durante una carga quedaba activo justo el ciclo más
  frecuente, el de 2 minutos.

Ninguno de los dos daba errores en el log. El coste era real pero invisible: consumo de la
tráfico contra la nube y conflicto con la app oficial, que en la nube de Chery admite **una
sola sesión por cuenta** — cuando el componente habla, la app del usuario se desconecta.

**El invariante, en un solo sitio.** Tras `close()` ningún timer puede volver a armarse:
`arm()` lo rechaza. No hace falta que los cinco sitios que programan timers se acuerden de
comprobar un flag — lo garantiza el registro. Las guardas repartidas sobran.

`arm()` recibe una *factory* y no un unsub ya creado: así, con el registro cerrado, el timer
ni siquiera se programa. Pasando el unsub se habría creado el timer para luego tirarlo,
dejando una callback igualmente programada mientras tanto.
"""
from __future__ import annotations

from collections.abc import Callable
import logging

_LOGGER = logging.getLogger(__name__)

# Nombres de los timers. Constantes y no cadenas sueltas: una errata en un `cancel("hv_pol")`
# fallaría en silencio dejando el timer armado — es decir, el bug de partida.
KEEPALIVE = "keepalive"          # refresco periódico de la sesión (token)
HV_POLL = "hv_poll"              # sondeo del canal realtime, al ritmo que marca el estado
STARTUP_PROBE = "startup_probe"  # sonda de una vez ~15s tras el arranque (siembra el seguimiento)
AWAKE = "awake_expiry"           # vencimiento del estado «coche despierto» (sin contacto con la nube)

# NB: existieron un `POLL` (sondeo periódico fijo) y un `DRIVE_WATCH` (latido de marcha) que
# ningún camino armaba ya — el sondeo dejó de ser periódico para pasar a depender del estado
# (ver poll_policy.py) y la marcha se detecta por ráfaga de MQTT, sin timer. Seguían en
# GRUPO_POLL, dando a entender que se cancelaba algo que nunca se había armado.

# Timers ligados al interruptor «Actualización automática»: se apagan TODOS juntos.
# El keep-alive NO es del grupo — mantiene viva la sesión (sin contacto con el coche) y debe
# seguir incluso con el sondeo apagado, si no el token caducaría y el usuario tendría que
# reautenticar sin motivo.
POLL_GROUP = (HV_POLL, STARTUP_PROBE)


class TimerRegistry:
    """Guarda los `unsub` de los timers y garantiza que tras `close()` no se armen más."""

    def __init__(self) -> None:
        self._unsubs: dict[str, Callable[[], None]] = {}
        self._closing = False

    @property
    def closing(self) -> bool:
        """True tras `close()`: la integración se está descargando, punto de no retorno."""
        return self._closing

    def arm(self, name: str, factory: Callable[[], Callable[[], None]]) -> bool:
        """Programa el timer `nombre` (sustituyendo el anterior). False si el registro está cerrado.

        Idempotente por nombre: rearmar un timer ya activo cancela el viejo en vez de dejar dos
        en vuelo — con un timer auto-reprogramable como el seguimiento HV, dos copias duplicarían
        las lecturas a la nube en cada vuelta."""
        if self._closing:
            _LOGGER.debug("[timer] %s no armado: registro cerrado", name)
            return False
        self.cancel(name)
        self._unsubs[name] = factory()
        return True

    def cancel(self, name: str) -> bool:
        """Cancela el timer `nombre`. True si estaba realmente armado."""
        unsub = self._unsubs.pop(name, None)
        if unsub is None:
            return False
        try:
            unsub()
        except Exception as err:
            _LOGGER.debug("[timer] error cancelando %s: %s", name, err)
        return True

    def cancel_many(self, names) -> None:
        for name in names:
            self.cancel(name)

    def cancel_all(self) -> None:
        for name in list(self._unsubs):
            self.cancel(name)

    def close(self) -> None:
        """Teardown definitivo: cancela todo y prohíbe cualquier futuro `arm()`.

        De aquí no se vuelve atrás ni aunque una llamada ya en vuelo intente rearmar al
        regresar: es exactamente el sondeo huérfano."""
        self._closing = True
        self.cancel_all()

    # ───────────────────────── consulta (usada por los tests) ─────────────────────────
    def is_armed(self, name: str) -> bool:
        return name in self._unsubs

    def armed(self) -> set[str]:
        """Los nombres de los timers actualmente armados."""
        return set(self._unsubs)
