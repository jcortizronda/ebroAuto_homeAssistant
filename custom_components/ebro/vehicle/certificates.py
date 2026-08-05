"""De dónde salen los certificados mutual-TLS del MQTT del coche y dónde acaban.

El broker EMQX del coche exige mutual-TLS: sin los tres certificados no hay conexión, y por
eso el aprovisionamiento corre ANTES que nada en `async_setup_entry` — si falla, la entrada se
queda en `ConfigEntryNotReady` y Home Assistant reintenta.

Tres fuentes, en orden:

1. **ya están** en la carpeta por vehículo → no se toca nada;
2. **carpeta indicada por el usuario** (`certs_src`), para quien los extrajo a mano de la app;
3. **bundle empaquetado** por región (ver `cert_bundle.py`): los certificados de cliente EMQX
   son constantes universales por región, no datos por cuenta.

Vivía dentro del coordinator, que no tiene por qué saber de rutas, permisos ni formatos de
certificado. Aquí, además, la operación es una función de sus argumentos: se puede probar
sobre un `tmp_path` sin construir un coordinator ni un `hass`.

Todo lo de este módulo es **bloqueante** (toca el sistema de archivos): el llamador lo ejecuta
en un executor, nunca en el bucle de eventos.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import shutil

from ..const import CERT_DIR_MODE, CERT_FILE_MODE, CERT_FILES

_LOGGER = logging.getLogger(__name__)

# Certificados mínimos para el mutual-TLS MQTT. El `.cer` del servidor está en `CERT_FILES`
# porque se copia si el usuario lo trae, pero `tls_set` no lo necesita: por eso el listado de
# lo IMPRESCINDIBLE es más corto que el de lo que se importa.
REQUIRED_CERTS = ("ca.pem", "client.pem", "client.key")


@dataclass(frozen=True)
class CertResult:
    """Resultado del aprovisionamiento: si hay certificados utilizables y por qué."""

    ok: bool
    detail: str

    def __iter__(self):
        """Desempaquetable como `(ok, detail)`, que es como lo consume el setup."""
        return iter((self.ok, self.detail))


def _has_required(certs_dir: str) -> bool:
    return all(os.path.isfile(os.path.join(certs_dir, f)) for f in REQUIRED_CERTS)


def _import_from_dir(certs_dir: str, source: str) -> list[str]:
    """Copia a `certs_dir` los certificados que encuentre en `source`. Devuelve los copiados."""
    copied: list[str] = []
    for name in CERT_FILES:
        src = os.path.join(source, name)
        if os.path.isfile(src):
            dest = os.path.join(certs_dir, name)
            shutil.copy2(src, dest)
            os.chmod(dest, CERT_FILE_MODE)   # son credenciales: solo su propietario
            copied.append(name)
    return copied


def _write_bundle(certs_dir: str, certs: dict[str, bytes]) -> str | None:
    """Vuelca el bundle desofuscado. Devuelve el detalle del error, o `None` si fue bien."""
    try:
        for name, data in certs.items():
            path = os.path.join(certs_dir, name)
            with open(path, "wb") as fh:
                fh.write(data)
            os.chmod(path, CERT_FILE_MODE)
    except OSError as err:
        return f"escritura de certificados del bundle fallida en {certs_dir}: {err}"
    return None


def provision(certs_dir: str, car_host: str, certs_src: str = "") -> CertResult:
    """Garantiza los certificados mutual-TLS en `certs_dir`. Bloqueante: ejecutar en executor.

    El aprovisionamiento desde cero no es automatizable: los certificados nacen del registro de
    dispositivo de la app oficial, que no se puede reproducir aquí. De ahí las tres fuentes.
    """
    os.makedirs(certs_dir, mode=CERT_DIR_MODE, exist_ok=True)

    if _has_required(certs_dir):
        return CertResult(True, "certificados presentes")

    # 1) override manual: carpeta indicada por el usuario en `certs_src`
    if certs_src and os.path.isdir(certs_src):
        copied = _import_from_dir(certs_dir, certs_src)
        if _has_required(certs_dir):
            return CertResult(True, f"certificados importados de {certs_src}: {', '.join(copied)}")

    # 2) bundle empaquetado por región
    try:
        from .cert_bundle import decrypt_region

        certs = decrypt_region(car_host)
    except Exception as err:
        _LOGGER.debug("[certs] bundle no disponible para %s: %s", car_host, err)
        certs = None
    if certs:
        error = _write_bundle(certs_dir, certs)
        if error:
            return CertResult(False, error)
        if _has_required(certs_dir):
            return CertResult(True, f"certificados auto-aprovisionados ({car_host})")

    return CertResult(False, (
        f"faltan los certificados mutual-TLS para {car_host}: región no en el bundle. "
        f"Copia {', '.join(REQUIRED_CERTS)} en {certs_dir} "
        f"(o indica una carpeta en certs_src)."
    ))


def tls_paths(certs_dir: str) -> dict[str, str]:
    """Los tres ficheros que `paho.tls_set` necesita, ya resueltos a rutas absolutas."""
    return {
        "ca_certs": os.path.join(certs_dir, "ca.pem"),
        "certfile": os.path.join(certs_dir, "client.pem"),
        "keyfile": os.path.join(certs_dir, "client.key"),
    }
