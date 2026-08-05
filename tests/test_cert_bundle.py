"""Tests de `cert_bundle.py` — desofuscación de los certificados mutual-TLS empaquetados.

Ambas funciones se tragan **todas** las excepciones y devuelven un valor falsy, así que los
tests de error afirman sobre el valor devuelto y nunca con `pytest.raises`.
"""

from __future__ import annotations

import base64
import json

import pytest

from custom_components.ebro.vehicle import cert_bundle


def test_available_regions_lee_el_store_empaquetado() -> None:
    regiones = cert_bundle.available_regions()
    assert regiones
    assert regiones == sorted(regiones)
    assert all(isinstance(r, str) for r in regiones)


def test_decrypt_region_devuelve_los_tres_certificados() -> None:
    """Camino feliz contra el `certs/store.json` real que se distribuye con la integración."""
    host = cert_bundle.available_regions()[0]
    out = cert_bundle.decrypt_region(host)

    assert out is not None
    assert set(out) == set(cert_bundle._REQUIRED)
    assert all(isinstance(v, bytes) and v for v in out.values())
    # los certificados desofuscados deben ser PEM legible: si el XOR fuera erróneo, paho
    # fallaría al cargarlos en el momento de conectar, ya en producción.
    assert out["ca.pem"].startswith(b"-----BEGIN ")
    assert out["client.pem"].startswith(b"-----BEGIN ")
    assert out["client.key"].startswith(b"-----BEGIN ")


def test_decrypt_region_host_desconocido() -> None:
    assert cert_bundle.decrypt_region("broker.que.no.existe") is None


def test_decrypt_region_falta_un_miembro_requerido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si al store le falta uno de los tres certificados se devuelve None entero, no un dict
    a medias: conectar con un juego incompleto fallaría más tarde y peor."""
    store = {
        "ks": base64.b64encode(b"\x00" * 64).decode(),
        "regions": {"host": {"ca.pem": base64.b64encode(b"x").decode()}},
    }
    monkeypatch.setattr(cert_bundle, "_load", lambda: store)

    assert cert_bundle.decrypt_region("host") is None


def test_decrypt_region_ciphertext_mas_largo_que_el_keystream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El keystream es de longitud fija: un blob más largo indicaría un store corrupto."""
    store = {
        "ks": base64.b64encode(b"\x00" * 4).decode(),
        "regions": {
            "host": {
                name: base64.b64encode(b"demasiado-largo").decode()
                for name in cert_bundle._REQUIRED
            }
        },
    }
    monkeypatch.setattr(cert_bundle, "_load", lambda: store)

    assert cert_bundle.decrypt_region("host") is None


def test_xor_con_keystream_conocido(monkeypatch: pytest.MonkeyPatch) -> None:
    """El algoritmo es un XOR que conserva la longitud, igual que hace `libapp.so`."""
    ks = bytes(range(16))
    claro = b"HOLA"
    cifrado = bytes(c ^ ks[i] for i, c in enumerate(claro))
    store = {
        "ks": base64.b64encode(ks).decode(),
        "regions": {
            "host": {
                name: base64.b64encode(cifrado).decode() for name in cert_bundle._REQUIRED
            }
        },
    }
    monkeypatch.setattr(cert_bundle, "_load", lambda: store)

    out = cert_bundle.decrypt_region("host")

    assert out == dict.fromkeys(cert_bundle._REQUIRED, claro)


def test_store_ilegible_no_lanza(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un store ausente o corrupto degrada a lista vacía / None, nunca a una excepción que
    rompiera el setup del entry."""

    def _boom() -> dict:
        raise OSError("store ilegible")

    monkeypatch.setattr(cert_bundle, "_load", _boom)

    assert cert_bundle.available_regions() == []
    assert cert_bundle.decrypt_region("cualquiera") is None


def test_el_store_empaquetado_es_json_valido() -> None:
    """Guarda contra un `store.json` truncado por un merge: sin él, el fallo aparecería solo
    al conectar el MQTT, ya en casa del usuario."""
    with open(cert_bundle._STORE, encoding="utf-8") as fh:
        store = json.load(fh)

    assert "ks" in store
    assert store["regions"]
    for host, reg in store["regions"].items():
        assert set(cert_bundle._REQUIRED) <= set(reg), host
