import type { OfficeAgent } from './types';

/** What the React shell needs from the renderer. Implemented by `PixiOffice`. */
export interface OfficeEngine {
  /** Create the renderer inside `host`. Safe to call destroy() while this is pending. */
  mount(host: HTMLElement): Promise<void>;
  setAgents(agents: OfficeAgent[]): void;
  setSelected(id: string | null): void;
  setReducedMotion(v: boolean): void;
  zoomIn(): void;
  zoomOut(): void;
  fit(): void;
  destroy(): void;
}

export interface OfficeEngineOptions {
  onSelect: (id: string) => void;
  reducedMotion: boolean;
  ariaLabel: string;
  /** called when the renderer cannot start (e.g. no WebGL) */
  onError?: (message: string) => void;
  /** called whenever the integer zoom level changes */
  onZoom?: (scale: number) => void;
}
