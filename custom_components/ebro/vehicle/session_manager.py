"""Salud de la sesión y del PIN: qué está roto y qué debe hacer Home Assistant.

Hay dos credenciales distintas y confundirlas es el error clásico de esta integración:

* la **sesión** (token de la cuenta) hace funcionar sensores y lecturas. Si muere, el remedio
  es reautenticarse;
* el **PIN de comandos** (4 cifras) solo autoriza los comandos remotos. Si es erróneo, la
  sesión sigue perfectamente viva y los sensores funcionan — proponer reautenticar sería el
  remedio equivocado, porque reautenticar no cambia el PIN.

Este módulo reúne lo que antes eran ocho métodos del coordinator: el keep-alive, el control de
sesión, la apertura de la reautenticación, el aviso persistente y el Repair del PIN. Todos
comparten una misma pregunta y estaban intercalados con MQTT, sondeo y carga.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from ..const import CONF_VEHICLE_NAME, DEFAULT_SESSION_EVERY, DEFAULT_VEHICLE_NAME, DOMAIN
from .timers import KEEPALIVE, TimerRegistry

_LOGGER = logging.getLogger(__name__)

# Marcador estable devuelto por `core/session.check()` para «token muerto». Se enruta sobre
# ESTO, nunca sobre el texto humano: basta retocar (o traducir) un mensaje para que la tarjeta
# «Reautenticar» dejara de abrirse.
STATUS_EXPIRED = "EXPIRED"

# Aviso de sesión caducada. NO vive en `strings.json` porque ese fichero tiene un esquema fijo
# que hassfest valida, y `persistent_notification` no admite `translation_key`. Es la única
# cadena localizada a mano del componente; el resto sale de translations/.
_SESSION_NOTICE = {
    "es": (
        "{vehiculo}: sesión caducada",
        "La integración ya no puede hablar con el coche: los comandos y los sensores "
        "se quedan parados hasta que vuelvas a autenticarte.\n\n"
        "Ve a **Ajustes → Dispositivos y servicios → Ebro Auto → Reautenticar** e "
        "introduce de nuevo la contraseña de tu cuenta."
    ),
    "en": (
        "{vehiculo}: session expired",
        "The integration can no longer talk to the car: commands and sensors "
        "stay frozen until you re-authenticate.\n\n"
        "Go to **Settings → Devices & services → Ebro Auto → Reconfigure/"
        "Re-authenticate** and enter your account password again."
    ),
}


class SessionManager:
    """Mantiene viva la sesión y enruta el remedio cuando algo la rompe.

    Recibe sus dependencias explícitamente (y no el coordinator entero) para que se vea de un
    vistazo qué necesita: el entry para abrir la reautenticación, el registro de timers para
    el keep-alive, y dos callbacks — de dónde sale el contexto del vehículo y dónde se
    publica el estado.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        timers: TimerRegistry,
        *,
        get_ctx: Callable[[], object],
        publish: Callable[[dict], None],
        get_diag: Callable[[], object | None] = lambda: None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._timers = timers
        self._get_ctx = get_ctx
        self._publish = publish
        self._get_diag = get_diag

    # ───────────────── keep-alive (refresco de token periódico) ─────────────────
    def start_keepalive(self) -> None:
        """Programa un refresco de sesión periódico para no dejar caducar el token.

        `_bff_login` renueva por sí solo el access_token con el refresh_token cuando caduca →
        volver a comprobar la sesión cada `DEFAULT_SESSION_EVERY` mantiene viva la sesión
        también en reposo (rotación del refresh_token antes de que su ventana se cierre) y
        evita una reautenticación sorpresa por inactividad. Como bonus actualiza las entidades
        de sesión. No protege de la apertura de la app oficial (sesión única del lado de la
        nube → hace falta reautenticar igual)."""
        if self._timers.is_armed(KEEPALIVE):
            return
        self._timers.arm(KEEPALIVE, lambda: async_track_time_interval(
            self.hass, self._keepalive_cb, timedelta(seconds=DEFAULT_SESSION_EVERY)
        ))

    async def _keepalive_cb(self, _now) -> None:
        try:
            # Primero la renovación PROACTIVA: si el token está al final de su vida se renueva
            # ahora, con la sesión aún viva. Esperar a que caduque significa renovar hasta 15
            # minutos después del vencimiento (la cadencia de este timer), al extremo de la
            # ventana útil del refresh_token. Solo lectura hacia la nube.
            renewed, reason = await self.hass.async_add_executor_job(self._refresh_early)
            if renewed:
                _LOGGER.debug("[keepalive] token renovado por adelantado")
            elif reason not in ("no_hace_falta", "no_determinable"):
                _LOGGER.debug("[keepalive] renovación anticipada fallida: %s", reason)
            ok, detail = await self.async_check()
            _LOGGER.debug("[keepalive] sesión %s — %s", "ok" if ok else "KO", detail)
        except Exception as err:
            _LOGGER.debug("[keepalive] error no bloqueante: %s", err)

    def _refresh_early(self) -> tuple[bool, str]:
        from ..core import session as SESSION

        return SESSION.refresh_if_expiring(self._get_ctx())

    # ───────────────── control de sesión ─────────────────
    async def async_check(self) -> tuple[bool, str]:
        """Comprueba la sesión, publica el estado y abre el remedio si hace falta."""
        ok, detail, status = await self.hass.async_add_executor_job(self._check)
        self._publish({"session_ok": ok, "session_detail": detail})

        diag = self._get_diag()
        if diag is not None:
            # `status` es el marcador estable, `detail` el texto humano: registrar ambos deja
            # ver enseguida un posible desalineamiento entre los dos (una sesión KO que NO abre
            # la reauth, o viceversa).
            diag.record("session", ok=ok, status=status, detail=detail,
                        triggered_reauth=(status == STATUS_EXPIRED))

        # Solo EXPIRED = token muerto; un NET_ERROR es transitorio y NO debe hacer
        # reautenticar en balde al usuario. HA deduplica: si una reauth ya está en curso no
        # abre otra.
        if status == STATUS_EXPIRED:
            self.notify_expired()
            self.entry.async_start_reauth(self.hass)
        elif ok:
            self.dismiss_expired()
        return ok, detail

    def _check(self) -> tuple[bool, str, str]:
        from ..core import session as SESSION

        ok, detail, status = SESSION.check(self._get_ctx())
        # Defensa contra el drift de los dos literales: si core/session.py cambiara el valor de
        # STATUS_EXPIRED, la reauth seguiría saltando (nos alineamos con el módulo, que es la
        # fuente, en vez de comparar a ciegas la constante local).
        if status == getattr(SESSION, "STATUS_EXPIRED", STATUS_EXPIRED):
            status = STATUS_EXPIRED
        return ok, detail, status

    # ───────────────── aviso persistente ─────────────────
    # La sola tarjeta «Reautenticar» es fácil de no ver: la integración puede quedarse parada
    # durante horas sin que nadie se dé cuenta (ha pasado de verdad). La notificación dice en
    # claro QUÉ está roto y QUÉ hacer, y desaparece sola cuando la sesión vuelve a estar viva.
    @property
    def _notice_id(self) -> str:
        return f"{DOMAIN}_sessione_{self.entry.entry_id}"

    def notify_expired(self) -> None:
        from homeassistant.components import persistent_notification

        vehiculo = self.entry.data.get(CONF_VEHICLE_NAME) or DEFAULT_VEHICLE_NAME
        idioma = (self.hass.config.language or "en").split("-")[0]
        titulo, texto = _SESSION_NOTICE.get(idioma, _SESSION_NOTICE["en"])
        persistent_notification.async_create(
            self.hass, texto, title=titulo.format(vehiculo=vehiculo),
            notification_id=self._notice_id)

    def dismiss_expired(self) -> None:
        from homeassistant.components import persistent_notification

        persistent_notification.async_dismiss(self.hass, self._notice_id)

    # ───────────────── Repair «PIN de comandos erróneo» ─────────────────
    @property
    def pin_issue_id(self) -> str:
        return f"pin_wrong_{self.entry.entry_id}"

    @callback
    def raise_pin_issue(self, detail: str) -> None:
        """Crea un aviso de reparación (fixable) por el PIN de comandos erróneo.

        NO toca `session_ok`: la sesión es válida y los sensores funcionan; el problema es solo
        el PIN de 4 cifras de los comandos remotos. El aviso abre la reconfiguración del PIN."""
        from homeassistant.helpers import issue_registry as ir

        ir.async_create_issue(
            self.hass, DOMAIN, self.pin_issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="pin_wrong",
            data={"entry_id": self.entry.entry_id},
        )

    @callback
    def clear_pin_issue(self) -> None:
        """Cierra el aviso «PIN erróneo» (un comando tuvo éxito → PIN ahora correcto)."""
        from homeassistant.helpers import issue_registry as ir

        ir.async_delete_issue(self.hass, DOMAIN, self.pin_issue_id)

    # ───────────────── enrutado del remedio ─────────────────
    def route_remedy(self, err: Exception, *, in_loop: bool = True) -> str:
        """Error de comando → acción de remedio, decidida por la tabla única de `core/routing`.

        ÚNICO punto de enrutado: lo comparten el comando desde la UI y el respaldo del
        despertar. `in_loop=False` cuando se está en un hilo executor, donde las llamadas a
        Home Assistant deben programarse en el bucle de eventos.

        Antes eran dos cadenas de `if` escritas a mano, y la del respaldo clasificaba como
        «PIN erróneo» rechazos que eran de permisos o de sesión: proponía el remedio
        equivocado y, peor, acercaba el bloqueo real de la cuenta por una causa ajena al PIN.
        """
        from ..core import routing

        action = routing.action_for_reason(getattr(err, "reason", None))
        if action == routing.ACTION_REAUTH:
            if in_loop:
                self.entry.async_start_reauth(self.hass)
            else:
                self.hass.add_job(self.entry.async_start_reauth, self.hass)
        elif action == routing.ACTION_REPAIR_PIN:
            if in_loop:
                self.raise_pin_issue(str(err))
            else:
                self.hass.add_job(self.raise_pin_issue, str(err))
        # ACCION_AVISO: ningún remedio automático. El detalle ya lo publicó el `emit` en
        # «Resultado del comando», así que el usuario lo ve igualmente.
        return action
