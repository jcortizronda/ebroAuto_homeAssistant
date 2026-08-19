"""Coordinator de Ebro Auto — la lógica MQTT del coche + sonda, de forma nativa.

Recibe por MQTT (mutual-TLS, paho en un hilo) los eventos 5A02 del coche, los parsea
(mismo mapeo: ver SENSORS) y mantiene el estado actual en `self.data`. Las entidades nativas
leen de aquí. Comandos/despertar/sonda/sesión se delegan al "núcleo de protocolo" en `core/`
(ejecutado en executor).
"""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ..const import (
    COMMAND_QUEUE_WAIT,
    COMMAND_SETTLE_S,
    COMMAND_SETTLE_STEP_S,
    CONF_PLUGGED_WAIT_MAX,
    CONF_POLL_CHARGING,
    CONF_POLL_MOVING,
    CONF_POLL_MOVING_IDLE,
    CONF_POLL_PARKED,
    CONF_POLL_PLUGGED,
    CONF_VEHICLE_NAME,
    DATA_VEHICLE_BRAND,
    DATA_VEHICLE_MODEL,
    DEFAULT_AWAKE_WINDOW,
    DEFAULT_PLUGGED_WAIT_MAX_MIN,
    DEFAULT_POLL_CHARGING_MIN,
    DEFAULT_POLL_MOVING_IDLE_MIN,
    DEFAULT_POLL_MOVING_MIN,
    DEFAULT_POLL_PARKED_MIN,
    DEFAULT_POLL_PLUGGED_MIN,
    DOMAIN,
    HV_WAIT_ATTEMPTS,
    POLL_WAKE_WAIT,
    VEHICLE_BRAND,
)
from ..helpers import field_on, realtime, to_float, truncate_status
from ..models import ChargePreferences, EbroConfigEntry
from . import certificates, charging
from .config import VehicleConfig, build_ctx
from .monitor import DiagMonitor
from .mqtt_client import EbroMqttClient, MqttConfig
from .poll_policy import PollIntervals
from .polling import PollController
from .session_manager import SessionManager
from .state import VehicleState
from .telemetry import (
    CLOCK_FIELDS,
    GEO_KEYS,
    content_fingerprint,
    format_command_result,
    geo_only,
    parse_car_message,
    unknown_fields,
)
from .timers import (
    AWAKE,
    TimerRegistry,
)

_LOGGER = logging.getLogger(__name__)


class EbroCoordinator(DataUpdateCoordinator):
    """Mantiene la conexión MQTT al coche y el estado; expone acciones vía core/."""

    def __init__(self, hass: HomeAssistant, entry: EbroConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        # El entry parseado UNA vez (ver vehicle_config): identidad, región y comportamiento.
        self.config = VehicleConfig.from_entry(entry)
        self.vin = self.config.vin
        self.tuserid = self.config.tuserid
        self.channel_id = self.config.channel_id
        self.car_host = self.config.car_host
        self.car_port = self.config.car_port
        self.tsp_host = self.config.tsp_host
        self.bff = self.config.bff
        self.pin = self.config.pin
        self.email = self.config.email
        self.sign_key = self.config.sign_key
        self.certs_src = self.config.certs_src
        self.awake_window = DEFAULT_AWAKE_WINDOW
        # almacenamiento por entrada (token + certs) en la carpeta de configuración de HA
        self.token_path = hass.config.path(f"{DOMAIN}_{self.vin}_token.json")
        self.certs_dir = hass.config.path(f"{DOMAIN}_{self.vin}_certs")

        opt = entry.options or {}
        # identidad del vehículo para el dispositivo HA. Prioridad: override manual (opciones) →
        # valor guardado en entry.data (config flow / relleno) → None (→ fallback en entity.py).
        override = str(opt.get(CONF_VEHICLE_NAME) or "").strip()
        data = entry.data
        self.vehicle_name = override or data.get(CONF_VEHICLE_NAME) or None
        self.vehicle_model = data.get(DATA_VEHICLE_MODEL) or None
        self.vehicle_brand = data.get(DATA_VEHICLE_BRAND) or None

        # La configuración la transporta el `CoreCtx` (ver `_build_ctx`), pasado por argumento,
        # no `os.environ`. Dos consecuencias concretas:
        #   * PIN y email NO entran nunca en el entorno del proceso Home Assistant, que es
        #     legible por cualquier cosa que corra dentro;
        #   * con dos vehículos configurados el segundo entry ya no sobrescribe la configuración
        #     del primero (antes era entorno de PROCESO, compartido por ambos).

        self._car: EbroMqttClient | None = None
        #: topic que esta integración sabe interpretar (se fija al conectar)
        self._car_topic: str | None = None
        # Registro ÚNICO de los timers (keep-alive, poll, seguimiento HV, sonda inicial, latido
        # de marcha). Antes eran cinco atributos `_*_unsub` cancelados en tres sitios y rearmados
        # en otros dos → sondeos huérfanos que contactaban la nube con la integración apagada.
        # Ahora el invariante «tras el stop ningún timer se rearma» vive en un solo sitio:
        # `TimerRegistry.close()`.
        self._timers = TimerRegistry()
        # Sondeo del canal REALTIME por ESTADO (solo lectura, nunca despierta el coche). Cinco
        # intervalos en MINUTOS de las opciones; 0 = desactivado en ese estado.
        self.poll_parked_min = int(opt.get(CONF_POLL_PARKED, DEFAULT_POLL_PARKED_MIN))
        self.poll_charging_min = int(opt.get(CONF_POLL_CHARGING, DEFAULT_POLL_CHARGING_MIN))
        self.poll_plugged_min = int(opt.get(CONF_POLL_PLUGGED, DEFAULT_POLL_PLUGGED_MIN))
        self.poll_moving_min = int(opt.get(CONF_POLL_MOVING, DEFAULT_POLL_MOVING_MIN))
        self.poll_moving_idle_min = int(opt.get(CONF_POLL_MOVING_IDLE, DEFAULT_POLL_MOVING_IDLE_MIN))
        # Tope (segundos; 0 = sin límite) de "enchufado sin cargar".
        self._plugged_wait_max_s = int(opt.get(CONF_PLUGGED_WAIT_MAX, DEFAULT_PLUGGED_WAIT_MAX_MIN)) * 60
        # Límite de carga por software. El interruptor «Limitar carga al %» y el number
        # «Límite de carga» escriben en las propiedades de abajo, que delegan aquí.
        self._charge_limiter = charging.ChargeLimiter()
        # Preferencias locales (hora/duración de carga, duración del clima) que fijan las
        # entidades `time` y `number` al añadirse. Nacen con sus valores por defecto para que
        # `build_charge_plan` sea válido incluso antes de que esas entidades existan.
        self.preferences = ChargePreferences()
        # Foto de las opciones aplicadas por ESTA instancia: el update listener la compara con
        # las actuales para saber si una actualización de la entrada tocó de verdad las opciones
        # (→ hace falta un reload) o solo `entry.data` (→ sin reload). Ver
        # `_async_options_updated` en __init__.py.
        self.applied_options = dict(opt)
        self._cmd_gate = asyncio.Lock()  # serializa los comandos: el coche ejecuta uno cada vez (cola, no rechazo)
        # Estado en vivo del coche (telemetría, posición, «cuándo habló»). El lock que lo
        # protege de los tres hilos que lo tocan — paho, executor y bucle de eventos — vive
        # dentro: aquí ya no se escribe ningún `with`. Ver `vehicle_state.py`.
        self.state = VehicleState(self.awake_window)
        self.data = {"fields": {}, "position": None,
                     "awake": False, "car_connected": False,
                     "car_subscribed": None, "car_subscribe_detail": None,
                     "car_topic": None,
                     "session_ok": None, "session_detail": "",
                     # — sensores de diagnóstico —
                     "cmd_status": None, "wake_status": None, "probe_status": None,
                     "last_seen": None, "last_wake": None, "last_pos_fix": None,
                     # Instante en que la telemetría del coche CAMBIÓ por última vez: dice cómo
                     # de fresco es el dato batería/odómetro mostrado. Ver `_on_probe_data` para
                     # el porqué de no usar el timestamp de la nube.
                     "car_data_ts": None,
                     "realtime": None}
        # Huella de la última telemetría vista, para notar cuándo cambia de verdad.
        # `None` = ninguna lectura hecha aún en esta sesión.
        self._data_fingerprint: str | None = None
        # Monitor de diagnóstico para el DESARROLLADOR (ver diag_monitor.py). Dormido salvo
        # que exista la bandera: mientras lo esté, cada punto de enganche es un solo
        # `is not None` — vale sobre todo para `_on_car_message`, que corre en cada push.
        self._diag_monitor = DiagMonitor(hass, self.vin, self.email)
        # contexto de los módulos core/ (config + estado por vehículo). Creado de forma perezosa
        # por la propiedad `ctx`, una sola vez: contiene el anti-bloqueo del PIN y la caché del
        # taskId, que deben sobrevivir de un comando a otro.
        self._ctx = None
        self._mqtt_up_ts = 0.0   # instante de la última conexión MQTT (uptime en el monitor)
        # Salud de la sesión y del PIN: keep-alive, reauth, aviso persistente y Repair. Recibe
        # callbacks en vez del coordinator entero, para que se vea qué necesita de él.
        # El bucle de sondeo: cuándo se le pregunta al coche. Ver `polling.py`.
        self.polling = PollController(self, PollIntervals(
            parked=self.poll_parked_min,
            plugged=self.poll_plugged_min,
            charging=self.poll_charging_min,
            moving=self.poll_moving_min,
            moving_idle=self.poll_moving_idle_min,
        ))
        self.session = SessionManager(
            hass, entry, self._timers,
            get_ctx=lambda: self.ctx,
            publish=self._update,
            get_diag=lambda: self._diag,
        )

    # ───────────────── límite de carga por software ─────────────────
    # `switch.EbroChargeLimitSwitch` y `number.EbroConfigNumber` escriben estos dos nombres;
    # delegan en el `ChargeLimiter`, que es quien decide y quien recuerda si ya se cortó.
    @property
    def charge_limit_enabled(self) -> bool:
        return self._charge_limiter.enabled

    @charge_limit_enabled.setter
    def charge_limit_enabled(self, value: bool) -> None:
        self._charge_limiter.enabled = bool(value)

    @property
    def charge_limit_soc(self) -> int:
        return self._charge_limiter.target_soc

    @charge_limit_soc.setter
    def charge_limit_soc(self, value: int) -> None:
        self._charge_limiter.target_soc = int(value)

    # ───────────────── certificados mutual-TLS ─────────────────
    async def async_provision_certs(self) -> tuple[bool, str]:
        """Garantiza los certificados mutual-TLS de este vehículo. Devuelve (ok, detalle).

        La estrategia (ya están / carpeta del usuario / bundle por región) vive en
        `certificates.provision`; aquí solo se traslada al executor, porque toca el disco.
        """
        return await self.hass.async_add_executor_job(self._provision_certs)

    def _provision_certs(self) -> tuple[bool, str]:
        return tuple(certificates.provision(self.certs_dir, self.car_host, self.certs_src))

    # ───────────────── ciclo de vida MQTT del coche ─────────────────
    async def async_start(self) -> None:
        """Inicia la conexión MQTT al coche (paho corre en su propio hilo)."""
        await self.async_setup_diag()
        await self.hass.async_add_executor_job(self._connect_car)

    # ───────────────── monitor de diagnóstico (desarrollador) ─────────────────
    async def async_setup_diag(self) -> None:
        """Enciende el monitor si existe la bandera. Ver `diag_monitor.DiagMonitor`."""
        await self._diag_monitor.async_setup()

    # ───────────────── sesión (delegada en SessionManager) ─────────────────
    def async_start_keepalive(self) -> None:
        """Arranca el refresco periódico de sesión. Ver `session_manager.SessionManager`."""
        self.session.start_keepalive()

    async def async_check_session(self) -> tuple[bool, str]:
        """Comprueba la sesión, publica el estado y abre el remedio si hace falta."""
        return await self.session.async_check()

    # ───────────────── estado «coche despierto» (con vencimiento) ─────────────────
    # El coche se considera despierto mientras siga mandando mensajes. Antes el flag se
    # encendía en cada mensaje y no se apagaba NUNCA: bastaba un solo push para que quedara
    # `on` durante días. No era un defecto solo estético — `do_wake` pregunta justo a este flag
    # si el coche ya está despierto (`core/wake.py`), así que el botón «Despertar coche»
    # respondía «ya despierto» y no mandaba nada, saltándose también el respaldo por Localizar.
    @property
    def timers(self) -> TimerRegistry:
        """El registro de timers. Lo usa `PollController` para armar y cancelar el bucle."""
        return self._timers

    @property
    def charge_limiter(self):
        """El límite de carga por software. Lo consulta el sondeo para afinar cerca del corte."""
        return self._charge_limiter

    @property
    def plugged_wait_max_s(self) -> int:
        """Tope (segundos; 0 = sin límite) de «enchufado sin cargar». Lo publica el informe
        de diagnóstico, que no debe leer atributos privados."""
        return self._plugged_wait_max_s

    @property
    def _diag(self):
        """El grabador del monitor, o `None` si está dormido (que es lo normal).

        Se conserva el nombre corto porque aparece en cada punto de enganche del componente y
        el patrón `if self._diag is not None:` es lo que mantiene el coste en cero."""
        return self._diag_monitor.recorder

    @property
    def diag_recorder(self):
        """El monitor de diagnóstico, o `None`. Lo publica el informe de diagnóstico, que no
        debe leer atributos privados."""
        return self._diag_monitor.recorder

    @property
    def is_awake(self) -> bool:
        """El coche está publicando AHORA (se mide por el tiempo desde el último mensaje).

        Es la fuente de verdad, no el flag `data["awake"]`: entre el vencimiento y el timer que
        actualiza el flag hay una ventana en la que el flag miente."""
        return self._is_car_awake()

    def _is_car_awake(self) -> bool:
        """Fuente de verdad: se mide por el tiempo desde el último mensaje del coche."""
        return self.state.is_awake()

    @callback
    def _arm_awake_expiry(self) -> None:
        """Programa el apagado del flag al final de la ventana. Sin contacto con la nube: por
        eso NO pertenece al grupo del interruptor «Actualización automática»."""
        self._timers.arm(AWAKE, lambda: async_call_later(
            self.hass, self.awake_window + 1, self._awake_expiry_cb))

    async def _awake_expiry_cb(self, _now) -> None:
        if self._is_car_awake():
            self._arm_awake_expiry()   # mientras tanto ha llegado algo más: se reinicia
            return
        if self.data.get("awake"):
            self._update({"awake": False})

    # ───────────── sondeo del canal realtime (delegado en PollController) ─────────────
    def async_start_telemetry_poll(self) -> None:
        """Siembra el bucle de sondeo. Ver `polling.PollController`."""
        self.polling.start()

    @callback
    def set_poll_enabled(self, on: bool) -> None:
        """Activa/desactiva el sondeo en runtime (interruptor «Actualización automática»).

        Lo llama `switch.EbroPollingSwitch`; la firma se conserva por eso."""
        self.polling.set_enabled(on)

    @property
    def poll_enabled(self) -> bool:
        return self.polling.enabled

    async def async_probe(self, force: bool = False) -> None:
        """Una lectura del canal realtime, y la reprogramación del bucle.

        La usan el botón «Actualizar ubicación», el flanco de despertar y el propio bucle."""
        await self.hass.async_add_executor_job(self._probe, force)
        # Si mientras la lectura estaba en vuelo llegó el stop, NO rearmar: ese es exactamente
        # el sondeo huérfano que siguió interrogando a la nube con la integración apagada.
        if self.timers.closing:
            return
        self.polling.schedule_next()

    def _probe(self, force: bool = False) -> None:
        from ..core import probe as PROBE

        emit = self._status_emitter("probe_status", "probe")
        # force=True (bucle periódico): ignora el cooldown de la sonda.
        PROBE.probe_once(self.ctx, emit, force=force, on_data=self._on_probe_data)

    def _connect_car(self) -> None:
        """Conecta el cliente MQTT del coche. Bloqueante → corre en executor."""
        config = MqttConfig(host=self.car_host, port=self.car_port, tuserid=self.tuserid,
                            channel_id=self.channel_id, certs_dir=self.certs_dir,
                            # Solo con el monitor de diagnóstico encendido: escuchar toda la
                            # cuenta es una herramienta de investigación, no el modo normal.
                            discovery=self._diag is not None)
        # el topic que esta integración sabe interpretar; lo demás que llegue se apunta y ya
        self._car_topic = config.topic
        self._car = EbroMqttClient(
            config,
            on_message=self._on_car_message,
            on_connected=self._on_mqtt_connected,
            on_disconnected=self._on_mqtt_disconnected,
            on_subscribed=self._on_mqtt_subscribed,
        )
        self._car.connect()

    # Los tres callbacks de abajo llegan DESDE EL HILO DE PAHO: `_update` ya salta al bucle de
    # eventos por su cuenta (call_soon_threadsafe), y el monitor de diagnóstico escribe en su
    # propio hilo, así que ninguno toca Home Assistant directamente.
    def _on_mqtt_connected(self, ok: bool, rc) -> None:
        self._update({"car_connected": ok})
        if self._diag is not None:
            self._mqtt_up_ts = time.time()
            self._diag.record("mqtt_conn", event="connect", rc=str(rc), ok=ok)

    def _on_mqtt_subscribed(self, ok: bool, detail: str, topic: str = "") -> None:
        """El broker puede aceptar la CONEXIÓN y denegar el TOPIC, y hasta ahora los dos casos
        se veían igual desde fuera: `car_connected: true` y ni un mensaje. Publicarlo separa
        «el coche no ha dicho nada» de «no nos dejan escuchar».

        El TOPIC concedido importa tanto como el sí/no: con el descubrimiento activo, un
        «Granted QoS 1» a secas no distingue el comodín del topic exacto de respaldo, y son
        conclusiones opuestas."""
        self._update({"car_subscribed": ok, "car_subscribe_detail": detail,
                      "car_topic": topic or None})
        if self._diag is not None:
            self._diag.record("mqtt_conn", event="subscribe", ok=ok, detail=detail, topic=topic)

    def _on_mqtt_disconnected(self, rc) -> None:
        self._update({"car_connected": False})
        if self._diag is not None:
            # uptime de la sesión recién caída: sesiones cortísimas = flapping.
            up = round(time.time() - self._mqtt_up_ts, 1) if self._mqtt_up_ts else None
            self._mqtt_up_ts = 0.0
            self._diag.record("mqtt_conn", event="disconnect", rc=str(rc), uptime_s=up)

    def async_stop(self) -> None:
        """[MED] Apagado MQTT. Bloqueante (loop_stop hace join del hilo paho) → llamado en
        executor desde async_unload_entry. `disconnect()` ANTES de `loop_stop()`: así el loop
        procesa el CONNACK/DISCONNECT y el hilo sale limpio sin un join que espera el keepalive."""
        # teardown ÚNICO. `close()` cancela todos los timers y prohíbe cualquier futuro `arm()`:
        # una lectura ya en vuelo que al regresar intentara rearmar el seguimiento ya no puede.
        # Antes cada timer se cancelaba a mano aquí, y bastaba olvidar uno (o rearmarlo en otro
        # sitio) para dejar un poll huérfano en la nube.
        self._timers.close()
        # NB: sin `disarm` — la descarga no consume la ventana del monitor, que retoma en el
        # reload con el vencimiento original (calculado sobre el mtime de la bandera).
        self._diag_monitor.cancel_expiry()
        self._diag_monitor.shutdown()
        if self._car is not None:
            self._car.disconnect()
            self._car = None

    def _on_car_message(self, payload: bytes, topic: str | None = None) -> None:
        """Mensaje del coche (hilo paho → push hacia HA).

        La INTERPRETACIÓN del payload vive en `telemetry.parse_car_message` (pura). Aquí queda
        lo que necesita estado vivo: el lock que comparte con el executor, el flanco de
        despertar y los disparadores del sondeo."""
        if topic is not None and topic != self._car_topic:
            # Llega de un topic que no es el que la integración sabe interpretar: se APUNTA y no
            # se toca el estado. Lo que se busca aquí es descubrir por dónde publica la nube lo
            # que el topic conocido no trae (ver `MqttConfig.discovery_topic`); dar por hecho el
            # formato de un canal que no conocemos sería inventarse la telemetría.
            _LOGGER.info("[auto] mensaje en un topic NO conocido: %s (%d bytes)",
                         topic, len(payload or b""))
            if self._diag is not None:
                self._diag.record("mqtt_topic", topic=topic, size=len(payload or b""),
                                  sample=(payload or b"")[:400].decode("utf-8", "replace"))
            return
        message = parse_car_message(payload)
        if message is None:
            return
        data = message.data
        now = time.time()
        now_dt = dt_util.utcnow()
        # log detallado (debug): nombre y valor de cada campo, con el GPS enmascarado por privacidad
        _fields_log = {k: ("…" if k in GEO_KEYS else v) for k, v in data.items()}
        _LOGGER.debug("[auto] mensaje recibido (svc %s): %d campos %s",
                      message.service_type or "?", len(data), _fields_log)

        patch = {"last_seen": now_dt}

        # [MED] push de POSICIÓN → device_tracker. `message.geo` ya está filtrado: solo
        # geolocalización, y solo si el TIPO de mensaje es el de posición.
        if message.geo:
            patch["position"] = self.state.set_position(message.geo)
            patch["last_pos_fix"] = now_dt

        # El estado y el flanco de despertar se resuelven en la MISMA operación: `was_awake` se
        # mide contra el instante anterior, y leerlo aparte abriría una ventana en la que otro
        # mensaje ya habría movido la marca.
        fields_copy, was_awake = self.state.record_message(message.state_fields, now)

        # [diag] auto-descubrimiento de campos que el coche manda y `META` no mapea. Fuera del
        # lock y solo con el monitor encendido: el camino caliente queda en un `is not None`.
        if self._diag is not None:
            for k, v in unknown_fields(message):
                self._diag.note_unknown_field(k, v, message.service_type)

        patch.update({"fields": fields_copy, "awake": True})
        if message.is_confirmation:
            patch["cmd_status"] = format_command_result(data)
            # la confirmación hace avanzar last_seen → _settle_after_command sale enseguida, así el
            # siguiente comando en cola parte en cuanto el coche confirma este.
        if not message.meaningful:
            patch.pop("last_seen", None)      # el latido de marcha no es "contacto con datos"
        # refresca HA si hay datos reales, o en el flanco de despertar (para que "Coche despierto"
        # pase a on); los latidos sucesivos ya no generan churn de estado ni de recorder.
        if message.meaningful or not was_awake:
            self._update(patch)

        # El estado «despierto» tiene un vencimiento: sin él, quedaría encendido para siempre
        # (ver `_coche_despierto`). Se rearma en cada mensaje, desde el loop porque aquí estamos
        # en el hilo de paho.
        self.hass.loop.call_soon_threadsafe(self._arm_awake_expiry)

        # [H3] flanco de despertar → una sonda realtime (solo lectura). La programación de la
        # tarea DEBE hacerse en el loop: desde el hilo paho usa call_soon_threadsafe.
        if not was_awake:
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self.async_probe())
            )

        # [disparadores del sondeo] dos eventos MQTT gratis arrancan o aceleran el bucle: el
        # FLANCO de la ráfaga (el coche se ha puesto en marcha) y el CABLE al conectarse o
        # desconectarse (posible carga o fin de carga). Sin ninguno de los dos, el bucle se
        # relaja solo en su próxima reprogramación por tiempo.
        trigger = self.polling.note_message(now)
        if "chargeGunState" in data:
            self.polling.note_plug_change(bool(field_on(data["chargeGunState"])), now)
            trigger = True
        # respeta el switch "Actualización automática" (y el stop); no dispares en confirmaciones.
        if trigger and not message.is_confirmation and self.poll_enabled and not self.timers.closing:
            self.hass.loop.call_soon_threadsafe(
                lambda: self.hass.async_create_task(self.async_probe(force=True))
            )

    def _status_emitter(self, key: str, label: str):
        """Fabrica el callback `emit` que los módulos de `core/` usan para narrar sus pasos.

        Los cinco sitios que lo necesitaban (comando, despertar, sonda, refresco completo)
        escribían el mismo closure: log + publicación en el sensor de diagnóstico
        correspondiente, recortada al máximo que admite un estado de HA.
        """
        def emit(message) -> None:
            _LOGGER.info("[%s] %s", label, message)
            self._update({key: truncate_status(message)})

        return emit

    def _update(self, patch: dict) -> None:
        """Actualiza self.data y notifica a las entidades (thread-safe desde el hilo paho)."""
        self.hass.loop.call_soon_threadsafe(self._apply_update, patch)

    def _apply_update(self, patch: dict) -> None:
        self.data = {**self.data, **patch}
        self.async_set_updated_data(self.data)

    # ───────────────── contexto para los módulos core/ ─────────────────
    def _build_ctx(self):
        """Construye el `CoreCtx` de ESTE vehículo. La fábrica es única (`vehicle_config`),
        compartida con el config flow; aquí solo se aportan las rutas por VIN.

        El taskId se guarda en la carpeta de configuración y por VIN: así sobrevive a las
        actualizaciones de HACS y no se comparte entre vehículos."""
        return build_ctx(
            self.config,
            token_path=self.token_path,
            taskid_file=self.hass.config.path(f"{DOMAIN}_{self.vin}_taskid.txt"),
        )

    @property
    def ctx(self):
        """El contexto del vehículo, creado una sola vez.

        El ESTADO por vehículo (anti-bloqueo del PIN, taskId en caché, cooldown de despertar y
        sonda) vive aquí dentro: debe ser el mismo objeto durante toda la vida del entry, si no
        el contador anti-bloqueo se pondría a cero en cada comando y la protección contra el
        bloqueo de la cuenta no saltaría nunca."""
        if self._ctx is None:
            self._ctx = self._build_ctx()
        # el monitor de diagnóstico se enciende/apaga en runtime → se realinea en cada uso
        # (es una simple referencia a función, coste nulo)
        self._ctx.diag_hook = self._diag_monitor.record
        return self._ctx

    # NB: no existe un `_apply_pin_change`. Reconfigurar el PIN RECARGA el entry, así que nace un
    # coordinator nuevo con un contexto nuevo — el PIN actualizado nos llega solo. Sigue haciendo
    # falta el reseteo EXPLÍCITO del anti-bloqueo (`ctx.reset_pin_lockout()` en `repairs.py`/
    # `config_flow.py`), porque ese vive en memoria y sobreviviría.

    # ───────────────── cola de comandos (el coche ejecuta uno cada vez) ─────────────────
    async def _settle_after_command(self) -> None:
        """Pausa tras un comando, antes de que parta el siguiente de la cola: espera la
        confirmación del coche (la llegada del push MQTT hace avanzar `last_seen`) o
        COMMAND_SETTLE_S, lo que llegue antes. Sirve para no volver a golpear al coche mientras
        sigue ocupado (A00082)."""
        anchor = self.data.get("last_seen")
        deadline = time.monotonic() + COMMAND_SETTLE_S
        while time.monotonic() < deadline:
            await asyncio.sleep(COMMAND_SETTLE_STEP_S)
            if self.data.get("last_seen") != anchor:
                return   # el coche ha confirmado → se puede pasar al siguiente comando

    # ───────────────── acciones (delega a core/, en executor) ─────────────────
    async def async_send_command(self, key: str, params: dict | None = None) -> str:
        """Envía un comando serializado tras una cola: el coche ejecuta uno cada vez, así que un
        segundo comando ESPERA su turno en vez de fallar. Límite de espera COMMAND_QUEUE_WAIT.

        El llamador vuelve en cuanto el comando se ha enviado (la UI sigue reactiva); el hueco de
        la cola queda ocupado un poco más en segundo plano — hasta la confirmación del coche o
        COMMAND_SETTLE_S — para que el SIGUIENTE de la cola no parta mientras el coche sigue ocupado."""
        try:
            await asyncio.wait_for(self._cmd_gate.acquire(), timeout=COMMAND_QUEUE_WAIT)
        except TimeoutError as err:
            raise HomeAssistantError(
                "El coche sigue ocupado con los comandos anteriores — reinténtalo en unos instantes."
            ) from err
        t0 = time.monotonic()
        try:
            res = await self.hass.async_add_executor_job(self._send_command, key, params)
        except Exception as err:
            self._cmd_gate.release()   # envío fallido → libera enseguida el hueco
            if self._diag is not None:
                self._diag.record("command", key=key, ok=False,
                                  duration_ms=int((time.monotonic() - t0) * 1000),
                                  reason=getattr(err, "reason", None),
                                  code=getattr(err, "code", None),
                                  err_type=type(err).__name__, msg=str(err))
            # enrutado por CAUSA mediante la tabla única. Ambos caminos (este y el respaldo de
            # `_wake`) llaman al MISMO helper: antes eran dos cadenas de `if` escritas a mano, y
            # la del respaldo clasificaba como «PIN erróneo» rechazos que eran de permisos o de
            # sesión.
            self.session.route_remedy(err)
            raise
        if self._diag is not None:
            self._diag.record("command", key=key, ok=True,
                              duration_ms=int((time.monotonic() - t0) * 1000), result=res)
        # comando conseguido → un posible aviso "PIN erróneo" ya no tiene razón de ser
        self.session.clear_pin_issue()

        async def _hold_then_release() -> None:
            try:
                await self._settle_after_command()
            finally:
                self._cmd_gate.release()

        self.hass.async_create_background_task(_hold_then_release(), f"{DOMAIN}_cmd_settle")
        return res

    def _send_command(self, key: str, params: dict | None = None) -> str:
        from ..core import commands as CMD
        msgs: list[str] = []

        publish = self._status_emitter("cmd_status", "cmd")

        def emit(m):
            msgs.append(str(m))
            publish(m)

        CMD.send(self.ctx, key, emit=emit, params=params)
        return msgs[-1] if msgs else "enviado"

    async def async_query_theft(self) -> int | None:
        """Estado de la alarma vía REST (solo lectura); None si no está disponible."""
        return await self.hass.async_add_executor_job(self._query_theft)

    def _query_theft(self) -> int | None:
        from ..core import commands as CMD
        return CMD.query_theft_switch(self.ctx)

    async def async_wake(self) -> None:
        await self.hass.async_add_executor_job(self._wake)

    def _wake(self) -> None:
        from ..core import wake as WAKE

        emit = self._status_emitter("wake_status", "wake")

        self._update({"last_wake": dt_util.utcnow()})
        # is_awake: si el coche ya está publicando en MQTT no hace falta el SMS.
        result = WAKE.do_wake(self.ctx, emit,
                              is_awake=self._is_car_awake, send_sms=True)
        # [FALLBACK] smsAwaken poco fiable (test 2026-06-21: A07900 dos veces) → si no despertó
        # el coche, recurre a un comando REAL (vehicleLocation), que lo despierta al primer golpe
        # y devuelve también el GPS. A nivel coordinator para no crear imports circulares (wake.py
        # lo importa commands.py).
        if not (isinstance(result, dict) and result.get("online")):
            if self._is_car_awake():
                return  # mientras tanto ha llegado un mensaje MQTT → ya despierto
            emit("SMS de despertar sin efecto → recurro a Localizar (vehicleLocation)…")
            try:
                self._send_command("localizar_coche_gps")
            except Exception as err:
                emit(f"Localizar de reserva falló: {err}")
                # `_send_command` aquí se llama "a mano" (fuera de async_send_command), así que
                # el enrutado del remedio no saltaba: un PIN erróneo quemaba un intento de
                # anti-bloqueo EN SILENCIO, sin Repair ni reauth. Ahora pasa por el MISMO helper
                # de la UI. `dal_loop=False`: estamos en executor.
                self.session.route_remedy(err, in_loop=False)

    def build_charge_plan(self, switch_status: int) -> dict:
        """Plan de carga programada con la hora/duración actuales (entidades time/number).

        Compartido por el interruptor «Carga programada» y el botón «Aplicar carga programada».
        La construcción del plan (UTC, mínimo del coche, días) vive en `charging.build_plan`.
        """
        return charging.build_plan(
            start_minutes=self.preferences.charge_start_minutes,
            duration_minutes=self.preferences.charge_duration_minutes,
            switch_status=switch_status,
        )

    async def _send_charge_plan(self, plan: dict) -> None:
        """Envía un plan de carga programada con el interruptor general encendido.

        El body (`mainSwitch` + el array anidado) estaba escrito en dos sitios: aquí y en la
        parada por ventana vencida. Es el mismo sobre para dos planes distintos."""
        await self.async_send_command(
            "carga_prog_on", {"mainSwitch": 1, "chargeAppointPlans": [plan]})

    async def async_apply_scheduled_charge(self) -> None:
        """Botón "Aplicar carga programada": reenvía el plan al coche con la hora/duración actuales
        (mainSwitch=1), sin tener que apagar y volver a encender el interruptor. Útil tras cambiar
        la hora de inicio o la duración."""
        await self._send_charge_plan(self.build_charge_plan(1))

    def current_soc(self) -> float | None:
        """% de batería actual (dumpEnergy del canal realtime), o None si no es válido. Con la alta
        tensión apagada el coche manda 0 = marcador → se devuelve None (no un 0% real)."""
        soc = to_float(realtime(self.data).get("dumpEnergy"))
        return soc if soc is not None and soc > 0 else None

    async def async_stop_charge_via_schedule(self) -> None:
        """Para la carga imponiendo una programación cuya ventana YA terminó — la única forma
        de pararla en este coche (`chargeStartStopControl` da A00084). Ver `charging.build_stop_plan`."""
        await self._send_charge_plan(charging.build_stop_plan())

    def check_charge_limit(self) -> None:
        """Corta la carga si se alcanzó el objetivo. Se llama tras cada lectura realtime.

        La decisión (incluido el «una sola vez por sesión») vive en `ChargeLimiter`."""
        if self._charge_limiter.should_stop(
                charging=self.polling.is_charging(), soc=self.current_soc()):
            self.hass.async_create_task(self.async_stop_charge_via_schedule())

    def _probe(self, force: bool = False) -> None:
        from ..core import probe as PROBE

        emit = self._status_emitter("probe_status", "probe")

        # force=True (poll periódico): ignora el cooldown de la sonda.
        PROBE.probe_once(self.ctx, emit, force=force, on_data=self._on_probe_data)

    def _on_probe_data(self, data: dict) -> None:
        """Datos realtime (GPS/batería/velocidad/online) de la sonda → estado de posición.

        Corre en el hilo executor: `VehicleState` serializa el acceso y devuelve siempre una
        COPIA, así que la posición no se comparte por referencia con el bucle de eventos."""
        patch: dict = {"realtime": dict(data) if isinstance(data, dict) else data}
        # Frescura del dato batería/odómetro. YA NO se usa `resultTime`: medido el 22/07 que se
        # queda atrás mientras los valores cambian de verdad — resultTime parado a las 08:37:37
        # con la batería pasada de 41% a 40% después de las 08:59, y `time` a las 09:01:59. El
        # sensor anunciaba «datos viejos de 24 minutos» mientras estaban actualizados, es decir
        # exactamente lo contrario de su propósito. Se mira en cambio el CONTENIDO: el timestamp
        # avanza cuando la telemetría cambia respecto a la lectura anterior. Con el coche parado
        # se queda atrás — y es justo lo que el usuario quiere saber.
        if isinstance(data, dict):
            fingerprint = content_fingerprint(data)
            if self._data_fingerprint is None:
                # Primer frame tras el arranque: no hay nada con qué comparar. Se parte del
                # timestamp declarado por el coche en vez de hacer pasar por "ahora" un dato
                # que podría ser viejo de horas.
                for tk in CLOCK_FIELDS:
                    ms = to_float(data.get(tk))
                    if ms:
                        patch["car_data_ts"] = dt_util.utc_from_timestamp(ms / 1000)
                        break
            elif fingerprint != self._data_fingerprint:
                patch["car_data_ts"] = dt_util.utcnow()
            self._data_fingerprint = fingerprint
        if isinstance(data, dict) and "lat" in data and "lon" in data:
            patch["position"] = self.state.merge_position(geo_only(data))
            patch["last_pos_fix"] = dt_util.utcnow()
        self._update(patch)

    async def async_refresh_full_status(self) -> None:
        """Botón «Actualizar estado completo»: trae a HA odómetro/batería/tensión REALES.

        El canal realtime da los valores reales SOLO con la alta tensión encendida; no existe un
        comando "ligero" que fuerce un reporte fresco (verificado por la ingeniería inversa del
        SDK Chery). Por tanto: si la HV ya está encendida (marcha/carga) lee y basta; si no,
        enciende BREVEMENTE el clima (única forma de encender la alta tensión), lee los datos
        reales, y luego apaga el clima. ⚠️ ACTÚA sobre el coche: el clima queda encendido ~1 min."""
        emit = self._status_emitter("probe_status", "refresh")

        # 1) lectura inmediata: si la HV ya está encendida, ya está todo fresco
        await self.async_probe(force=True)
        if self.polling.is_hv_on():
            emit("Estado actualizado — alta tensión ya encendida ✅")
            return

        # 2) enciende el clima para despertar la alta tensión
        emit("Enciendo el clima ~1 min para leer datos reales (cuentakm/batería)…")
        try:
            await self.async_send_command("clima_on")
        except Exception as err:
            emit(f"No puedo encender el clima: {err}")
            return

        # 3) lee el realtime hasta que la alta tensión esté encendida (máx ~2,5 min)
        got = False
        for _ in range(HV_WAIT_ATTEMPTS):
            await asyncio.sleep(POLL_WAKE_WAIT)
            try:
                await self.async_probe(force=True)
            except Exception as err:
                _LOGGER.debug("[refresh] lectura realtime fallida: %s", err)
            if self.polling.is_hv_on():
                got = True
                break

        # 4) apaga siempre el clima (también en caso de fracaso)
        try:
            await self.async_send_command("clima_off")
        except Exception as err:
            _LOGGER.debug("[refresh] apagado del clima fallido: %s", err)
        emit("Estado actualizado con datos reales ✅" if got
             else "El coche no se encendió a tiempo — reinténtalo, o se actualizará en el próximo viaje")

    async def async_ensure_vehicle_identity(self) -> None:
        """Relleno best-effort de la identidad del vehículo (nombre/modelo/marca) para el
        dispositivo HA.

        Sirve a los entry creados ANTES de que el config flow la guardara: si falta en
        entry.data (y no hay override manual) la lee UNA vez de queryList y la persiste, así en
        los reinicios siguientes ya está en caché (sin nuevas llamadas). Solo lectura, no bloquea
        el setup: en caso de error queda el fallback "Ebro Auto"."""
        if str((self.entry.options or {}).get(CONF_VEHICLE_NAME) or "").strip():
            return  # override manual: no sobrescribir
        if self.entry.data.get(CONF_VEHICLE_NAME):
            return  # ya en caché
        info = await self.hass.async_add_executor_job(self._fetch_vehicle_identity)
        if not info or not info.get(CONF_VEHICLE_NAME):
            return
        self.vehicle_name = info.get(CONF_VEHICLE_NAME)
        self.vehicle_model = info.get(DATA_VEHICLE_MODEL)
        self.vehicle_brand = info.get(DATA_VEHICLE_BRAND)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, **info})  # → un reload (luego está en caché)

    def _fetch_vehicle_identity(self) -> dict | None:
        """queryList (solo lectura) → identidad del vehículo para el dispositivo de HA.

        La llamada y el parseo (el backend devuelve la lista bajo cuatro claves distintas)
        viven en `core/vehicles`, compartidos con el config flow."""
        try:
            from ..core import vehicles, wake

            ctx = self.ctx
            wake._bff_login(ctx)
            info = vehicles.identity(vehicles.query_list(ctx), self.vin)
            if not info:
                return None
            return {CONF_VEHICLE_NAME: info["name"],
                    DATA_VEHICLE_MODEL: info["model"],
                    # La marca es constante: esta integración es solo para Ebro.
                    DATA_VEHICLE_BRAND: VEHICLE_BRAND}
        except Exception as err:
            _LOGGER.debug("[vehiculo] identidad no recuperada: %s", err)
            return None
