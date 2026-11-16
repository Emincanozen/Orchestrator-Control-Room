import { useEffect, useSyncExternalStore } from 'react';
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import { getToken, onAuthChange } from './api/auth';
import { Shell } from './components/Shell';
import { NotAuthorised } from './components/NotAuthorised';
import { useStore } from './store/store';
import { OfficeView } from './views/OfficeView';
import { MissionControl } from './views/mission/MissionControl';
import { ApprovalsView } from './views/ApprovalsView';
import { LeadershipView } from './views/LeadershipView';
import { ProvidersView } from './views/ProvidersView';
import { RoutingView } from './views/RoutingView';
import { AnalyticsView } from './views/AnalyticsView';
import { KnowledgeView } from './views/KnowledgeView';
import { SkillsView } from './views/SkillsView';
import { LogsView } from './views/LogsView';
import { SettingsView } from './views/SettingsView';

export function App() {
  const token = useSyncExternalStore(onAuthChange, getToken);
  const authFailed = useStore((s) => s.authFailed);
  const bootstrap = useStore((s) => s.bootstrap);
  const shutdown = useStore((s) => s.shutdown);

  useEffect(() => {
    if (!token) return;
    void bootstrap();
    return () => shutdown();
  }, [token, bootstrap, shutdown]);

  if (!token || authFailed) return <NotAuthorised expired={!!token || authFailed} />;

  return (
    <HashRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Navigate to="/mission" replace />} />
          <Route path="office" element={<OfficeView />} />
          <Route path="mission" element={<MissionControl />} />
          <Route path="approvals" element={<ApprovalsView />} />
          <Route path="leadership" element={<LeadershipView />} />
          <Route path="providers" element={<ProvidersView />} />
          <Route path="routing" element={<RoutingView />} />
          <Route path="analytics" element={<AnalyticsView />} />
          <Route path="knowledge" element={<KnowledgeView />} />
          <Route path="skills" element={<SkillsView />} />
          <Route path="logs" element={<LogsView />} />
          <Route path="settings" element={<SettingsView />} />
          <Route path="*" element={<Navigate to="/mission" replace />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}
