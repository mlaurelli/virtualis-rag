import type { AgentStepName } from '../api/types';

export interface StepInfo {
  step: AgentStepName;
  detail: string;
  /** timestamp client (ms) di arrivo dell'evento: permette la durata per passo */
  at?: number;
}

const STEP_LABELS: Record<AgentStepName, string> = {
  classify: 'Classificazione',
  plan: 'Pianificazione',
  kg_lookup: 'Knowledge graph',
  research: 'Ricerca guidata',
  research_tool: 'Strumento di ricerca',
  retrieve: 'Recupero',
  evaluate: 'Valutazione',
  synthesize: 'Sintesi',
  verify: 'Verifica',
  refine: 'Revisione',
};

/** Icone sobrie per passo (unicode, nessuna libreria). Fallback per passi futuri. */
const STEP_ICONS: Record<string, string> = {
  classify: '🏷️',
  plan: '🧭',
  kg_lookup: '🕸️',
  research: '🔬',
  research_tool: '🛠️',
  retrieve: '🔍',
  evaluate: '⚖️',
  synthesize: '✍️',
  verify: '✅',
  refine: '♻️',
};

/** Durata leggibile: "0,4 s" / "1,2 s". */
function formatDuration(ms: number): string {
  return `${(ms / 1000).toFixed(1).replace('.', ',')} s`;
}

interface StepTimelineProps {
  steps: StepInfo[];
  /** true mentre lo stream è in corso (ultimo passo "attivo") */
  active?: boolean;
}

/** Timeline collassabile dei passi dell'agente (eventi `step` dello stream). */
export default function StepTimeline({ steps, active = false }: StepTimelineProps) {
  if (steps.length === 0) return null;

  return (
    <details className="step-timeline" open={active}>
      <summary>
        <span className="chevron" aria-hidden="true">
          ▶
        </span>
        {active ? (
          <>
            <span className="spinner" style={{ width: 12, height: 12 }} aria-hidden="true" />
            <span>Elaborazione in corso · {steps.length} passi</span>
          </>
        ) : (
          <span>{steps.length} passi dell'agente</span>
        )}
      </summary>
      <ol className="step-list">
        {steps.map((s, i) => {
          const next = steps[i + 1];
          const duration =
            s.at !== undefined && next?.at !== undefined ? next.at - s.at : null;
          return (
            <li
              key={`${s.step}-${i}`}
              className={active && i === steps.length - 1 ? 'step-active' : undefined}
              style={{ animationDelay: `${Math.min(i * 60, 360)}ms` }}
            >
              <span className="step-icon" aria-hidden="true">
                {STEP_ICONS[s.step] ?? '⚙️'}
              </span>
              <span className="step-name">{STEP_LABELS[s.step] ?? s.step}</span>
              <span className="step-detail">{s.detail}</span>
              {duration !== null && duration >= 0 && (
                <span className="step-duration">{formatDuration(duration)}</span>
              )}
            </li>
          );
        })}
      </ol>
    </details>
  );
}
