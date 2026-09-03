#!/usr/bin/env python3
"""
commands.py — Catálogo + envío de los comandos del coche Ebro Auto (tspconsole EU REST).

Reutiliza la cadena verificada:
  - userToken vía  wake._bff_login()      (token en token.json, refresco automático)
  - firma          tsp_sign.sign_body()   (base64(sha256(base)).upper())
  - taskId         get_taskid()           (archivo piggyback -> checkPassword auto-generado)

POST  https://tspconsole-eu.ebroauto.com/asc/vehicleControl/<endpoint>
Header: Authorization=<userToken>, timestamp=<ms>, Content-Type=application/json; charset=utf-8,
        User-Agent=okhttp/4.9.2

⚠️  Cada send() con un taskId válido ACTÚA sobre el coche. Está pensado para invocarse SOLO
    con el toque del usuario en un botón de Home Assistant (= su consentimiento explícito).
    Los body del catálogo están reconstruidos 1:1 desde los envelopes reales capturados.
"""
import contextlib
import json
import logging
import time
import urllib.error
import urllib.request

from . import codes, routing, tsp_sign, wake
from .catalog import CMD_MAP
from .errors import CommandError
from .taskid import get_taskid, invalidate_taskid

_LOGGER = logging.getLogger(__name__)


# Se eliminó `importlib.reload(tsp_sign)` en tiempo de import (side-effect inútil; tsp_sign no
# se muta en otro sitio y recargarlo al importar podía anular posibles monkeypatches).

# VIN, PIN, host, archivo del taskId y generación automática NO son globales de módulo
# reescritos antes de cada llamada: llegan del `CoreCtx` del vehículo, primer argumento de
# cada función pública. Con dos coches configurados, el enfoque antiguo hacía que el segundo
# entry sobrescribiera la configuración del primero y un comando pudiera partir hacia el coche
# equivocado.

# Códigos de respuesta tspconsole → texto legible: ahora desde el mapa ÚNICO core/codes.py.
CODE_MEANING = codes.CODE_MEANING

# Resultado del comando: el backend responde SIEMPRE HTTP 200, el resultado real está en el
# `code` del body. `SUCCESS_CODES` = comando aceptado por el backend (luego el coche confirma
# vía MQTT 110x); `FAILURE_CODES` = comando NO ejecutado (coche ocupado/en reposo, permiso
# denegado, taskId o token no válidos). Distinguir los dos es lo que permite a las entidades
# optimistas NO mostrar un falso "éxito" cuando el coche ha rechazado (ver EbroOptimisticMixin).
#
# Estos conjuntos están ahora DERIVADOS de la tabla única de routing, ya no son listas
# escritas a mano al lado. Eran listas paralelas que podían divergir en silencio de cómo se
# enrutaban de verdad los códigos.
SUCCESS_CODES = routing.SUCCESS_CODES
FAILURE_CODES = routing.FAILURE_CODES
RETRYABLE_CODES = routing.RETRYABLE_CODES


def send(ctx, cmd_key, emit=lambda m: None, params=None):
    """Envía un comando. emit(str) recibe los pasos (para publicarlos en HA).
       `params` (opcional) = override/añadidos al body del catálogo ANTES de los campos comunes
       → permite los comandos paramétricos (clima: temperature/times; carga inmediata:
       controlType; carga programada: mainSwitch + chargeAppointPlans). Los campos de sistema
       (clientType/seq/taskId/vin) siguen siendo siempre los generados aquí.
       Devuelve una cadena-resultado legible."""
    c = CMD_MAP.get(cmd_key)
    if not c:
        emit(f"comando desconocido: {cmd_key}")
        raise CommandError(f"Comando desconocido: {cmd_key}")

    token, tuid = wake._bff_login(ctx)
    if not token:
        emit("acceso fallido (¿token caducado?)")
        raise CommandError(
            "Sesión caducada — vuelve a autenticarte desde el aviso de Home Assistant",
            reason="reauth")

    # path explícito (ej. alarma en /act/theftAlarm/setSwitch) o el clásico
    # /asc/vehicleControl/<endpoint> para los comandos de vehículo estándar.
    url = ctx.tsp_host + (c.get("path") or ("/asc/vehicleControl/" + c["endpoint"]))

    # Intento 1 con el taskId reutilizado (rápido). Si el coche lo rechaza como no válido/
    # caducado, se regenera (checkPassword) y se reintenta UNA sola vez.
    for attempt in (1, 2):
        # get_taskid propaga CommandError (PIN/anti-bloqueo/sesión) con su `reason`.
        taskid, src = get_taskid(ctx, tuid, emit, force_mint=(attempt == 2))
        if not taskid:
            # ningún taskId PERO ninguna excepción = la generación está DESACTIVADA
            # (EBRO_MINT_TASKID=0) y no había un taskId ni en env ni en archivo. El PIN no tiene
            # que ver: decir «PIN erróneo» mandaba al usuario a reconfigurar un PIN sano.
            emit("no hay taskId disponible (generación desactivada)")
            raise CommandError(
                "Generación de taskId desactivada (EBRO_MINT_TASKID=0) y no hay ningún "
                "taskId disponible: los comandos no pueden ejecutarse. Reactiva la generación "
                "automática para usar los botones.",
                reason="config")

        ts = int(time.time() * 1000)
        body = dict(c["body"])
        if params:
            body.update(params)    # override paramétrico (temperatura/duración/controlType/plan)
        body.update({"clientType": "1", "seq": f"{ctx.vin}-{ts}",
                     "taskId": taskid, "vin": ctx.vin})
        m = tsp_sign.sign_body(body, ts, half=ctx.sign_key)
        payload = json.dumps(m, separators=(",", ":"), ensure_ascii=False).encode()
        headers = {"Authorization": token, "timestamp": str(ts),
                   "Content-Type": "application/json; charset=utf-8", "User-Agent": "okhttp/4.9.2"}
        emit(f"enviando {c['name']} (taskId:{src})…")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            status = e.code
        except Exception as e:
            emit(f"error de red: {e}")
            raise CommandError(f"Error de red al enviar el comando: {e}") from e

        code = None
        with contextlib.suppress(Exception):   # cuerpo no-JSON: se queda sin código
            code = json.loads(raw).get("code")

        # la tabla dice si el taskId hay que rehacerlo. En la primera vuelta se regenera y se
        # reintenta, así el usuario no ve un falso error por un taskId simplemente caducado.
        outcome = routing.classify(code, routing.CONTEXT_COMMAND)
        if attempt == 1 and outcome.regenerate_taskid and ctx.mint_taskid:
            invalidate_taskid(ctx)
            emit("taskId ya no válido → lo renuevo y reintento…")
            continue
        break

    meaning = CODE_MEANING.get(code, raw[:120])
    out = f"{c['name']}: HTTP {status} {code or ''} — {meaning}"
    emit(out)
    # Resultado real desde el `code` (el backend responde siempre HTTP 200). Un fallo conocido =
    # comando NO ejecutado → CommandError, así las entidades optimistas anulan el estado en vez
    # de mostrar un falso éxito. Los códigos desconocidos quedan no bloqueantes por prudencia: no
    # se inventa un fallo que el backend no ha declarado.
    if outcome.failed:
        raise CommandError(out, code=outcome.code, reason=outcome.reason)
    return out


def query_charge_schedule(ctx):
    """Lee la programación de carga que tiene el coche (SOLO LECTURA).

    `/asd/chargeAppointManage/chargeAppointQuery`, sin taskId: no ejecuta nada ni despierta al
    coche. Devuelve el `body` en crudo (`mainSwitch` + `chargeAppointPlans`) o `None`.

    Existe porque la programación se puede cambiar desde la app oficial o desde el propio
    coche, y por ahí la integración no se entera de nada: las entidades de hora y duración son
    la PREFERENCIA de lo que se enviará, no lo que el vehículo tiene puesto."""
    token, _tuid = wake._bff_login(ctx)
    if not token:
        return None
    try:
        _status, j = wake._signed_post(ctx, token, "/asd/chargeAppointManage/chargeAppointQuery",
                                       {"vin": ctx.vin})
    except Exception:
        return None
    body = j.get("body") if isinstance(j, dict) else None
    return body if isinstance(body, dict) else None


def query_theft_switch(ctx):
    """Lee el estado de la alarma (SOLO LECTURA, /act/theftAlarm/querySwitch).
       Devuelve 1/0 (int) o None si no está disponible. NO usa taskId ni ejecuta nada:
       la respuesta pone el valor bajo `body.theftAlarmSwitch`."""
    token, _tuid = wake._bff_login(ctx)
    if not token:
        return None
    try:
        _status, j = wake._signed_post(ctx, token, "/act/theftAlarm/querySwitch",
                                       {"vin": ctx.vin})
    except Exception:
        return None
    if isinstance(j, dict):
        body = j.get("body") if isinstance(j.get("body"), dict) else {}
        v = body.get("theftAlarmSwitch")
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
    return None
