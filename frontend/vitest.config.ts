import { defineConfig } from 'vitest/config'

/* =============================================================================
   Test runner for the console's pure logic. CLAUDE.md §12.
   =============================================================================

   STANDALONE, NOT AN EXTENSION OF vite.config.ts, on purpose.

   Vitest reads `vitest.config.ts` in preference to `vite.config.ts`, so this
   file replaces the app config for test runs rather than adding to it. That is
   the wanted shape here: nothing under test renders, so the React plugin and
   the Tailwind plugin would be startup cost paid on every run for transforms no
   test file needs. Extending vite.config.ts would also have meant editing a
   file this workstream does not own, and swapping its `defineConfig` import
   from 'vite' to 'vitest/config' to get a typed `test` key.

   ENVIRONMENT IS 'node', AND THERE IS NO jsdom.

   These tests cover functions that take values and return values. Nothing here
   mounts a component. `erm.copyText` is the one function that reaches for
   browser globals, and its test stubs the three it touches (`navigator`,
   `window`, `document`) with hand-written objects — that tests the fallback
   LADDER, which is the logic worth locking, rather than a DOM implementation's
   clipboard behaviour.

   ENV VARS ARE PINNED HERE.

   `lib/supabase.ts` THROWS at module scope when VITE_SUPABASE_URL or
   VITE_SUPABASE_ANON_KEY is missing, and every file under test imports it
   transitively through `lib/api.ts`. Without these, whether the suite ran at
   all would depend on the developer having a populated `.env.local` — a test
   run that passes on one machine and cannot import a module on another. These
   values are deliberately not real: no test in this suite makes a network call,
   and a live key here would be a credential in the repo.
   ============================================================================= */

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    env: {
      VITE_SUPABASE_URL: 'http://127.0.0.1:54321',
      VITE_SUPABASE_ANON_KEY: 'not-a-real-anon-key',
      VITE_API_BASE_URL: '',
    },
  },
})
