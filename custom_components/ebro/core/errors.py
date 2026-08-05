#!/usr/bin/env python3
"""El error de un comando rechazado.

Vive en su propio módulo porque lo comparten los dos lados del camino: `taskid`, que falla
cuando el PIN no vale, y `commands`, que falla cuando el coche rechaza el envío. Tenerlo en uno
de los dos creaba un import circular — y, más de fondo, habría sugerido que el error pertenece a
una de las dos etapas cuando describe el resultado de la operación entera.
"""
from . import routing

# Reintentar tiene sentido solo con los códigos que la tabla marca como transitorios (coche
# ocupado). Derivado de `routing`, no una lista paralela.
RETRYABLE_CODES = routing.RETRYABLE_CODES


class CommandError(Exception):
    """Comando rechazado por el backend/coche (NO ejecutado). `code` = código tspconsole,
    `retryable` = True si reintentar tiene sentido (ej. coche ocupado). El coordinator lo deja
    propagar; la entidad optimista lo captura para anular el estado optimista y mostrar el
    error real al usuario, en vez de quedarse bloqueada en un falso éxito.

    `reason` enruta el REMEDIO en el coordinator (routing por causa, no solo por código):
      - "pin"    = PIN de comandos erróneo / anti-bloqueo / PIN ausente → reconfigurar el PIN
                   (Repair fixable / Configurar → Reconfigurar). NO es un problema de sesión: el
                   token es válido, los sensores funcionan.
      - "reauth" = sesión/token caducados (login fallido, code A00000) → reautenticación nativa
                   de HA. Reautenticar NO cambia el PIN: los dos canales son distintos.
      - "config" = rechazo NO imputable al PIN ni a la sesión (permisos del vehículo, petición
                   malformada, generación de taskId desactivada): ningún remedio automático,
                   solo aviso. No abre el Repair del PIN y no cuenta para el anti-bloqueo.
      - None     = otro rechazo del coche (ocupado, no permitido, en reposo): solo aviso."""

    def __init__(self, message: str, code: str | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = code in RETRYABLE_CODES
