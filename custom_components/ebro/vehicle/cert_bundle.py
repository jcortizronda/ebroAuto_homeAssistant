"""Aprovisionamiento automático de los certificados mutual-TLS MQTT (broker EMQX del coche).

Los certificados de cliente EMQX son **constantes universales por región** (`Subject:
CN=client`), **NO** datos por cuenta: son idénticos para todos los usuarios de una región.
Provienen tal cual de los assets **PÚBLICOS** del APK oficial (`assets/tspemqx-app-<host>_*`),
donde están ofuscados con un cifrado de flujo de keystream fijo y desofuscados en tiempo de
ejecución por `libapp.so`. Aquí están empaquetados en la misma forma cifrada + el keystream
(ver `certs/store.json`) y se desofuscan en el setup. No se envía ningún dato por usuario.

El aislamiento entre cuentas se hace vía usuario/contraseña MQTT (clientId + md5) y ACL sobre
los topics, NO mediante el certificado → un único certificado compartido es el modelo de la
propia app.
"""
from __future__ import annotations

import base64
import json
import os

_STORE = os.path.join(os.path.dirname(__file__), "certs", "store.json")
_REQUIRED = ("ca.pem", "client.pem", "client.key")


def _load() -> dict:
    with open(_STORE, encoding="utf-8") as f:
        return json.load(f)


def available_regions() -> list[str]:
    """Hosts MQTT (regiones) para los que existe un juego de certificados empaquetado."""
    try:
        return sorted(_load().get("regions", {}))
    except Exception:
        return []


def decrypt_region(host: str) -> dict[str, bytes] | None:
    """Devuelve {'ca.pem','client.pem','client.key': bytes} para el broker `host`, o None.

    Desofusca los assets (XOR con el keystream fijo, conservando la longitud) — idéntico a lo
    que hace `libapp.so` cuando carga los mismos assets del APK."""
    try:
        store = _load()
        reg = store.get("regions", {}).get(host)
        if not reg:
            return None
        ks = base64.b64decode(store["ks"])
        out: dict[str, bytes] = {}
        for name in _REQUIRED:
            b64 = reg.get(name)
            if not b64:
                return None
            ct = base64.b64decode(b64)
            if len(ct) > len(ks):
                return None
            out[name] = bytes(c ^ ks[i] for i, c in enumerate(ct))
        return out
    except Exception:
        return None
