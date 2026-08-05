"""El vehículo: su estado, su conexión y sus decisiones.

Este subpaquete es el **runtime** de la integración, y se distingue de sus dos vecinos por lo
que sabe:

* `core/` habla el protocolo de Chery y no sabe qué es Home Assistant;
* el nivel superior es el **contrato con Home Assistant** — `__init__.py`, `config_flow.py`,
  `diagnostics.py`, `repairs.py` y las diez plataformas: sus nombres y su ubicación los fija
  HA, que los descubre por ruta, así que no se pueden mover;
* aquí en medio está lo nuestro: el coordinator y los colaboradores en los que delega.

Vivía todo mezclado arriba, 26 módulos planos en los que no se distinguía lo que HA obliga a
poner ahí de lo que era decisión nuestra.

Las dependencias van en un solo sentido — `const`/`helpers`/`models` ← `core/` ← `vehicle/` ←
`entity` ← plataformas. Nada de aquí importa una plataforma.
"""
