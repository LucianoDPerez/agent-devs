import { Paciente, CreatePacienteInput, UpdatePacienteInput } from "../../domain/Paciente";
import { Consulta, CreateConsultaInput } from "../../domain/Consulta";
import { PaginatedResult, DashboardData } from "../../domain/Dashboard";
import { apiClient } from "../../infrastructure/http/apiClient";

export const pacientesApi = {
  list: (page = 1, limit = 20) =>
    apiClient.get<PaginatedResult<Paciente>>(`/api/pacientes?page=${page}&limit=${limit}`),
  search: (q: string, page = 1, limit = 20) =>
    apiClient.get<PaginatedResult<Paciente>>(`/api/pacientes?q=${encodeURIComponent(q)}&page=${page}&limit=${limit}`),
  getById: (id: number) => apiClient.get<Paciente>(`/api/pacientes/${id}`),
  create: (data: CreatePacienteInput) => apiClient.post<Paciente>("/api/pacientes", data),
  update: (id: number, data: UpdatePacienteInput) =>
    apiClient.put<Paciente>(`/api/pacientes/${id}`, data),
  delete: (id: number) => apiClient.delete<void>(`/api/pacientes/${id}`),
};

export const consultasApi = {
  listByPaciente: (pacienteId: number, page = 1, limit = 10) =>
    apiClient.get<PaginatedResult<Consulta>>(`/api/pacientes/${pacienteId}/consultas?page=${page}&limit=${limit}`),
  recentByPaciente: (pacienteId: number, limit = 5) =>
    apiClient.get<Consulta[]>(`/api/pacientes/${pacienteId}/consultas/recent?limit=${limit}`),
  create: (data: CreateConsultaInput) =>
    apiClient.post<Consulta>(`/api/consultas`, data),
  delete: (id: number) => apiClient.delete<void>(`/api/consultas/${id}`),
};

export const dashboardApi = {
  get: () => apiClient.get<DashboardData>("/api/dashboard"),
};

export const migracionApi = {
  extract: (fotoBase64: string) =>
    apiClient.post<{ data: any }>("/api/migracion/extract", { fotoBase64 }),
  create: (data: any) => apiClient.post<MigracionResponse>("/api/migracion", data),
  list: (page = 1, limit = 10) =>
    apiClient.get<{ data: MigracionResponse[]; total: number; page: number; limit: number }>(`/api/migracion/list?page=${page}&limit=${limit}`),
  confirm: (migracionId: number, pacienteId?: number) =>
    apiClient.post<{ data: { pacienteId: number; migracionId: string } }>(`/api/migracion/${migracionId}/confirm`, { pacienteId }),
};

export interface MigracionResponse {
  id: string;
  nombre: string;
  documento: string;
  telefono: string | null;
  email: string | null;
  edadMenarquia: number | null;
  paridad: string | null;
  ultimaMenstruacion: string | null;
  fotoUrl: string;
  status: "pending" | "reviewing" | "created";
  createdAt: string;
}