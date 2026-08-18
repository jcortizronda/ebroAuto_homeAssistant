"""Cliente MQTT mutual-TLS del coche (paho, en su propio hilo).

El coordinator no tiene por qué saber que debajo hay paho, ni cómo se deriva la contraseña del
broker, ni qué topic hay que suscribir. Aquí queda todo eso y solo eso: conectar, suscribir,
entregar los mensajes en crudo y desconectar limpiamente.

**El hilo importa.** paho corre su propio bucle en un hilo aparte, así que los callbacks NO
están en el bucle de eventos de Home Assistant: quien los reciba debe saltar al loop antes de
tocar nada de HA. Este módulo no lo hace por su cuenta — se limita a entregar, y el
coordinator decide.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import logging
import os
import ssl

from homeassistant.exceptions import ConfigEntryNotReady

from ..const import (
    CAR_SEED,
    MQTT_KEEPALIVE_S,
    MQTT_RECONNECT_MAX_S,
    MQTT_RECONNECT_MIN_S,
)
from . import certificates

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MqttConfig:
    """Lo que hace falta para hablar con el broker de ESTE vehículo."""

    host: str
    port: int
    tuserid: str
    channel_id: str
    certs_dir: str

    @property
    def client_id(self) -> str:
        """Formato EXACTO que espera la ACL del broker: cambiarlo es que rechace la conexión."""
        return f"app_{self.channel_id}_{self.tuserid}"

    @property
    def topic(self) -> str:
        return f"app/{self.channel_id}/{self.tuserid}/account/msgCenter/msg"

    @property
    def password(self) -> str:
        """Contraseña del broker: md5(tuserid + semilla de la app).

        La semilla es una constante compartida de la app (no un secreto del usuario); se puede
        sobrescribir por entorno solo para diagnóstico."""
        seed = os.environ.get("CAR_SEED", CAR_SEED)
        return hashlib.md5((self.tuserid + seed).encode()).hexdigest()


class EbroMqttClient:
    """Ciclo de vida del cliente paho. Todos sus métodos son BLOQUEANTES: ejecutar en executor.

    Los cuatro callbacks se invocan **desde el hilo de paho**:

    * `on_message(payload: bytes)` — un mensaje del coche, sin interpretar;
    * `on_connected(ok: bool, rc)` — resultado de un intento de conexión;
    * `on_subscribed(ok: bool, detail)` — resultado del SUBSCRIBE;
    * `on_disconnected(rc)` — la sesión se ha caído.
    """

    def __init__(
        self,
        config: MqttConfig,
        *,
        on_message: Callable[[bytes], None],
        on_connected: Callable[[bool, object], None],
        on_disconnected: Callable[[object], None],
        on_subscribed: Callable[[bool, str], None] = lambda ok, detail: None,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_subscribed = on_subscribed
        self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self) -> None:
        """Conecta y arranca el bucle de paho. Bloqueante: ejecutar en executor.

        Un fallo del connect inicial (DNS/certificado/red) se traduce a `ConfigEntryNotReady`
        para que Home Assistant reintente el setup, en vez de dejar el cliente colgado."""
        # import aquí (executor): a nivel de módulo causa un blocking-call warning en el loop.
        import paho.mqtt.client as mqtt

        cfg = self._config
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.client_id, protocol=mqtt.MQTTv311, clean_session=False)
        client.username_pw_set(cfg.tuserid, cfg.password)
        client.tls_set(**certificates.tls_paths(cfg.certs_dir),
                       cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.tls_insecure_set(True)  # el broker usa un CN que no coincide

        def _on_connect(cl, _userdata, _flags, rc, _props=None):
            ok = (rc == 0) or (getattr(rc, "value", 1) == 0)
            _LOGGER.info("[auto] MQTT on_connect rc=%s → %s (sub %s)",
                         rc, "conectado" if ok else "RECHAZADO", cfg.topic if ok else "-")
            if ok:
                cl.subscribe(cfg.topic, qos=1)
            self._on_connected(ok, rc)

        def _on_disconnect(_cl, _userdata, *args):
            _LOGGER.info("[auto] MQTT desconectado")
            # La firma varía entre versiones de paho: el rc puede ser el primer o el segundo
            # posicional. Se normaliza aquí para que el llamador no tenga que saberlo.
            rc = args[1] if len(args) > 1 else (args[0] if args else None)
            self._on_disconnected(rc)

        def _on_subscribe(_cl, _userdata, _mid, reason_codes, _props=None):
            """Resultado del SUBSCRIBE. Un broker puede ACEPTAR la conexión y DENEGAR el topic.

            Sin esto, ese caso es indistinguible de «el coche no ha dicho nada»: la integración
            se queda conectada y muda para siempre, y el informe de diagnóstico enseña un
            `car_connected: true` tranquilizador que no significa lo que parece."""
            # MQTT 3.1.1: 0/1/2 = QoS concedido, 0x80 = rechazado. paho VERSION2 los envuelve
            # en ReasonCode, que compara y se imprime como su valor.
            granted = [str(rc) for rc in (reason_codes or [])]
            ok = bool(reason_codes) and all(
                getattr(rc, "value", rc) != 0x80 for rc in reason_codes
            )
            detail = ", ".join(granted) or "sin respuesta"
            if ok:
                _LOGGER.info("[auto] MQTT suscrito a %s (QoS %s)", cfg.topic, detail)
            else:
                _LOGGER.error(
                    "[auto] MQTT SUSCRIPCIÓN RECHAZADA a %s (%s): la conexión está viva pero el "
                    "coche no podrá entregar telemetría", cfg.topic, detail)
            self._on_subscribed(ok, detail)

        def _on_message(_cl, _userdata, msg):
            self._on_message(msg.payload)

        client.on_connect = _on_connect
        client.on_disconnect = _on_disconnect
        client.on_subscribe = _on_subscribe
        client.on_message = _on_message
        # [H4] backoff de reconexión: el de paho por defecto es 1 s fijo, o sea un intento por
        # segundo indefinidamente con la red caída — justo el patrón que los gateways sancionan.
        client.reconnect_delay_set(min_delay=MQTT_RECONNECT_MIN_S, max_delay=MQTT_RECONNECT_MAX_S)
        try:
            client.connect(cfg.host, cfg.port, keepalive=MQTT_KEEPALIVE_S)
        except Exception as err:
            raise ConfigEntryNotReady(
                f"conexión MQTT al coche fallida ({cfg.host}:{cfg.port}): {err}"
            ) from err
        client.loop_start()
        self._client = client

    def disconnect(self) -> None:
        """Apagado limpio. Bloqueante (`loop_stop` hace join del hilo) → ejecutar en executor.

        `disconnect()` ANTES de `loop_stop()`: así el bucle procesa el DISCONNECT y el hilo sale
        sin quedarse esperando al keepalive."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception as err:
            _LOGGER.debug("[auto] error al desconectar MQTT: %s", err)
