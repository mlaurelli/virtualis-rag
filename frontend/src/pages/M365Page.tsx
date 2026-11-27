import { Fragment, useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import type {
  M365AuthMode,
  M365AuthRequest,
  M365DeviceFlow,
  M365JobPhase,
  M365JobStatus,
  M365StartRequest,
  M365StartResponse,
  M365TestResponse,
} from '../api/types';
import { useAuth } from '../auth/context';
import ConfirmModal from '../components/ConfirmModal';

const POLL_INTERVAL_MS = 2000;
const DEVICE_POLL_INTERVAL_MS = 5000;
const MAX_EVENTS = 12;

/** Nessun tenant precompilato nella versione pubblica. */
const DEFAULT_DEVICE_TENANT = '';

const PHASES: { key: M365JobPhase; label: string }[] = [
  { key: 'auth', label: 'Autenticazione' },
  { key: 'scan', label: 'Scansione' },
  { key: 'download', label: 'Download' },
  { key: 'ingest', label: 'Indicizzazione' },
];

// ---------- Formattazione ----------

const numberFmt = new Intl.NumberFormat('it-IT');
const sizeFmt = new Intl.NumberFormat('it-IT', { maximumFractionDigits: 1 });

/** Byte → stringa leggibile (KB / MB / GB). */
function formatBytes(bytes: number): string {
  if (bytes >= 1_073_741_824) return `${sizeFmt.format(bytes / 1_073_741_824)} GB`;
  if (bytes >= 1_048_576) return `${sizeFmt.format(bytes / 1_048_576)} MB`;
  if (bytes >= 1024) return `${sizeFmt.format(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/** Byte al secondo → velocità leggibile. */
function formatSpeed(bps: number): string {
  if (bps <= 0) return '—';
  return `${formatBytes(bps)}/s`;
}

/** Secondi → "3 min 20 s" / "45 s" / "1 h 5 min". */
function formatEta(seconds: number | null): string {
  if (seconds === null || seconds < 0) return '—';
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) {
    const rest = s % 60;
    return rest > 0 ? `${m} min ${rest} s` : `${m} min`;
  }
  const h = Math.floor(m / 60);
  const restMin = m % 60;
  return restMin > 0 ? `${h} h ${restMin} min` : `${h} h`;
}

/** Timestamp ISO → ora locale (per il feed eventi). */
function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

interface TestResult {
  ok: boolean;
  message: string;
}

/**
 * Pagina di import da Microsoft 365 (solo admin): configurazione delle
 * credenziali, verifica connessione, avvio anteprima (dry-run) o import
 * completo, e vista di progresso live con polling dello stato del job.
 */
export default function M365Page() {
  const { user } = useAuth();

  // ---- Configurazione ----
  const [mode, setMode] = useState<M365AuthMode>('device_code');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [tenant, setTenant] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [userUpn, setUserUpn] = useState('');
  const [includeOnedrive, setIncludeOnedrive] = useState(true);
  const [includeSharepoint, setIncludeSharepoint] = useState(true);
  const [includeMail, setIncludeMail] = useState(true);
  // opzioni avanzate: scoping delle cartelle OneDrive e tetto dimensione file
  const [foldersText, setFoldersText] = useState('');
  const [maxFileMb, setMaxFileMb] = useState(50);

  // ---- Flusso device code ----
  const [deviceTenant, setDeviceTenant] = useState(DEFAULT_DEVICE_TENANT);
  const [device, setDevice] = useState<M365DeviceFlow | null>(null);
  const [deviceExpiresAt, setDeviceExpiresAt] = useState<number | null>(null);
  const [deviceSecondsLeft, setDeviceSecondsLeft] = useState(0);
  const [beginning, setBeginning] = useState(false);

  // ---- Stato operativo ----
  const [status, setStatus] = useState<M365JobStatus | null>(null);
  const [testing, setTesting] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmImport, setConfirmImport] = useState(false);

  const running = status?.state === 'running';
  const controlsDisabled = running || testing || starting || beginning;

  const fetchStatus = useCallback(async () => {
    try {
      setStatus(await api.get<M365JobStatus>('/m365/status'));
    } catch {
      // il polling è accessorio: un errore transitorio non deve sporcare la UI
    }
  }, []);

  // primo fetch al mount (può esserci un job già attivo da un'altra sessione)
  useEffect(() => {
    void fetchStatus();
  }, [fetchStatus]);

  // polling ogni 2s solo mentre il job è in corso; si ferma sullo stato terminale
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      void fetchStatus();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [running, fetchStatus]);

  // countdown di scadenza del codice dispositivo (tick ogni secondo)
  useEffect(() => {
    if (!device || device.authorized || deviceExpiresAt === null) return;
    const tick = () =>
      setDeviceSecondsLeft(Math.max(0, Math.round((deviceExpiresAt - Date.now()) / 1000)));
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [device, deviceExpiresAt]);

  // polling del flusso device code ogni 5s finché attivo e non ancora autorizzato
  useEffect(() => {
    if (!device?.active || device.authorized || deviceSecondsLeft <= 0) return;
    const timer = setInterval(() => {
      api
        .get<M365DeviceFlow>('/m365/device/status')
        .then((flow) => {
          if (!flow.active) {
            // flusso scaduto o rimosso lato server
            setDeviceSecondsLeft(0);
          }
          setDevice(flow);
        })
        .catch(() => {
          // errore transitorio di polling: si riprova al prossimo tick
        });
    }, DEVICE_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [device, deviceSecondsLeft]);

  const buildAuth = (): M365AuthRequest => {
    if (mode === 'password') {
      return {
        mode,
        username: username.trim(),
        password,
        ...(tenant.trim() ? { tenant: tenant.trim() } : {}),
      };
    }
    if (mode === 'client_credentials') {
      return {
        mode,
        tenant_id: tenantId.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret,
      };
    }
    // device_code: nessuna credenziale, tenant opzionale
    return { mode, ...(deviceTenant.trim() ? { tenant: deviceTenant.trim() } : {}) };
  };

  const configValid =
    mode === 'device_code'
      ? true
      : mode === 'password'
        ? username.trim() !== '' && password !== ''
        : tenantId.trim() !== '' && clientId.trim() !== '' && clientSecret !== '';

  const sourcesValid = includeOnedrive || includeSharepoint || includeMail;

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    setActionError(null);
    try {
      await api.post<M365TestResponse>('/m365/test', buildAuth());
      setTestResult({ ok: true, message: 'Connessione riuscita: credenziali valide.' });
    } catch (err) {
      setTestResult({ ok: false, message: errorMessage(err, 'Errore di connessione al server.') });
    } finally {
      setTesting(false);
    }
  };

  const handleBeginDevice = async () => {
    setBeginning(true);
    setActionError(null);
    setTestResult(null);
    try {
      const flow = await api.post<M365DeviceFlow>('/m365/device/begin', {
        ...(deviceTenant.trim() ? { tenant: deviceTenant.trim() } : {}),
      });
      setDevice(flow);
      setDeviceExpiresAt(Date.now() + flow.expires_in_seconds * 1000);
      setDeviceSecondsLeft(flow.expires_in_seconds);
    } catch (err) {
      setActionError(errorMessage(err, 'Impossibile generare il codice di accesso.'));
    } finally {
      setBeginning(false);
    }
  };

  const startJob = async (dryRun: boolean) => {
    setStarting(true);
    setActionError(null);
    setTestResult(null);
    try {
      // righe non vuote → lista cartelle; vuota = nessun filtro (null)
      const folders = foldersText
        .split('\n')
        .map((line) => line.trim())
        .filter((line) => line !== '');
      const body: M365StartRequest = {
        auth: buildAuth(),
        include_onedrive: includeOnedrive,
        include_sharepoint: includeSharepoint,
        include_mail: includeMail,
        user: userUpn.trim() || null,
        folders: folders.length > 0 ? folders : null,
        max_file_mb: maxFileMb,
        dry_run: dryRun,
      };
      await api.post<M365StartResponse>('/m365/start', body);
      await fetchStatus();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setActionError('Un import è già in corso: attendi il termine o annullalo.');
        await fetchStatus();
      } else {
        setActionError(errorMessage(err, "Impossibile avviare l'import."));
      }
    } finally {
      setStarting(false);
      setConfirmImport(false);
    }
  };

  const handleCancel = async () => {
    setCancelling(true);
    try {
      await api.post('/m365/cancel');
      await fetchStatus();
    } catch (err) {
      setActionError(errorMessage(err, "Impossibile annullare l'import."));
    } finally {
      setCancelling(false);
    }
  };

  if (user?.role !== 'admin') {
    return (
      <div className="page page-narrow">
        <div className="page-header">
          <div>
            <h1>Microsoft 365</h1>
            <p className="subtitle">Import di contenuti da un tenant Microsoft 365.</p>
          </div>
        </div>
        <div className="alert alert-warning" role="alert">
          Questa pagina è riservata agli amministratori.
        </div>
      </div>
    );
  }

  // ---- Derivati per la vista di progresso ----
  const phaseIdx = status ? Math.max(0, PHASES.findIndex((p) => p.key === status.phase)) : 0;
  const allDone = status?.state === 'done';
  const percent = Math.min(100, Math.max(0, Math.round(status?.download.percent ?? 0)));
  // prima del download la percentuale è 0: barra indeterminata mentre il job lavora
  const indeterminate = running && status?.phase !== 'download' && percent === 0;
  const ingestActive =
    status !== null && (status.phase === 'ingest' || status.ingest.queued > 0 || status.ingest.indexed > 0);

  // ---- Derivati per il flusso device code ----
  const deviceExpired = device !== null && !device.authorized && deviceSecondsLeft <= 0;
  const deviceCountdown = `${Math.floor(deviceSecondsLeft / 60)}:${String(
    deviceSecondsLeft % 60,
  ).padStart(2, '0')}`;

  const stepClass = (i: number): string => {
    if (allDone || i < phaseIdx) return 'done';
    if (i === phaseIdx) {
      if (running) return 'active';
      if (status?.state === 'error') return 'failed';
      if (status?.state === 'cancelled') return 'cancelled';
    }
    return '';
  };

  return (
    <div className="page page-narrow">
      <div className="page-header">
        <div>
          <h1>Microsoft 365</h1>
          <p className="subtitle">
            Import di file e allegati da OneDrive, SharePoint e Posta del tenant.
          </p>
        </div>
      </div>

      {/* ---------- Card configurazione ---------- */}
      <div className="card m365-config">
        <h2>Connessione al tenant</h2>

        <div className="m365-modes" role="radiogroup" aria-label="Modalità di autenticazione">
          <label className="m365-check">
            <input
              type="radio"
              name="m365-mode"
              checked={mode === 'device_code'}
              onChange={() => setMode('device_code')}
              disabled={controlsDisabled}
            />
            Codice dispositivo <span className="m365-recommended">(consigliato, compatibile MFA)</span>
          </label>
          <label className="m365-check">
            <input
              type="radio"
              name="m365-mode"
              checked={mode === 'password'}
              onChange={() => setMode('password')}
              disabled={controlsDisabled}
            />
            Utenza (username e password)
          </label>
          <label className="m365-check">
            <input
              type="radio"
              name="m365-mode"
              checked={mode === 'client_credentials'}
              onChange={() => setMode('client_credentials')}
              disabled={controlsDisabled}
            />
            App registration (client credentials)
          </label>
        </div>

        {mode === 'password' && (
          <div className="m365-fields">
            <div className="field">
              <label htmlFor="m365-username">Utente (UPN)</label>
              <input
                id="m365-username"
                className="input"
                type="text"
                autoComplete="username"
                placeholder="nome@azienda.it"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                disabled={controlsDisabled}
              />
            </div>
            <div className="field">
              <label htmlFor="m365-password">Password</label>
              <input
                id="m365-password"
                className="input"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={controlsDisabled}
              />
            </div>
            <div className="field">
              <label htmlFor="m365-tenant">
                Tenant <span className="faint">(opzionale)</span>
              </label>
              <input
                id="m365-tenant"
                className="input"
                type="text"
                placeholder="azienda.onmicrosoft.com"
                value={tenant}
                onChange={(e) => setTenant(e.target.value)}
                disabled={controlsDisabled}
              />
            </div>
          </div>
        )}
        {mode === 'client_credentials' && (
          <div className="m365-fields">
            <div className="field">
              <label htmlFor="m365-tenant-id">Tenant ID</label>
              <input
                id="m365-tenant-id"
                className="input"
                type="text"
                placeholder="00000000-0000-0000-0000-000000000000"
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                disabled={controlsDisabled}
              />
            </div>
            <div className="field">
              <label htmlFor="m365-client-id">Client ID</label>
              <input
                id="m365-client-id"
                className="input"
                type="text"
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                disabled={controlsDisabled}
              />
            </div>
            <div className="field">
              <label htmlFor="m365-client-secret">Client secret</label>
              <input
                id="m365-client-secret"
                className="input"
                type="password"
                autoComplete="off"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                disabled={controlsDisabled}
              />
            </div>
          </div>
        )}

        {mode === 'device_code' && (
          <div className="m365-device">
            <div className="field" style={{ maxWidth: 360 }}>
              <label htmlFor="m365-device-tenant">
                Tenant (dominio) <span className="faint">(opzionale)</span>
              </label>
              <input
                id="m365-device-tenant"
                className="input"
                type="text"
                placeholder="organizations"
                value={deviceTenant}
                onChange={(e) => setDeviceTenant(e.target.value)}
                disabled={controlsDisabled}
              />
            </div>

            <div className="m365-actions" style={{ marginBottom: 'var(--space-4)' }}>
              <button
                type="button"
                className="btn"
                onClick={() => void handleBeginDevice()}
                disabled={controlsDisabled}
              >
                {beginning && <span className="spinner" aria-hidden="true" />}
                {beginning ? 'Generazione in corso…' : 'Genera codice di accesso'}
              </button>
            </div>

            {device && device.authorized && (
              <div className="m365-device-approved" role="status">
                <span className="m365-device-badge" aria-hidden="true" />
                Approvato — pronto per l'import.
              </div>
            )}

            {device && deviceExpired && (
              <div className="alert alert-warning" role="alert">
                Il codice è scaduto: generane uno nuovo.
              </div>
            )}

            {device && !device.authorized && !deviceExpired && (
              <div className="m365-device-card">
                <div className="m365-device-code" aria-label={`Codice di accesso ${device.user_code}`}>
                  {device.user_code}
                </div>
                <ol className="m365-device-steps">
                  <li>
                    Apri la pagina di accesso Microsoft:{' '}
                    <a href={device.verification_uri} target="_blank" rel="noreferrer">
                      {device.verification_uri}
                    </a>
                  </li>
                  <li>Accedi con l'utenza del tenant.</li>
                  <li>Inserisci il codice e approva con l'MFA sul telefono.</li>
                </ol>
                <div className="m365-device-footer">
                  <a
                    className="btn"
                    href={device.verification_uri}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Apri pagina di accesso
                  </a>
                  <span className="m365-device-countdown">
                    Scade tra <strong>{deviceCountdown}</strong>
                  </span>
                </div>
              </div>
            )}
          </div>
        )}

        <div className="field">
          <label htmlFor="m365-user">
            Limita a un utente <span className="faint">(UPN, opzionale)</span>
          </label>
          <input
            id="m365-user"
            className="input"
            type="text"
            placeholder="Tutti gli utenti accessibili"
            value={userUpn}
            onChange={(e) => setUserUpn(e.target.value)}
            disabled={controlsDisabled}
          />
        </div>

        <div className="m365-checks">
          <label className="m365-check">
            <input
              type="checkbox"
              checked={includeOnedrive}
              onChange={(e) => setIncludeOnedrive(e.target.checked)}
              disabled={controlsDisabled}
            />
            OneDrive
          </label>
          <label className="m365-check">
            <input
              type="checkbox"
              checked={includeSharepoint}
              onChange={(e) => setIncludeSharepoint(e.target.checked)}
              disabled={controlsDisabled}
            />
            SharePoint
          </label>
          <label className="m365-check">
            <input
              type="checkbox"
              checked={includeMail}
              onChange={(e) => setIncludeMail(e.target.checked)}
              disabled={controlsDisabled}
            />
            Posta
          </label>
        </div>

        {/* Opzioni di scoping supportate dal backend (cartelle OneDrive, tetto MB) */}
        <details className="m365-advanced">
          <summary>Opzioni avanzate</summary>
          <div className="field">
            <label htmlFor="m365-folders">
              Cartelle da importare{' '}
              <span className="faint">(una per riga, formato utente:/percorso — vuoto = tutto)</span>
            </label>
            <textarea
              id="m365-folders"
              className="textarea"
              rows={3}
              placeholder="user@your-tenant.onmicrosoft.com:/Documents"
              value={foldersText}
              onChange={(e) => setFoldersText(e.target.value)}
              disabled={controlsDisabled}
            />
          </div>
          <div className="field" style={{ maxWidth: 240 }}>
            <label htmlFor="m365-max-file-mb">Dimensione massima file (MB)</label>
            <input
              id="m365-max-file-mb"
              className="input"
              type="number"
              min={1}
              max={500}
              value={maxFileMb}
              onChange={(e) => {
                const v = Number(e.target.value);
                // clamp al range accettato dal backend (1..500)
                setMaxFileMb(Number.isFinite(v) ? Math.min(500, Math.max(1, Math.round(v))) : 50);
              }}
              disabled={controlsDisabled}
            />
          </div>
        </details>

        {testing && mode === 'device_code' && (
          <div className="alert alert-info" role="status" style={{ marginBottom: 'var(--space-4)' }}>
            <span className="spinner" aria-hidden="true" /> In attesa di approvazione: completa
            l'accesso con il codice dispositivo. La verifica può richiedere qualche minuto.
          </div>
        )}
        {testResult && (
          <div
            className={testResult.ok ? 'alert alert-success' : 'alert'}
            role={testResult.ok ? 'status' : 'alert'}
            style={{ marginBottom: 'var(--space-4)' }}
          >
            {testResult.message}
          </div>
        )}
        {actionError && (
          <div className="alert" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
            {actionError}
          </div>
        )}

        <div className="m365-actions">
          <button
            type="button"
            className="btn"
            onClick={() => void handleTest()}
            disabled={controlsDisabled || !configValid}
          >
            {testing && <span className="spinner" aria-hidden="true" />}
            {testing
              ? mode === 'device_code'
                ? 'In attesa di approvazione…'
                : 'Verifica in corso…'
              : 'Verifica connessione'}
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => void startJob(true)}
            disabled={controlsDisabled || !configValid || !sourcesValid}
          >
            Avvia anteprima (scansione)
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setConfirmImport(true)}
            disabled={controlsDisabled || !configValid || !sourcesValid}
          >
            Avvia import completo
          </button>
          {running && (
            <button
              type="button"
              className="btn btn-danger"
              onClick={() => void handleCancel()}
              disabled={cancelling}
            >
              {cancelling ? 'Annullamento…' : 'Annulla import'}
            </button>
          )}
        </div>
      </div>

      {/* ---------- Vista di progresso ---------- */}
      {status && status.state !== 'idle' && (
        <div className="card m365-progress-card">
          <div className="m365-progress-head">
            <h2>
              {status.dry_run ? 'Anteprima (scansione)' : 'Import'} in corso
              {running && <span className="spinner" aria-hidden="true" />}
            </h2>
            <span className="m365-percent" aria-label={`Avanzamento ${percent}%`}>
              {percent}%
            </span>
          </div>

          {/* Stepper delle 4 fasi */}
          <div className="m365-stepper" aria-label="Fasi del job">
            {PHASES.map((p, i) => (
              <Fragment key={p.key}>
                {i > 0 && (
                  <div className={`m365-step-line${i <= phaseIdx || allDone ? ' filled' : ''}`} />
                )}
                <div className={`m365-step ${stepClass(i)}`}>
                  <span className="m365-step-dot" aria-hidden="true" />
                  <span>{p.label}</span>
                </div>
              </Fragment>
            ))}
          </div>

          {/* Barra di progresso principale */}
          <div
            className="m365-progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
          >
            {indeterminate ? (
              <div className="m365-progress-indeterminate" />
            ) : (
              <div className="m365-progress-fill" style={{ width: `${percent}%` }} />
            )}
          </div>

          {/* File corrente */}
          {running && status.download.current_file && (
            <p className="m365-current-file" title={status.download.current_file}>
              {status.download.current_file}
            </p>
          )}

          {/* Statistiche live */}
          <div className="m365-stats">
            <div className="m365-stat">
              <div className="m365-stat-value">
                {numberFmt.format(status.download.done_files)}
                <span className="m365-stat-sub"> / {numberFmt.format(status.download.total_files)}</span>
              </div>
              <div className="m365-stat-label">File scaricati</div>
            </div>
            <div className="m365-stat">
              <div className="m365-stat-value">
                {formatBytes(status.download.done_bytes)}
                <span className="m365-stat-sub"> / {formatBytes(status.download.total_bytes)}</span>
              </div>
              <div className="m365-stat-label">Dati scaricati</div>
            </div>
            <div className="m365-stat">
              <div className="m365-stat-value">{formatSpeed(status.download.speed_bps)}</div>
              <div className="m365-stat-label">Velocità</div>
            </div>
            <div className="m365-stat">
              <div className="m365-stat-value">{formatEta(status.download.eta_seconds)}</div>
              <div className="m365-stat-label">Tempo rimanente stimato</div>
            </div>
            <div className="m365-stat">
              <div className="m365-stat-value m365-stat-danger">
                {numberFmt.format(status.download.failed_files)}
              </div>
              <div className="m365-stat-label">Falliti</div>
            </div>
            <div className="m365-stat">
              <div className="m365-stat-value">
                {numberFmt.format(status.download.skipped_files)}
              </div>
              <div className="m365-stat-label">Saltati</div>
            </div>
          </div>

          {/* Stato indicizzazione */}
          {ingestActive && (
            <div className="m365-ingest">
              <span className="m365-ingest-label">Indicizzazione:</span>
              <span>
                <strong>{numberFmt.format(status.ingest.indexed)}</strong> indicizzati
              </span>
              <span>
                <strong>{numberFmt.format(status.ingest.failed)}</strong> falliti
              </span>
              <span>
                <strong>{numberFmt.format(status.ingest.pending)}</strong> in coda
              </span>
            </div>
          )}

          {/* Riepilogo scansione */}
          <div className="m365-scan">
            <div className="m365-scan-item">
              <span className="m365-scan-count">
                {numberFmt.format(status.scan.onedrive_files)}
              </span>
              <span className="m365-scan-label">File OneDrive</span>
            </div>
            <div className="m365-scan-item">
              <span className="m365-scan-count">
                {numberFmt.format(status.scan.sharepoint_files)}
              </span>
              <span className="m365-scan-label">
                File SharePoint ({numberFmt.format(status.scan.sharepoint_sites)} siti)
              </span>
            </div>
            <div className="m365-scan-item">
              <span className="m365-scan-count">
                {numberFmt.format(status.scan.mail_messages)}
              </span>
              <span className="m365-scan-label">Messaggi di posta</span>
            </div>
            <div className="m365-scan-item">
              <span className="m365-scan-count">
                {numberFmt.format(status.scan.mail_attachments)}
              </span>
              <span className="m365-scan-label">Allegati</span>
            </div>
          </div>

          {/* Banner di stato terminale */}
          {status.state === 'done' && (
            <div className="alert alert-success" role="status" style={{ marginTop: 'var(--space-4)' }}>
              {status.dry_run
                ? 'Anteprima completata: la scansione è terminata. Nessun file è stato scaricato.'
                : 'Import completato: i contenuti sono stati scaricati e indicizzati.'}
            </div>
          )}
          {status.state === 'error' && (
            <div className="alert" role="alert" style={{ marginTop: 'var(--space-4)' }}>
              Import interrotto: {status.error ?? 'errore sconosciuto'}.
            </div>
          )}
          {status.state === 'cancelled' && (
            <div className="alert alert-warning" role="status" style={{ marginTop: 'var(--space-4)' }}>
              Import annullato dall'utente.
            </div>
          )}
          {status.state === 'done' && !status.dry_run && (
            <div style={{ marginTop: 'var(--space-4)' }}>
              <Link to="/documents" className="btn btn-primary">
                Vai ai documenti
              </Link>
            </div>
          )}

          {/* Errori per file (collassabile) */}
          {status.file_errors.length > 0 && (
            <details className="m365-errors">
              <summary>
                Errori su {numberFmt.format(status.file_errors.length)} file
              </summary>
              <ul>
                {status.file_errors.map((fe, i) => (
                  <li key={`${fe.file}-${i}`}>
                    <span className="mono">{fe.file}</span>
                    <span className="m365-error-msg">{fe.error}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}

          {/* Feed eventi live */}
          {status.events.length > 0 && (
            <div className="m365-events-wrap">
              <h3>Eventi</h3>
              <div className="m365-events" aria-live="polite">
                {status.events.slice(0, MAX_EVENTS).map((ev, i) => (
                  <div key={`${ev.at}-${i}`} className={`m365-event m365-event-${ev.level}`}>
                    <span className="m365-event-level" aria-hidden="true" />
                    <span className="m365-event-time">{formatTime(ev.at)}</span>
                    <span className="m365-event-message">{ev.message}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {confirmImport && (
        <ConfirmModal
          title="Avvia import completo"
          confirmLabel={starting ? 'Avvio in corso…' : 'Avvia import'}
          onConfirm={() => void startJob(false)}
          onCancel={() => setConfirmImport(false)}
        >
          Verranno scaricati e indicizzati tutti i contenuti selezionati
          {userUpn.trim() ? ` dell'utente ${userUpn.trim()}` : ''} dal tenant Microsoft 365.
          L'operazione può richiedere molto tempo. Procedere?
        </ConfirmModal>
      )}
    </div>
  );
}
