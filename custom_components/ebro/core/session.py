#!/usr/bin/env python3
"""
session.py — salud del token Ebro (keep-alive de la sesión).

El token que hace funcionar los botones de comando vive en token.json (wake.TOKEN_PATH).
Puede "caerse" de dos formas:
  1) el access_token caduca con normalidad  -> refresh() lo renueva con el refresh_token;
  2) se abre la app oficial                 -> la sesión se invalida y ni el refresh basta
                                               -> hace falta volver a autenticarse (contraseña).

Este módulo expone las primitivas que el componente conecta a Home Assistant:
  - check()   -> (ok, detalle, status) : ¿el token es válido? (prueba un login BFF)
  - refresh() -> bool                  : renueva el access_token (keep-alive)
"""
import time

# import relativo de paquete
from . import wake  # reutiliza _bff_login / _refresh_token / TOKEN_PATH

# Carpeta de este paquete: la usan los contextos reducidos de diagnóstico.

# Marcadores ESTABLES del resultado de check(). El llamador enruta el remedio según estos,
# NUNCA según el texto humano (que está localizado y puede cambiar con cada retoque de copy).
STATUS_OK = "OK"                # login BFF correcto con el token actual
STATUS_EXPIRED = "EXPIRED"      # token/sesión muertos → hace falta reautenticar
STATUS_NET_ERROR = "NET_ERROR"  # error de red/transitorio → NO es una sesión caducada

# Cuánto tiempo el motivo de la última renovación sigue siendo fiable para decidir el remedio.
# La renovación y el control de sesión ocurren en la misma vuelta (fracciones de segundo): un
# motivo más viejo que esto habla de otro intento y se ignora.
_REASON_FRESH_S = 60.0
# Fracción de vida del token pasada la cual se renueva por adelantado (0.8 = a las 9h36m de 12h):
# lo bastante pronto para tener margen, lo bastante tarde para no malgastar renovaciones.
RENEWAL_QUOTA = 0.8


def check(ctx):
    """Devuelve (ok: bool, detalle: str, status: str).

    `status` es el marcador estable (STATUS_OK/EXPIRED/NET_ERROR): es sobre lo que el
    coordinator decide si abrir la reautenticación. `detalle` es solo para el usuario.
    Distinguir EXPIRED de NET_ERROR es esencial: un corte de red NO debe hacer aparecer la
    tarjeta «Reautenticar» (el usuario reautenticaría en balde)."""
    try:
        ut, _tu = wake._bff_login(ctx)
    except Exception as e:
        return False, f"error de red: {type(e).__name__}", STATUS_NET_ERROR
    if ut:
        return True, "Sesión activa ✅", STATUS_OK
    # El login ha fallado. Pero si falló porque la RENOVACIÓN ni siquiera arrancó (red caída,
    # timeout, DNS), la sesión puede perfectamente seguir viva al otro lado: declararla
    # caducada haría aparecer la tarjeta «Reautenticar» sin motivo.
    # Se confía en el marcador solo si es RECIENTE: uno viejo se refiere a otra vuelta.
    reason = ctx.state.refresh_reason or ""
    fresh = (time.time() - (ctx.state.refresh_ts or 0.0)) < _REASON_FRESH_S
    if fresh and reason.startswith("red:"):
        return False, f"renovación fallida por red ({reason[4:]})", STATUS_NET_ERROR
    return (False,
            "Sesión caducada ❌ — vuelve a autenticarte desde Home Assistant",
            STATUS_EXPIRED)


def refresh(ctx):
    """Renueva el access_token con el refresh_token. True si se renovó."""
    try:
        return bool(wake._refresh_token(ctx))
    except Exception:
        return False


def refresh_if_expiring(ctx, quota: float = RENEWAL_QUOTA) -> tuple[bool, str]:
    """Renovación PROACTIVA: renueva cuando el access token ha consumido `quota` de su vida,
    en vez de esperar a que ya esté muerto.

    Por qué no basta la renovación reactiva: el control de sesión corre cada 15 minutos, y la
    reactiva salta solo DESPUÉS de que el token caduca — es decir, hasta un cuarto de hora
    tarde, con la ventana del refresh_token ya cerrándose. Anticipando se renueva siempre con
    la sesión aún viva; y si la renovación falla se descubre mientras todavía hay tiempo, en
    vez de con la sesión ya muerta.

    Devuelve (renovado, motivo). `(False, "no_hace_falta")` = no tocaba, ninguna llamada.
    Nunca lanza: es una optimización, no debe poder romper el keep-alive."""
    try:
        eta, lifetime = wake._eta_token(ctx)
        if eta < 0 or lifetime <= 0:
            return False, "no_determinable"
        if eta < lifetime * quota:
            return False, "no_hace_falta"
        return wake._refresh_token_detail(ctx)
    except Exception as e:
        return False, f"red:{type(e).__name__}"
