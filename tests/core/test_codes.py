"""Tests de `core/codes.py` — el mapa único código → frase legible."""

from __future__ import annotations

import pytest
from syrupy.assertion import SnapshotAssertion

from custom_components.ebro.core import codes


def test_mapa_completo(snapshot: SnapshotAssertion) -> None:
    """Snapshot del mapa entero.

    Son los únicos textos de diagnóstico que ve el usuario: fijarlos hace que cualquier
    reformulación pase por una revisión explícita del diff.
    """
    assert snapshot == codes.CODE_MEANING


@pytest.mark.parametrize(
    ("code", "esperado"),
    [
        ("000000", "ok ✅"),
        ("A00082", codes.CODE_MEANING["A00082"]),
        # no-cadena: se normaliza antes de buscar
        (0, "código 0"),
        # desconocido → genérico con el código en crudo, nunca un texto inventado
        ("A99999", "código A99999"),
        (None, "sin código"),
    ],
)
def test_meaning(code, esperado) -> None:
    assert codes.meaning(code) == esperado


@pytest.mark.parametrize("code", [None, "A99999"])
def test_meaning_respeta_el_default(code) -> None:
    """El `default` gana tanto para `None` como para un código desconocido."""
    assert codes.meaning(code, default="mi texto") == "mi texto"


def test_meaning_ignora_el_default_si_el_codigo_es_conocido() -> None:
    assert codes.meaning("000000", default="mi texto") == "ok ✅"


def test_los_codigos_enrutados_tienen_texto() -> None:
    """Todo código con una decisión asociada en `routing` debe tener también una frase.

    Si no, el usuario vería «código A00546» en vez de una explicación — el módulo `codes`
    existe justamente para eso.
    """
    from custom_components.ebro.core import routing

    sin_texto = set(routing._TABLE) - set(codes.CODE_MEANING)
    # Los códigos de "solo aviso de configuración" no necesitan frase propia: el detalle del
    # backend ya llega en el mensaje. Los demás sí.
    assert sin_texto == {"A00374", "A00554", "A00604", "A00643", "A00757"}
