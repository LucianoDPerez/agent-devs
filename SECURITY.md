# Política de Seguridad

## Versiones con soporte

| Versión | Soportada |
|---------|-----------|
| 0.1.x   | ✅        |

## Cómo reportar una vulnerabilidad

**No abras un issue público.**

Usá el reporte privado de GitHub: pestaña **Security → Report a vulnerability**
(Private Vulnerability Reporting). Respondemos y coordinamos el fix sin exponer
el detalle hasta que haya parche.

## Alcance — qué nos interesa saber

AgentDevs es un agente que, dirigido por un LLM local, **ejecuta herramientas
reales sobre tu máquina**: lee/escribe archivos del repo objetivo, corre
comandos de verificación (lint/tests/build), opera git y se conecta a un MCP
local (codebase-memory-mcp).

Es especialmente relevante todo lo que implique:

- Que contenido del repo analizado (o respuesta del modelo) dispare acciones
  **fuera del repositorio objetivo** o destructivas no solicitadas
  *(prompt injection / escape de sandbox)*.
- Escalada de privilegios o ejecución de comandos arbitrarios más allá de las
  tools declaradas.
- Fugas de información local hacia servicios remotos (el proyecto promete
  funcionar 100% offline salvo la conexión al LLM local).
- Bypasses de los guards de seguridad interna: límites de tool calls,
  protecciones anti-sobrescritura, compuerta de verificación, rutas
  protegidas (`PROTECTED_TASK_DIRS`, etc.).

## Buenas prácticas para usuarios

- Corré AgentDevs sobre repos donde un write inesperado no sea catastrófico y
  con git limpio (el propio harness te lo avisa al arrancar).
- Revisá siempre el diff antes de commitear; el agente nunca pushea sin que
  se lo pidas explícitamente.
