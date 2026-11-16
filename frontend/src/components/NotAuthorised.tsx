import { ShieldAlert } from 'lucide-react';

export function NotAuthorised({ expired }: { expired: boolean }) {
  return (
    <main className="grid h-full place-items-center p-6">
      <div className="pixel-card max-w-lg p-6" role="alert">
        <div className="mb-3 flex items-center gap-3">
          <ShieldAlert className="size-7 text-amber" aria-hidden />
          <h1 className="display text-lg">Not authorised</h1>
        </div>
        <p className="text-sm text-muted">
          {expired
            ? 'The backend rejected the access token (it may have changed after a restart).'
            : 'This dashboard needs the local API token, which was not found in the address or this browser session.'}
        </p>
        <p className="mt-3 text-sm">
          Open this page as <code className="font-mono text-phosphor">/#token=&lt;token&gt;</code>. The token is the content of the <code className="font-mono">api-token</code> file in the
          backend data directory.
        </p>
        <p className="mt-3 text-xs text-faint">
          The token is kept in this tab&apos;s sessionStorage only and is removed from the address bar immediately.
        </p>
      </div>
    </main>
  );
}
