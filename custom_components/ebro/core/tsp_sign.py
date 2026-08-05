#!/usr/bin/env python3
"""
tsp_sign.py — Firma REST del Chery Vehicle SDK (PROD EU), reconstruida byte a byte
desde el smali `smali_dex2/h/ldkb.smali`:

  - método b(Map,J,String)  : construye la cadena a firmar
  - método c(String)        : SHA-256 -> hex -> toUpperCase()  (¡MAYÚSCULAS!)
  - método a(Map,J,String)  : para tagEncrypt="1" añade appId, firma, añade sign
  - método a(J,String)      : header Authorization=token, timestamp, x-TenantId=""

Algoritmo (EU, tagEncrypt="1"):
  half      = caracteres de posición PAR de APP_SECRET (-> "EUEBROProd89ec59274d23491084af")
  base      = <parámetros ordenados alfab. "k=v&" no vacíos> + "secretKey=" + half + "&timestamp=" + ts
  sign      = SHA256(base) en HEX/base64 MAYÚSCULA
  body JSON = { ...params..., "appId": <APP_ID>, "sign": <sign> }
  header    = { Authorization: <userToken>, timestamp: <ts>, x-TenantId: "" }
"""
import base64
import hashlib
import os

# Constantes de REGIÓN. Por defecto Ebro EU.
APP_ID     = os.environ.get("TSP_APP_ID", "euebro-1")
# APP_SECRET real de Ebro (extraído del binario, 2026-07): los caracteres de posición PAR
# dan la HALF "EUEBROProd89ec59274d23491084af". VERIFICADO: reproduce byte a byte la firma de
# peticiones reales capturadas (ej. /asr/manager/realtime → 3ORGRGOITQCGB+WHK6T8AD8Z8XDGFP6QERFHW/ND+WQ=).
APP_SECRET = "EqU3E4B6RtO7P8r9otd4879descd5f9g2f7r4rdd2d3d4d9d1d0d8d4dadfw"
TAG_ENCRYPT = "1"          # EU = SHA-256 (NO HMAC)

def half_secret(secret: str = APP_SECRET) -> str:
    """Caracteres de posición PAR (índices 0,2,4,...) → 'EUEBROProd89ec59274d23491084af'."""
    return "".join(secret[i] for i in range(len(secret)) if i % 2 == 0)

HALF = half_secret()   # "EUEBROProd89ec59274d23491084af" (verificado)


def _flatten_value(v):
    """Serializa un valor-ARRAY anidado como hace el SDK nativo (a(JSONObject) en
    ldkb.smali) ANTES del cálculo del sign. Por cada elemento:
      - objeto  → 'clave=valor&' con claves ordenadas alfab. (valores vacíos saltados),
                  y su posible sublista aplanada recursivamente;
      - escalar → str(elemento) concatenado SIN separador (ej. [1,2,3] → '123').
    El '&' final se elimina. Verificado byte a byte en 4/4 envelopes reales
    `chargeAppointControl` (cycleData [1..7] → '1234567')."""
    if isinstance(v, list):
        sb = ""
        for el in v:
            if isinstance(el, dict):
                fl = _flatten_obj(el)
                for k in sorted(fl.keys()):
                    val = fl[k]
                    if val is None or val == "":
                        continue
                    sb += f"{k}={val}&"
            else:
                sb += str(el)
        if sb.endswith("&"):
            sb = sb[:-1]
        return sb
    return v


def _flatten_obj(obj: dict) -> dict:
    """Copia del objeto con solo los valores-lista aplanados (ver _flatten_value).
    Los valores escalares quedan intactos → para los body PLANOS es un no-op (el algoritmo
    histórico queda idéntico, verificado en 63/63 envelopes planos)."""
    return {k: (_flatten_value(v) if isinstance(v, list) else v) for k, v in obj.items()}


def build_sign(params: dict, ts_ms: int, half: str = HALF) -> str:
    """Replica b(Map,J,String) para tagEncrypt='1': SHA-256 MAYÚSCULA.

    Los valores-array anidados (ej. `chargeAppointPlans`) se aplanan primero como hace el
    SDK nativo (_flatten_obj); sin arrays el comportamiento es invariante. NB: aplana una
    COPIA → el body devuelto por sign_body conserva el array real."""
    flat = _flatten_obj(params)
    parts = []
    for k in sorted(flat.keys()):                # Arrays.sort sobre las claves
        v = flat[k]
        if v is None or v == "":                 # null/"" saltados
            continue
        parts.append(f"{k}={v}&")
    base = "".join(parts) + f"secretKey={half}&timestamp={ts_ms}"
    # DESCUBRIMIENTO S23 (2026-06-20, captura eCapture/Conscrypt): la codificación REAL del
    # sign es base64(sha256(base)).upper(), NO hexdigest().upper(). Verificado en 71 envelopes
    # reales (airControl/coolingControl/heatingControl/lockControl/seatControl/window/findCar...).
    return base64.b64encode(hashlib.sha256(base.encode("utf-8")).digest()).decode().upper()

def sign_body(body_params: dict, ts_ms: int, half: str | None = None) -> dict:
    """Replica a(Map,J,String) tag1: devuelve el body JSON final {params, appId, sign}."""
    m = dict(body_params)
    m["appId"] = APP_ID                          # appId en los parámetros firmados
    sign = build_sign(m, ts_ms, half=half or HALF)
    m["sign"] = sign                             # sign añadido DESPUÉS de la firma
    return m

def auth_headers(user_token: str, ts_ms: int, tenant_id: str = "") -> dict:
    """Replica a(J,String) tag1: Authorization=token, timestamp, x-TenantId."""
    return {
        "Authorization": user_token,
        "timestamp": str(ts_ms),
        "x-TenantId": tenant_id or "",
    }
