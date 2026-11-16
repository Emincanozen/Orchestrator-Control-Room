import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';

/** Local fake skill sources + a small catalogue for the e2e: no network, nothing is ever fetched from GitHub. */

const ENV = {
  ...process.env,
  GIT_AUTHOR_NAME: 'e2e',
  GIT_AUTHOR_EMAIL: 'e2e@example.invalid',
  GIT_COMMITTER_NAME: 'e2e',
  GIT_COMMITTER_EMAIL: 'e2e@example.invalid',
  GIT_CONFIG_NOSYSTEM: '1',
};
const git = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, env: ENV, stdio: 'ignore' });

export const SKILL_MD = (name: string) => `---\nname: ${name}\ndescription: A fake e2e skill called ${name}\n---\n\n# ${name}\n\nDo the thing. <b>not html</b>\n`;

export function makeSkillRepo(root: string, name: string): string {
  const dir = path.join(root, name);
  mkdirSync(path.join(dir, 'notes'), { recursive: true });
  git(dir, 'init', '--quiet', '-b', 'main');
  git(dir, 'config', 'core.autocrlf', 'false');
  writeFileSync(path.join(dir, 'SKILL.md'), SKILL_MD(name));
  writeFileSync(path.join(dir, 'notes', 'extra.md'), 'extra\n');
  git(dir, 'add', '-A');
  git(dir, 'commit', '--quiet', '-m', 'init');
  return dir;
}

const entry = (id: string, o: Record<string, unknown>) => ({
  id,
  user_label: id,
  name: id,
  kind: 'agent_skill',
  category: 'e2e',
  source_url: `local:${id}`,
  source_verified: true,
  uncertainty: null,
  candidates: [],
  description: `E2E skill ${id}`,
  license: 'MIT',
  maintenance: { stars: 1 },
  default_branch: 'main',
  compat: { claude_code: 'yes', codex: 'unknown', mcp: 'n/a' },
  install_method: { type: 'manual', command: 'npx skills add example/e2e', notes: 'documented only' },
  dependencies: [],
  permissions: [],
  requires_credentials: false,
  paid_service: 'no',
  security: { risk: 'low', notes: '' },
  priority_candidate: false,
  initial_state: 'CATALOGUED_ON_DEMAND',
  ...o,
});

/** Writes repos + catalogue (JSON is valid YAML) + the launcher config under `root`. */
export function prepareSkillSources(root: string): void {
  const repos = path.join(root, 'skill-repos');
  mkdirSync(repos, { recursive: true });
  const map: Record<string, string> = {};
  for (const n of ['e2e-alpha']) map[n] = makeSkillRepo(repos, n);
  const catalog = {
    catalog_version: 1,
    researched_at: '2026-11-11',
    skills: [
      entry('e2e-alpha', { priority_candidate: true }),
      entry('e2e-nosrc', {
        source_url: null,
        source_verified: false,
        uncertainty: 'Two repositories match this name.',
        candidates: ['https://github.com/example/nosrc-one', 'https://github.com/example/nosrc-two'],
      }),
      entry('e2e-docs', { kind: 'docs', source_url: null, category: 'docs', install_method: { type: 'manual', command: null, notes: 'Read the website; nothing to install.' } }),
    ],
  };
  const catalogPath = path.join(root, 'skills-catalog.e2e.json');
  writeFileSync(catalogPath, JSON.stringify(catalog));
  writeFileSync(path.join(root, 'skills-config.json'), JSON.stringify({ catalog: catalogPath, repos: map }));
}
