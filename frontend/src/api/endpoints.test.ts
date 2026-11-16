import { describe, expect, it } from 'vitest';
import { stripYamlOnly } from './endpoints';

describe('stripYamlOnly', () => {
  it('never sends worker_permissions.claude_shell', () => {
    expect(stripYamlOnly({ worker_permissions: { default: 'workspace_write', claude_shell: true } })).toEqual({ worker_permissions: { default: 'workspace_write' } });
    const v = { limits: { a: 1 } };
    expect(stripYamlOnly(v)).toBe(v);
  });
});
