#!/usr/bin/env python3
"""Tabla ÚNICA de enrutado de los códigos del backend.

**Qué decide este módulo.** El backend Chery responde siempre HTTP 200 y pone el
resultado real en un `code` (`A00xxx`). De ese código dependen cuatro decisiones
independientes:

1. ¿el comando ha tenido éxito?
2. qué **remedio** proponer al usuario (`reason`) — y por tanto qué acción realiza Home
   Assistant: reautenticación, Repair del PIN, o solo un aviso;
3. ¿el error debe **contar** hacia el anti-bloqueo del PIN?
4. ¿conviene **regenerar el taskId** y reintentar?

Tenerlas repartidas tenía un coste medible: el respaldo del despertar clasificaba como
«PIN erróneo» rechazos que eran de permisos o de sesión, así que mostraba el remedio
equivocado y — peor — acercaba el bloqueo real de la cuenta por una causa que con el PIN
no tenía nada que ver.

**Reparto de tareas.** Aquí están las DECISIONES; en `codes.py` quedan los TEXTOS
legibles. Es la regla del proyecto: nunca se decide sobre una cadena localizada, porque
traducir o reformular un mensaje no debe poder desactivar la reautenticación.

**El contexto importa.** El mismo código puede significar cosas distintas según de dónde
llegue: `A00567` durante `checkPassword` es una petición malformada (no es el PIN), pero
en respuesta a un comando significa «taskId no válido» → se regenera y se reintenta. Por
eso `clasifica()` pide siempre el contexto.

**El valor por defecto es deliberadamente asimétrico.** Un código desconocido en
`checkPassword` cae en la rama PIN (conservador: mejor proponer revisar el PIN que dejar
al usuario sin remedio), mientras que en respuesta a un comando queda no bloqueante — no
se inventa un fallo que el backend no ha declarado.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# ───────────────────────── remedios (`reason`) ─────────────────────────
# Valores ESTABLES: el coordinator enruta según estos, nunca según el texto del mensaje.
REASON_PIN = "pin"        # PIN de comandos erróneo → Repair «PIN de comandos erróneo»
REASON_REAUTH = "reauth"  # sesión/token muertos → reautenticación nativa de HA
REASON_CONFIG = "config"  # ni PIN ni sesión (permisos, petición malformada) → solo aviso
REASON_NONE = None     # rechazo del coche (ocupado / no permitido / en reposo) → aviso

# ───────────────────────── acciones del coordinator ─────────────────────────
ACTION_REAUTH = "reauth"          # entry.async_start_reauth
ACTION_REPAIR_PIN = "repair_pin"  # abre el aviso de reparación del PIN
ACTION_NOTICE = "aviso"           # ningún remedio automático: se muestra y ya

_ACTION_BY_REASON = {
    REASON_REAUTH: ACTION_REAUTH,
    REASON_PIN: ACTION_REPAIR_PIN,
    REASON_CONFIG: ACTION_NOTICE,
    REASON_NONE: ACTION_NOTICE,
}


def action_for_reason(reason: str | None) -> str:
    """Remedio → acción concreta de Home Assistant.

    Un `reason` desconocido degrada a aviso: nunca a una acción invasiva como forzar una
    reautenticación o abrir un Repair que el usuario no puede resolver."""
    return _ACTION_BY_REASON.get(reason, ACTION_NOTICE)


# ───────────────────────── contextos ─────────────────────────
CONTEXT_CHECKPASSWORD = "checkpassword"  # generación del taskId (verificación del PIN)
CONTEXT_COMMAND = "comando"              # envío de un comando al coche


@dataclass(frozen=True)
class Classification:
    """Qué hacer con un código, en un contexto."""

    code: str | None = None
    reason: str | None = REASON_NONE
    # resultado del envío: "ok" aceptado, "ko" rechazado, "unknown" = el backend no se ha
    # expresado de forma reconocible → no bloqueante, por prudencia.
    outcome: str = "unknown"
    retryable: bool = False            # tiene sentido reintentar tal cual (coche ocupado)
    regenerate_taskid: bool = False       # el taskId hay que rehacerlo: regenera y reintenta UNA vez
    counts_for_lockout: bool = False        # incrementa el anti-bloqueo del PIN

    @property
    def action(self) -> str:
        return action_for_reason(self.reason)

    @property
    def success(self) -> bool:
        return self.outcome == "ok"

    @property
    def failed(self) -> bool:
        return self.outcome == "ko"


def _entry(**kw) -> Classification:
    return Classification(**kw)


# ───────────────────────── la tabla ─────────────────────────
# Válida en AMBOS contextos salvo override explícito más abajo.
_TABLE: dict[str, Classification] = {
    # — aceptados —
    "000000": _entry(outcome="ok"),
    "A00079": _entry(outcome="ok"),

    # — rechazos del coche: ningún remedio automático, solo aviso —
    # el coche ejecuta UN comando cada vez → transitorio, reintentable
    "A00082": _entry(outcome="ko", retryable=True),
    # permiso denegado para ESA función (visto en vivo con remoteStart)
    "A00084": _entry(outcome="ko"),
    "A07312": _entry(outcome="ko"),   # rate-limit del despertar
    "A07900": _entry(outcome="ko"),   # coche en reposo / firma o car_token no válidos

    # — taskId a rehacer: se regenera y se reintenta una vez —
    "A00089": _entry(outcome="ko", regenerate_taskid=True),
    "A00546": _entry(outcome="ko", regenerate_taskid=True),
    "A00567": _entry(outcome="ko", regenerate_taskid=True),

    # — sesión muerta: el único remedio es reautenticar (el PIN es irrelevante) —
    "A00000": _entry(outcome="ko", reason=REASON_REAUTH),

    # — no es el PIN: permisos sobre el vehículo o petición mal construida —
    # NO cuentan para el anti-bloqueo: contarlos acercaría el bloqueo real de la cuenta
    # por una causa que con el PIN no tiene nada que ver (bug P1-2).
    "A00374": _entry(reason=REASON_CONFIG),   # permisos del vehículo
    "A00554": _entry(reason=REASON_CONFIG),   # autorización del vehículo
    "A00604": _entry(reason=REASON_CONFIG),   # clientType ausente/erróneo
    "A00643": _entry(reason=REASON_CONFIG),   # taskId ausente en la petición
    "A00757": _entry(reason=REASON_CONFIG),   # petición malformada
}

# Override por contexto. `A00567` es el caso a tener en cuenta: en `checkPassword` es una
# petición incompleta (el PIN puede ser correcto), en respuesta a un comando es un taskId
# a rehacer. Mismo código, dos remedios distintos.
_OVERRIDE_CHECKPASSWORD: dict[str, Classification] = {
    "A00567": _entry(reason=REASON_CONFIG),
    # PIN/contraseña erróneos: los únicos que de verdad deben contar para el bloqueo.
    "A00285": _entry(reason=REASON_PIN, counts_for_lockout=True),
    "A00282": _entry(reason=REASON_PIN, counts_for_lockout=True),
}

# Código nunca visto. La asimetría es intencionada — ver el docstring del módulo.
_DEFAULT_CHECKPASSWORD = _entry(reason=REASON_PIN, counts_for_lockout=True)
_DEFAULT_COMMAND = _entry(outcome="unknown")


def classify(code, context: str) -> Classification:
    """Código del backend → qué hacer, en el contexto dado.

    `code` puede ser `None` o no-cadena (respuesta ilegible): se normaliza.
    """
    key = str(code) if code is not None else ""

    if context == CONTEXT_CHECKPASSWORD:
        entry_ = _OVERRIDE_CHECKPASSWORD.get(key) or _TABLE.get(key)
        if entry_ is None:
            entry_ = _DEFAULT_CHECKPASSWORD
    else:
        entry_ = _TABLE.get(key, _DEFAULT_COMMAND)

    return replace(entry_, code=key or None)


# ───────────────────────── vistas derivadas (compatibilidad) ─────────────────────────
# Conjuntos derivados DE la tabla, no escritos a mano al lado: antes eran listas paralelas
# que podían divergir en silencio de cómo se enrutaban los códigos.
SUCCESS_CODES = frozenset(c for c, v in _TABLE.items() if v.outcome == "ok")
FAILURE_CODES = frozenset(c for c, v in _TABLE.items() if v.outcome == "ko")
RETRYABLE_CODES = frozenset(c for c, v in _TABLE.items() if v.retryable)
TASKID_INVALID = frozenset(c for c, v in _TABLE.items() if v.regenerate_taskid)
