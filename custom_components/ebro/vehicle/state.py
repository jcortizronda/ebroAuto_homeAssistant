"""El estado en vivo del coche, y el lock que lo protege.

**Por qué existe.** Tres hilos distintos tocan estos datos: el de **paho**, que entrega los
mensajes MQTT; el **executor**, donde corren la sonda y los comandos; y el **bucle de eventos**
de Home Assistant, desde el que leen las entidades. Estaban como cuatro atributos privados del
coordinator (`_fields`, `position`, `_last_msg_ts`, `_state_lock`) y cada acceso repetía a mano
el `with self._state_lock:`. Basta olvidarlo una vez para tener una lectura a medio escribir, y
es el tipo de fallo que no aparece en los tests: aparece de noche, en el coche del usuario.

Aquí el lock no es opcional. No hay forma de leer ni escribir sin pasar por un método que ya lo
toma, y todo lo que sale es una **copia**: si se devolviera el dict vivo, el hilo de paho podría
estar mutándolo mientras Home Assistant lo serializa.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class VehicleState:
    """Telemetría 5A02, última posición y «cuándo habló el coche por última vez».

    `awake_window` = cuántos segundos sin recibir nada hay que esperar antes de dar el coche por
    dormido.
    """

    def __init__(self, awake_window: float) -> None:
        self._lock = threading.Lock()
        self._awake_window = awake_window
        self._fields: dict[str, str] = {}
        self._position: dict[str, Any] | None = None
        self._last_msg_ts: float = 0.0

    # ───────────────────────── escritura ─────────────────────────
    def record_message(self, fields: dict[str, str], now: float) -> tuple[dict[str, str], bool]:
        """Aplica los campos de un mensaje del coche. Devuelve `(copia, estaba_despierto)`.

        Las dos cosas se hacen bajo el MISMO lock a propósito: `was_awake` se mide contra el
        instante ANTERIOR, así que leerlo por separado abriría una ventana en la que otro
        mensaje ya habría movido la marca y el flanco de despertar se perdería.
        """
        with self._lock:
            was_awake = self._is_awake_locked(now)
            self._last_msg_ts = now
            self._fields.update(fields)
            return dict(self._fields), was_awake

    def set_position(self, geo: dict[str, Any]) -> dict[str, Any]:
        """Sustituye la posición (push 1301). Devuelve una copia."""
        with self._lock:
            self._position = dict(geo)
            return dict(self._position)

    def merge_position(self, geo: dict[str, Any]) -> dict[str, Any]:
        """Funde una posición parcial sobre la conocida (sonda realtime). Devuelve una copia.

        La sonda puede traer solo algunos campos geográficos, y perder los demás dejaría el
        device_tracker peor de lo que estaba.
        """
        with self._lock:
            self._position = {**(self._position or {}), **geo}
            return dict(self._position)

    # ───────────────────────── lectura ─────────────────────────
    def fields(self) -> dict[str, str]:
        """Copia de la telemetría 5A02."""
        with self._lock:
            return dict(self._fields)

    def field(self, key: str) -> str | None:
        """Un campo de la telemetría, sin copiar el dict entero."""
        with self._lock:
            return self._fields.get(key)

    @property
    def position(self) -> dict[str, Any] | None:
        """Copia de la última posición conocida, o `None`."""
        with self._lock:
            return dict(self._position) if self._position is not None else None

    def _is_awake_locked(self, now: float) -> bool:
        return bool(self._last_msg_ts) and (now - self._last_msg_ts) < self._awake_window

    def is_awake(self, now: float | None = None) -> bool:
        """El coche está publicando AHORA.

        Se mide por el tiempo transcurrido desde el último mensaje, no por un flag: un flag hay
        que acordarse de apagarlo, y cuando eso se olvidó el botón «Despertar coche» respondía
        «ya está despierto» y no mandaba nada durante días.
        """
        now = time.time() if now is None else now
        with self._lock:
            return self._is_awake_locked(now)

    # ─────────────── siembra para los tests y la restauración ───────────────
    def seed(self, *, fields: dict[str, str] | None = None,
             position: dict[str, Any] | None = None, last_msg_ts: float | None = None) -> None:
        """Fija el estado de golpe. Lo usa el arnés de tests para simular un coche vivo."""
        with self._lock:
            if fields is not None:
                self._fields = dict(fields)
            if position is not None:
                self._position = dict(position)
            if last_msg_ts is not None:
                self._last_msg_ts = last_msg_ts
