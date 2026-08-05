#!/usr/bin/env python3
"""
wake.py — "Despertar coche" de Ebro Auto: réplica EXACTA del flujo de la app oficial
          (1× smsAwaken → sondeo realtime/location), pensado para invocarse desde el
          botón de Home Assistant.

Flujo (verificado sobre el código real):
  1) bff_login()  : token.json (access_token) → POST {BFF}/tsp/v1/app/auth/login → userToken/tUserId
  2) smsAwaken    : POST {TSP}/asc/vehicleControl/smsAwaken {vin}, firmado con tsp_sign
                    code "000000" = despertar aceptado; "A07312" = rate-limit/cuota de SMS-wake.
  3) sondeo ~60s  : /asr/manager/realtime + /asc/vehicleControl/queryVehicleLocation cada 5s.
                    En paralelo el listener MQTT captura los 5A02 → is_awake() pasa a True.

⚠️  smsAwaken TIENE UN RATE-LIMIT REAL. El botón NO se debe machacar:
    - `do_wake` respeta un COOLDOWN (`ctx.wake_cooldown_s`, 300 s) entre dos smsAwaken enviados;
    - un solo `do_wake` a la vez (lock anti doble toque).

Uso estrictamente personal (coche/cuenta del usuario). NO publicar token/certificados.
"""
import hashlib
import json
import os
import time

# imports relativos de paquete.
from . import codes, ebro_auth as A

# VIN, TSP_HOST y la ruta del token NO son globales de módulo reescritos antes de cada
# llamada: llegan del `CoreCtx` del vehículo (primer argumento de cada función). Ídem los
# locks, que antes eran de proceso: dos coches configurados se los disputaban sin motivo.




# NB sobre el cooldown del despertar: vive en `ctx.state.last_sms_ts`, o sea en el estado del
# vehículo (en memoria). Antes estaba en un archivo JSON compartido por todos los vehículos:
# con dos coches, despertar uno bloqueaba el otro.


# ───────────────────────── llamadas REST (parcheables en los tests) ──────────────────
def _access_token(ctx):
    """Lee el access_token del token.json del vehículo. Defensivo: maneja tanto
    {data:{...}} como el formato plano, y no explota con KeyError si falta el campo
    (devuelve None). Único punto de lectura del token: commands/provision usan este."""
    with open(ctx.token_path) as fh:
        tok = json.load(fh)
    if not isinstance(tok, dict):
        return None
    d = tok.get("data", tok)
    if isinstance(d, dict) and d.get("access_token"):
        return d["access_token"]
    return tok.get("access_token")

def _refresh_outcome(ctx, ok: bool, reason: str, *, diag: bool = True, **extra) -> tuple[bool, str]:
    """Registra el resultado de la renovación (estado por vehículo + monitor de diagnóstico)
    y lo devuelve.

    El MOTIVO es un marcador estable, no un texto para el usuario: se enruta sobre él
    (ver `session.check`). En el diag solo van resultado/motivo/código HTTP — NUNCA el
    token, que es una credencial.

    `diag=False` para los resultados decididos en local sin contactar con el servidor:
    registrarlos llenaría el archivo de diagnóstico con un evento cada pocos minutos durante
    toda una sesión muerta, enterrando el único evento que cuenta (el rechazo original)."""
    ctx.state.refresh_reason = reason
    ctx.state.refresh_ts = time.time()
    if diag:
        ctx.diag("token_refresh", ok=ok, reason=reason, **extra)
    return ok, reason


def _fingerprint(rt: str) -> str:
    """Huella del refresh_token: se compara esta, nunca la credencial en claro."""
    return hashlib.sha256(rt.encode("utf-8")).hexdigest()


def _refresh_token(ctx) -> bool:
    """True si la renovación tuvo éxito. El PORQUÉ (marcador estable) está en
    `_refresh_token_detail`, que es lo que consulta `session.check` para distinguir un token
    revocado de una red caída."""
    ok, _reason = _refresh_token_detail(ctx)
    return ok


def _refresh_token_detail(ctx) -> tuple[bool, str]:
    """Renueva el access_token con el grant `refresh_token` y reescribe token.json.

    Devuelve `(ok, motivo)`, donde `motivo` es un marcador ESTABLE:
      ""                  renovación correcta
      "ausente"           ningún refresh_token utilizable → hace falta reautenticar
      "red:<Tipo>"       la petición no salió o no volvió → NO es una revocación
      "rechazado:<key>"   el servidor dijo que no (ej. `invalid_grant`) → reautenticar
      "respuesta"          respuesta 2xx pero sin access_token → tratada como rechazo

    La distinción entre "red:" y "rechazado:" es el objetivo de toda la función: sin ella,
    una conexión inestable haría aparecer la tarjeta «Reautenticar» y el usuario reautentica
    en balde por un problema que se habría resuelto solo.

    Protegido por el lock del token con doble comprobación. Se fotografía el access_token que
    el llamador vio (ANTES del lock); dentro del lock se relee token.json: si en disco el
    access_token ya cambió, otro hilo ya renovó → NO se rehace el refresh (rehacerlo quemaría
    el nuevo refresh_token e invalidaría la sesión)."""
    # instantánea pre-lock: el token que el llamador consideraba caducado
    try:
        with open(ctx.token_path) as fh:
            tok0 = json.load(fh)
    except Exception:
        return _refresh_outcome(ctx, False, "ausente")
    seen_at = (tok0.get("data", tok0) or {}).get("access_token") if isinstance(tok0, dict) else None

    with ctx.state.token_lock:
        # doble comprobación DENTRO del lock: releo el estado actual del archivo
        try:
            with open(ctx.token_path) as fh:
                tok = json.load(fh)
        except Exception:
            return _refresh_outcome(ctx, False, "ausente")
        d = (tok.get("data", tok) or {}) if isinstance(tok, dict) else {}
        cur_at = d.get("access_token")
        if seen_at and cur_at and cur_at != seen_at:
            # ya renovado por otro hilo: el token en disco es válido, no tocarlo
            return _refresh_outcome(ctx, True, "", concurrente=True)
        rt = d.get("refresh_token")
        if not rt:
            return _refresh_outcome(ctx, False, "ausente")
        # FRENO: si el servidor ya rechazó EXACTAMENTE este refresh_token, no hay nada que
        # ganar volviéndoselo a presentar — un token revocado no vuelve a ser válido solo,
        # hace falta reautenticar. Sin este freno cada llamada a la nube (control de sesión,
        # sonda, latido de marcha) reintenta: medido el 22/07, 5 intentos idénticos en 6
        # minutos, y durante las ~10 horas de sesión muerta de la noche anterior. Machacar el
        # endpoint de autenticación con credenciales ya rechazadas es justo el comportamiento
        # que los gateways sancionan.
        # Se reintenta de todos modos una vez cada `REINTENTA_REFRESH_TRAS_S`, para no quedar
        # bloqueados para siempre si aquel rechazo fue un desliz del servidor; y el freno se
        # suelta solo en cuanto en el archivo aparece un refresh_token distinto.
        fingerprint = _fingerprint(rt)
        burned = ctx.state.refresh_burned
        elapsed = time.time() - (ctx.state.refresh_burned_ts or 0.0)
        if fingerprint == burned and elapsed < ctx.retry_refresh_after_s:
            return _refresh_outcome(ctx, False, "rechazado:ya_rechazado", diag=False)
        # Receta VERIFICADA (ebro_login): oauth2/token con grant refresh_token, cliente
        # legendApp:legendApp, tenant 3000010, NINGUNA firma de gateway. host = ctx.bff (legend).
        from . import ebro_login as EL
        try:
            ok_r, res = EL.refresh_token(rt, host=ctx.bff)
        except Exception as err:
            return _refresh_outcome(ctx, False, f"red:{type(err).__name__}")
        if not ok_r:
            # rechazado por el servidor (ej. refresh_token revocado) → hace falta un nuevo login
            key = str(res)[:40]
            # arma el freno: este token queda quemado hasta que llegue otro
            ctx.state.refresh_burned = fingerprint
            ctx.state.refresh_burned_ts = time.time()
            return _refresh_outcome(ctx, False, f"rechazado:{key}")
        j = res["raw"]
        http = 200
        # escritura atómica: nuevo token en archivo temporal y luego rename (token.json nunca corrupto)
        try:
            path = ctx.token_path
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False)
            os.chmod(tmp, 0o600)   # el token es una credencial: legible solo por el propietario
            os.replace(tmp, path)
        except Exception as err:
            return _refresh_outcome(ctx, False, f"escritura:{type(err).__name__}", http=http)
        return _refresh_outcome(ctx, True, "", http=http)


def _eta_token(ctx) -> tuple[float, int]:
    """(segundos transcurridos desde la última renovación, duración declarada en segundos).

    La edad se mide sobre el mtime de token.json, que se reescribe en cada renovación: el
    token es opaco (no es un JWT), así que la fecha de emisión no es legible desde dentro.
    Devuelve (-1, 0) si no es determinable."""
    try:
        eta = max(0.0, time.time() - os.stat(ctx.token_path).st_mtime)
        with open(ctx.token_path) as fh:
            tok = json.load(fh)
    except Exception:
        return -1.0, 0
    d = (tok.get("data", tok) or {}) if isinstance(tok, dict) else {}
    try:
        lifetime = int(d.get("expires_in") or 0)
    except (TypeError, ValueError):
        lifetime = 0
    return eta, lifetime

def _bff_login(ctx, _allow_refresh=True):
    """Devuelve (userToken, tUserId). Lanza en error de red; (None,None) si es rechazado.
    Si la sesión ha caducado intenta UN refresh_token automático y reintenta una vez."""
    tok = _access_token(ctx)
    H = A.headers_post("/tsp/v1/app/auth/login", ctx=ctx, extra={
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/plain, */*"})
    # `bff_post` normaliza el caso real de token caducado/424, en el que el BFF devuelve un
    # cuerpo cuyo nivel superior es una CADENA y no un objeto: llega como {} y se trata como
    # sesión no válida, en vez de reventar con AttributeError dentro de un executor.
    from .http import READ_TIMEOUT_S, bff_post

    j = bff_post(ctx, "/tsp/v1/app/auth/login", {"channelId": ctx.channel_id},
                 headers=H, timeout=READ_TIMEOUT_S)
    d = j.get("data", {})
    # el gate de la renovación está en la AUSENCIA de userToken, no solo en `data` no-dict.
    # Antes, un `data` que ERA un dict pero sin userToken (ej. {} o un body de error
    # estructurado) se saltaba del todo la renovación → se devolvía (None,None) y al usuario se
    # le pedía reautenticar cuando habría bastado el refresh_token silencioso.
    ut = d.get("userToken") if isinstance(d, dict) else None
    if not ut:
        # sesión caducada: prueba UNA renovación automática del token y reintenta una sola vez
        if _allow_refresh and _refresh_token(ctx):
            return _bff_login(ctx, _allow_refresh=False)
        return None, None
    return ut, d.get("tUserId")

def _signed_post(ctx, ut: str, path: str, params: dict):
    """POST firmado al TSP. La construcción vive en `core/http`; este nombre se conserva
    porque `probe`, `commands` y los tests lo parchean por él."""
    from .http import signed_post

    return signed_post(ctx, ut, path, params)

def _code_of(j):
    return j.get("code") if isinstance(j, dict) else j

def _payload(j):
    """Payload útil de la respuesta tspconsole: bajo "data" en algunos endpoints y bajo
    "body" en otros (ej. /asr/manager/realtime → "body" con 84 campos). Devuelve el primer
    dict no vacío, o None."""
    if not isinstance(j, dict):
        return None
    for k in ("data", "body"):
        v = j.get(k)
        if isinstance(v, dict) and v:
            return v
    return None


def _has_live_data(j):
    return _payload(j) is not None


# ───────────────────────── orquestación del botón ──────────────────────────
def do_wake(ctx, publish, is_awake=None, send_sms=True):
    """Ejecuta el flujo de despertar y reporta el estado (cadenas ya legibles) vía `publish`.

      ctx            -> CoreCtx del vehículo (VIN, host, token, cooldown)
      publish(text)  -> callback que escribe el estado en HA + monitor (llamada varias veces)
      is_awake()     -> callback opcional: True si ya se reciben eventos MQTT (coche despierto)
      send_sms       -> si False NO envía realmente smsAwaken (solo para pruebas/diagnóstico)

    Devuelve un dict resumen {ok, online, code, ...}. Nunca lanza: cada error → status.
    """
    # lock POR VEHÍCULO: dos coches pueden despertarse en paralelo, el mismo no.
    if not ctx.state.wake_lock.acquire(blocking=False):
        publish("⏳ Despertar ya en curso, espera…")
        return {"ok": False, "reason": "busy"}
    try:
        return _do_wake_inner(ctx, publish, is_awake, send_sms)
    except Exception as e:
        publish(f"⚠️ Error al despertar: {type(e).__name__}: {e}")
        return {"ok": False, "reason": "exception", "error": str(e)}
    finally:
        ctx.state.wake_lock.release()


def _do_wake_inner(ctx, publish, is_awake, send_sms):
    now = time.time()

    # 0) cooldown anti rate-limit (solo si vamos a enviar realmente el SMS)
    if send_sms:
        last = ctx.state.last_sms_ts
        wait = ctx.wake_cooldown_s - (now - last)
        if last and wait > 0:
            mm, ss = divmod(int(wait), 60)
            publish(f"⏳ Anti límite de frecuencia: espera aún {mm}m{ss:02d}s antes de volver a despertar")
            return {"ok": False, "reason": "cooldown", "wait_s": int(wait)}

    # si el coche ya está publicando en MQTT, ya está despierto: sin SMS
    if is_awake and is_awake():
        publish("🟢 El coche ya está despierto (está enviando datos) — no hace falta despertarlo")
        return {"ok": True, "online": True, "reason": "already_awake"}

    # 1) login BFF → userToken
    publish("🔑 Accediendo…")
    ut, _tu = _bff_login(ctx)
    if not ut:
        publish("🔑 Sesión caducada (token viejo o app oficial abierta): vuelve a autenticarte")
        return {"ok": False, "reason": "no_usertoken"}

    # 2) smsAwaken (una sola vez)
    code = None
    if send_sms:
        _sc, j = _signed_post(ctx, ut, "/asc/vehicleControl/smsAwaken", {"vin": ctx.vin})
        code = _code_of(j)
        ctx.state.last_sms_ts = time.time()     # registra YA para el cooldown, aunque haya error
        if str(code) in ("000000", "A00079"):
            publish("✅ Despertar enviado — espero a que el coche se conecte…")
        elif str(code) == "A07312":
            publish("🚫 Límite de despertar (A07312): el coche rechaza más despertares ahora. Reinténtalo más tarde")
            return {"ok": False, "online": False, "code": code, "reason": "rate_limit"}
        else:
            publish(f"⚠️ Despertar no aceptado ({code}: {codes.meaning(code)}). Aun así intento escuchar…")
    else:
        publish("🧪 (test) smsAwaken NO enviado; paso solo al sondeo")

    # 3) sondeo realtime/location + escucha MQTT (attempts × every_s segundos en total)
    for i in range(ctx.wake_poll_attempts):
        if is_awake and is_awake():
            publish("🟢 Coche ONLINE — está enviando datos en tiempo real")
            return {"ok": True, "online": True, "code": code, "via": "mqtt"}
        _sc1, j1 = _signed_post(ctx, ut, "/asr/manager/realtime", {"vin": ctx.vin})
        _sc2, j2 = _signed_post(ctx, ut, "/asc/vehicleControl/queryVehicleLocation",
                               {"vin": ctx.vin})
        if _has_live_data(j1) or _has_live_data(j2):
            publish("🟢 Coche ONLINE — datos en tiempo real recibidos")
            return {"ok": True, "online": True, "code": code, "via": "rest",
                    "data": _payload(j1) or _payload(j2)}
        secs_left = (ctx.wake_poll_attempts - i - 1) * ctx.wake_poll_every_s
        publish(f"… esperando el despertar ({_code_of(j1)}) — aún ~{secs_left}s")
        time.sleep(ctx.wake_poll_every_s)

    publish("⌛ El coche sigue en reposo (A07900). Reinténtalo cuando se haya usado hace poco o tenga buena cobertura")
    return {"ok": True, "online": False, "code": code, "reason": "still_asleep"}
