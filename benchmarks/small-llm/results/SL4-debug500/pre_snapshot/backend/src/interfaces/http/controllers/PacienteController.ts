import { Request, Response, NextFunction } from "express";
import { CreatePaciente } from "../../../application/pacientes/CreatePaciente";
import { GetPaciente } from "../../../application/pacientes/GetPaciente";
import { ListPacientes } from "../../../application/pacientes/ListPacientes";
import { UpdatePaciente } from "../../../application/pacientes/UpdatePaciente";
import { DeletePaciente } from "../../../application/pacientes/DeletePaciente";
import { CreatePacienteDTO } from "../../dtos/CreatePacienteDTO";

export class PacienteController {
  constructor(
    private readonly createPaciente: CreatePaciente,
    private readonly getPaciente: GetPaciente,
    private readonly listPacientes: ListPacientes,
    private readonly updatePaciente: UpdatePaciente,
    private readonly deletePaciente: DeletePaciente
  ) {}

  async create(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
      const dto = req.body as CreatePacienteDTO;
      const paciente = await this.createPaciente.execute(dto);
      res.status(201).json(paciente);
    } catch (error) {
      next(error);
    }
  }

  async getById(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
      const id = Number(req.params.id);
      const paciente = await this.getPaciente.execute(id);
      res.json(paciente);
    } catch (error) {
      next(error);
    }
  }

  async list(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
      const query = req.query.q as string | undefined;
      const page = Math.max(1, Number(req.query.page) || 1);
      const limit = Math.min(100, Math.max(1, Number(req.query.limit) || 20));
      console.log("PacienteController.list called with:", { query, page, limit });
      const result = await this.listPacientes.execute({ query, page, limit });
      res.json(result);
    } catch (error) {
      console.error("Error in PacienteController.list:", error);
      next(error);
    }
  }

  async update(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
      const id = Number(req.params.id);
      const paciente = await this.updatePaciente.execute(id, req.body);
      res.json(paciente);
    } catch (error) {
      next(error);
    }
  }

  async delete(req: Request, res: Response, next: NextFunction): Promise<void> {
    try {
      const id = Number(req.params.id);
      await this.deletePaciente.execute(id);
      res.status(204).send();
    } catch (error) {
      next(error);
    }
  }
}