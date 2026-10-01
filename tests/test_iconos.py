"""Los nombres de icono `mdi:` que usa el componente existen de verdad.

Un icono inexistente NO da error en ninguna parte: Home Assistant guarda el nombre en el
atributo `icon` tal cual, el frontend no encuentra el SVG y dibuja un hueco. La entidad
funciona, los tests de snapshot pasan (el snapshot guarda el nombre equivocado como correcto)
y lo único que falla es lo que se ve. Pasó de verdad con `mdi:map-marker-clock`, que no existe
— el bueno es `mdi:map-clock` — y no lo detectó nada hasta mirar la interfaz.

**De dónde sale el catálogo.** `tests/fixtures/mdi_icons.txt` lleva los nombres que el
frontend de Home Assistant es capaz de dibujar, extraídos de `hass_frontend`. Está copiado en
el repo a propósito: el paquete `home-assistant-frontend` ocupa ~300 MB instalado, que es un
precio absurdo en CI y en el venv de test para comprobar 50 cadenas. La copia son 118 KB.

Para regenerarlo cuando se quiera un icono nuevo de una versión más moderna de MDI, desde un
entorno que SÍ tenga el frontend (el venv de Home Assistant core, por ejemplo):

    python - <<'EOF'
    import json, pathlib
    from hass_frontend import where
    nombres = set()
    for f in (pathlib.Path(where()) / "static" / "mdi").glob("*.json"):
        d = json.loads(f.read_text())
        if isinstance(d, dict):
            nombres.update(k for k in d if isinstance(k, str))
    pathlib.Path("tests/fixtures/mdi_icons.txt").write_text("\n".join(sorted(nombres)) + "\n")
    EOF
"""
from __future__ import annotations

import pathlib
import re

COMPONENTE = pathlib.Path(__file__).parent.parent / "custom_components" / "ebro"
CATALOGO = pathlib.Path(__file__).parent / "fixtures" / "mdi_icons.txt"

#: solo dentro de comillas: así un comentario que MENCIONE un icono retirado (como el
#: `mdi:map-marker-clock` del docstring de arriba) no hace fallar el test.
USO_DE_ICONO = re.compile(r"""["']mdi:([a-z0-9-]+)["']""")


def _catalogo() -> set[str]:
    return {linea.strip() for linea in CATALOGO.read_text().splitlines() if linea.strip()}


def _iconos_usados() -> dict[str, list[str]]:
    """{nombre de icono: [ficheros donde aparece]}, recorriendo todo el componente."""
    usados: dict[str, list[str]] = {}
    for py in sorted(COMPONENTE.rglob("*.py")):
        for nombre in USO_DE_ICONO.findall(py.read_text()):
            usados.setdefault(nombre, []).append(py.relative_to(COMPONENTE.parent.parent).as_posix())
    return usados


def test_el_catalogo_esta_completo() -> None:
    """Red de seguridad del propio test: un fichero truncado o vacío validaría cualquier cosa.

    MDI ronda los 7.500 iconos; si la copia bajase de 5.000 es que la regeneración salió mal,
    y entonces este test dejaría de proteger nada sin que se notase."""
    catalogo = _catalogo()
    assert len(catalogo) > 5000, f"catálogo MDI sospechosamente corto: {len(catalogo)} nombres"
    assert "map-clock" in catalogo
    assert "map-marker-clock" not in catalogo   # el que NO existe, el del bug


def test_todos_los_iconos_del_componente_existen() -> None:
    catalogo = _catalogo()
    usados = _iconos_usados()
    assert usados, "no se ha encontrado ni un icono: ¿cambió la forma de declararlos?"
    inexistentes = {n: sitios for n, sitios in sorted(usados.items()) if n not in catalogo}
    assert not inexistentes, "iconos que MDI no tiene (se verán como un hueco en la interfaz): " + \
        "; ".join(f"mdi:{n} en {', '.join(sitios)}" for n, sitios in inexistentes.items())
