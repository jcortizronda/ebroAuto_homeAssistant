"""Monitor de diagnóstico — herramienta para el DESARROLLADOR del componente, no para el usuario.

Registra en archivo los eventos de runtime que sirven para cazar bugs de campo (generación
de taskId con PIN concurrente, campos de telemetría no mapeados, resultado/latencia de los
comandos, reconexiones MQTT, timers HV huérfanos, salud de sesión). NO es una función de la
integración: no tiene interruptor en la interfaz, no aparece en el changelog y no se anuncia
al usuario.

ACTIVACIÓN — archivo «bandera» en la carpeta de configuración de Home Assistant:

    /config/ebro_diag.on        contenido = número de DÍAS (por defecto 3, máx 7)
                                  o bien `0` = SIN vencimiento (apagado manual)

Presente  → el monitor arranca al cargar la integración y se apaga SOLO al vencer (contado
            desde la fecha de modificación del archivo), que se renombra a `ebro_diag.off`.
            Ningún intervención manual necesaria.
            Con `0` en cambio queda encendido indefinidamente y se apaga SOLO borrando la
            bandera: usar cuando el evento a observar es raro y una ventana fija podría
            cerrarse justo antes de que ocurra. El archivo queda igualmente limitado por la
            rotación (2 MB + un `.1`).
Ausente   → el código está completamente DORMIDO: `coordinator.diag_recorder` y `commands.DIAG_HOOK`
            quedan `None`, así que cada punto de enganche es una sola comparación `is not
            None` y no se reserva nada. Coste nulo en el camino caliente (el callback MQTT
            corre por cada push del coche).

OCULTACIÓN EN ORIGEN — el principio que hace el archivo compartible. Los datos sensibles se
enmascaran cuando el evento ENTRA en el buffer, no al exportar: el `.jsonl` en disco NACE ya
ofuscado, así que aunque acabara adjunto a una issue por error no contendría secretos. La
geolocalización no se enmascara sino que se ELIMINA (dónde vives no debe salir ni aproximado).
Quedan a propósito EN CLARO solo `cp_code`/`cp_msg`, es decir código y mensaje en crudo de
`checkPassword`: no son sensibles y son la única forma de distinguir un PIN realmente erróneo
de un rechazo por permisos/parámetros.

Seguridad entre hilos: los eventos llegan de hilos distintos (el hilo paho para el MQTT, los
executors para los comandos, el loop de HA para la sesión) → deque y contadores viven bajo un
lock dedicado. La escritura en disco se delega a un hilo escritor con cola, para que ningún
I/O bloquee nunca el hilo paho ni el event loop.
"""
from __future__ import annotations

from collections import Counter, deque
import contextlib
import json
import math
import os
import queue
import re
import threading
import time
from typing import Any

from ..const import SECONDS_PER_DAY

# ───────────────────────── parámetros ─────────────────────────

# El nombre de la bandera vive en `const.DIAG_SWITCH_FILE`: es lo que `monitor.py`
# compone contra la carpeta de configuración de HA, y tenerlo aquí otra vez era un
# segundo sitio que actualizar. Aquí solo hace falta el nombre de DESPUÉS del apagado.
SWITCH_OFF = "ebro_diag.off"

DEFAULT_DAYS = 3
MAX_DAYS = 7
# Contenidos de la bandera que desactivan el autoapagado (comparación en minúscula).
_NO_EXPIRY = frozenset({"0", "siempre", "always", "inf"})
BUFFER_MAX = 500                 # eventos mantenidos en RAM (ring buffer para el diagnóstico HA)
FILE_MAX_BYTES = 2 * 1024 * 1024  # rotación del .jsonl (se mantiene también un .jsonl.1)
QUEUE_MAX = 2000                 # líneas en espera de escritura; pasado eso, se descarta y se cuenta
MAX_DEPTH = 6                    # profundidad máxima explorada en la ocultación recursiva
MAX_ITEMS = 60                   # elementos máximos por dict/lista (payloads patológicos)
MAX_STR = 400                    # longitud máxima de una cadena registrada

# ───────────────────────── ocultación ─────────────────────────

# Claves cuyo VALOR se sustituye por completo (comparación case-insensitive, a cada nivel de
# anidamiento). Identidad de cuenta, identidad de vehículo, material criptográfico.
REDACT_KEYS = {
    "email", "pin", "password", "passwd", "token", "usertoken", "access_token",
    "refresh_token", "accesstoken", "refreshtoken", "authorization", "sign",
    "taskid", "tuserid", "tuid", "secret", "certs_src",
    # NB: solo claves criptográficas ESPECÍFICAS. Un genérico "key" aquí ocultaría el NOMBRE
    # del comando y de las claves 5A02 no mapeadas (`key` es su campo) — es decir, justo lo que
    # los dos hooks sirven para mostrar. Los secretos en campos con otro nombre quedan cubiertos
    # por la pasada regex sobre hexadecimal largo/JWT/PEM.
    "privatekey", "secretkey", "apikey", "appkey", "keyfile", "clientkey",
    "vin", "carvin", "seq", "nickname", "fullname", "plate", "targa",
}

# Claves ELIMINADAS del todo, no enmascaradas: la posición no debe salir de ninguna forma.
DROP_KEYS = {
    "lat", "lon", "latitude", "longitude", "position", "gpslat", "gpslon",
    "gpstime", "positiontime", "altitude", "heading", "direction",
}

# Claves dejadas EN CLARO: código/mensaje en crudo de checkPassword, no sensibles e
# indispensables para entender si un fallo es realmente un PIN erróneo (ver docstring).
CLEAR_KEYS = {"cp_code", "cp_msg"}

REDACTED = "**REDACTED**"

# El único patrón de las coordenadas, aislado: lo reutiliza también `diagnostics.py` (el
# archivo de «Descargar diagnóstico»), donde el mismo defecto había vuelto a aparecer. Una
# sola implementación — dos copias de la misma regla divergen, y la que diverge es siempre la
# que uno se olvida de actualizar.
#
# Forma: 1-3 cifras enteras, punto, AL MENOS 4 decimales. Las coordenadas tienen 4-7 decimales;
# la telemetría real (tensiones "350.3", consumos "5.6") tiene 1-2 y no se toca.
# El `(?<![:\d.])` es ESENCIAL: sin él, el patrón se comía los segundos-y-microsegundos de los
# timestamps ISO (`…:07.428912` → `…**GEO**`), corrompiendo `last_seen`/`last_pos_fix` en el
# diagnóstico — es decir, justo los campos que sirven al soporte. Excluyendo lo que sigue a
# `:` (timestamp) o una cifra/punto (parte de un número más largo, ej. un epoch), quedan solo
# las coordenadas reales, precedidas por `=`, espacio, `,` o `(`.
_RE_COORD = re.compile(r"(?<![:\d.])-?\d{1,3}\.\d{4,}\b")


def scrub_coordinates(s: str) -> str:
    """Sustituye por `**GEO**` cada coordenada encontrada dentro de una cadena."""
    return _RE_COORD.sub("**GEO**", s) if s else s


# Red de seguridad sobre las CADENAS: intercepta un secreto incluso dentro de un campo con
# nombre desconocido, que la deny-list por clave no cubriría. El orden importa: los patrones
# más específicos (JWT, PEM) preceden a los genéricos (hexadecimal largo).
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN[^-]{0,50}-----.*?-----END[^-]{0,50}-----", re.S), "**PEM**"),
    (re.compile(r"-----BEGIN[^-]{0,50}-----"), "**PEM**"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+){0,2}"), "**JWT**"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "**EMAIL**"),
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "**VIN**"),
    (re.compile(r"\b\d{15,}\b"), "**NUM**"),          # tUserId y afines (ids numéricos largos)
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "**HEX**"),  # token/hash/SM4
    (re.compile(r"/config/ebro_\S*"), "**PATH**"),   # ruta por VIN en la carpeta de configuración
    # COORDENADA GEOGRÁFICA en un campo con cualquier nombre. `DROP_KEYS` quita la posición
    # cuando se reconoce por el NOMBRE de la clave; este patrón la coge también cuando el
    # nombre no dice nada — es el caso que causó la fuga vista en campo el 2026-07-20, donde
    # una coordenada había acabado bajo la clave `sample`.
    # A propósito estrecho: parte entera de 1-3 cifras y AL MENOS 4 decimales. No toca los
    # valores de telemetría reales (temperaturas "21.0", tensiones "384", porcentajes "72")
    # ni los timestamps epoch, que tienen la parte entera bastante más larga de 3 cifras.
    (_RE_COORD, "**GEO**"),
]


def _redact_str(s: str, extra: tuple[str, ...] = ()) -> str:
    """Oculta una cadena: primero los valores conocidos de la entrada (VIN, email), luego los patrones."""
    if not s:
        return s
    for val in extra:
        if val and len(val) >= 4 and val in s:
            s = s.replace(val, REDACTED)
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s[:MAX_STR]


def redact(obj: Any, extra: tuple[str, ...] = (), _depth: int = 0) -> Any:
    """Ocultación recursiva de un objeto arbitrario, aplicada EN LA CAPTURA.

    Tres reglas, en orden: las claves geo desaparecen, las claves sensibles pasan a
    `**REDACTED**`, todo lo demás baja recursivamente y cada cadena pasa igualmente por los
    patrones. Las claves de `CLEAR_KEYS` son la única excepción intencionada."""
    if _depth > MAX_DEPTH:
        return "**DEPTH**"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= MAX_ITEMS:
                out["**TRUNCATED**"] = len(obj) - MAX_ITEMS
                break
            ks = str(k)
            kl = ks.lower()
            if kl in DROP_KEYS:
                continue          # geolocalización: eliminada, no enmascarada
            if kl in CLEAR_KEYS:
                out[ks] = v if v is None else str(v)[:MAX_STR]
                continue
            if kl in REDACT_KEYS:
                out[ks] = REDACTED if v is not None else None
                continue
            out[ks] = redact(v, extra, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, extra, _depth + 1) for v in list(obj)[:MAX_ITEMS]]
    if isinstance(obj, str):
        return _redact_str(obj, extra)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _redact_str(str(obj), extra)


# ───────────────────── bandera de activación ─────────────────────

def read_switch(path: str) -> float | None:
    """Lee la bandera. Devuelve el instante de vencimiento (epoch) o None si el monitor no
    debe activarse. Si la ventana ya venció, apaga renombrando el archivo a `.off`.

    Contenido `0` (o `siempre`/`always`) → **sin vencimiento**: devuelve `math.inf` y el
    monitor queda encendido hasta que se apaga a mano borrando la bandera. Sirve cuando no se
    sabe cuánto durará la observación — un evento raro (comandos solapados, reload durante la
    carga) puede no ocurrir dentro de una ventana fija, y encontrar el monitor apagado solo
    significa haber perdido los días de espera. El archivo en disco queda igualmente limitado
    por la rotación (2 MB + un `.1`), así que «encendido para siempre» no es un riesgo de
    espacio; el coste es solo el logger verboso.

    La duración parte de la fecha de MODIFICACIÓN del archivo: `touch` sobre la bandera renueva
    la ventana sin tener que cambiar su contenido. Solo lectura + un posible rename: llamar en
    executor, nunca en el loop."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        with open(path) as fh:
            raw = fh.read(32).strip()
    except OSError:
        raw = ""
    if raw.lower() in _NO_EXPIRY:
        return math.inf
    try:
        days = int(raw) if raw else DEFAULT_DAYS
    except ValueError:
        days = DEFAULT_DAYS
    days = max(1, min(MAX_DAYS, days))
    until = st.st_mtime + days * SECONDS_PER_DAY
    if time.time() >= until:
        disarm_switch(path)
        return None
    return until


def disarm_switch(path: str) -> None:
    """Apaga la bandera renombrándola (`.on` → `.off`): el monitor no vuelve a arrancar en el
    próximo inicio, pero queda rastro de cuándo estuvo activo. Nunca lanza."""
    with contextlib.suppress(OSError):
        os.replace(path, os.path.join(os.path.dirname(path), SWITCH_OFF))


# ───────────────────────── registrador ─────────────────────────

class DiagRecorder:
    """Ring buffer + contadores + archivo JSONL rotativo, todo ya ocultado en origen."""

    def __init__(self, jsonl_path: str, vin: str = "", email: str = "",
                 until: float | None = None) -> None:
        self.path = jsonl_path
        self.until = until
        self._extra = tuple(v for v in (vin, email) if v)
        self._lock = threading.Lock()
        self._events: deque[dict] = deque(maxlen=BUFFER_MAX)
        self._counters: Counter[str] = Counter()
        self._cp_codes: Counter[str] = Counter()
        self._unknown: Counter[str] = Counter()
        self._seen_unknown: set[str] = set()
        self._latency: dict[str, list[int]] = {}
        self._dropped = 0
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._closed = False
        self._writer = threading.Thread(target=self._writer_loop, name="ebro_diag",
                                        daemon=True)
        self._writer.start()
        self.record("diag_start", until=_iso(until) if until else None)

    # ---------- captura ----------

    def record(self, etype: str, **fields: Any) -> None:
        """Registra un evento. Es el `DIAG_HOOK` pasado a los módulos core/.

        NUNCA lanza: un monitor defectuoso no debe poder romper la integración que está
        observando (en particular no debe hacer fallar un comando al coche)."""
        try:
            ev = {"ts": _iso(time.time()), "type": etype}
            ev.update(redact(fields, self._extra))
            with self._lock:
                if self._closed:
                    return
                self._events.append(ev)
                self._counters[etype] += 1
                self._tally(etype, ev)
            try:
                self._q.put_nowait(json.dumps(ev, default=str, ensure_ascii=False))
            except queue.Full:
                with self._lock:
                    self._dropped += 1
        except Exception:
            pass

    def _tally(self, etype: str, ev: dict) -> None:
        """Contadores agregados — la SÍNTESIS que deja ver un problema sin leer 500 eventos.
        Llamado ya bajo `self._lock`."""
        if etype == "command":
            self._counters["commands_total"] += 1
            if not ev.get("ok"):
                self._counters["commands_failed"] += 1
            ms = ev.get("duration_ms")
            if isinstance(ms, int):
                self._latency.setdefault("command", []).append(ms)
        elif etype == "pin_event":
            outcome = ev.get("outcome")
            self._counters[f"pin_{outcome}"] += 1
            code = ev.get("cp_code")
            if code:
                self._cp_codes[str(code)] += 1
        # NB: `pin_fail_concurrent` no tiene una rama aquí — el contador por tipo de evento
        # (ya incrementado por el llamador) lleva de suyo ese nombre. Contarlo de nuevo lo
        # duplicaría, haciendo parecer la condición de carrera el doble de frecuente de lo que es.
        elif etype == "mqtt_conn":
            self._counters[f"mqtt_{ev.get('event')}"] += 1
            up = ev.get("uptime_s")
            if isinstance(up, (int, float)):
                cur_min = self._counters.get("_mqtt_up_min")
                self._counters["_mqtt_up_min"] = up if cur_min is None else min(cur_min, up)
                self._counters["_mqtt_up_max"] = max(self._counters.get("_mqtt_up_max", 0), up)
        elif etype == "unknown_field":
            key = ev.get("key")
            if key:
                self._unknown[str(key)] += 1
        elif etype == "hv_followup":
            self._counters["hv_followup_orphan" if ev.get("orphan")
                           else "hv_followup_arms"] += 1
        elif etype == "session":
            self._counters["session_ok" if ev.get("ok") else "session_fail"] += 1
            if ev.get("triggered_reauth"):
                self._counters["reauth_triggered"] += 1

    def note_unknown_field(self, key: str, value: Any, svc: str) -> None:
        """Auto-descubrimiento de los campos 5A02 aún no mapeados en META.

        Emite el evento SOLO la primera vez que ve una clave (si no, cada push del coche
        generaría uno) pero incrementa el contador siempre: es el recuento el que dice si el
        campo es estable y vale la pena mapearlo.

        ⚠️ El `sample` es el punto más delicado del monitor: es el único sitio donde un valor
        del coche se registra **bajo un nombre de clave que no es el suyo** (`sample`). La
        ocultación por clave no puede protegerlo — y de hecho el 2026-07-20 una coordenada GPS
        acabó en claro en el archivo justo por aquí. El valor pasa ahora por `redact()` como
        todo lo demás (que desde esa misma fecha reconoce también las coordenadas), y para las
        claves geográficas la muestra ni siquiera se registra: para saber si un campo vale un
        sensor basta el NOMBRE."""
        with self._lock:
            first = key not in self._seen_unknown
            self._seen_unknown.add(key)
            if not first:
                self._unknown[key] += 1
                return
        if str(key).lower() in DROP_KEYS:
            # posición: el nombre de la clave basta, el valor no sirve para nada
            self.record("unknown_field", key=key, sample="**GEO**", svc=svc)
            return
        self.record("unknown_field", key=key, sample=str(value)[:80], svc=svc)

    # ---------- lectura ----------

    def snapshot(self) -> dict[str, Any]:
        """Ring buffer + contadores, para el diagnóstico descargable de HA. Ya ocultado."""
        with self._lock:
            lat = {op: _percentiles(v) for op, v in self._latency.items()}
            counters = {k: v for k, v in self._counters.items() if not k.startswith("_")}
            counters["mqtt_uptime_min_s"] = self._counters.get("_mqtt_up_min")
            counters["mqtt_uptime_max_s"] = self._counters.get("_mqtt_up_max")
            return {
                "until": _iso(self.until) if self.until else None,
                "buffer_size": len(self._events),
                "dropped_lines": self._dropped,
                "counters": counters,
                "checkPassword_codes": dict(self._cp_codes),
                "unknown_fields": dict(self._unknown),
                "latency": lat,
                "events": list(self._events),
            }

    # ---------- escritura en disco ----------

    def _writer_loop(self) -> None:
        """Hilo dedicado: ningún I/O en el hilo paho ni en el event loop de HA."""
        while True:
            line = self._q.get()
            if line is None:
                return
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.path) < FILE_MAX_BYTES:
                return
        except OSError:
            return
        with contextlib.suppress(OSError):   # sin rotación se sigue escribiendo, sin más
            os.replace(self.path, self.path + ".1")

    def close(self) -> None:
        """Cierra el monitor: vacía la cola y para el hilo escritor."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with contextlib.suppress(queue.Full):   # cola llena: el hilo saldrá por el timeout
            self._q.put_nowait(None)
        self._writer.join(timeout=5)


# ───────────────────────── utilidades ─────────────────────────

def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    # `math.inf` = monitor sin vencimiento (bandera a `0`): no es una fecha y
    # `time.localtime` lanzaría. Se etiqueta, para que el .jsonl y el diagnóstico de HA digan
    # de un vistazo que queda encendido hasta que se apaga a mano.
    if ts == math.inf:
        return "sin vencimiento"
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _percentiles(vals: list[int]) -> dict[str, int]:
    if not vals:
        return {}
    s = sorted(vals)
    return {"n": len(s), "p50": s[len(s) // 2], "p95": s[min(len(s) - 1, int(len(s) * 0.95))],
            "max": s[-1]}
