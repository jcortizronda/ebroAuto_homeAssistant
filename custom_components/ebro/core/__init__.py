"""Núcleo de protocolo Ebro/Chery — subpaquete de `custom_components.ebro`.

Contiene la lógica verificada sobre el terreno: autenticación BFF, firma de las peticiones,
catálogo y envío de comandos, sonda realtime, despertar. Las entidades de Home Assistant no
hablan nunca directamente con la nube: pasan todas por aquí.

Los módulos de esta carpeta se importan entre sí con imports relativos de paquete
(`from .core import …` / `from . import …`), así es Python quien garantiza qué módulo se
carga y los loggers quedan atribuidos a la integración.
"""
