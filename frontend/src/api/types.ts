/**
 * Tipi TypeScript derivati dal contratto API (backend/API.md).
 * Base URL: /api — autenticazione Bearer JWT.
 */

// ---------- Comuni ----------

export type Role = 'admin' | 'user';

export type DocStatus = 'pending' | 'processing' | 'indexed' | 'failed';

export type AgentPath = 'fast' | 'agentic';

export type AgentStepName =
  | 'classify'
  | 'plan'
  | 'kg_lookup'
  | 'research'
  | 'research_tool'
  | 'retrieve'
  | 'evaluate'
  | 'synthesize'
  | 'verify'
  | 'refine';

export type SourceType = 'local_fs' | 'upload' | 'gdrive' | 'm365';

/** Formato errori del backend: {"detail": "..."} */
export interface ApiErrorBody {
  detail: string;
}

// ---------- Auth ----------

export interface User {
  id: number;
  username: string;
  role: Role;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

// ---------- Setup (wizard di primo avvio) ----------

export interface SetupStatus {
  needs_setup: boolean;
}

export interface SetupRequest {
  username: string;
  password: string;
}

// ---------- Chat ----------

/**
 * Rettangolo di un passaggio citato nel PDF: punti PDF, origine bottom-left
 * (formato nativo Docling). page_width/page_height presenti quando il backend
 * conosce le dimensioni della pagina. Il viewer converte in coordinate
 * top-left con viewport.convertToViewportRectangle.
 */
export interface CitationBBox {
  page: number;
  l: number;
  t: number;
  r: number;
  b: number;
  page_width?: number;
  page_height?: number;
}

export interface Citation {
  n: number;
  document_id: number;
  title: string;
  page: number | null;
  section_path: string[];
  snippet: string;
  source_name: string;
  /** Rettangoli da evidenziare nel viewer PDF (assente/vuoto: solo pagina). */
  bboxes?: CitationBBox[];
}

export interface ChatRequest {
  message: string;
  session_id?: number;
}

export interface ChatSessionSummary {
  id: number;
  title: string;
  created_at: string;
  message_count: number;
}

export interface ChatSessionsResponse {
  sessions: ChatSessionSummary[];
}

export interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  citations?: Citation[];
  created_at: string;
}

export interface ChatSessionDetail {
  id: number;
  title: string;
  created_at: string;
  messages: ChatMessage[];
}

// Eventi SSE dello stream POST /api/chat
export interface StepEvent {
  type: 'step';
  step: AgentStepName;
  detail: string;
}

export interface TokenEvent {
  type: 'token';
  text: string;
}

/** Azzera il testo streamato: la bozza è sostituita dalla risposta corretta (reflexion) */
export interface ResetEvent {
  type: 'reset';
}

export interface CitationsEvent {
  type: 'citations';
  citations: Citation[];
}

export interface AnswerEvent {
  type: 'answer';
  text: string;
  path: AgentPath;
  warnings: string[];
  /** affidabilità stimata dalla critic avversariale (assente se disabilitata) */
  confidence?: 'alta' | 'media' | 'bassa';
}

export interface DoneEvent {
  type: 'done';
  session_id: number;
  audit_id: number;
}

export interface ErrorEvent {
  type: 'error';
  message: string;
}

export type ChatStreamEvent =
  | StepEvent
  | TokenEvent
  | ResetEvent
  | CitationsEvent
  | AnswerEvent
  | DoneEvent
  | ErrorEvent;

// ---------- Feedback ----------

export type FeedbackRating = -1 | 1;

export interface FeedbackRequest {
  message_id: number;
  rating: FeedbackRating;
  comment?: string;
}

export interface FeedbackEntry {
  id: number;
  message_id: number;
  rating: FeedbackRating;
  comment: string | null;
  question: string;
  answer_excerpt: string;
  created_at: string;
  updated_at: string;
  username?: string;
}

export interface FeedbackListResponse {
  entries: FeedbackEntry[];
}

export interface FeedbackStats {
  total: number;
  positive: number;
  negative: number;
  positive_pct: number | null;
  recent_negative: FeedbackEntry[];
}

// ---------- Ricerca ----------

export interface SearchFilters {
  source_id?: number;
  doc_type?: string;
  date_from?: string;
  date_to?: string;
}

export interface SearchRequest {
  query: string;
  filters?: SearchFilters;
  limit?: number;
}

export interface SearchResult {
  chunk_id: number;
  document_id: number;
  title: string;
  snippet: string;
  score: number;
  page: number | null;
  section_path: string[];
  source_name: string;
}

export interface SearchResponse {
  results: SearchResult[];
}

// ---------- Documenti ----------

export interface DocumentItem {
  id: number;
  source_id: number;
  external_id: string;
  title: string;
  doc_type: string;
  author: string | null;
  language: string | null;
  status: DocStatus;
  error: string | null;
  page_count: number | null;
  size_bytes: number;
  modified_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentsResponse {
  documents: DocumentItem[];
  /** Totale documenti che soddisfano i filtri (per la paginazione). */
  total: number;
}

// ---------- Ingestion ----------

export interface UploadResponse {
  document_id: number;
  status: DocStatus;
}

export interface SyncRequest {
  source_id?: number;
}

export interface SyncResponse {
  started: boolean;
  sources: number[];
}

export interface IngestQueue {
  pending: number;
  processing: number;
}

export interface IngestPerSource {
  source_id: number;
  name: string;
  enabled: boolean;
  last_sync_at: string | null;
  indexed: number;
  failed: number;
}

export interface IngestRecentItem {
  document_id: number;
  title: string;
  status: DocStatus;
  error: string | null;
}

export interface IngestStatus {
  queue: IngestQueue;
  per_source: IngestPerSource[];
  recent: IngestRecentItem[];
}

// ---------- Admin ----------

export interface AdminUser {
  id: number;
  username: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}

export interface AdminUsersResponse {
  users: AdminUser[];
}

export interface CreateUserRequest {
  username: string;
  password: string;
  role: Role;
}

export interface UpdateUserRequest {
  password?: string;
  role?: Role;
  is_active?: boolean;
}

export interface AuditStep {
  step?: string;
  detail?: string;
  [key: string]: unknown;
}

export interface AuditEntry {
  id: number;
  user_id: number;
  session_id: number;
  question: string;
  path: AgentPath;
  steps: AuditStep[];
  retrieved_chunk_ids: number[];
  answer: string;
  warnings: string[];
  latency_ms: number;
  created_at: string;
}

export interface AuditResponse {
  entries: AuditEntry[];
  /** Totale voci nel registro (per la paginazione). */
  total: number;
}

export interface Source {
  id: number;
  name: string;
  type: SourceType;
  config: Record<string, unknown>;
  enabled: boolean;
  last_sync_at: string | null;
}

export interface SourcesResponse {
  sources: Source[];
}

export interface CreateSourceRequest {
  name: string;
  type: SourceType;
  config: Record<string, unknown>;
  enabled: boolean;
}

export interface UpdateSourceRequest {
  name?: string;
  config?: Record<string, unknown>;
  enabled?: boolean;
}

// ---------- Knowledge graph (GraphRAG) ----------
// Endpoint /api/kg/* (backend/app/api/routes_kg.py, backend/app/knowledge/query.py).
// KG disabilitato → 404 con {detail} su tutti gli endpoint.

/** Vocabolario chiuso dei tipi entità (backend/app/knowledge/extractor.py). */
export type KgEntityType =
  | 'persona'
  | 'organizzazione'
  | 'contratto'
  | 'progetto'
  | 'data'
  | 'importo'
  | 'prodotto'
  | 'altro';

/** Campi pubblici di un'entità (knowledge.query._entity_out). */
export interface KgEntity {
  id: number;
  name: string;
  display_name: string;
  type: string;
  description: string;
}

/** Entità nei risultati di ricerca, con conteggio dei documenti che la menzionano. */
export interface KgEntitySummary extends KgEntity {
  document_count: number;
}

/** GET /api/kg/entities?q=&limit= */
export interface KgEntitiesResponse {
  entities: KgEntitySummary[];
}

/** Documento che menziona un'entità (nel contesto entità). */
export interface KgDocumentRef {
  document_id: number;
  title: string;
}

/**
 * Controparte di una relazione. Se l'entità collegata non è più presente, il
 * backend restituisce solo {id, display_name: "", type: ""}: name/description
 * possono quindi mancare.
 */
export interface KgRelationRef {
  id: number;
  display_name: string;
  type: string;
  name?: string;
  description?: string;
}

export interface KgRelation {
  id: number;
  predicate: string;
  subject: KgRelationRef;
  object: KgRelationRef;
  document_id: number;
  document_title: string;
}

/** GET /api/kg/entities/{id} */
export interface KgEntityContext {
  entity: KgEntity;
  documents: KgDocumentRef[];
  relations: KgRelation[];
}

export type KgMatchMode = 'all' | 'any';

export interface KgDocumentMatch {
  document_id: number;
  title: string;
  doc_type: string;
  matched_entities: string[];
  matched_count: number;
}

/** GET /api/kg/documents?entities=a,b,c&mode=all|any */
export interface KgDocumentsResponse {
  documents: KgDocumentMatch[];
  mode: KgMatchMode;
}

/** Arco del grafo completo (vista grafica): estremità per id. */
export interface KgGraphEdge {
  id: number;
  subject_id: number;
  object_id: number;
  predicate: string;
  document_id: number;
  document_title: string;
}

/**
 * GET /api/kg/graph?limit=N — le top-N entità per document_count e i soli
 * archi interni al sottoinsieme. total_nodes/total_edges sono i totali del
 * grafo completo (per indicare il troncamento nella vista).
 */
export interface KgGraphResponse {
  nodes: KgEntitySummary[];
  edges: KgGraphEdge[];
  total_nodes: number;
  total_edges: number;
}

// ---------- Microsoft 365 ----------
// Endpoint /api/m365/* (solo admin): test connessione, avvio job di import,
// stato in polling e annullamento.

export type M365AuthMode = 'password' | 'client_credentials' | 'device_code';

/** Credenziali per /m365/test e /m365/start (campi in base alla modalità).
 *  Con mode 'device_code' non servono credenziali (tenant opzionale). */
export interface M365AuthRequest {
  mode: M365AuthMode;
  username?: string;
  password?: string;
  tenant?: string;
  tenant_id?: string;
  client_id?: string;
  client_secret?: string;
}

/** POST /api/m365/device/begin — avvia il flusso device code. */
export interface M365DeviceBeginRequest {
  tenant?: string;
  client_id?: string;
}

/** Stato del flusso device code (device/begin e device/status). */
export interface M365DeviceFlow {
  active: boolean;
  user_code: string;
  verification_uri: string;
  authorized: boolean;
  expires_in_seconds: number;
}

export interface M365TestResponse {
  ok: boolean;
}

export interface M365StartRequest {
  auth: M365AuthRequest;
  include_onedrive: boolean;
  include_sharepoint: boolean;
  include_mail: boolean;
  user?: string | null;
  /** Cartelle OneDrive ("upn:/percorso"); null/omesso = tutto il drive. */
  folders?: string[] | null;
  /** Tetto di dimensione per singolo file (MB, 1..500). */
  max_file_mb?: number;
  dry_run: boolean;
}

export interface M365StartResponse {
  started: boolean;
  dry_run: boolean;
}

export type M365JobState = 'idle' | 'running' | 'done' | 'error' | 'cancelled';

export type M365JobPhase = 'auth' | 'scan' | 'download' | 'ingest';

export interface M365ScanStats {
  onedrive_files: number;
  sharepoint_sites: number;
  sharepoint_files: number;
  mail_messages: number;
  mail_attachments: number;
}

export interface M365DownloadStats {
  total_files: number;
  done_files: number;
  failed_files: number;
  skipped_files: number;
  total_bytes: number;
  done_bytes: number;
  current_file: string;
  speed_bps: number;
  eta_seconds: number | null;
  percent: number;
}

export interface M365IngestStats {
  queued: number;
  indexed: number;
  failed: number;
  pending: number;
}

export interface M365JobEvent {
  at: string;
  level: 'info' | 'warning' | 'error';
  message: string;
}

export interface M365FileError {
  file: string;
  error: string;
}

/** GET /api/m365/status — snapshot completo del job (polling ogni 2s). */
export interface M365JobStatus {
  state: M365JobState;
  phase: M365JobPhase;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  dry_run: boolean;
  scan: M365ScanStats;
  download: M365DownloadStats;
  ingest: M365IngestStats;
  /** Eventi dal più recente al più vecchio. */
  events: M365JobEvent[];
  file_errors: M365FileError[];
}

// ---------- Salute ----------

export interface HealthResponse {
  status: string;
  llm_configured: boolean;
}
