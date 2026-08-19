import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/* =============================================================================
   Build config.
   =============================================================================

   TWO SPLITS, DOING TWO DIFFERENT JOBS.

   The first lives in the source: `src/ops/OpsRoot.tsx` and `src/App.tsx` load
   every screen through `React.lazy`, so Rollup emits a chunk per route and a
   persona downloads the screens they open rather than all seventeen. That is
   the one that cuts FIRST LOAD, and it is documented where it is written.

   The second is here, and it does NOT cut first load — every vendor byte below
   is needed to render anything, so splitting them moves bytes between files
   without removing any. It is about CACHE LIFETIME. Before it, one 500 kB entry
   chunk mixed React with our own code, so changing a label in AppShell
   invalidated React, Supabase, the router and the query client for every user.
   After it, an app deploy re-downloads ~10 kB gzip of app entry and leaves
   ~141 kB gzip of vendor in the browser cache, and a dependency bump moves only
   the dependency that changed. Deploys are frequent and dependency bumps are
   not, so this is the right way round.

   MEASURED, on this repo, with this Vite, `npm run build` each time:

     this code, unsplit      1 JS chunk    818.27 kB │ gzip 230.49 kB
     + route split          30 JS chunks   entry 500.83 kB │ gzip 147.47 kB
     + vendor split (this)  30 JS chunks   entry  28.39 kB │ gzip   9.96 kB
                                           v-react    192.35 │ gzip  60.29 kB
                                           v-supabase 216.82 │ gzip  57.10 kB
                                           v-router    37.12 │ gzip  13.39 kB
                                           v-query     35.89 │ gzip  10.60 kB

   FIRST LOAD, summed over the chunks a persona's landing screen actually pulls:

     Manager on Home    164.57 kB gzip   (was 230.49) — 28.6% less
     College login      158.57 kB gzip                — 31.2% less

   Of that, 141.38 kB gzip is the four vendor chunks, which is the floor and is
   not ours to remove. Each further route costs 0.4–13.9 kB gzip on navigation,
   and the whole app is still 252 kB gzip across all 30 chunks — the split moved
   bytes off the critical path rather than deleting any.

   ONE OBSERVATION WORTH RECORDING RATHER THAN ACTING ON. `@supabase/supabase-js`
   is the largest single dependency, and `grep -rn "realtime\|\.channel(" src`
   returns nothing — the realtime client is dead weight. Trimming it means
   importing the sub-packages directly instead of the umbrella client, which
   changes how every query in the app is constructed. Not worth it while the
   first-load budget is this comfortable; worth knowing when it is not.

   The grouping is by PACKAGE rather than by a generic "everything in
   node_modules": one 141 kB vendor blob would put React and Supabase on the
   same cache line, and they are versioned entirely independently.
   ============================================================================= */

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, open: true },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          // Order matters: @supabase pulls in its own sub-packages, and
          // react-dom/scheduler must land with react or the runtime splits
          // across chunks that initialise in the wrong order.
          if (id.includes('@supabase')) return 'v-supabase'
          if (id.includes('@tanstack')) return 'v-query'
          if (id.includes('react-router')) return 'v-router'
          if (
            id.includes('/react/') ||
            id.includes('/react-dom/') ||
            id.includes('/scheduler/')
          ) {
            return 'v-react'
          }
          return undefined
        },
      },
    },
  },
})
