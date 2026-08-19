import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import {
  PERSONA_ROOT_KEY,
  collectPreloadFiles,
  type PreloadChunk,
} from './src/lib/preload'

/* =============================================================================
   Build config.
   =============================================================================

   THREE THINGS HAPPEN HERE. TWO SPLITS AND ONE UN-SPLIT.

   The first lives in the source: `src/ops/OpsRoot.tsx` and `src/App.tsx` load
   every screen through `React.lazy`, so Rollup emits a chunk per route and a
   persona downloads the screens they open rather than all seventeen. That is
   the one that cuts FIRST LOAD, and it is documented where it is written.

   The un-split is the LANDING route, which is deliberately no longer lazy, and
   the plugin at the bottom of this file preloads what is left. Both exist
   because the route split traded round trips for bytes without saying so — see
   "THE THIRD THING" below, which is the part to read if you are here about
   perceived load time rather than about download size.

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

   -----------------------------------------------------------------------------
   THE THIRD THING: THE WATERFALL THE ROUTE SPLIT CREATED, AND ITS FIX.
   -----------------------------------------------------------------------------

   The figures above are byte counts, and byte counts were the only thing the
   split was measured on. Measured again as ROUND TRIPS, the same build looked
   like this — the numbers come from `.vite/manifest.json` and gzipping the
   emitted files, not from a guess:

     wave 1  from index.html   entry + 4 vendor chunks + css   166.32 kB gzip
     wave 2  entry executes    OpsRoot                           1.73 kB gzip
     wave 3  OpsRoot renders   HomePage + bento + bounds + keys 10.21 kB gzip
                                                        TOTAL  174.08 kB gzip

   Three sequential round trips, the last two carrying 11.9 kB between them.
   Vite writes `<link rel="modulepreload">` for the entry chunk's STATIC
   dependencies only — which is correct, because it cannot know which dynamic
   import the app will make — so each `React.lazy` on the landing path is a
   fetch nothing can discover until the chunk before it has arrived AND RUN. At
   the ~200 ms round trip this console is read over on campus, that is ~400 ms
   of blank screen buying 11.9 kB.

   Fixed in two halves, and neither gives back the byte win:

     1. ops/OpsRoot.tsx imports its LANDING route statically and the other
        sixteen lazily. Rollup folds HomePage into the OpsRoot chunk, so wave 3
        stops existing. Nothing that a persona does not open is now shipped.

     2. `personaRootPreload()` below injects `<link rel="modulepreload">` for
        the persona root's chunk and its unique static deps, so wave 2 joins
        wave 1. Hashed filenames are only known after the bundle is written,
        which is why this is a plugin and not a line in index.html.

   Result on the same measurement: ONE round trip, 174.08 kB gzip. Same bytes,
   two fewer serial fetches. The full before/after table is in the report that
   accompanied this change; re-derive it with `vite build --manifest`.
   ============================================================================= */

/**
 * Preload the persona root the returning user will actually open.
 *
 * WHY IT IS CONDITIONAL. Preloading the ops root for everybody would hand every
 * college login and every signed-out visitor ~10 kB gzip of a console they
 * cannot open — undoing, for those two, precisely what the root split in
 * App.tsx was for. So the injected script reads `bytexl-persona-root` from
 * localStorage (written by AuthProvider when a profile resolves, cleared on
 * sign-out) and preloads only the matching list. No key, no extra fetch, and
 * the app behaves exactly as it did before this plugin existed — which is the
 * right answer for a first-ever visit, where the root is genuinely unknown.
 *
 * THE KEY IS A LOAD HINT, NOT A PERMISSION. It decides which bytes arrive early
 * and nothing else: `Gate` in App.tsx picks the root from `profile.role` read
 * from the database, and RLS decides every row underneath (CLAUDE.md R5). A
 * forged value downloads a chunk that is then not used. See the long comment in
 * src/lib/preload.ts, which owns the key and the graph walk — imported here
 * rather than copied, so the two halves cannot drift apart.
 *
 * The links are built with `crossOrigin = ''` and `as = 'script'` to match what
 * Vite's own `__vitePreload` runtime emits. That matters: the runtime skips any
 * href already present as a link, so a matching tag is reused rather than
 * duplicated, and the modulepreload polyfill's MutationObserver picks these up
 * for browsers that need it.
 */
function personaRootPreload(): Plugin {
  const ROOTS: Record<string, string> = {
    ops: 'src/ops/OpsRoot.tsx',
    college: 'src/college/CollegeRoot.tsx',
  }
  let base = '/'

  return {
    name: 'bytexl:persona-root-preload',
    apply: 'build',
    configResolved(config) {
      base = config.base
    },
    transformIndexHtml: {
      order: 'post',
      handler(_html, ctx) {
        if (!ctx.bundle) return
        const chunks: PreloadChunk[] = Object.values(ctx.bundle)
          .filter((o): o is Extract<typeof o, { type: 'chunk' }> => o.type === 'chunk')
          .map((c) => ({
            fileName: c.fileName,
            facadeModuleId: c.facadeModuleId,
            isEntry: c.isEntry,
            imports: c.imports,
            css: c.viteMetadata ? [...c.viteMetadata.importedCss] : [],
          }))

        const byRoot: Record<string, string[]> = {}
        for (const [root, module] of Object.entries(ROOTS)) {
          const files = collectPreloadFiles(chunks, { rootModule: module, base })
          if (files.length) byRoot[root] = files
        }
        if (!Object.keys(byRoot).length) return

        // Kept small and defensive on purpose: this runs on every cold load,
        // before anything else, and a throw here would take the app with it.
        const script =
          `(function(){try{` +
          `var m=${JSON.stringify(byRoot)};` +
          `var f=m[localStorage.getItem(${JSON.stringify(PERSONA_ROOT_KEY)})];` +
          `if(!f)return;` +
          `for(var i=0;i<f.length;i++){` +
          `var c=f[i].slice(-4)===".css";` +
          `var l=document.createElement("link");` +
          `l.rel=c?"stylesheet":"modulepreload";if(!c)l.as="script";` +
          `l.crossOrigin="";l.href=f[i];` +
          `document.head.appendChild(l);}` +
          `}catch(e){}})();`

        return [{ tag: 'script', injectTo: 'head' as const, children: script }]
      },
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), personaRootPreload()],
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
