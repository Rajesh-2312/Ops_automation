/// <reference types="vite/client" />

/**
 * Typed environment. Only VITE_-prefixed variables reach the browser bundle,
 * which is a useful guardrail: SUPABASE_SERVICE_ROLE_KEY and OPENROUTER_API_KEY
 * live in the repo-root .env without the prefix and therefore cannot be
 * referenced from here even by mistake.
 */
interface ImportMetaEnv {
  readonly VITE_SUPABASE_URL: string
  readonly VITE_SUPABASE_ANON_KEY: string
  /** FastAPI base URL. Empty string means "same origin". */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
