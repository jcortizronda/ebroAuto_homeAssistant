"""Tests de `diag.py` — el monitor de diagnóstico del desarrollador.

Es un módulo de puro Python (nada de HA), así que estos tests son rápidos y directos. El
grueso está en la OCULTACIÓN: el archivo que genera acaba en manos de terceros, así que cada
regla de `redact`/`scrub_coordinates` tiene su caso — incluidos los falsos positivos, que
importan tanto como los negativos: un patrón demasiado goloso destrozaría los timestamps y
dejaría el diagnóstico inservible.
"""

from __future__ import annotations

import json
import math
import os
import time

from freezegun.api import FrozenDateTimeFactory
import pytest

from custom_components.ebro.const import DIAG_SWITCH_FILE
from custom_components.ebro.vehicle import diag

# ───────────────────────── scrub_coordinates ─────────────────────────


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        # coordenadas: al menos 4 decimales y parte entera de 1-3 cifras
        ("lat=40.9012345", "lat=**GEO**"),
        ("lon=-3.7037900", "lon=**GEO**"),
        ("lat=40.9012, lon=14.3456", "lat=**GEO**, lon=**GEO**"),
    ],
)
def test_scrub_coordinates_caza_las_coordenadas(texto: str, esperado: str) -> None:
    assert diag.scrub_coordinates(texto) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        # timestamp ISO: el `(?<![:\d.])` impide que se coma la parte de los segundos
        "2026-01-15T12:00:07.428912",
        "12:00:07.428912",
        # epoch: parte entera de mucho más de 3 cifras
        "1768478400.123456",
        # telemetría real: pocos decimales
        "temperatura=21.0",
        "tension=384.5",
        "presion=2.92",
        "",
    ],
)
def test_scrub_coordinates_no_destroza_lo_demas(texto: str) -> None:
    """Un patrón demasiado goloso dejaría el diagnóstico inservible: sin timestamps legibles
    no se puede correlacionar nada."""
    assert diag.scrub_coordinates(texto) == texto


# ───────────────────────── redact ─────────────────────────


def test_redact_enmascara_las_claves_sensibles() -> None:
    out = diag.redact({"pin": "1234", "token": "abc", "password": "x"})

    assert out == {"pin": diag.REDACTED, "token": diag.REDACTED, "password": diag.REDACTED}


def test_redact_elimina_las_claves_geo_en_vez_de_enmascararlas() -> None:
    """La posición se DESCARTA, no se enmascara: dejar `lat: **REDACTED**` seguiría diciendo
    que hay un fix y en qué momento — información que este archivo no debe llevar."""
    out = diag.redact({"lat": "40.9", "lon": "-3.7", "dumpEnergy": "64"})

    assert out == {"dumpEnergy": "64"}


def test_redact_deja_en_claro_las_clear_keys() -> None:
    """`cp_code`/`cp_msg` son la respuesta CRUDA de checkPassword: es la única forma de saber
    si un rechazo fue de verdad el PIN u otra causa. Se dejan a propósito."""
    out = diag.redact({"cp_code": "A00285", "cp_msg": "wrong password"})

    assert out == {"cp_code": "A00285", "cp_msg": "wrong password"}


def test_redact_baja_recursivamente() -> None:
    out = diag.redact({"nivel1": {"nivel2": {"pin": "1234", "ok": True}}})

    assert out["nivel1"]["nivel2"] == {"pin": diag.REDACTED, "ok": True}


def test_redact_corta_la_profundidad() -> None:
    """Guarda contra un payload patológico que hiciera explotar la recursión."""
    profundo: dict = {"v": 1}
    for _ in range(diag.MAX_DEPTH + 3):
        profundo = {"n": profundo}

    assert "**DEPTH**" in json.dumps(diag.redact(profundo))


def test_redact_corta_los_dicts_enormes() -> None:
    out = diag.redact({f"k{i}": i for i in range(diag.MAX_ITEMS + 10)})

    assert out["**TRUNCATED**"] == 10
    assert len(out) == diag.MAX_ITEMS + 1


def test_redact_corta_las_listas_enormes() -> None:
    assert len(diag.redact(list(range(diag.MAX_ITEMS + 10)))) == diag.MAX_ITEMS


def test_redact_corta_las_cadenas_largas() -> None:
    assert len(diag.redact("x" * (diag.MAX_STR + 100))) == diag.MAX_STR


@pytest.mark.parametrize(
    ("texto", "marca"),
    [
        ("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----", "**PEM**"),
        ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def", "**JWT**"),
        ("contacto: alguien@example.com", "**EMAIL**"),
        ("vehiculo LSJA0000000000001 listo", "**VIN**"),
        ("tuserid 123456789012345678", "**NUM**"),
        ("hash " + "a" * 40, "**HEX**"),
        ("fichero /config/ebro_LSJA_token.json", "**PATH**"),
        ("posicion 40.9012345", "**GEO**"),
    ],
)
def test_los_patrones_cazan_secretos_en_campos_desconocidos(texto: str, marca: str) -> None:
    """Red de seguridad sobre las CADENAS: intercepta un secreto aunque esté bajo una clave
    con nombre inocuo. Es el caso de la fuga vista en campo el 2026-07-20, donde una
    coordenada había acabado bajo la clave `sample`.
    """
    assert marca in diag.redact({"campo_cualquiera": texto})["campo_cualquiera"]


def test_redact_oculta_los_valores_conocidos_de_la_entrada() -> None:
    """`extra` lleva el VIN y el email reales: se sustituyen aunque no encajen en ningún patrón."""
    out = diag.redact({"msg": "coche MICOCHE parado"}, extra=("MICOCHE",))

    assert "MICOCHE" not in out["msg"]


def test_redact_ignora_los_extra_demasiado_cortos() -> None:
    """Sustituir una cadena de 1-3 caracteres destrozaría cualquier texto."""
    out = diag.redact({"msg": "abc def"}, extra=("a",))

    assert out["msg"] == "abc def"


@pytest.mark.parametrize("valor", [1, 1.5, True, None])
def test_redact_conserva_los_escalares(valor) -> None:
    assert diag.redact({"v": valor})["v"] == valor


# ───────────────────────── read_switch ─────────────────────────


def test_read_switch_sin_fichero(tmp_path) -> None:
    """Estado por defecto en producción y en los tests: el monitor duerme y su coste es cero."""
    assert diag.read_switch(str(tmp_path / DIAG_SWITCH_FILE)) is None


@pytest.mark.parametrize("contenido", ["0", "siempre", "always", "inf", "INF"])
def test_read_switch_sin_vencimiento(tmp_path, contenido: str) -> None:
    """Un evento raro puede no ocurrir dentro de una ventana fija; encontrar el monitor
    apagado significaría haber perdido los días de espera."""
    bandera = tmp_path / DIAG_SWITCH_FILE
    bandera.write_text(contenido, encoding="utf-8")

    assert diag.read_switch(str(bandera)) == math.inf


def test_read_switch_con_dias(tmp_path, freezer: FrozenDateTimeFactory) -> None:
    bandera = tmp_path / DIAG_SWITCH_FILE
    bandera.write_text("2", encoding="utf-8")

    until = diag.read_switch(str(bandera))

    assert until == pytest.approx(os.stat(bandera).st_mtime + 2 * 86400)


def test_read_switch_vacio_usa_el_default(tmp_path) -> None:
    bandera = tmp_path / DIAG_SWITCH_FILE
    bandera.write_text("", encoding="utf-8")

    until = diag.read_switch(str(bandera))

    assert until == pytest.approx(os.stat(bandera).st_mtime + diag.DEFAULT_DAYS * 86400)


@pytest.mark.parametrize(("contenido", "dias"), [("0.5", 3), ("99", 7), ("-5", 1), ("x", 3)])
def test_read_switch_acota_los_dias(tmp_path, contenido: str, dias: int) -> None:
    """Se acota a 1-7 días; un contenido ilegible cae al valor por defecto."""
    bandera = tmp_path / DIAG_SWITCH_FILE
    bandera.write_text(contenido, encoding="utf-8")

    until = diag.read_switch(str(bandera))

    assert until == pytest.approx(os.stat(bandera).st_mtime + dias * 86400)


def test_read_switch_vencido_se_autoapaga(tmp_path, freezer: FrozenDateTimeFactory) -> None:
    """Al vencer se renombra a `.off`: el monitor no vuelve a arrancar, pero queda rastro de
    cuándo estuvo activo."""
    bandera = tmp_path / DIAG_SWITCH_FILE
    bandera.write_text("1", encoding="utf-8")

    freezer.tick(86400 + 10)

    assert diag.read_switch(str(bandera)) is None
    assert not bandera.exists()
    assert (tmp_path / diag.SWITCH_OFF).exists()


def test_disarm_switch_nunca_lanza(tmp_path) -> None:
    diag.disarm_switch(str(tmp_path / "no-existe.on"))


# ───────────────────────── DiagRecorder ─────────────────────────


@pytest.fixture
def recorder(tmp_path):
    """Recorder real, con su hilo escritor, sobre `tmp_path`."""
    rec = diag.DiagRecorder(str(tmp_path / "diag.jsonl"), vin="LSJA0000000000001")
    yield rec
    rec.close()


def test_recorder_registra_el_arranque(recorder, tmp_path) -> None:
    recorder.close()  # fuerza el vaciado del hilo escritor

    lineas = (tmp_path / "diag.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lineas[0])["type"] == "diag_start"


def test_recorder_cuenta_los_eventos(recorder) -> None:
    """Los contadores son PLANOS (`commands_total`, `commands_failed`): es la síntesis que
    deja ver un problema sin tener que leer 500 eventos."""
    recorder.record("command", key="bloquear", ok=True)
    recorder.record("command", key="desbloquear", ok=False)

    contadores = recorder.snapshot()["counters"]

    assert contadores["commands_total"] == 2
    assert contadores["commands_failed"] == 1


def test_recorder_no_lanza_nunca(recorder) -> None:
    """El monitor observa, no participa: un fallo suyo no puede tumbar un comando."""
    recorder.record("command", objeto_raro=object())


def test_note_unknown_field_solo_la_primera_vez(recorder) -> None:
    """El auto-descubrimiento emite una muestra por campo: repetirla en cada push llenaría el
    archivo con el mismo dato."""
    recorder.note_unknown_field("campoNuevo", "42", "5A02")
    recorder.note_unknown_field("campoNuevo", "43", "5A02")

    assert list(recorder.snapshot()["unknown_fields"]) == ["campoNuevo"]


def test_note_unknown_field_borra_las_muestras_geo(recorder) -> None:
    """Es la fuga de campo del 2026-07-20: una coordenada acababa registrada como «muestra»
    de un campo por mapear."""
    recorder.note_unknown_field("sample", "40.9012345", "5A02")

    assert "40.9012345" not in json.dumps(recorder.snapshot()["unknown_fields"])


def test_snapshot_pasa_por_la_ocultacion(recorder) -> None:
    recorder.record("pin_event", outcome="fail", pin="1234")

    assert "1234" not in json.dumps(recorder.snapshot())


def test_rotacion_del_fichero(tmp_path) -> None:
    """A 2 MiB se rota a `.jsonl.1`: «encendido para siempre» no debe ser un riesgo de espacio."""
    ruta = tmp_path / "diag.jsonl"
    rec = diag.DiagRecorder(str(ruta), vin="V")
    try:
        rec.record("relleno", data="x" * 1000)
        rec.close()
        # se simula un archivo ya pasado del umbral y se fuerza la rotación
        ruta.write_bytes(b"x" * (diag.FILE_MAX_BYTES + 1))
        rec2 = diag.DiagRecorder(str(ruta), vin="V")
        rec2.record("tras_rotar", ok=True)
        rec2.close()
    finally:
        pass

    assert (tmp_path / "diag.jsonl.1").exists()


@pytest.mark.parametrize(
    ("ts", "esperado"), [(None, None), (math.inf, "sin vencimiento")]
)
def test_iso(ts, esperado) -> None:
    assert diag._iso(ts) == esperado


def test_iso_formatea_un_epoch() -> None:
    assert diag._iso(time.time()).startswith("20")


def test_percentiles() -> None:
    p = diag._percentiles([10, 20, 30, 40, 50])

    assert p["n"] == 5
    assert p["max"] == 50
    assert p["p50"] == 30


def test_percentiles_lista_vacia() -> None:
    """Sin muestras no se publica una sección de latencia vacía y engañosa: se devuelve {}."""
    assert diag._percentiles([]) == {}
