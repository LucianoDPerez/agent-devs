import { PrismaClient } from "@prisma/client";
import { IPacienteRepository, PaginationParams, PaginatedResult } from "../../../domain/repositories/IPacienteRepository";
import { Paciente, CreatePacienteInput, UpdatePacienteInput } from "../../../domain/entities/Paciente";

export class PrismaPacienteRepository implements IPacienteRepository {
  constructor(private readonly prisma: PrismaClient) {}

  async create(data: CreatePacienteInput): Promise<Paciente> {
    const prismaData: any = {
      nombre: data.nombre,
      telefono: data.telefono ?? null,
      email: data.email ?? null,
      documento: data.documento ?? null,
      edadMenarquia: data.edadMenarquia ?? null,
      paridad: data.paridad ?? null,
      ultimaMenstruacion: data.ultimaMenstruacion ? new Date(data.ultimaMenstruacion) : null,
      fotoTarjetaUrl: data.fotoTarjetaUrl ?? null,
    };
    return this.prisma.paciente.create({ data: prismaData });
  }

  async findById(id: number): Promise<Paciente | null> {
    const result = await this.prisma.paciente.findUnique({ where: { id } });
    if (!result) return null;
    return {
      ...result,
      ultimaMenstruacion: result.ultimaMenstruacion ? new Date(result.ultimaMenstruacion) : null,
    };
  }

  async findAll(params: PaginationParams): Promise<PaginatedResult<Paciente>> {
    const { page, limit } = params;
    const skip = (page - 1) * limit;
    const [data, total] = await this.prisma.$transaction([
      this.prisma.paciente.findMany({
        skip,
        take: limit,
        orderBy: { nombre: "asc" },
      }),
      this.prisma.paciente.count(),
    ]);
    const pacientes = data.map(p => ({
      ...p,
      ultimaMenstruacion: p.ultimaMenstruacion ? new Date(p.ultimaMenstruacion) : null,
    }));
    return { data: pacientes, total, page, limit, totalPages: Math.ceil(total / limit) };
  }

  async searchByName(query: string, params: PaginationParams): Promise<PaginatedResult<Paciente>> {
    const { page, limit } = params;
    const skip = (page - 1) * limit;
    const where = { nombre: { contains: query } };
    const [data, total] = await this.prisma.$transaction([
      this.prisma.paciente.findMany({ where, skip, take: limit, orderBy: { nombre: "asc" } }),
      this.prisma.paciente.count({ where }),
    ]);
    const pacientes = data.map(p => ({
      ...p,
      ultimaMenstruacion: p.ultimaMenstruacion ? new Date(p.ultimaMenstruacion) : null,
    }));
    return { data: pacientes, total, page, limit, totalPages: Math.ceil(total / limit) };
  }

  async searchByDocumento(query: string, params: PaginationParams): Promise<PaginatedResult<Paciente>> {
    const { page, limit } = params;
    const skip = (page - 1) * limit;
    const where = { documento: { contains: query } };
    const [data, total] = await this.prisma.$transaction([
      this.prisma.paciente.findMany({ where, skip, take: limit, orderBy: { nombre: "asc" } }),
      this.prisma.paciente.count({ where }),
    ]);
    const pacientes = data.map(p => ({
      ...p,
      ultimaMenstruacion: p.ultimaMenstruacion ? new Date(p.ultimaMenstruacion) : null,
    }));
    return { data: pacientes, total, page, limit, totalPages: Math.ceil(total / limit) };
  }

  async searchByNameOrDocumento(query: string, params: PaginationParams): Promise<PaginatedResult<Paciente>> {
    console.log("PrismaPacienteRepository.searchByNameOrDocumento called with:", query, params);
    const { page, limit } = params;
    const skip = (page - 1) * limit;
    const where = {
      OR: [
        { nombre: { contains: query } },
        { documento: { contains: query } },
      ],
    };
    const [data, total] = await this.prisma.$transaction([
      this.prisma.paciente.findMany({ where, skip, take: limit, orderBy: { nombre: "asc" } }),
      this.prisma.paciente.count({ where }),
    ]);
    const pacientes = data.map(p => ({
      ...p,
      ultimaMenstruacion: p.ultimaMenstruacion ? new Date(p.ultimaMenstruacion) : null,
    }));
    console.log("Search result:", { data: data.length, total });
    return { data: pacientes, total, page, limit, totalPages: Math.ceil(total / limit) };
  }

  async update(id: number, data: UpdatePacienteInput): Promise<Paciente> {
    const updateData: any = {};
    if (data.nombre !== undefined) updateData.nombre = data.nombre;
    if (data.telefono !== undefined) updateData.telefono = data.telefono ?? null;
    if (data.email !== undefined) updateData.email = data.email ?? null;
    if (data.documento !== undefined) updateData.documento = data.documento ?? null;
    if (data.edadMenarquia !== undefined) updateData.edadMenarquia = data.edadMenarquia ?? null;
    if (data.paridad !== undefined) updateData.paridad = data.paridad ?? null;
    if (data.ultimaMenstruacion !== undefined) {
      updateData.ultimaMenstruacion = data.ultimaMenstruacion ? new Date(data.ultimaMenstruacion) : null;
    }
    if (data.fotoTarjetaUrl !== undefined) updateData.fotoTarjetaUrl = data.fotoTarjetaUrl ?? null;

    const result = await this.prisma.paciente.update({ where: { id }, data: updateData });
    return {
      ...result,
      ultimaMenstruacion: result.ultimaMenstruacion ? new Date(result.ultimaMenstruacion) : null,
    };
  }

  async delete(id: number): Promise<void> {
    await this.prisma.paciente.delete({ where: { id } });
  }
}