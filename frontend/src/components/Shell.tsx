import { NavLink, Outlet } from 'react-router-dom';
import {
  BarChart3,
  BookOpen,
  Building2,
  CheckSquare,
  Crown,
  Gauge,
  KanbanSquare,
  Puzzle,
  Route as RouteIcon,
  ScrollText,
  Settings,
} from 'lucide-react';
import { useStore } from '../store/store';
import { pendingApprovals, runningTaskCount } from '../store/reducer';
import type { ConnStatus } from '../store/stream';

const NAV = [
  { to: '/office', label: 'Office', icon: Building2 },
  { to: '/mission', label: 'Mission Control', icon: KanbanSquare },
  { to: '/approvals', label: 'Approvals', icon: CheckSquare },
  { to: '/leadership', label: 'Leadership', icon: Crown },
  { to: '/providers', label: 'Providers & Usage', icon: Gauge },
  { to: '/routing', label: 'Routing', icon: RouteIcon },
  { to: '/analytics', label: 'Analytics', icon: BarChart3 },
  { to: '/knowledge', label: 'Knowledge', icon: BookOpen },
  { to: '/skills', label: 'Skills & MCP', icon: Puzzle },
  { to: '/logs', label: 'Logs', icon: ScrollText },
  { to: '/settings', label: 'Settings', icon: Settings },
] as const;

const CONN: Record<ConnStatus, { label: string; color: string }> = {
  idle: { label: 'offline', color: 'var(--color-faint)' },
  connecting: { label: 'connecting', color: 'var(--color-amber)' },
  syncing: { label: 'syncing', color: 'var(--color-amber)' },
  live: { label: 'live', color: 'var(--color-phosphor)' },
  reconnecting: { label: 'reconnecting', color: 'var(--color-amber)' },
  closed: { label: 'disconnected', color: 'var(--color-danger)' },
};

export function Shell() {
  const conn = useStore((s) => s.conn);
  const data = useStore((s) => s.data);
  const bootError = useStore((s) => s.bootError);
  const booting = useStore((s) => s.booting);
  const bootstrap = useStore((s) => s.bootstrap);
  const pending = pendingApprovals(data).length;
  const running = runningTaskCount(data);
  const c = CONN[conn];

  return (
    <div className="grid h-full grid-cols-[var(--spacing-nav)_1fr] grid-rows-[var(--spacing-topbar)_1fr]">
      <header className="col-span-2 flex items-center gap-5 border-b border-line bg-void/80 px-4" role="banner">
        <div className="display flex items-center gap-2 text-sm tracking-[0.2em] text-phosphor">
          <span aria-hidden className="inline-block size-3 bg-phosphor" style={{ boxShadow: '4px 0 0 var(--color-amber), 0 4px 0 var(--color-signal)' }} />
          ORCHESTRATOR
        </div>
        <div className="ml-auto flex items-center gap-4 text-xs" aria-label="System status">
          <span className="flex items-center gap-1.5" title="WebSocket event stream" data-testid="conn-status">
            <span aria-hidden className={`size-2 ${conn === 'live' ? 'pulse-dot' : ''}`} style={{ background: c.color }} />
            <span className="label" style={{ color: c.color }}>
              {c.label}
            </span>
          </span>
          <span className="flex items-center gap-1.5" data-testid="leader">
            <span className="label">leader</span>
            <span className="font-mono">{data.leader ? `${data.leader.provider}${data.leader.model ? ` / ${data.leader.model}` : ''}` : data.leaderState === 'detecting' ? 'detecting…' : data.leaderState === 'none' ? 'none' : 'unknown'}</span>
          </span>
          <span className="flex items-center gap-1.5" data-testid="running-count">
            <span className="label">running</span>
            <span className="tick-num text-phosphor">{running}</span>
          </span>
          <NavLink to="/approvals" className="flex items-center gap-1.5" aria-label={`${pending} pending approvals`} data-testid="approvals-badge">
            <span className="label">approvals</span>
            <span className={`tick-num min-w-5 rounded-pixel px-1 text-center ${pending ? 'bg-amber text-black' : 'bg-raised text-muted'}`}>
              {pending}
            </span>
          </NavLink>
        </div>
      </header>

      <nav aria-label="Primary" className="overflow-y-auto border-r border-line bg-void/50 p-2">
        <ul className="flex flex-col gap-0.5">
          {NAV.map(({ to, label, icon: Icon }) => (
            <li key={to}>
              <NavLink
                to={to}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded-pixel border-l-2 px-3 py-2 text-[13px] transition-colors ${
                    isActive ? 'border-phosphor bg-raised text-phosphor' : 'border-transparent text-muted hover:bg-panel hover:text-ink'
                  }`
                }
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <main className="min-h-0 min-w-0 overflow-auto" id="main">
        {bootError ? (
          <div className="p-5">
            <div role="alert" className="pixel-card flex items-center justify-between gap-3 border-danger/60 p-4 text-sm">
              <span>
                <b className="display">Cannot load data.</b> {bootError}
                {/reach the orchestrator backend/i.test(bootError) ? (
                  <span data-testid="offline-hint" className="mt-1 block text-muted">
                    The background server is not running. Start it with <code className="font-mono">scripts\start.cmd</code>{' '}
                    (or <code className="font-mono">scripts\orchestrator.cmd dashboard</code>), then press Retry. This tab
                    keeps its access token.
                  </span>
                ) : null}
              </span>
              <button className="btn" onClick={() => void bootstrap()} disabled={booting}>
                Retry
              </button>
            </div>
          </div>
        ) : (
          <Outlet />
        )}
      </main>
    </div>
  );
}
