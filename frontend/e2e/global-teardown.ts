import { stopBackend } from './support/backend';

export default async function globalTeardown(): Promise<void> {
  await stopBackend();
}
