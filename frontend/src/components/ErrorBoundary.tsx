import { Component, type ErrorInfo, type ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Error boundary globale: un errore di rendering non gestito (es. dati
 * inattesi dal backend) non deve lasciare l'utente su una pagina bianca.
 * Deve essere un class component: React non espone componentDidCatch /
 * getDerivedStateFromError come hook.
 */
export default class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // traccia in console per la diagnosi (stack del componente incluso)
    console.error('Errore non gestito nel rendering:', error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="empty-state card" role="alert" style={{ margin: 'var(--space-6) auto', maxWidth: 560 }}>
          <p>
            <strong>Si è verificato un errore imprevisto.</strong>
          </p>
          <p className="small">
            L'interfaccia ha smesso di rispondere correttamente. Ricarica la pagina per
            riprendere il lavoro; se il problema persiste contatta un amministratore.
          </p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => window.location.reload()}
          >
            Ricarica
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
