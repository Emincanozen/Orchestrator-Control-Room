import { describe, expect, it } from 'vitest';
import { planVerifyInfo, verifyKind, verifyToString } from './verify';

const blank = { name: null, cmd: null, timeout: null, file_exists: null, contains: null, file_absent: null, file_unchanged: null, json_valid: null };

describe('structured verify checks', () => {
  it('describes every kind readably', () => {
    expect(verifyToString({ ...blank, file_exists: 'a.txt', contains: 'hi' })).toBe('file exists: a.txt and contains "hi"');
    expect(verifyToString({ ...blank, file_absent: 'tmp.log' })).toContain('must not exist: tmp.log');
    expect(verifyToString({ ...blank, file_unchanged: 'LICENSE' })).toContain('unchanged');
    expect(verifyToString({ ...blank, json_valid: 'x.json' })).toBe('valid JSON: x.json');
    expect(verifyToString({ ...blank, cmd: ['uv', 'run', 'pytest', '-q'] })).toBe('uv run pytest -q');
    expect(verifyToString('npm test')).toBe('npm test');
    expect(verifyToString(['a', 'b'])).toBe('a b');
  });
  it('classifies kinds', () => {
    expect(verifyKind({ ...blank, json_valid: 'x' })).toBe('json_valid');
    expect(verifyKind({ ...blank, file_absent: 'x' })).toBe('file_absent');
    expect(verifyKind('npm test')).toBe('cmd');
  });
  it('reads plan.verify_origin and verify_not_adopted', () => {
    expect(planVerifyInfo({ verify_origin: 'approved', verify_not_adopted: 'why' })).toEqual({ origin: 'approved', notAdopted: 'why' });
    expect(planVerifyInfo(null)).toEqual({ origin: null, notAdopted: null });
    expect(planVerifyInfo({ verify_origin: 'bogus' }).origin).toBeNull();
  });
});
