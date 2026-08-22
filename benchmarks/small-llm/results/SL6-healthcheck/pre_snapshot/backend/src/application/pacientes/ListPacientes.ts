import { IPacienteRepository, PaginatedResult } from "../../domain/repositories/IPacienteRepository";
import { Paciente } from "../../domain/entities/Paciente";

export interface ListPacientesInput {
  query?: string;
  page: number;
  limit: number;
}

export class ListPacientes {
  constructor(private readonly pacienteRepository: IPacienteRepository) {}

  async execute(input: ListPacientesInput): Promise<PaginatedResult<Paciente>> {
    if (input.query && input.query.trim().length > 0) {
      // Busca en nombre Y documento
      return this.pacienteRepository.searchByNameOrDocumento(input.query, {
        page: input.page,
        limit: input.limit,
      });
    }
    return this.pacienteRepository.findAll({ page: input.page, limit: input.limit });
  }
}
