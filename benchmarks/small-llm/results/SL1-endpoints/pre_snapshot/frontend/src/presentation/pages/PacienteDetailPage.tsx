import { useState } from "react";
import { useParams, Link } from "react-router-dom";
import { usePaciente } from "../../application/hooks/usePaciente";
import { useRecentConsultas } from "../../application/hooks/useRecentConsultas";
import { CreateConsultaModal } from "../components/CreateConsultaModal";
import { CreateConsultaInput } from "../../domain/Consulta";
import "../styles/components.css";
import "./pages.css";

export function PacienteDetailPage() {
  const { id } = useParams<{ id: string }>();
  const pacienteId = id ? Number(id) : null;

  const { paciente, loading: loadingPaciente } = usePaciente(pacienteId);
  const { consultas, loading: loadingConsultas, error, create, remove } = useRecentConsultas(Number(id) || null, 5);
  const [modalOpen, setModalOpen] = useState(false);

  const loading = loadingPaciente || loadingConsultas;

  async function handleCreate(input: CreateConsultaInput) {
    await create(input);
  }

  if (loading) {
    return (
      <div className="page-container">
        <Link to="/pacientes" className="back-link">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12" />
            <polyline points="12,19 5,12 12,5" />
          </svg>
          Volver a Pacientes
        </Link>
        <p className="loading-text">Cargando...</p>
      </div>
    );
  }

  if (!paciente) {
    return (
      <div className="page-container">
        <Link to="/pacientes" className="back-link">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12" />
            <polyline points="12,19 5,12 12,5" />
          </svg>
          Volver a Pacientes
        </Link>
        <div className="empty-state">
          <svg className="empty-state-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <p>Paciente no encontrado.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="page-container">
      <CreateConsultaModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSubmit={handleCreate}
        pacienteId={pacienteId!}
      />

      <Link to="/pacientes" className="back-link">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12,19 5,12 12,5" />
        </svg>
        Volver a Pacientes
      </Link>

      <header className="detail-header">
        <div className="detail-header-main">
          <div className="detail-id">Paciente #{id}</div>
          <div className="detail-name">{paciente.nombre}</div>
          <div className="detail-meta">
            {paciente.documento && (
              <div className="detail-meta-item">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
                  <line x1="9" y1="3" x2="9" y2="21" />
                </svg>
                Doc: {paciente.documento}
              </div>
            )}
            {paciente.telefono && (
              <div className="detail-meta-item">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12 19.79 19.79 0 0 1 1.61 3.18 2 2 0 0 1 3.6 1h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.59a16 16 0 0 0 6 6l.95-.95a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 21.5 16a2 2 0 0 1 .42 1.92z" />
                </svg>
                {paciente.telefono}
              </div>
            )}
            {paciente.email && (
              <div className="detail-meta-item">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
                  <polyline points="22,6 12,13 2,6" />
                </svg>
                {paciente.email}
              </div>
            )}
          </div>
        </div>
      </header>

      {/* Resumen Ginecológico */}
      {(paciente.edadMenarquia || paciente.paridad || paciente.ultimaMenstruacion) && (
        <section className="detail-section">
          <h3 className="section-title">Resumen Ginecológico</h3>
          <div className="gyno-grid">
            {paciente.edadMenarquia && (
              <div className="gyno-item">
                <div className="gyno-label">Edad Menarquia</div>
                <div className="gyno-value">{paciente.edadMenarquia} años</div>
              </div>
            )}
            {paciente.paridad && (
              <div className="gyno-item">
                <div className="gyno-label">Paridad</div>
                <div className="gyno-value">{paciente.paridad}</div>
              </div>
            )}
            {paciente.ultimaMenstruacion && (
              <div className="gyno-item">
                <div className="gyno-label">Última Menstruación</div>
                <div className="gyno-value">{new Date(paciente.ultimaMenstruacion).toLocaleDateString()}</div>
              </div>
            )}
            {paciente.fotoTarjetaUrl && (
              <div className="gyno-item">
                <div className="gyno-label">Foto Tarjeta</div>
                <div className="gyno-value">
                  <img src={paciente.fotoTarjetaUrl} alt="Tarjeta" style={{ maxWidth: "100px", maxHeight: "100px", borderRadius: "4px" }} />
                </div>
              </div>
            )}
          </div>
        </section>
      )}

      <div className="list-toolbar" style={{ justifyContent: "space-between" }}>
        <h2 className="section-title" style={{ margin: 0, border: "none", padding: 0 }}>
          Últimas Consultas
        </h2>
        <button className="btn btn-primary" onClick={() => setModalOpen(true)}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          Nueva Consulta
        </button>
      </div>

      {error && <div className="error-text">{error}</div>}
      {loadingConsultas ? (
        <p className="loading-text">Cargando consultas...</p>
      ) : (
        <>
          {consultas.length === 0 ? (
            <div className="empty-state">
              <svg className="empty-state-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                <circle cx="9" cy="7" r="4" />
                <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                <path d="M16 3.13a4 4 0 0 1 0 7.75" />
              </svg>
              <p>No hay consultas registradas.</p>
            </div>
          ) : (
            <div className="table-wrapper">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Fecha</th>
                    <th>Síntomas</th>
                    <th>Diagnóstico</th>
                    <th>Recomendaciones</th>
                    <th>Acciones</th>
                  </tr>
                </thead>
                <tbody>
                  {consultas.map((consulta) => (
                    <tr key={consulta.id}>
                      <td data-label="Fecha">{new Date(consulta.fecha).toLocaleDateString('es-AR')}</td>
                      <td data-label="Síntomas">{consulta.sintomas}</td>
                      <td data-label="Diagnóstico">{consulta.diagnostico || "—"}</td>
                      <td data-label="Recomendaciones">{consulta.recomendaciones || "—"}</td>
                      <td data-label="Acciones">
                        <div className="table-actions">
                          <button
                            className="btn-danger-sm"
                            onClick={() => remove(consulta.id)}
                            disabled={false}
                          >
                            Eliminar
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {pacienteId && (
            <div style={{ textAlign: "center", marginTop: "1rem" }}>
              <Link to={`/pacientes/${pacienteId}/consultas`} className="btn btn-ghost">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="9,18 15,12 9,6" />
                </svg>
                Ver historial completo de consultas
              </Link>
            </div>
          )}
        </>
      )}
    </div>
  );
}
