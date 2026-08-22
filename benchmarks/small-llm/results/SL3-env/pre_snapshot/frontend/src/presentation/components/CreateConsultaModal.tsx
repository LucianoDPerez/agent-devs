import { useState, useEffect } from "react";
import { CreateConsultaInput } from "../../domain/Consulta";
import "../styles/components.css";

interface CreateConsultaModalProps {
  open: boolean;
  onClose: () => void;
  onSubmit: (input: CreateConsultaInput) => Promise<void>;
  pacienteId: number;
}

export function CreateConsultaModal({ open, onClose, onSubmit, pacienteId }: CreateConsultaModalProps) {
  const [sintomas, setSintomas] = useState("");
  const [diagnostico, setDiagnostico] = useState("");
  const [recomendaciones, setRecomendaciones] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setSintomas("");
      setDiagnostico("");
      setRecomendaciones("");
    }
  }, [open]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!sintomas.trim() || submitting) return;
    setSubmitting(true);
    try {
      await onSubmit({
        pacienteId,
        sintomas: sintomas.trim(),
        diagnostico: diagnostico.trim() || undefined,
        recomendaciones: recomendaciones.trim() || undefined,
      });
      onClose();
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) return null;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2 className="modal-title">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: "middle", marginRight: "0.5rem" }}>
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14,2 14,8 20,8" />
              <line x1="12" y1="18" x2="12" y2="12" />
              <line x1="9" y1="15" x2="15" y2="15" />
            </svg>
            Nueva Consulta
          </h2>
          <button className="modal-close" onClick={onClose} aria-label="Cerrar">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="modal-body">
          <form onSubmit={handleSubmit}>
            <div className="form-row">
              <label style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--text-secondary)", marginBottom: "0.25rem", display: "block" }}>
                Síntomas *
              </label>
              <textarea
                className="form-textarea"
                placeholder="Describí los síntomas del paciente..."
                value={sintomas}
                onChange={(e) => setSintomas(e.target.value)}
                required
                rows={3}
                autoFocus
                style={{ minHeight: "88px" }}
              />
            </div>
            <div className="form-row" style={{ marginTop: "1rem" }}>
              <label style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--text-secondary)", marginBottom: "0.25rem", display: "block" }}>
                Diagnóstico
              </label>
              <input
                type="text"
                className="form-input"
                placeholder="Diagnóstico preliminar"
                value={diagnostico}
                onChange={(e) => setDiagnostico(e.target.value)}
              />
            </div>
            <div className="form-row" style={{ marginTop: "1rem" }}>
              <label style={{ fontSize: "0.8125rem", fontWeight: 600, color: "var(--text-secondary)", marginBottom: "0.25rem", display: "block" }}>
                Recomendaciones
              </label>
              <textarea
                className="form-textarea"
                placeholder="Indicaciones o recomendaciones"
                value={recomendaciones}
                onChange={(e) => setRecomendaciones(e.target.value)}
                rows={2}
                style={{ minHeight: "64px" }}
              />
            </div>
            <div className="form-actions">
              <button type="button" className="btn btn-ghost" onClick={onClose}>
                Cancelar
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={submitting || !sintomas.trim()}
              >
                {submitting ? "Guardando..." : "Registrar Consulta"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}