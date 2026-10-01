"""Ningún nombre se define dos veces en el mismo sitio.

Python se queda con la ÚLTIMA definición y no dice nada. Es un fallo silencioso que ya ha
aparecido dos veces en este componente:

* `sensor.py` llamaba `add(ents)` dos veces seguidas → cada entidad se intentaba dar de alta
  dos veces y el log se llenaba de «cannot be added a second time» (se publicó en la 1.1.0);
* `coordinator.py` tenía DOS `def _probe` en la misma clase, separados por 400 líneas. El
  primero no se ejecutaba nunca, así que editarlo no tenía ningún efecto — y eso es peor que
  un error, porque el cambio parece hecho.

Ruff no lo caza: `F811` sí salta en un caso de laboratorio, pero se calla en `coordinator.py`
(comprobado sobre el fichero real). Así que hace falta mirarlo aparte.

Las parejas `@property` / `@x.setter` NO son duplicados: son la forma normal de escribir una
propiedad con escritura, y `@overload` es lo mismo para los tipos.
"""
from __future__ import annotations

import ast
import collections
import pathlib

COMPONENTE = pathlib.Path(__file__).parent.parent / "custom_components" / "ebro"

#: sufijos de decorador que AUTORIZAN repetir el nombre (property/overload).
REDEFINICIONES_LEGITIMAS = (".setter", ".getter", ".deleter")


def _decoradores(nodo: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in nodo.decorator_list]


def _es_redefinicion_legitima(nodo: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(d.endswith(REDEFINICIONES_LEGITIMAS) or d.split(".")[-1] == "overload"
               for d in _decoradores(nodo))


def _duplicados_en(cuerpo: list[ast.stmt], donde: str, fichero: str) -> list[str]:
    """Nombres definidos más de una vez en ESTE cuerpo (no recursivo: cada ámbito por separado)."""
    definiciones: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = \
        collections.defaultdict(list)
    for nodo in cuerpo:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definiciones[nodo.name].append(nodo)
    fallos = []
    for nombre, nodos in definiciones.items():
        if len(nodos) > 1 and not all(_es_redefinicion_legitima(n) for n in nodos[1:]):
            lineas = ", ".join(str(n.lineno) for n in nodos)
            fallos.append(f"{fichero}: {donde}.{nombre} definido en las líneas {lineas}")
    return fallos


def test_ninguna_funcion_se_define_dos_veces() -> None:
    fallos: list[str] = []
    for py in sorted(COMPONENTE.rglob("*.py")):
        rel = py.relative_to(COMPONENTE.parent.parent).as_posix()
        arbol = ast.parse(py.read_text())
        fallos += _duplicados_en(arbol.body, "<módulo>", rel)
        for clase in (n for n in ast.walk(arbol) if isinstance(n, ast.ClassDef)):
            fallos += _duplicados_en(clase.body, clase.name, rel)
    assert not fallos, "definiciones que tapan a otra (Python se queda con la última):\n" + \
        "\n".join(fallos)


def test_el_detector_distingue_una_property_de_un_duplicado() -> None:
    """Red de seguridad del propio test: si diera por bueno cualquier decorador, no protegería
    nada — un `_probe` duplicado tampoco lleva decorador, pero la pareja property/setter sí."""
    legitimo = ast.parse(
        "class A:\n"
        "    @property\n"
        "    def x(self): ...\n"
        "    @x.setter\n"
        "    def x(self, v): ...\n"
    )
    duplicado = ast.parse(
        "class A:\n"
        "    def x(self): ...\n"
        "    def x(self): ...\n"
    )
    assert _duplicados_en(legitimo.body[0].body, "A", "t.py") == []
    assert _duplicados_en(duplicado.body[0].body, "A", "t.py")
