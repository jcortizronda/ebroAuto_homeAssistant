#!/usr/bin/env python3
"""
probe.py — "Sonda de posición" de Ebro Auto.

Pregunta que responde: cuando el coche está REALMENTE despierto (está publicando
5A02 en MQTT), ¿el canal tspconsole-eu devuelve posición GPS / datos realtime, o
todavía `A07900`?

Descubrimiento (verificado sobre el código de AMBAS apps, EU + rusa):
  - NO existe un endpoint BFF para los comandos. El BFF "legend" solo tiene
    auth/cpm(PIN)/env/map/vac/vmc. Los comandos/posición pasan TODOS por el SDK
    de Chery → tspconsole-eu `/asc/vehicleControl/*` y `/asr/manager/realtime`,
    es decir EXACTAMENTE los path que ya usamos. Ningún canal oculto.
  - Así que la única variable que queda es el ESTADO DESPIERTO del coche. La app
    lo consigue porque manda con el coche recién usado; nosotros caemos en A07900
    (duerme) y no podemos despertarlo (smsAwaken en A07312, cuota por cuenta).

Esta sonda es de SOLO LECTURA: llama a `/asr/manager/realtime {vin}` y
`/asc/vehicleControl/queryVehicleLocation {vin}` (los mismos del sondeo de
wake.py). NO envía smsAwaken, NO manda comandos, NO toca el PIN. Es benigna:
es lo que hace la app cuando abres la página de "posición".

Se invoca en el flanco de subida dormido→despierto, con un cooldown para no
repetir en cada 5A02.

Uso estrictamente personal (coche/cuenta del usuario). NO publicar token/certificados.
"""
import json
import logging
import os
import time

# Reutiliza la infraestructura ya verificada de wake.py: login BFF + POST firmado tspconsole.
# imports relativos de paquete.
from . import codes, wake as W

_LOGGER = logging.getLogger(__name__)

# campos "ricos" del CVRealtimeResBean que más nos interesan (si es que llegan)
RICH_KEYS = ("lat", "lon", "altitude", "direction", "gpsSpeed", "vehicleSpeed",
             "odometer", "dumpEnergy", "electricRange", "pureElectricRange",
             "chargeState", "inCarTemperature", "onlineStatus")

# Subconjunto GEOGRÁFICO de RICH_KEYS: la POSICIÓN. Excluido del mensaje legible —
# no de la telemetría. El mensaje `probe_status` acaba en el estado de un sensor (y por
# tanto en la base de datos de HA), en el archivo «Descargar diagnóstico» y en el log de la
# integración; escribir ahí «lat=…, lon=…» hacía salir en claro dónde está el coche en los
# tres sitios (encontrado en campo el 2026-07-20). La posición sigue llegando al
# device_tracker por su camino (`on_data` recibe los datos EN CRUDO, no este resumen), así
# que no se pierde nada: solo desaparece del TEXTO. Odómetro/energía/autonomía/temperatura
# se quedan, para que el mensaje siga siendo útil para el diagnóstico.
_GEO_KEYS = ("lat", "lon", "altitude", "direction")
_MSG_KEYS = tuple(k for k in RICH_KEYS if k not in _GEO_KEYS)

# Marcas de tiempo que el coche pone en el payload realtime, de la más a la menos fiable.
_CLOCK_KEYS = ("time", "resultTime")


def freshness(data: dict, now: float | None = None) -> str:
    """Describe DE CUÁNDO son los datos que acaba de devolver la sonda.

    El endpoint realtime responde igual de bien con el coche despierto que dormido, pero no
    significan lo mismo: despierto contesta el coche, dormido devuelve la última instantánea
    que la nube guardó, que puede tener media hora. Anunciar las dos cosas como «datos en
    tiempo real con el coche despierto» —que es lo que hacía— manda al usuario a buscar el
    fallo donde no está: pulsa «Actualizar», el sensor le dice que todo ha ido bien, y el
    maletero que acaba de abrir sigue sin aparecer.

    `onlineStatus` es de la propia respuesta, no una deducción nuestra.
    """
    online = str(data.get("onlineStatus", "")).strip() in ("1", "1.0")
    if online:
        return "🟢🛰️ ¡Datos en tiempo real recibidos con el coche despierto!"
    edad = _age_min(data, time.time() if now is None else now)
    if edad is None:
        return "🟡🛰️ Instantánea de la nube: el coche está dormido, no ha contestado él"
    return (f"🟡🛰️ Instantánea de la nube de hace {edad} min: el coche está dormido "
            "y no ha contestado él")


def _age_min(data: dict, now: float) -> int | None:
    """Minutos transcurridos desde la marca de tiempo del payload. `None` si no trae ninguna."""
    for key in _CLOCK_KEYS:
        try:
            ms = float(data.get(key))
        except (TypeError, ValueError):
            continue
        if ms:
            return max(0, int((now - ms / 1000) // 60))
    return None


def _log(path: str, rec: dict):
    if not path:
        return
    rec = {"ts": round(time.time(), 3),
           "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()), **rec}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _rich(data: dict) -> dict:
    """Extrae los campos interesantes para el resumen legible — SIN la posición.

    Ver `_GEO_KEYS`: las coordenadas no entran en el mensaje (que se publica como estado de
    un sensor, por tanto persistido y potencialmente compartido). Quedan todos los demás
    campos ricos. La posición viaja igualmente intacta hacia el device_tracker mediante
    `on_data`, que recibe el diccionario en crudo, no este."""
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in _MSG_KEYS if k in data}


def probe_once(ctx, publish, force=False, on_data=None):
    """Ejecuta UNA sonda de solo lectura y reporta el resultado vía `publish(text)`.

    `ctx` = CoreCtx del vehículo: VIN, host, token y cooldown de la sonda. Antes eran
    globales de módulo, así que con dos coches configurados la sonda de uno consumía el
    cooldown del otro — y leía igualmente el VIN de la última entrada arrancada.

    Devuelve un dict {ok, online, got_data, codes, rich}. Nunca lanza.
    `force=True` ignora el cooldown (para la prueba manual).
    `on_data(data)` (opcional): callback invocada con el dict `data` en crudo SOLO cuando
    se reciben datos en vivo (coche despierto) — se usa para publicar GPS/batería en HA.
    """
    if not ctx.state.probe_lock.acquire(blocking=False):
        return {"ok": False, "reason": "busy"}
    try:
        now = time.time()
        last_probe = ctx.state.last_probe_ts
        if not force and last_probe and (now - last_probe) < ctx.probe_cooldown_s:
            # DECIRLO. Volver en silencio dejaba al usuario mirando un botón que no producía
            # ni un mensaje ni un cambio: indistinguible de una integración rota.
            wait_s = int(ctx.probe_cooldown_s - (now - last_probe))
            publish(f"⏳ Sonda: lectura reciente, espero {wait_s} s antes de volver a preguntar")
            return {"ok": False, "reason": "cooldown", "wait_s": wait_s}
        ctx.state.last_probe_ts = now

        # NO afirmar aquí que el coche está despierto: todavía no se ha preguntado nada. El
        # mensaje es de cuando la sonda solo se lanzaba en el flanco dormido→despierto; ahora la
        # dispara también el botón y el bucle, con el coche en cualquier estado. Quien lo sabe es
        # `freshness()`, DESPUÉS de la respuesta y mirando su `onlineStatus` — y quedaba el
        # absurdo de anunciar «el coche está despierto» y concluir dos líneas después que dormía.
        publish("🛰️ Sonda de posición: pregunto por GPS y datos en tiempo real…")
        ut, _tu = W._bff_login(ctx)
        if not ut:
            publish("🔑 Sonda: sesión caducada (vuelve a autenticarte). Reintento al próximo despertar")
            _log(ctx.probe_log_path, {"event": "probe", "ok": False, "reason": "no_usertoken"})
            return {"ok": False, "reason": "no_usertoken"}

        _sc1, j1 = W._signed_post(ctx, ut, "/asr/manager/realtime", {"vin": ctx.vin})
        _sc2, j2 = W._signed_post(ctx, ut, "/asc/vehicleControl/queryVehicleLocation",
                                 {"vin": ctx.vin})
        # travelQuery: buscamos km/odómetro (campo aún no visto; se revelará en el 1er wake real)
        _sc3, j3 = W._signed_post(ctx, ut, "/asd/travelManage/travelQuery", {"vin": ctx.vin})
        c1, c2, c3 = W._code_of(j1), W._code_of(j2), W._code_of(j3)
        got1, got2, got3 = W._has_live_data(j1), W._has_live_data(j2), W._has_live_data(j3)

        # data combinado: realtime va el ÚLTIMO y por tanto manda donde coincidan. El payload
        # está bajo "data" o "body" según el endpoint (realtime → "body"): W._payload maneja
        # ambos, si no los 84 campos realtime se perdían.
        #
        # **La POSICIÓN sale de `queryVehicleLocation` y de ningún otro sitio.** La respuesta de
        # `/asr/manager/realtime` NO trae `lat`/`lon` — verificado sobre una captura real de la
        # app oficial: 80 campos y ninguna coordenada. Así que aquí no hay competencia entre
        # fuentes que resolver, y el orden de la fusión no afecta a la posición.
        #
        # Y `queryVehicleLocation` es una CONSULTA, no una orden: devuelve la última posición
        # conocida por la nube, sin pedirle nada al coche. Quien fuerza un fix nuevo es el
        # comando `vehicleLocation` («Localizar coche (GPS)»), que va con taskId y hace que el
        # coche reporte. Los nombres lo dicen: `query…` lee, el otro actúa. Es deliberado — la
        # sonda existe justamente para no tocar el coche.
        data = {}
        for src, got in ((j2, got2), (j3, got3), (j1, got1)):
            payload = W._payload(src)
            if got and isinstance(payload, dict):
                data.update(payload)

        rich = _rich(data)
        _log(ctx.probe_log_path, {"event": "probe", "ok": True, "realtime_code": c1, "location_code": c2,
              "travel_code": c3, "got_realtime": got1, "got_location": got2, "got_travel": got3,
              "rich": rich, "data": data or None,
              "travel_data": j3.get("data") if got3 else None})

        got1 = got1 or got3   # si travelQuery trae datos con el coche despierto, cuenta como "live"
        if (got1 or got2) and on_data and data:
            try:
                on_data(data)
            except Exception as e:
                publish(f"⚠️ Sonda: error al publicar datos ({type(e).__name__})")

        if got1 or got2:
            titular = freshness(data, now)
            if rich:
                bits = ", ".join(f"{k}={v}" for k, v in rich.items())
                publish(f"{titular} {bits}")
            else:
                publish(titular)
            return {"ok": True, "online": True, "got_data": True,
                    "codes": [c1, c2, c3], "rich": rich}

        publish(f"🟡 Sonda: sin posición ni datos "
                f"(realtime={c1} [{codes.meaning(c1)}], location={c2}, travel={c3})")
        return {"ok": True, "online": False, "got_data": False, "codes": [c1, c2, c3]}
    except Exception as e:
        publish(f"⚠️ Error de sonda: {type(e).__name__}: {e}")
        return {"ok": False, "reason": "exception", "error": str(e)}
    finally:
        ctx.state.probe_lock.release()
