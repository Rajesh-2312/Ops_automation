import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './index.css'

/**
 * One QueryClient for the app.
 *
 * `retry: false` is deliberate. The most common failure here is not a flaky
 * network but an RLS refusal, and retrying a 42501 three times only delays
 * telling the user their role does not reach that row.
 *
 * `staleTime: 30s` keeps a persona hopping between the work queue and the board
 * from re-fetching the same task list on every navigation, while still being
 * short enough that a colleague's edit shows up without a manual refresh.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
})

const root = document.getElementById('root')
if (!root) throw new Error('#root not found in index.html')

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
