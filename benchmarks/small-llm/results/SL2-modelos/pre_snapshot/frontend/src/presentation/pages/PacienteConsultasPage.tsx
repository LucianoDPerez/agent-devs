import { useState } from "react";
import { useParams, Link } from "react-router-dom";
import { usePacientes } from "../../application/hooks/usePacientes";
import { useConsultasList } from "../../application/hooks/useConsultasList";
import { ConsultaList } from "../components/ConsultaList";
import { CreateConsultaModal } from "../components/CreateConsultaModal";
import { Pagination } from "../components/Pagination";
import { consultasApi } from "../../application/services/api";
import { CreateConsultaInput } from "../../domain/Consulta";
import "../styles/components.css";
import "./pages.css";

export function PacienteConsultasPage() {
  const { id } = useParams<{ id: string }>();
  const pacienteId = id ? Number(id) : null;

  const { pacientes } = usePacientes();
  const { consultas, total, page, totalPages, loading, error, setPage, refetch } = useConsultasList(pacienteId, 1, 10);
  const [modalOpen, setModalOpen] = useState(false);

  const paciente = pacientes.find((p) => p.id === pacienteId);

  async function handleCreate(input: Omit<CreateConsultaInput, "pacienteId">) {
    if (!pacienteId) return;
    await consultasApi.create({ ...input, pacienteId });
    await refetch();
  }

  async function handleDelete(id: number) {
    await consultasApi.delete(id);
    await refetch();
  }

  return (
    <div className="page-container">
      <CreateConsultaModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSubmit={handleCreate}
        pacienteId={pacienteId!}
      />

      <Link to={`/pacientes/${pacienteId}`} className="back-link">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12,19 5,12 12,5" />
        </svg>
        Volver al paciente
      </Link>

      <header className="page-header">
        <h1 className="page-title">
          Historial de Consultas
        </h1>
        <p className="page-subtitle">
          {paciente?.nombre ?? `Paciente #${id}`} &mdash; {total} consulta{total !== 1 ? "s" : ""}
        </p>
      </header>

      <div className="list-toolbar" style={{ justifyContent: "flex-end" }}>
        <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          Nueva Consulta
        </button>
      </div>

      {error && <div className="error-text">{error}</div>}
      {loading ? (
        <p className="loading-text">Cargando consultas...</p>
      ) : (
        <>
          <ConsultaList consultas={consultas} onDelete={handleDelete} />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      )}
    </div>
  );
}