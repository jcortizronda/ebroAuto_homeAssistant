"""Ciclo de vida del monitor de diagnóstico.

El monitor (`diag.py`) es una herramienta para quien DESARROLLA, no una función de usuario: no
tiene interruptor en la interfaz y se enciende creando un archivo bandera en la carpeta de
configuración. Lo que este módulo gobierna es *cuándo* está vivo, que resultó ser más delicado
de lo que parece:

* debe encenderse **antes** del primer control de sesión, porque ese control es justo el evento
  que se quiere releer tras un reinicio;
* debe apagarse **solo**. Deja un log verboso y un archivo creciendo en disco, así que no puede
  quedarse encendido por olvido: al armarlo se programa su propio vencimiento;
* la excepción es deliberada — con la bandera a `0` no vence nunca, porque a veces el evento a
  observar es raro y una ventana fija podría cerrarse justo antes de que ocurra;
* al **descargar** la integración NO se consume la ventana: el usuario aún tiene días y el
  monitor debe retomar en el reload con su vencimiento original.

Eran ~70 líneas dentro del coordinator, que no tiene nada que ver con esto.
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..const import DIAG_MIN_AUTOSTOP_S, DIAG_SWITCH_FILE, DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)


class DiagMonitor:
    """Enciende, vence y apaga el monitor de diagnóstico de un vehículo.

    Mientras está dormido — que es lo normal — `recorder` es `None`, y cada punto de enganche
    del componente queda en un solo `is not None` sin reservar nada. Importa: uno de esos
    puntos es `_on_car_message`, que corre en cada push del coche.
    """

    def __init__(self, hass: HomeAssistant, vin: str, email: str) -> None:
        self.hass = hass
        self._vin = vin
        self._email = email
        self._recorder: Any | None = None
        self._expiry_unsub: Callable[[], None] | None = None

    @property
    def recorder(self):
        """El grabador, o `None` si el monitor está dormido."""
        return self._recorder

    @property
    def record(self):
        """El callback de registro, o `None`. Es lo que se engancha en el `CoreCtx`."""
        return self._recorder.record if self._recorder is not None else None

    @property
    def _switch_path(self) -> str:
        return self.hass.config.path(DIAG_SWITCH_FILE)

    async def async_setup(self) -> None:
        """Enciende el monitor si existe la bandera. Idempotente.

        `async_setup_entry` lo arma pronto y `async_start` lo vuelve a llamar poco después: la
        segunda llamada no debe crear un segundo escritor sobre el mismo archivo ni rearmar el
        autoapagado.
        """
        if self._recorder is not None:
            return
        from .diag import DiagRecorder, read_switch

        path = self._switch_path
        until = await self.hass.async_add_executor_job(read_switch, path)
        if until is None:
            return   # sin bandera: todo queda dormido
        jsonl = self.hass.config.path(f"{DOMAIN}_{self._vin}_diag.jsonl")
        self._recorder = await self.hass.async_add_executor_job(
            DiagRecorder, jsonl, self._vin, self._email, until
        )
        if until == math.inf:
            _LOGGER.warning(
                "[diag] monitor de diagnóstico ACTIVO SIN VENCIMIENTO → %s (datos ya "
                "ofuscados; solo se apaga borrando %s)",
                jsonl, path,
            )
            return
        self._expiry_unsub = async_call_later(
            self.hass, max(DIAG_MIN_AUTOSTOP_S, until - time.time()), self._expiry_cb
        )
        _LOGGER.warning(
            "[diag] monitor de diagnóstico ACTIVO hasta el %s → %s (datos ya ofuscados; "
            "elimina %s para apagarlo antes)",
            dt_util.as_local(dt_util.utc_from_timestamp(until)).isoformat(timespec="minutes"),
            jsonl, path,
        )

    async def _expiry_cb(self, _now) -> None:
        self._expiry_unsub = None
        # En executor: `close()` hace el join del hilo escritor y el desarme toca el sistema de
        # archivos — ninguno de los dos debe correr en el bucle de eventos.
        await self.hass.async_add_executor_job(self.shutdown, True)
        _LOGGER.warning("[diag] monitor de diagnóstico terminado (vencimiento alcanzado)")

    def cancel_expiry(self) -> None:
        """Desprograma el autoapagado (lo llama el teardown de la integración)."""
        if self._expiry_unsub is not None:
            self._expiry_unsub()
            self._expiry_unsub = None

    def shutdown(self, disarm: bool = False) -> None:
        """Apaga el monitor. Bloqueante (join del hilo escritor) → ejecutar en executor.

        `disarm=True` renombra además la bandera, para que no vuelva a arrancar en el próximo
        inicio: es lo que hace el vencimiento. En la descarga de la integración se deja como
        está, porque al usuario aún le quedan días de ventana.
        """
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.close()
        if disarm:
            from .diag import disarm_switch

            disarm_switch(self._switch_path)
