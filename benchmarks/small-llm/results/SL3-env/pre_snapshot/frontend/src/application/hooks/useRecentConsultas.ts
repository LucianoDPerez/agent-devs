import { useState, useEffect, useCallback } from "react";
import { Consulta, CreateConsultaInput } from "../../domain/Consulta";
import { consultasApi } from "../services/api";

interface UseRecentConsultasReturn {
  consultas: Consulta[];
  loading: boolean;
  error: string | null;
  create: (input: CreateConsultaInput) => Promise<void>;
  remove: (id: number) => Promise<void>;
  refetch: () => Promise<void>;
}

export function useRecentConsultas(pacienteId?: number | null, limit = 5): UseRecentConsultasReturn {
  const [consultas, setConsultas] = useState<Consulta[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchRecent = useCallback(async () => {
    if (!pacienteId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await consultasApi.recentByPaciente(pacienteId, limit);
      setConsultas(data);
    } catch {
      setError("Error al cargar consultas");
    } finally {
      setLoading(false);
    }
  }, [pacienteId, limit]);

  useEffect(() => { fetchRecent(); }, [fetchRecent]);

  const create = useCallback(async (input: CreateConsultaInput) => {
    if (!pacienteId) return;
    const consulta = await consultasApi.create(input);
    setConsultas((prev) => [consulta, ...prev].slice(0, limit));
  }, [pacienteId, limit]);

  const remove = useCallback(async (id: number) => {
    await consultasApi.delete(id);
    setConsultas((prev) => prev.filter((c) => c.id !== id));
  }, []);

  return { consultas, loading, error, create, remove, refetch: fetchRecent };
}