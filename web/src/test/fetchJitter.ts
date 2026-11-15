// `npm run test:jitter`: every test's fake fetch (vi.stubGlobal("fetch", …)) answers after a random 0–150 ms,
// each call on its own, instead of at once and in the order asked. A test that reads the page before the
// answer it needs has arrived (a getBy… right after mount or a click, a value read once) then fails here
// every few runs, as it does now and then on a busy CI machine. The call itself is still recorded at once.
import { vi } from "vitest";

const MAX_MS = 150;
const realSetTimeout = globalThis.setTimeout; // taken before a test can switch to fake timers
const stubGlobal = vi.stubGlobal.bind(vi);

vi.stubGlobal = ((name: string | number | symbol, value: unknown) => {
  if (name !== "fetch" || typeof value !== "function") return stubGlobal(name, value);
  const late = new Proxy(value as (...args: unknown[]) => Promise<unknown>, {
    apply(target, self, args) {
      return Promise.resolve(Reflect.apply(target, self, args)).then(
        (answer) => new Promise((resolve) => realSetTimeout(() => resolve(answer), Math.random() * MAX_MS)));
    },
  });
  return stubGlobal(name, late);
}) as typeof vi.stubGlobal;
