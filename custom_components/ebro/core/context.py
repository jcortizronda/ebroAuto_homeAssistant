#!/usr/bin/env python3
"""`CoreCtx` — configuración y estado de UN vehículo, pasados explícitamente.

Un `CoreCtx` por vehículo, creado por el coordinator y pasado como primer argumento a
cada función de `core/`. Sin globales mutables de módulo, sin `os.environ` en el camino
activo de Home Assistant.

**Por qué el contexto contiene también ESTADO y no solo configuración.** El anti-bloqueo
del PIN, la caché del taskId, el cooldown del despertar y de la sonda son todos
*por vehículo*. Si estuvieran en globales de módulo, con dos coches configurados los
errores de PIN de uno bloquearían los comandos del otro, y el taskId generado para uno se
usaría para el otro. Ligarlos al contexto lo resuelve por construcción.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import os
import threading

from .pin_lockout import PinLockout

HERE = os.path.dirname(os.path.abspath(__file__))

# Valores por defecto de REGIÓN. Por defecto Ebro Auto (VERIFICADO 2026-07-27).
#  - BFF (login/auth/vmc) está en legend.ebroauto.com; la telemetría/control en tspconsole.
#  - tenant del login = "3000010" (numérico); el x-TenantId de la telemetría TSP = "euebro".
DEFAULT_BFF = "https://legend.ebroauto.com/api"
DEFAULT_TSP_HOST = "https://tspconsole-eu.ebroauto.com"
DEFAULT_CHANNEL_ID = "4"
DEFAULT_COUNTRY_ID = "1"
DEFAULT_TENANT_CODE = "3000010"          # tenant del login/BFF (legend). La telemetría TSP usa "euebro".
DEFAULT_TSP_TENANT = "euebro"            # x-TenantId para las llamadas de telemetría/control (tspconsole)



@dataclass
class _VehicleState:
    """Estado mutable que pertenece a UN vehículo (no es configuración).

    Todo lo que aquí dentro fuera global de módulo se convertiría, con dos coches
    configurados, en una interferencia entre cuentas distintas."""

    # anti-bloqueo del PIN: umbral y ventana son por cuenta (ver pin_lockout.py)
    lockout: PinLockout = field(default_factory=PinLockout)
    # taskId en caché: generarlo cuesta una vuelta de checkPassword (la parte lenta de cada
    # comando), pero está ligado al PIN y al VIN → no es compartible entre vehículos.
    taskid: str | None = None
    taskid_ts: float = 0.0
    # cooldown del despertar por SMS (rate-limit real del lado Chery) y de la sonda realtime
    last_sms_ts: float = 0.0
    last_probe_ts: float = 0.0
    # "uno cada vez" por vehículo: dos coches pueden despertarse/sondearse en paralelo
    wake_lock: threading.Lock = field(default_factory=threading.Lock)
    probe_lock: threading.Lock = field(default_factory=threading.Lock)
    # serializa el refresco del token: Chery rota el refresh_token en cada uso, dos
    # renovaciones en paralelo sobre el MISMO archivo invalidarían la sesión.
    token_lock: threading.Lock = field(default_factory=threading.Lock)
    # Resultado del ÚLTIMO refresco intentado (marcador estable, ver wake._refresh_token_detail)
    # + cuándo se intentó. Sirve a `session.check` para distinguir «el servidor ha revocado el
    # token» de «la red no ha funcionado»: sin esto, un corte de red se disfraza de sesión
    # muerta y hace pedir al usuario una reautenticación que no hacía falta.
    refresh_reason: str = ""
    refresh_ts: float = 0.0
    # Huella (sha256) del refresh_token que el servidor ha rechazado EXPLÍCITAMENTE, y
    # cuándo. Sirve para no volver a presentar una credencial ya rechazada: el token no
    # vuelve a ser válido por sí solo, así que reintentarlo en cada llamada es solo ruido
    # hacia el gateway Chery. Se pone a cero solo en cuanto el archivo contiene un token distinto.
    refresh_burned: str = ""
    refresh_burned_ts: float = 0.0
    # Generaciones de taskId SOLAPADAS. Se cuenta a la ENTRADA, fuera del lock del
    # anti-bloqueo: dentro vería siempre 1, porque ese lock serializa. Si al entrar resulta >1
    # hay un segundo hilo esperando el lock justo ahora — la condición de carrera que se vio en
    # vivo y que acerca el bloqueo de la cuenta. Es POR VEHÍCULO: como global de proceso, dos
    # coches configurados se contaminaban el contador (y el conftest tenía que resetearlo).
    mint_inflight: int = 0
    mint_inflight_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class CoreCtx:
    """Todo lo que necesita `core/` para operar sobre un vehículo.

    Se construye una vez por config entry (ver `EbroCoordinator._build_ctx`) y se pasa
    como primer argumento a las funciones de `core/`.
    """

    # — identidad de la cuenta/vehículo (por cuenta: nunca valores por defecto) —
    vin: str = ""
    tuserid: str = ""
    pin: str = ""
    email: str = ""
    sign_key: str = ""

    # — rutas por entrada en la carpeta de configuración de Home Assistant —
    token_path: str = ""
    taskid_file: str = ""
    # carpeta de los fuentes para los subprocesos de login (por defecto: este paquete)
    src_dir: str = HERE

    # — parámetros de región —
    tsp_host: str = DEFAULT_TSP_HOST
    bff: str = DEFAULT_BFF
    channel_id: str = DEFAULT_CHANNEL_ID
    country_id: str = DEFAULT_COUNTRY_ID
    tenant_code: str = DEFAULT_TENANT_CODE

    # — comportamiento —
    # generación automática del taskId: sin ella, los comandos no pueden partir (hace falta un
    # taskId validado por checkPassword). Desactivable solo para diagnóstico.
    mint_taskid: bool = True
    taskid_ttl: int = 600      # reutilización del taskId en caché, en segundos

    # — ritmos del despertar y de la sonda —
    # Vivían como constantes de módulo leídas de `os.environ` AL IMPORTAR, en `wake.py` y
    # `probe.py`. Eso las hacía imposibles de cambiar en runtime y de testear con
    # `monkeypatch.setenv` — había un test que documentaba justamente esa limitación en vez de
    # comprobar un comportamiento. Aquí son configuración del vehículo, como todo lo demás.
    #
    # `smsAwaken` TIENE un rate-limit real del lado de Chery: el cooldown no es una cortesía.
    wake_cooldown_s: int = 300      # mínimo entre dos smsAwaken realmente enviados
    wake_poll_attempts: int = 12    # ciclos de sondeo tras el despertar
    wake_poll_every_s: int = 5      # segundos entre un ciclo y el siguiente
    # Cada cuánto se concede un nuevo intento con un refresh_token que el servidor ya rechazó:
    # lo bastante raro para no ser machaque, lo bastante frecuente para reponerse solo si aquel
    # rechazo hubiera sido un desliz del gateway.
    retry_refresh_after_s: int = 3600
    # La sonda realtime es de SOLO LECTURA y el coche sigue online un buen rato tras
    # despertarse, así que las lecturas oportunistas frecuentes salen gratis. Los sondeos
    # programados usan `force=True` y se saltan el cooldown igualmente.
    probe_cooldown_s: int = 120
    # PRIVACIDAD: el registro en crudo de la sonda contiene VIN y coordenadas → OPT-IN. Vacío =
    # no se escribe nada. Solo para diagnóstico manual.
    probe_log_path: str = ""
    # Monitor de diagnóstico (diag.py): callback fijada por el coordinator SOLO con el monitor
    # encendido. `None` = dormido, coste nulo. `core/` no conoce el coordinator.
    diag_hook: object | None = None

    # — estado por vehículo —
    state: _VehicleState = field(default_factory=_VehicleState)

    def __post_init__(self) -> None:
        # Rutas de respaldo solo para uso por línea de comandos/diagnóstico: en Home
        # Assistant las fija siempre el coordinator (por VIN, en la carpeta de configuración).
        if not self.token_path:
            self.token_path = os.path.join(HERE, "token.json")
        if not self.taskid_file:
            self.taskid_file = os.path.join(HERE, "data", "taskid.txt")

    # ───────────────────────── comodidad ─────────────────────────
    @property
    def lockout(self) -> PinLockout:
        return self.state.lockout

    def diag(self, tipo: str, **fields_) -> None:
        """Registra un evento de diagnóstico, si el monitor está encendido. NUNCA debe
        alterar el flujo: el monitor observa, no participa."""
        hook = self.diag_hook
        if hook is None:
            return
        # un monitor roto no debe romper un comando: observa, no participa.
        with contextlib.suppress(Exception):
            hook(tipo, **fields_)

    def invalidate_taskid(self) -> None:
        """Descarta el taskId en caché (el coche lo ha rechazado o el PIN ha cambiado)."""
        self.state.taskid = None
        self.state.taskid_ts = 0.0

    def reset_pin_lockout(self) -> None:
        """Pone a cero el anti-bloqueo y el taskId: se usa cuando el usuario reconfigura el PIN."""
        self.state.lockout.reset()
        self.invalidate_taskid()


def ctx_from_environ() -> CoreCtx:
    """Contexto construido desde `os.environ` — SOLO para el uso por línea de comandos.

    Los módulos `core/` se pueden aún lanzar a mano para diagnóstico (`python -m …`) y en
    ese caso el entorno es la forma más cómoda de pasar los parámetros. Home Assistant NO
    pasa nunca por aquí: el coordinator construye el contexto desde el config entry, que es
    la fuente de verdad."""
    return CoreCtx(
        vin=os.environ.get("VIN", ""),
        tuserid=os.environ.get("TUSERID", ""),
        pin=os.environ.get("EBRO_PIN", ""),
        email=os.environ.get("EBRO_EMAIL", ""),
        sign_key=os.environ.get("EBRO_SIGN_KEY", ""),
        token_path=os.environ.get("EBRO_TOKEN_PATH", ""),
        taskid_file=os.environ.get("EBRO_TASKID_FILE", ""),
        src_dir=os.environ.get("EBRO_SRC_DIR", HERE),
        tsp_host=os.environ.get("TSP_HOST", DEFAULT_TSP_HOST),
        bff=os.environ.get("EBRO_BFF", DEFAULT_BFF),
        channel_id=os.environ.get("CHANNEL_ID", DEFAULT_CHANNEL_ID),
        country_id=os.environ.get("EBRO_COUNTRY_ID", DEFAULT_COUNTRY_ID),
        tenant_code=os.environ.get("EBRO_TENANT_CODE", DEFAULT_TENANT_CODE),
        mint_taskid=os.environ.get("EBRO_MINT_TASKID", "1") not in ("0", "", "false", "no"),
        wake_cooldown_s=int(os.environ.get("WAKE_COOLDOWN", "300")),
        wake_poll_attempts=int(os.environ.get("WAKE_POLL_N", "12")),
        wake_poll_every_s=int(os.environ.get("WAKE_POLL_EVERY", "5")),
        retry_refresh_after_s=int(os.environ.get("EBRO_RETRY_REFRESH", "3600")),
        probe_cooldown_s=int(os.environ.get("PROBE_COOLDOWN", "120")),
        probe_log_path=os.environ.get("EBRO_PROBE_LOG", ""),
    )
