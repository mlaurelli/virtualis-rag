/**
 * Vista a grafo del knowledge graph (nodi = entità, archi = relazioni).
 *
 * Layout force-directed calcolato con d3-force (solo fisica, nessun DOM):
 * la simulazione è animata dal vivo e le posizioni vengono applicate in modo
 * imperativo (setAttribute dentro requestAnimationFrame) agli elementi SVG
 * renderizzati da React — nessun setState a ogni tick, così la tab resta
 * fluida anche con centinaia di nodi.
 *
 * Interazioni: drag dei nodi, pan dello sfondo, zoom con rotellina, tooltip
 * HTML su nodi e archi, evidenza del vicinato in hover/selezione, ricerca,
 * filtro per tipo entità, fit alla vista (pulsante o doppio click).
 */

import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { KgEntitySummary, KgGraphResponse } from '../api/types';

const WIDTH = 1100;
const HEIGHT = 720;
/** Opzioni del select "Entità" (limit passato a /kg/graph). */
const LIMIT_OPTIONS = [100, 300, 600];
const DEFAULT_LIMIT = 300;
/** Sopra questo zoom si mostrano tutte le etichette. */
const LABEL_ZOOM = 1.5;
/** A zoom lontano si etichettano solo i nodi più citati (le etichette hanno
 *  dimensione costante su schermo: un numero contenuto evita l'affollamento). */
const TOP_LABELS = 40;
/** Dimensione delle etichette SU SCHERMO (px CSS), indipendente dallo zoom:
 *  il counter-scale 1/(k·s) la mantiene costante; i limiti 9–14 px sono il
 *  vincolo di leggibilità entro cui deve sempre cadere. */
const LABEL_SCREEN_PX = 11;
const LABEL_MIN_PX = 9;
const LABEL_MAX_PX = 14;
/** Spessore su schermo dell'anello di selezione (px CSS, counter-scalato). */
const RING_SCREEN_PX = 2.5;
/** Raggio nodo: scala sqrt sul document_count, range marcato. */
const MIN_R = 5;
const MAX_R = 26;
/** Predicati elencati nel tooltip di un arco aggregato. */
const TOOLTIP_PREDICATES = 5;

/** Colori nodo per tipo entità (allineati ai badge della tabella). */
const TYPE_COLOR: Record<string, string> = {
  persona: 'var(--accent)',
  organizzazione: 'var(--info)',
  contratto: 'var(--warning)',
  progetto: 'var(--success)',
  data: 'var(--text-muted)',
  importo: 'var(--violet)',
  prodotto: 'var(--pink)',
  altro: 'var(--text-muted)',
};

/** Tipi fuori vocabolario finiscono nel secchio "altro" (colore e legenda). */
function normalizeType(type: string): string {
  return TYPE_COLOR[type] ? type : 'altro';
}

function nodeColor(type: string): string {
  return TYPE_COLOR[normalizeType(type)];
}

function truncate(label: string, max = 22): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

/** Nodo della simulazione: d3 muta x/y/vx/vy sull'oggetto. */
interface SimNode extends SimulationNodeDatum {
  entity: KgEntitySummary;
  r: number;
  x: number;
  y: number;
}

/** Relazione originale conservata per il tooltip di un arco aggregato. */
interface LinkRelation {
  subjectId: number;
  predicate: string;
  documentTitle: string;
}

/** Arco aggregato: tutte le relazioni tra la stessa coppia di nodi. */
interface AggLink extends SimulationLinkDatum<SimNode> {
  key: string;
  source: SimNode;
  target: SimNode;
  count: number;
  /** Prime relazioni (per il tooltip); count può essere maggiore. */
  relations: LinkRelation[];
}

type TooltipData =
  | { kind: 'node'; node: SimNode }
  | { kind: 'edge'; link: AggLink };

interface ViewTransform {
  x: number;
  y: number;
  k: number;
}

/** Spessore dell'arco proporzionale al numero di relazioni aggregate. */
function linkWidth(count: number): number {
  return Math.min(1.2 + (count - 1) * 0.8, 6);
}

interface KgGraphProps {
  selectedId: number | null;
  onSelect: (id: number) => void;
}

export default function KgGraph({ selectedId, onSelect }: KgGraphProps) {
  const [graph, setGraph] = useState<KgGraphResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [query, setQuery] = useState('');
  const [hiddenTypes, setHiddenTypes] = useState<ReadonlySet<string>>(new Set());
  const [hoverNodeId, setHoverNodeId] = useState<number | null>(null);
  const [hoverEdgeKey, setHoverEdgeKey] = useState<string | null>(null);
  /** Evidenza locale (anello + vicinato): il click sullo sfondo la azzera
   *  senza chiudere il pannello di dettaglio gestito da KgPage. */
  const [focusId, setFocusId] = useState<number | null>(null);
  const [labelsAll, setLabelsAll] = useState(false);
  const [tooltip, setTooltip] = useState<TooltipData | null>(null);

  const wrapRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const rootGRef = useRef<SVGGElement | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  const lastPtrRef = useRef({ x: 0, y: 0 });
  const viewRef = useRef<ViewTransform>({ x: 0, y: 0, k: 1 });
  const nodeElsRef = useRef(new Map<number, SVGGElement>());
  const linkElsRef = useRef(new Map<string, SVGLineElement[]>());
  const simRef = useRef<Simulation<SimNode, AggLink> | null>(null);
  const dirtyRef = useRef(false);
  const didFitRef = useRef(false);
  const panRef = useRef<{ px: number; py: number; moved: boolean } | null>(null);
  const dragRef = useRef<{ node: SimNode; moved: boolean } | null>(null);
  const dragMovedRef = useRef(false);
  const visibleNodesRef = useRef<SimNode[]>([]);
  const visibleLinksRef = useRef<AggLink[]>([]);

  // ---------- Caricamento dati ----------

  useEffect(() => {
    let cancelled = false;
    setGraph(null);
    setFocusId(null);
    setHoverNodeId(null);
    setHoverEdgeKey(null);
    setTooltip(null);
    didFitRef.current = false;
    api
      .get<KgGraphResponse>(`/kg/graph?limit=${limit}`)
      .then((res) => {
        if (!cancelled) {
          setGraph(res);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : 'Errore di connessione al server.');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [limit]);

  // la selezione fatta altrove (tabella, pannello relazioni) evidenzia il nodo
  useEffect(() => {
    if (selectedId !== null) setFocusId(selectedId);
  }, [selectedId]);

  // ---------- Preparazione nodi/archi per la simulazione ----------

  const simNodes = useMemo<SimNode[]>(() => {
    if (!graph) return [];
    const maxDc = graph.nodes.reduce((m, n) => Math.max(m, n.document_count), 1);
    return graph.nodes.map((entity, i) => {
      // spirale di Fibonacci: posizioni iniziali stabili e ben distribuite
      const angle = i * 2.399963;
      const rad = 14 * Math.sqrt(i);
      return {
        entity,
        r: MIN_R + (MAX_R - MIN_R) * Math.sqrt(entity.document_count / maxDc),
        x: WIDTH / 2 + rad * Math.cos(angle),
        y: HEIGHT / 2 + rad * Math.sin(angle),
      };
    });
  }, [graph]);

  const { visibleNodes, visibleLinks, neighbors, visibleEdgeCount } = useMemo(() => {
    const nodes = simNodes.filter((n) => !hiddenTypes.has(normalizeType(n.entity.type)));
    const byId = new Map(nodes.map((n) => [n.entity.id, n]));
    // aggregazione degli archi paralleli: un solo arco per coppia di nodi
    const linkMap = new Map<string, AggLink>();
    let edgeCount = 0;
    for (const e of graph?.edges ?? []) {
      const a = byId.get(e.subject_id);
      const b = byId.get(e.object_id);
      if (!a || !b || a === b) continue;
      edgeCount++;
      const key =
        e.subject_id < e.object_id
          ? `${e.subject_id}-${e.object_id}`
          : `${e.object_id}-${e.subject_id}`;
      let link = linkMap.get(key);
      if (!link) {
        link = { key, source: a, target: b, count: 0, relations: [] };
        linkMap.set(key, link);
      }
      link.count++;
      if (link.relations.length <= TOOLTIP_PREDICATES) {
        link.relations.push({
          subjectId: e.subject_id,
          predicate: e.predicate,
          documentTitle: e.document_title,
        });
      }
    }
    const links = [...linkMap.values()];
    const adj = new Map<number, Set<number>>();
    for (const l of links) {
      const s = l.source.entity.id;
      const t = l.target.entity.id;
      if (!adj.has(s)) adj.set(s, new Set());
      if (!adj.has(t)) adj.set(t, new Set());
      adj.get(s)!.add(t);
      adj.get(t)!.add(s);
    }
    return { visibleNodes: nodes, visibleLinks: links, neighbors: adj, visibleEdgeCount: edgeCount };
  }, [simNodes, graph, hiddenTypes]);

  /** Nodi etichettati anche a zoom lontano (i più citati nei documenti). */
  const topLabelIds = useMemo(() => {
    const sorted = [...visibleNodes].sort(
      (a, b) => b.entity.document_count - a.entity.document_count,
    );
    return new Set(sorted.slice(0, TOP_LABELS).map((n) => n.entity.id));
  }, [visibleNodes]);

  /** Tipi presenti nel grafo, nell'ordine del vocabolario (per la legenda). */
  const typesPresent = useMemo(() => {
    const present = new Set(simNodes.map((n) => normalizeType(n.entity.type)));
    return Object.keys(TYPE_COLOR).filter((t) => present.has(t));
  }, [simNodes]);

  // ---------- Ricerca ----------

  const searchActive = query.trim().length > 0;
  const searchMatchIds = useMemo(() => {
    if (!searchActive) return null;
    const q = query.trim().toLowerCase();
    return new Set(
      visibleNodes
        .filter((n) => n.entity.display_name.toLowerCase().includes(q))
        .map((n) => n.entity.id),
    );
  }, [visibleNodes, query, searchActive]);

  const searchResults = useMemo(() => {
    if (!searchMatchIds) return [];
    return visibleNodes
      .filter((n) => searchMatchIds.has(n.entity.id))
      .sort((a, b) => b.entity.document_count - a.entity.document_count)
      .slice(0, 8);
  }, [visibleNodes, searchMatchIds]);

  // ---------- Vista (pan/zoom imperativi) ----------

  const applyView = useCallback(() => {
    const v = viewRef.current;
    rootGRef.current?.setAttribute('transform', `translate(${v.x} ${v.y}) scale(${v.k})`);
    // Counter-scale di etichette e anello di selezione: dimensione costante
    // SU SCHERMO indipendente dallo zoom. Il fattore complessivo user unit →
    // px CSS è k·s (s = scala viewBox → contenitore); si passa una CSS
    // variable sull'<svg> così non si tocca ogni elemento a ogni zoom.
    const svg = svgRef.current;
    if (svg) {
      const rect = svg.getBoundingClientRect();
      const s = rect.width > 0 ? rect.width / WIDTH : 1;
      const screenPx = Math.min(Math.max(LABEL_SCREEN_PX, LABEL_MIN_PX), LABEL_MAX_PX);
      svg.style.setProperty('--kg-label-size', `${screenPx / (v.k * s)}px`);
      svg.style.setProperty('--kg-ring-w', `${RING_SCREEN_PX / (v.k * s)}px`);
    }
    const all = v.k >= LABEL_ZOOM;
    setLabelsAll((prev) => (prev === all ? prev : all));
  }, []);

  const fitView = useCallback(() => {
    const nodes = visibleNodesRef.current;
    if (nodes.length === 0) return;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const n of nodes) {
      const pad = n.r + 24; // margine per le etichette
      minX = Math.min(minX, n.x - pad);
      maxX = Math.max(maxX, n.x + pad);
      minY = Math.min(minY, n.y - pad);
      maxY = Math.max(maxY, n.y + pad);
    }
    const k = Math.min(
      Math.max(Math.min(WIDTH / (maxX - minX), HEIGHT / (maxY - minY)) * 0.94, 0.25),
      2.2,
    );
    viewRef.current = {
      k,
      x: WIDTH / 2 - (k * (minX + maxX)) / 2,
      y: HEIGHT / 2 - (k * (minY + maxY)) / 2,
    };
    applyView();
  }, [applyView]);

  /** Centra e zooma su un nodo (risultato di ricerca). */
  const centerOnNode = useCallback(
    (n: SimNode) => {
      const k = Math.max(viewRef.current.k, 1.6);
      viewRef.current = { k, x: WIDTH / 2 - n.x * k, y: HEIGHT / 2 - n.y * k };
      applyView();
      setFocusId(n.entity.id);
    },
    [applyView],
  );

  /** Da coordinate schermo a coordinate della simulazione. */
  const toSimCoords = useCallback((clientX: number, clientY: number) => {
    const svg = svgRef.current;
    const v = viewRef.current;
    if (!svg) return { x: 0, y: 0 };
    const rect = svg.getBoundingClientRect();
    const sx = ((clientX - rect.left) / rect.width) * WIDTH;
    const sy = ((clientY - rect.top) / rect.height) * HEIGHT;
    return { x: (sx - v.x) / v.k, y: (sy - v.y) / v.k };
  }, []);

  // ---------- Disegno imperativo delle posizioni ----------

  const drawPositions = useCallback(() => {
    for (const n of visibleNodesRef.current) {
      nodeElsRef.current
        .get(n.entity.id)
        ?.setAttribute('transform', `translate(${n.x} ${n.y})`);
    }
    for (const l of visibleLinksRef.current) {
      const lines = linkElsRef.current.get(l.key);
      if (!lines) continue;
      for (const line of lines) {
        line.setAttribute('x1', String(l.source.x));
        line.setAttribute('y1', String(l.source.y));
        line.setAttribute('x2', String(l.target.x));
        line.setAttribute('y2', String(l.target.y));
      }
    }
  }, []);

  // ---------- Simulazione d3-force ----------

  useEffect(() => {
    visibleNodesRef.current = visibleNodes;
    visibleLinksRef.current = visibleLinks;
    if (visibleNodes.length === 0) return;

    const sim = forceSimulation<SimNode, AggLink>(visibleNodes)
      // Barnes-Hut: repulsione n·log(n), con raggio massimo per non disperdere
      .force('charge', forceManyBody<SimNode>().strength(-140).theta(0.9).distanceMax(600))
      .force(
        'link',
        forceLink<SimNode, AggLink>(visibleLinks).distance(
          (l) => 46 + l.source.r + l.target.r,
        ),
      )
      // collisione su raggio nodo + margine etichetta
      .force('collide', forceCollide<SimNode>((n) => n.r + 10).iterations(2))
      // gravità debole verso il centro della viewBox
      .force('x', forceX<SimNode>(WIDTH / 2).strength(0.05))
      .force('y', forceY<SimNode>(HEIGHT / 2).strength(0.06))
      .alpha(1)
      .alphaDecay(0.035);
    simRef.current = sim;

    sim.on('tick', () => {
      dirtyRef.current = true;
      // primo assestamento: adatta la vista al contenuto una sola volta
      if (!didFitRef.current && sim.alpha() < 0.08) {
        didFitRef.current = true;
        fitView();
      }
    });

    // rAF: un solo frame di disegno per quanti tick siano passati
    let raf = requestAnimationFrame(function loop() {
      if (dirtyRef.current) {
        dirtyRef.current = false;
        drawPositions();
      }
      raf = requestAnimationFrame(loop);
    });

    return () => {
      cancelAnimationFrame(raf);
      sim.stop();
      simRef.current = null;
    };
  }, [visibleNodes, visibleLinks, drawPositions, fitView]);

  // Zoom con rotellina: listener nativo non-passivo (React attacca onWheel
  // come passivo, quindi lì preventDefault non avrebbe effetto e la pagina
  // scrollerebbe insieme al grafo).
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      const mx = ((e.clientX - rect.left) / rect.width) * WIDTH;
      const my = ((e.clientY - rect.top) / rect.height) * HEIGHT;
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      const v = viewRef.current;
      const k = Math.min(Math.max(v.k * factor, 0.25), 5);
      const scale = k / v.k;
      viewRef.current = { k, x: mx - (mx - v.x) * scale, y: my - (my - v.y) * scale };
      applyView();
    };
    svg.addEventListener('wheel', onWheel, { passive: false });
    // inizializza le CSS variables del counter-scale appena l'svg è montato
    applyView();
    return () => svg.removeEventListener('wheel', onWheel);
  }, [graph, applyView]);

  // ---------- Tooltip ----------

  const positionTooltip = useCallback(() => {
    const el = tooltipRef.current;
    const wrap = wrapRef.current;
    if (!el || !wrap) return;
    const rect = wrap.getBoundingClientRect();
    const left = Math.min(lastPtrRef.current.x - rect.left + 14, rect.width - 300);
    const top = Math.min(lastPtrRef.current.y - rect.top + 14, rect.height - 40);
    el.style.left = `${Math.max(left, 0)}px`;
    el.style.top = `${Math.max(top, 0)}px`;
  }, []);

  const trackPointer = useCallback(
    (e: React.PointerEvent) => {
      lastPtrRef.current = { x: e.clientX, y: e.clientY };
      positionTooltip();
    },
    [positionTooltip],
  );

  // ---------- Pan dello sfondo ----------

  const handleSvgPointerDown = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    panRef.current = { px: e.clientX, py: e.clientY, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);

  const handleSvgPointerMove = useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      const pan = panRef.current;
      if (!pan) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const dx = ((e.clientX - pan.px) / rect.width) * WIDTH;
      const dy = ((e.clientY - pan.py) / rect.height) * HEIGHT;
      if (Math.abs(e.clientX - pan.px) + Math.abs(e.clientY - pan.py) > 3) pan.moved = true;
      pan.px = e.clientX;
      pan.py = e.clientY;
      viewRef.current = { ...viewRef.current, x: viewRef.current.x + dx, y: viewRef.current.y + dy };
      applyView();
    },
    [applyView],
  );

  const handleSvgPointerUp = useCallback(() => {
    const moved = panRef.current?.moved ?? false;
    panRef.current = null;
    // il click di fine pan non deve deselezionare
    dragMovedRef.current = moved;
  }, []);

  const handleSvgClick = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if (dragMovedRef.current) {
      dragMovedRef.current = false;
      return;
    }
    // click sullo sfondo: azzera l'evidenza (il pannello resta aperto)
    if (e.target === e.currentTarget) setFocusId(null);
  }, []);

  const handleSvgDoubleClick = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (e.target === e.currentTarget) fitView();
    },
    [fitView],
  );

  // ---------- Drag dei nodi ----------

  const handleNodePointerDown = useCallback(
    (n: SimNode) => (e: React.PointerEvent<SVGGElement>) => {
      e.stopPropagation();
      e.currentTarget.setPointerCapture(e.pointerId);
      dragRef.current = { node: n, moved: false };
      n.fx = n.x;
      n.fy = n.y;
    },
    [],
  );

  const handleNodePointerMove = useCallback(
    (n: SimNode) => (e: React.PointerEvent<SVGGElement>) => {
      trackPointer(e);
      const drag = dragRef.current;
      if (!drag || drag.node !== n) return;
      const p = toSimCoords(e.clientX, e.clientY);
      if (!drag.moved && Math.hypot(p.x - n.x, p.y - n.y) > 2) {
        drag.moved = true;
        // riscalda la simulazione: il resto del grafo segue il trascinamento
        simRef.current?.alphaTarget(0.25).restart();
      }
      n.fx = p.x;
      n.fy = p.y;
      dirtyRef.current = true;
    },
    [toSimCoords, trackPointer],
  );

  const handleNodePointerUp = useCallback(
    (n: SimNode) => () => {
      const drag = dragRef.current;
      if (!drag || drag.node !== n) return;
      dragMovedRef.current = drag.moved;
      dragRef.current = null;
      simRef.current?.alphaTarget(0);
      n.fx = null;
      n.fy = null;
    },
    [],
  );

  const handleNodeClick = useCallback(
    (n: SimNode) => (e: React.MouseEvent<SVGGElement>) => {
      e.stopPropagation();
      if (dragMovedRef.current) {
        dragMovedRef.current = false;
        return;
      }
      setFocusId(n.entity.id);
      onSelect(n.entity.id);
    },
    [onSelect],
  );

  // ---------- Stati derivati per il rendering ----------

  const visibleIds = useMemo(
    () => new Set(visibleNodes.map((n) => n.entity.id)),
    [visibleNodes],
  );

  const highlightId = hoverNodeId ?? focusId;
  const highlightSet = useMemo(() => {
    // un nodo nascosto dal filtro tipi non deve attenuare il resto del grafo
    if (highlightId === null || !visibleIds.has(highlightId)) return null;
    return new Set([highlightId, ...(neighbors.get(highlightId) ?? [])]);
  }, [highlightId, neighbors, visibleIds]);
  // id effettivo per l'evidenza di nodi/archi (null se il nodo non è visibile)
  const activeHighlightId = highlightSet !== null ? highlightId : null;

  const relationLabel = (l: AggLink, rel: LinkRelation) => {
    const subj =
      rel.subjectId === l.source.entity.id ? l.source.entity : l.target.entity;
    const obj = subj === l.source.entity ? l.target.entity : l.source.entity;
    return `${subj.display_name} — ${rel.predicate} → ${obj.display_name}`;
  };

  // ---------- Rendering ----------

  if (error) {
    return (
      <div className="alert" role="alert">
        {error}
      </div>
    );
  }
  if (!graph) {
    return (
      <div className="empty-state card">
        <div className="spinner" role="status" aria-label="Caricamento grafo" />
      </div>
    );
  }
  if (graph.nodes.length === 0) {
    return (
      <div className="empty-state card">
        <p>Il grafo è vuoto.</p>
        <p className="small">Popolalo con l'estrazione di entità e relazioni dai documenti.</p>
      </div>
    );
  }

  return (
    <div className="kg-graph-wrap card" ref={wrapRef}>
      <div className="kg-graph-toolbar">
        <div className="kg-graph-search">
          <input
            className="input"
            type="search"
            placeholder="Cerca nel grafo…"
            aria-label="Cerca un'entità nel grafo"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                if (searchResults[0]) centerOnNode(searchResults[0]);
              }
            }}
          />
          {searchActive && searchResults.length > 0 && (
            <div className="kg-search-results" role="listbox" aria-label="Risultati ricerca grafo">
              {searchResults.map((n) => (
                <button
                  key={n.entity.id}
                  type="button"
                  className="kg-search-item"
                  onClick={() => centerOnNode(n)}
                >
                  <span
                    className="kg-legend-dot"
                    style={{ background: nodeColor(n.entity.type) }}
                    aria-hidden="true"
                  />
                  <span className="kg-search-name">{n.entity.display_name}</span>
                  <span className="faint small">{n.entity.document_count} doc.</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <select
          className="select kg-graph-limit"
          aria-label="Numero di entità mostrate"
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
        >
          {LIMIT_OPTIONS.map((n) => (
            <option key={n} value={n}>
              Entità: {n}
            </option>
          ))}
        </select>

        <div className="kg-legend" role="group" aria-label="Filtra per tipo entità">
          {typesPresent.map((t) => {
            const off = hiddenTypes.has(t);
            return (
              <button
                key={t}
                type="button"
                className={`kg-legend-chip${off ? ' off' : ''}`}
                aria-pressed={!off}
                title={off ? `Mostra le entità di tipo ${t}` : `Nascondi le entità di tipo ${t}`}
                onClick={() =>
                  setHiddenTypes((prev) => {
                    const next = new Set(prev);
                    if (next.has(t)) next.delete(t);
                    else next.add(t);
                    return next;
                  })
                }
              >
                <span
                  className="kg-legend-dot"
                  style={{ background: TYPE_COLOR[t] }}
                  aria-hidden="true"
                />
                {t}
              </button>
            );
          })}
        </div>

        <button type="button" className="btn btn-sm" onClick={fitView}>
          Adatta
        </button>

        <span className="small faint kg-graph-count">
          {visibleNodes.length} entità · {visibleEdgeCount} relazioni (su {graph.total_nodes} ·{' '}
          {graph.total_edges} totali)
        </span>
      </div>

      <svg
        ref={svgRef}
        className="kg-graph-svg"
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label="Grafo delle entità e delle relazioni"
        onPointerDown={handleSvgPointerDown}
        onPointerMove={handleSvgPointerMove}
        onPointerUp={handleSvgPointerUp}
        onClick={handleSvgClick}
        onDoubleClick={handleSvgDoubleClick}
      >
        <g
          ref={(el) => {
            rootGRef.current = el;
            if (el) {
              const v = viewRef.current;
              el.setAttribute('transform', `translate(${v.x} ${v.y}) scale(${v.k})`);
            }
          }}
        >
          <g>
            {visibleLinks.map((l) => {
              const hi =
                l.key === hoverEdgeKey ||
                (activeHighlightId !== null &&
                  (l.source.entity.id === activeHighlightId ||
                    l.target.entity.id === activeHighlightId));
              const dim = activeHighlightId !== null && !hi;
              return (
                <g
                  key={l.key}
                  className={`kg-graph-edge${hi ? ' kg-edge-hi' : ''}${dim ? ' kg-dim' : ''}`}
                  onPointerEnter={(e) => {
                    trackPointer(e);
                    setHoverEdgeKey(l.key);
                    setTooltip({ kind: 'edge', link: l });
                  }}
                  onPointerMove={trackPointer}
                  onPointerLeave={() => {
                    setHoverEdgeKey(null);
                    setTooltip((t) => (t?.kind === 'edge' && t.link.key === l.key ? null : t));
                  }}
                >
                  <line
                    className="kg-edge-line"
                    strokeWidth={linkWidth(l.count)}
                    ref={(el) => {
                      const lines = linkElsRef.current.get(l.key) ?? [];
                      if (el) {
                        lines[0] = el;
                        linkElsRef.current.set(l.key, lines);
                        el.setAttribute('x1', String(l.source.x));
                        el.setAttribute('y1', String(l.source.y));
                        el.setAttribute('x2', String(l.target.x));
                        el.setAttribute('y2', String(l.target.y));
                      }
                    }}
                  />
                  <line
                    className="kg-edge-hit"
                    ref={(el) => {
                      const lines = linkElsRef.current.get(l.key) ?? [];
                      if (el) {
                        lines[1] = el;
                        linkElsRef.current.set(l.key, lines);
                        el.setAttribute('x1', String(l.source.x));
                        el.setAttribute('y1', String(l.source.y));
                        el.setAttribute('x2', String(l.target.x));
                        el.setAttribute('y2', String(l.target.y));
                      } else {
                        linkElsRef.current.delete(l.key);
                      }
                    }}
                  />
                </g>
              );
            })}
          </g>
          <g>
            {visibleNodes.map((n) => {
              const id = n.entity.id;
              const isMatch = searchMatchIds?.has(id) ?? false;
              const dimmed =
                (highlightSet !== null && !highlightSet.has(id)) ||
                (searchMatchIds !== null && !isMatch && !(highlightSet?.has(id) ?? false));
              const labelVisible =
                labelsAll ||
                topLabelIds.has(id) ||
                (highlightSet?.has(id) ?? false) ||
                isMatch;
              return (
                <g
                  key={id}
                  className={`kg-graph-node${dimmed ? ' kg-dim' : ''}${
                    id === focusId ? ' kg-graph-node-selected' : ''
                  }${isMatch ? ' kg-node-match' : ''}`}
                  ref={(el) => {
                    if (el) {
                      nodeElsRef.current.set(id, el);
                      el.setAttribute('transform', `translate(${n.x} ${n.y})`);
                    } else {
                      nodeElsRef.current.delete(id);
                    }
                  }}
                  onPointerDown={handleNodePointerDown(n)}
                  onPointerMove={handleNodePointerMove(n)}
                  onPointerUp={handleNodePointerUp(n)}
                  onPointerEnter={(e) => {
                    trackPointer(e);
                    setHoverNodeId(id);
                    setTooltip({ kind: 'node', node: n });
                  }}
                  onPointerLeave={() => {
                    setHoverNodeId((prev) => (prev === id ? null : prev));
                    setTooltip((t) => (t?.kind === 'node' && t.node === n ? null : t));
                  }}
                  onClick={handleNodeClick(n)}
                >
                  <circle r={n.r} style={{ fill: nodeColor(n.entity.type) }} />
                  <text
                    className={`kg-graph-label${labelVisible ? '' : ' kg-label-hidden'}`}
                    y={n.r + 13}
                  >
                    {truncate(n.entity.display_name)}
                  </text>
                </g>
              );
            })}
          </g>
        </g>
      </svg>

      {tooltip && (
        <div
          className="kg-tooltip"
          ref={(el) => {
            tooltipRef.current = el;
            if (el) positionTooltip();
          }}
        >
          {tooltip.kind === 'node' ? (
            <>
              <div className="kg-tooltip-title">{tooltip.node.entity.display_name}</div>
              <div className="small faint">
                <span
                  className="kg-legend-dot"
                  style={{ background: nodeColor(tooltip.node.entity.type) }}
                  aria-hidden="true"
                />
                {normalizeType(tooltip.node.entity.type)} · {tooltip.node.entity.document_count}{' '}
                {tooltip.node.entity.document_count === 1 ? 'documento' : 'documenti'}
              </div>
            </>
          ) : (
            <>
              {tooltip.link.relations.slice(0, TOOLTIP_PREDICATES).map((rel, i) => (
                <div key={i} className="kg-tooltip-rel">
                  <div className="small">{relationLabel(tooltip.link, rel)}</div>
                  {rel.documentTitle && (
                    <div className="small faint">da {rel.documentTitle}</div>
                  )}
                </div>
              ))}
              {tooltip.link.count > TOOLTIP_PREDICATES && (
                <div className="small faint">
                  …e altre {tooltip.link.count - TOOLTIP_PREDICATES} relazioni
                </div>
              )}
            </>
          )}
        </div>
      )}

      <p className="kg-graph-hint small faint">
        Rotellina per lo zoom, trascina lo sfondo per spostarti, trascina un nodo per
        riposizionarlo, click su un nodo per il dettaglio, doppio click sullo sfondo per adattare
        la vista.
      </p>
    </div>
  );
}
