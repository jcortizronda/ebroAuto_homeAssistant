"""Tests de `certificates.py` — de dónde salen los certificados mutual-TLS del MQTT.

Antes esto solo se ejercitaba con `_provision_certs` mockeado en el conftest, es decir: no se
ejercitaba. Y no es código menor — si falla, la entrada no carga (`ConfigEntryNotReady`) y el
usuario no tiene coche en Home Assistant. Ahora que la operación es función de sus argumentos
se prueba de verdad, sobre un `tmp_path`.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
from unittest.mock import patch

from custom_components.ebro.vehicle.certificates import REQUIRED_CERTS, provision, tls_paths

HOST = "tspemqx-app-eu.ebroauto.com"


def _crear_certs(carpeta: Path, names=REQUIRED_CERTS) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    for name in names:
        (carpeta / name).write_bytes(b"-----BEGIN CERTIFICATE-----\n")


def test_si_ya_estan_no_se_toca_nada(tmp_path: Path) -> None:
    certs_dir = tmp_path / "certs"
    _crear_certs(certs_dir)
    antes = {f: (certs_dir / f).stat().st_mtime_ns for f in REQUIRED_CERTS}

    ok, detalle = provision(str(certs_dir), HOST)

    assert ok is True
    assert detalle == "certificados presentes"
    assert {f: (certs_dir / f).stat().st_mtime_ns for f in REQUIRED_CERTS} == antes


def test_importa_de_la_carpeta_indicada_por_el_usuario(tmp_path: Path) -> None:
    origen = tmp_path / "mis_certs"
    _crear_certs(origen)
    certs_dir = tmp_path / "certs"

    ok, detalle = provision(str(certs_dir), HOST, certs_src=str(origen))

    assert ok is True
    assert "importados" in detalle
    assert all((certs_dir / f).is_file() for f in REQUIRED_CERTS)


def test_los_certificados_importados_quedan_solo_para_su_propietario(tmp_path: Path) -> None:
    """Son credenciales: si el fichero es legible por todos, cualquier cosa dentro de HA
    puede hablar con el coche."""
    origen = tmp_path / "mis_certs"
    _crear_certs(origen)
    certs_dir = tmp_path / "certs"

    provision(str(certs_dir), HOST, certs_src=str(origen))

    for name in REQUIRED_CERTS:
        modo = stat.S_IMODE((certs_dir / name).stat().st_mode)
        assert modo == 0o600, f"{name} tiene permisos {modo:o}"
    assert stat.S_IMODE(certs_dir.stat().st_mode) == 0o700


def test_una_carpeta_incompleta_no_cuenta_como_exito(tmp_path: Path) -> None:
    """Con solo dos de los tres, el mutual-TLS no puede negociarse: es un fallo, no un
    éxito parcial.

    Se anula el bundle a propósito para aislar esta rama: con el bundle disponible la carpeta
    incompleta SÍ acaba en éxito, porque el bundle aporta el que falta — y eso es lo correcto,
    no un fallo (ver el test siguiente)."""
    origen = tmp_path / "mis_certs"
    _crear_certs(origen, names=("ca.pem", "client.pem"))
    certs_dir = tmp_path / "certs"

    with patch("custom_components.ebro.vehicle.cert_bundle.decrypt_region", return_value=None):
        ok, detalle = provision(str(certs_dir), HOST, certs_src=str(origen))

    assert ok is False
    assert "faltan los certificados" in detalle


def test_el_bundle_completa_una_carpeta_incompleta(tmp_path: Path) -> None:
    """Las fuentes se acumulan: lo que el usuario aporte a medias lo termina el bundle."""
    origen = tmp_path / "mis_certs"
    _crear_certs(origen, names=("ca.pem", "client.pem"))
    certs_dir = tmp_path / "certs"

    ok, _detalle = provision(str(certs_dir), HOST, certs_src=str(origen))

    assert ok is True
    assert all((certs_dir / f).is_file() for f in REQUIRED_CERTS)


def test_cae_al_bundle_empaquetado_por_region(tmp_path: Path) -> None:
    certs_dir = tmp_path / "certs"
    bundle = {n: f"contenido de {n}".encode() for n in REQUIRED_CERTS}

    with patch("custom_components.ebro.vehicle.cert_bundle.decrypt_region", return_value=bundle):
        ok, detalle = provision(str(certs_dir), HOST)

    assert ok is True
    assert "auto-aprovisionados" in detalle
    assert (certs_dir / "ca.pem").read_bytes() == b"contenido de ca.pem"


def test_region_desconocida_explica_que_hacer(tmp_path: Path) -> None:
    """El mensaje acaba en `ConfigEntryNotReady`, o sea a la vista del usuario: tiene que
    decirle dónde poner los ficheros."""
    certs_dir = tmp_path / "certs"

    with patch("custom_components.ebro.vehicle.cert_bundle.decrypt_region", return_value=None):
        ok, detalle = provision(str(certs_dir), "broker-de-otra-region.example.com")

    assert ok is False
    assert str(certs_dir) in detalle
    assert all(name in detalle for name in REQUIRED_CERTS)


def test_un_bundle_roto_no_lanza(tmp_path: Path) -> None:
    """Una excepción aquí dejaría la entrada en un error sin explicación."""
    certs_dir = tmp_path / "certs"

    with patch(
        "custom_components.ebro.vehicle.cert_bundle.decrypt_region", side_effect=ValueError("corrupto")
    ):
        ok, detalle = provision(str(certs_dir), HOST)

    assert ok is False
    assert "faltan los certificados" in detalle


def test_tls_paths_apunta_a_los_tres_ficheros_que_paho_necesita(tmp_path: Path) -> None:
    rutas = tls_paths(str(tmp_path))

    assert rutas == {
        "ca_certs": os.path.join(str(tmp_path), "ca.pem"),
        "certfile": os.path.join(str(tmp_path), "client.pem"),
        "keyfile": os.path.join(str(tmp_path), "client.key"),
    }
