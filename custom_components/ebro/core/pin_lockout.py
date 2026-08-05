#!/usr/bin/env python3
"""Anti-bloqueo del PIN de comandos — estado encapsulado, con lock interno.

**El riesgo que este módulo existe para contener.** El PIN de 4 cifras de los comandos
remotos lo verifica el backend Chery (`checkPassword`). Cada verificación fallida incrementa
un contador de errores DEL LADO DE CHERY: superado el umbral, la cuenta se bloquea — y ese
bloqueo no se resuelve desde Home Assistant. Por eso el componente se autolimita: tras
`max_fail` rechazos consecutivos dentro de `window_s` deja de interrogar al backend y pide al
usuario que reconfigure el PIN.

Aquí el lock no es opcional: solo se entra por `attempt()`, que es un context manager y
serializa el INTENTO ENTERO — guardia, llamada de red, actualización del contador. No existe
forma de usar esta API que reabra la condición de carrera.

**Nota.** La clase es instanciable a propósito: con el contexto por llamada cada
vehículo/cuenta tiene su propia instancia, en vez de compartir un contador de proceso (con
dos coches configurados, los errores de uno bloquearían los comandos del otro).
"""
from __future__ import annotations

import contextlib
import threading
import time


class PinLockedError(Exception):
    """El anti-bloqueo ha saltado: NO se ha contactado con el backend.

    Distinta de un rechazo real del backend precisamente porque aquí no se ha gastado ningún
    intento del lado de Chery — es la protección que ha funcionado, no un error nuevo."""

    def __init__(self, attempts: int, message: str | None = None) -> None:
        self.attempts = attempts
        super().__init__(message or f"PIN bloqueado tras {attempts} intentos erróneos")


class _Intento:
    """Resultado de un solo intento, declarado explícitamente por el llamador.

    A propósito NO se deduce el resultado de la posible excepción: un error de red o un
    rechazo por permisos del vehículo no son un PIN erróneo y no deben acercar el bloqueo de
    la cuenta. Quien genera el taskId decide, caso por caso, si el intento es "culpa del PIN".
    """

    __slots__ = ("_resultado",)

    def __init__(self) -> None:
        self._resultado: str | None = None

    def success(self) -> None:
        """taskId obtenido → el contador vuelve a cero."""
        self._resultado = "ok"

    def record_failure(self) -> None:
        """El backend ha rechazado Y es imputable al PIN → cuenta hacia el bloqueo."""
        self._resultado = "ko"


class PinLockout:
    """Contador anti-bloqueo con lock interno.

    Uso::

        with lockout.attempt() as intento:        # lanza PinLockedError si está bloqueado
            respuesta = llama_backend()           # serializado: un intento cada vez
            if respuesta.taskid:
                intento.exito()
            elif es_culpa_del_pin(respuesta):
                intento.fallido()
            # ninguna declaración = el intento no cuenta (red, permisos, sesión)
    """

    def __init__(self, max_failures: int = 2, window_s: int = 600) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        # RLock: `attempt()` puede llamar a `reset()` sin autobloquearse.
        self._lock = threading.RLock()
        self._n = 0
        self._ts = 0.0

    # ───────────────────────── consulta ─────────────────────────
    @property
    def failed_attempts(self) -> int:
        with self._lock:
            return self._n

    def is_locked(self) -> bool:
        """True si un nuevo intento sería rechazado sin contactar con el backend."""
        with self._lock:
            return self._is_locked()

    def _is_locked(self) -> bool:
        """Llamar con el lock ya tomado."""
        if self._n < self.max_failures:
            return False
        # la ventana es deslizante: pasados `window_s` desde el último error se reinicia
        return (time.time() - self._ts) < self.window_s

    # ───────────────────────── mutación ─────────────────────────
    def reset(self) -> None:
        """Pone a cero el bloqueo.

        Se debe llamar cuando el usuario hace un gesto explícito de remedio (reconfigura el PIN
        desde el config flow o desde el Repair) AUNQUE reintroduzca el mismo PIN: el bloqueo
        podía no ser culpa del PIN, y sin reset el usuario se quedaría parado — sin ninguna
        señal — hasta que venza la ventana. El estado no está en el config entry, así que una
        recarga de la integración por sí sola no lo pone a cero."""
        with self._lock:
            self._n = 0
            self._ts = 0.0

    @contextlib.contextmanager
    def attempt(self):
        """Serializa un intento y aplica la guardia. Lanza `PinLockedError` si está bloqueado.

        El lock queda tomado durante toda la duración del bloque `with` — llamada de red
        incluida. Es intencionado y no negociable: soltarlo antes de la respuesta reabriría
        exactamente la condición de carrera que esta clase existe para cerrar."""
        with self._lock:
            if self._is_locked():
                raise PinLockedError(self._n)
            attempt = _Intento()
            # `finally` NO es un detalle: el llamador declara `fallido()` y justo después LANZA
            # (CommandError con el remedio para el usuario). Sin `finally` la excepción se
            # saltaría la actualización del contador y el bloqueo no saltaría nunca — es decir,
            # la protección de la cuenta quedaría desactivada en silencio.
            try:
                yield attempt
            finally:
                if attempt._resultado == "ok":
                    self._n = 0
                    self._ts = 0.0
                elif attempt._resultado == "ko":
                    self._n += 1
                    self._ts = time.time()
