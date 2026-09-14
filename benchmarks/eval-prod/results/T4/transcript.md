# T4 — Seguridad auth (sin cambios)

Modelo: agents-a1-4b (llama.cpp local, 4B) | Harness: agent-lucho main (slash+evidence fixes)
Duración turno: 1051s | Rol final: Role.REVIEW
Tools llamadas: ['list_files', 'read_file', 'search_code']

## Prompt

Revisá el flujo de autenticación y autorización de este proyecto buscando vulnerabilidades o comportamientos inseguros. No quiero una lista genérica: inspeccioná la implementación real (middlewares en backend/src/interfaces/http/middlewares, rutas, controladores). Identificá los problemas que consideres relevantes, explicá su impacto y proponé correcciones priorizadas. No modifiques código todavía.

## Respuesta (última)

## Resumen
Revisé el flujo de autenticación/autorización del proyecto. **NO EXISTE middleware de autenticación** en ningún controlador ni ruta. Todas las operaciones (CRUD de pacientes, consultas, dashboard) están expuestas sin protección.

---

## Hallazgos CRITICAL
- **[backend/src/interfaces/http/controllers/PacienteController.ts:13]** El método `create` no verifica autenticación/autorización. Cualquier cliente puede crear pacientes.
- **[backend/src/interfaces/http/controllers/PacienteController.ts:23]** El método `getById` no verifica autenticación/autorización. Cualquier cliente puede leer datos de pacientes.
- **[backend/src/interfaces/http/controllers/PacienteController.ts:30]** El método `list` no verifica autenticación/autorización. Cualquier cliente puede listar todos los pacientes.
- **[backend/src/interfaces/http/controllers/PacienteController.ts:38]** El método `update` no verifica autenticación/autorización. Cualquier cliente puede modificar datos de pacientes.
- **[backend/src/interfaces/http/controllers/PacienteController.ts:45]** El método `delete` no verifica autenticación/autorización. Cualquier cliente puede eliminar pacientes.
- **[backend/src/interfaces/http/controllers/PacienteController.ts:52]** El método `deactivate` no verifica autenticación/autorización. Cualquier cliente puede desactivar pacientes.
- **[backend/src/interfaces/http/middlewares/validate.ts:1]** Solo existe middleware de validación de DTOs, NO hay middleware de autenticación/autorización definido.
- **[backend/src/interfaces/http/routes/pacientesRoutes.ts]** Las rutas de pacientes están expuestas sin protección (no se aplican middlewares de auth).
- **[backend/src/interfaces/http/routes/dashboardRoutes.ts]** Las rutas del dashboard están expuestas sin protección.
- **[backend/src/interfaces/http/routes/consultasRoutes.ts]** Las rutas de consultas están expuestas sin protección.
- **[backend/src/interfaces/http/routes/index.ts]** El punto de entrada no aplica middleware global de autenticación.

---

## Hallazgos WARNING
- **[backend/src/interfaces/http/controllers/PacienteController.ts:14]** No hay logging de intentos de acceso no autorizado (si se implementara auth, debería registrar fallas).
- **[backend/src/interfaces/http/controllers/PacienteController.ts:24]** No hay logging de acceso a datos sensibles (lectura de pacientes).

---

## Hallazgos SUGGESTION
- Considerar implementar middleware de autenticación JWT o session-based.
- Implementar roles/permisos diferenciados para operaciones críticas (delete, deactivate).
- Agregar logging de auditoría para operaciones sensibles.

---

## Verificados (criterios cumplidos con evidencia)
- **NO verificado**: No existe middleware de autenticación en el proyecto (evidencia: validate.ts solo tiene validación de DTOs, no hay auth middleware).

## git status ANTES

```
 M .gitignore
 M AGENTS.md
 M backend/prisma/schema.prisma
 M backend/src/domain/repositories/IPacienteRepository.ts
 M backend/src/infrastructure/database/repositories/PrismaPacienteRepository.ts
 M backend/src/interfaces/http/controllers/PacienteController.ts
 M backend/src/interfaces/http/middlewares/validate.ts
 M backend/src/interfaces/http/routes/pacientesRoutes.ts
?? backend/src/application/pacientes/DeactivatePacientes.ts
?? backend/src/interfaces/dtos/DeactivatePacientesDTO.ts

```

## git status DESPUÉS

```
 M .gitignore
 M AGENTS.md
 M backend/prisma/schema.prisma
 M backend/src/domain/repositories/IPacienteRepository.ts
 M backend/src/infrastructure/database/repositories/PrismaPacienteRepository.ts
 M backend/src/interfaces/http/controllers/PacienteController.ts
 M backend/src/interfaces/http/middlewares/validate.ts
 M backend/src/interfaces/http/routes/pacientesRoutes.ts
?? backend/src/application/pacientes/DeactivatePacientes.ts
?? backend/src/interfaces/dtos/DeactivatePacientesDTO.ts

```

## git diff --stat

```
 .gitignore                                         |  13 ++
 AGENTS.md                                          | 254 ++++++---------------
 backend/prisma/schema.prisma                       |   5 +-
 .../src/domain/repositories/IPacienteRepository.ts |   1 +
 .../repositories/PrismaPacienteRepository.ts       |   9 +
 .../http/controllers/PacienteController.ts         |  15 +-
 .../src/interfaces/http/middlewares/validate.ts    |  50 +++-
 .../src/interfaces/http/routes/pacientesRoutes.ts  |  13 +-
 8 files changed, 161 insertions(+), 199 deletions(-)

```
