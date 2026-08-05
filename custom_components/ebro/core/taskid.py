#!/usr/bin/env python3
"""El taskId: cómo se consigue, y por qué hay que tener cuidado al pedirlo.

Ningún comando parte sin un `taskId` validado por `checkPassword`, y `checkPassword` verifica
el PIN de 4 cifras contra el backend de Chery. **Cada verificación fallida incrementa un
contador de errores del lado de Chery**, y superado el umbral la cuenta se bloquea — un bloqueo
que no se resuelve desde Home Assistant. De ahí que este módulo haga tres cosas antes de
preguntar:

1. si el PIN está vacío, falla ANTES de tocar el backend: un PIN no configurado no es un
   intento erróneo y no debe consumir umbral;
2. reutiliza el taskId en caché mientras siga vivo, porque generarlo es la parte lenta de cada
   comando — así la mayoría se convierten en una sola POST firmada;
3. serializa el intento entero (guarda + llamada de red + actualización del contador) dentro de
   `PinLockout.attempt()`, de modo que la condición de carrera no es expresable.

Era la mitad de `commands.py`, mezclado con el catálogo y con el envío. Aquí tiene archivo
propio porque es lo que más cuidado merece del componente entero.
"""
import hashlib
import logging
import os
import time

from . import ebro_auth as A, routing, wake
from .errors import CommandError
from .http import bff_post
from .pin_lockout import PinLockedError

_LOGGER = logging.getLogger(__name__)

# anti-bloqueo: para tras N checkPassword fallidos consecutivos dentro de una ventana, para no
# hacer saltar el bloqueo del PIN de la CUENTA Chery (cada PIN erróneo incrementa los errores
# de su lado, y ese bloqueo no se resuelve desde Home Assistant).
#
# El estado y su lock viven ahora dentro de `PinLockout` (core/pin_lockout.py): la única forma
# de generar es `attempt()`, que serializa guarda + POST + actualización del contador.
# El contador vive en `ctx.lockout` — uno por vehículo. De lo contrario, con dos coches
# configurados los errores de PIN de uno bloquearían los comandos del otro.
#
# Las generaciones SOLAPADAS se cuentan en `ctx.state` (ver context.py): es estado por
# vehículo, no de proceso.
# la clasificación de checkPassword POR CÓDIGO vive ahora en `routing.py` (tabla única). Antes
# era «todo lo que no da un taskId = PIN erróneo», que es falso y hace dos daños: propone el
# remedio equivocado al usuario y cuenta hacia el anti-bloqueo un error que con el PIN no tiene
# nada que ver.


def reset_pin_lockout(ctx) -> None:
    """Pone a cero el contador anti-bloqueo de los PIN erróneos (y el taskId en caché).

    El estado vive en memoria, no en el config entry: un simple reload del entry (ej. tras
    corregir el PIN) NO lo pone a cero, así que sin esta llamada los comandos quedarían
    bloqueados hasta que venza la ventana o se reinicie HA. Se invoca en cada reconfiguración
    del PIN (config flow / Repair)."""
    ctx.reset_pin_lockout()


def _mint_taskid(ctx, tuid):
    """Genera un taskId. Con el monitor apagado es un paso directo a `_mint_taskid_impl`.

    Con el monitor encendido observa DOS cosas que en el log no se ven: las generaciones
    concurrentes (`pin_fail_concurrent`, la carrera que acerca el bloqueo de la cuenta) y el
    resultado real de checkPassword con el código EN CRUDO del backend — la única forma de
    distinguir un PIN realmente erróneo de un rechazo por permisos/parámetros. El PIN nunca se
    registra, en ninguna forma."""
    if ctx.diag_hook is None:
        return _mint_taskid_impl(ctx, tuid)
    with ctx.state.mint_inflight_lock:
        ctx.state.mint_inflight += 1
        inflight = ctx.state.mint_inflight
    if inflight > 1:
        ctx.diag("pin_fail_concurrent", inflight=inflight)
    try:
        tid = _mint_taskid_impl(ctx, tuid)
        ctx.diag("pin_event", outcome="ok", pin_fail_n=ctx.lockout.failed_attempts)
        return tid
    except CommandError as err:
        # "fail" = el backend respondió y rechazó; "empty" = PIN no configurado;
        # "blocked" = anti-bloqueo saltado. Los dos últimos NO han interrogado al backend:
        # distinguirlos importa, porque solo "fail" acerca el bloqueo de la cuenta. Se clasifica
        # por el marcador del mensaje, no por la presencia del código: un rechazo puede llegar
        # también sin código, y acabaría confundido con un bloqueo.
        code = getattr(err, "code", None)
        if "checkPassword" in str(err):
            outcome = "fail"
        elif not (ctx.pin or "").strip():
            outcome = "empty"
        else:
            outcome = "blocked"
        ctx.diag("pin_event", outcome=outcome, reason=getattr(err, "reason", None), cp_code=code,
                 pin_fail_n=ctx.lockout.failed_attempts,
                 pin_fail_max=ctx.lockout.max_failures)
        raise
    except Exception as err:
        ctx.diag("pin_event", outcome="error", err_type=type(err).__name__)
        raise
    finally:
        with ctx.state.mint_inflight_lock:
            ctx.state.mint_inflight -= 1


def _mint_taskid_impl(ctx, tuid):
    """Genera un taskId con la cadena BFF de la app (queryList→setVecDefault→checkPassword).
       FIX (2026-06-20): scene=0 (NO 2) → el taskId generado es aceptado por tspconsole
       (airControl A00079). scene=2 daba A00089; scene=1 A00089; scene>=3 A00546.

       Rechaza la generación si el PIN está vacío (NO llama a checkPassword en vacío) y se
       auto-bloquea tras demasiados PIN erróneos consecutivos para evitar el bloqueo de la cuenta.

       En caso de fallo lanza SIEMPRE CommandError con `reason` ("pin" o "reauth") para que el
       coordinator pueda enrutar el remedio correcto (reconfig PIN vs reautenticación)."""
    # PIN ausente: se falla ANTES de entrar en el anti-bloqueo. Un PIN no configurado no es un
    # intento erróneo — no debe consumir el umbral ni tocar el backend.
    if not (ctx.pin or "").strip():
        raise CommandError(
            "PIN de comandos no configurado — configúralo en los ajustes de la integración",
            reason="pin")

    # `attempt()` toma el lock, aplica la guarda y lo mantiene durante toda la llamada de red.
    # Es la única forma de generar: la condición de carrera ya no es expresable.
    try:
        with ctx.lockout.attempt() as attempt:
            return _checkpassword(ctx, tuid, attempt)
    except PinLockedError as locked:
        raise CommandError(
            f"PIN de comandos bloqueado temporalmente ({locked.attempts} intentos erróneos) — "
            "vuelve a configurar el PIN en los ajustes de la integración y reinténtalo",
            reason="pin") from None


def _checkpassword(ctx, tuid, attempt):
    """La cadena BFF propiamente dicha. Corre con el lock del anti-bloqueo ya tomado; declara
    el resultado en `intento` SOLO cuando es realmente atribuible al PIN."""
    from . import vehicles

    access = wake._access_token(ctx)
    extra = {"Authorization": f"Bearer {access}",
             "Content-Type": "application/json; charset=UTF-8",
             "Accept": "application/json, text/plain, */*"}

    def bff(path, body):
        return bff_post(ctx, path, body, headers=A.headers_post(path, extra=extra, ctx=ctx))

    # La cadena exacta de la app: listar los vehículos, fijar el nuestro como predeterminado y
    # solo entonces verificar el PIN. Saltarse los dos primeros pasos hace que checkPassword
    # responda sin taskId.
    vehicles.query_list(ctx)
    bff("/tsp/v1/app/vmc/setVecDefault", {"vin": ctx.vin})
    plain = hashlib.md5(ctx.pin.encode()).hexdigest()
    password = A.sm4_code(plain, "padRight32")
    j = bff("/tsp/v1/app/cpm/checkPassword",
            {"vin": ctx.vin, "tUserId": str(tuid), "channelId": ctx.channel_id,
             "password": password, "needDecode": 0, "scene": 0, "type": 0})
    data = j.get("data") if isinstance(j.get("data"), dict) else {}
    tid = data.get("taskId") or j.get("taskId")
    if tid:
        attempt.success()      # el PIN es correcto → el umbral vuelve a cero
        return tid

    # ningún taskId: distingo la CAUSA para enrutar el remedio correcto.
    code = j.get("code")
    # DIAGNÓSTICO (2026-07-06): el código/mensaje EN CRUDO de checkPassword es la ÚNICA forma de
    # saber si es de verdad un PIN erróneo u otra causa (permisos del vehículo, parámetros scene/
    # channelId, backend). Hasta ahora NO se registraba → en el log el anti-bloqueo decía solo
    # "PIN erróneos", que es nuestra INFERENCIA. Ahora registramos code + message reales (campos
    # no sensibles).
    cp_msg = str(j.get("message") or j.get("msg") or "").strip()
    detail = f"code={code}" + (f" '{cp_msg[:100]}'" if cp_msg else "")
    _LOGGER.warning("[taskId] checkPassword NO devolvió un taskId → %s "
                    "(respuesta cruda del servidor; si no es un PIN erróneo, la causa está aquí)", detail)

    # una única consulta a la tabla decide el remedio Y si contar para el bloqueo. Antes eran
    # dos `if` sobre conjuntos separados más una rama por defecto: tres puntos que mantener
    # alineados a mano, y ahí es donde la clasificación se había torcido.
    outcome = routing.classify(code, routing.CONTEXT_CHECKPASSWORD)
    if outcome.counts_for_lockout:
        attempt.record_failure()
    raise CommandError(_checkpassword_message(outcome, detail),
                       code=str(code) if code else None, reason=outcome.reason)


def _checkpassword_message(outcome, detail: str) -> str:
    """Mensaje para el usuario, coherente con el remedio que se propondrá.

    Separado de la decisión a propósito: el texto se puede reescribir o traducir sin tocar el
    enrutado, y viceversa."""
    if outcome.reason == routing.REASON_REAUTH:
        return (f"Sesión caducada [checkPassword {detail}] — vuelve a autenticarte desde el "
                "aviso de Home Assistant")
    if outcome.reason == routing.REASON_CONFIG:
        return (f"Comando rechazado por el servidor [checkPassword {detail}] — no es el PIN: "
                "la cuenta no tiene permiso sobre este coche o la petición fue "
                "rechazada. Reinténtalo más tarde; si persiste se necesitan los logs.")
    return (f"PIN de comandos rechazado por el servidor [checkPassword {detail}] — vuelve a "
            "configurarlo en los ajustes de la integración")


# Caché en memoria del taskId. Generarlo implica hacer toda la vuelta de checkPassword (PIN):
# es la parte LENTA de cada comando. Pero el taskId sigue siendo válido un rato → lo reutilizamos y lo
# solo lo regeneramos cuando el coche lo rechaza (TASKID_INVALID) o vence el TTL. Así la mayoría
# de los comandos se convierte en una sola POST firmada en vez de PIN + POST.
# la caché vive en `ctx.estado` — el taskId está ligado al PIN y al VIN, así que no es
# compartible entre vehículos (antes lo era, y suponía un comando hacia el coche equivocado).

# Códigos con los que el coche dice "este taskId no vale" → se regenera y se reintenta una vez.
# Derivado de la tabla única, ya no es una lista paralela.

# Códigos con los que el coche dice "este taskId no vale" → se regenera y se reintenta una vez.
# Derivado de la tabla única, ya no es una lista paralela.
TASKID_INVALID = routing.TASKID_INVALID


def invalidate_taskid(ctx):
    """Descarta el taskId en caché (el coche lo ha rechazado como no válido/caducado)."""
    ctx.invalidate_taskid()


def get_taskid(ctx, tuid, emit=lambda m: None, force_mint=False):
    """Fuente del taskId, en orden: archivo piggyback → caché → checkPassword generado.
    `force_mint=True` salta archivo/caché y genera uno nuevo (usado en el retry tras un rechazo:
    reusar la misma fuente rechazada daría de nuevo el mismo error)."""
    if not force_mint:
        try:
            if os.path.exists(ctx.taskid_file):
                with open(ctx.taskid_file) as fh:
                    v = fh.read().strip()
                if v:
                    return v, "file"
        except OSError:
            pass
        if ctx.state.taskid and (time.time() - ctx.state.taskid_ts) < ctx.taskid_ttl:
            return ctx.state.taskid, "cache"
    if ctx.mint_taskid:
        emit("generando taskId (checkPassword)…")
        try:
            tid = _mint_taskid(ctx, tuid)
        except CommandError as e:
            # PIN erróneo / anti-bloqueo / sesión: publica el detalle y PROPAGA → send() lo deja
            # subir con su `reason` para el enrutado del remedio.
            emit(str(e))
            raise
        except Exception as e:
            emit(f"checkPassword falló: {e}")
            raise CommandError(
                "PIN de comandos no verificable — vuelve a configurarlo en los ajustes "
                f"de la integración ({e})", reason="pin") from e
        if tid:
            ctx.state.taskid = tid
            ctx.state.taskid_ts = time.time()
            return tid, "checkPassword"
    return None, "none"


