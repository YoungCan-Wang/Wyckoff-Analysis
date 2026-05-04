import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import './app.css'
import { AuthGuard } from '@/components/auth-guard'
import { AppLayout } from '@/routes/layout'
import { LoginPage } from '@/routes/login'
import { ChatPage } from '@/routes/chat'
import { WyckoffLoading } from '@/components/loading'

const PortfolioPage = lazy(() => import('@/routes/portfolio').then(m => ({ default: m.PortfolioPage })))
const TrackingPage = lazy(() => import('@/routes/tracking').then(m => ({ default: m.TrackingPage })))
const SettingsPage = lazy(() => import('@/routes/settings').then(m => ({ default: m.SettingsPage })))
const AnalysisPage = lazy(() => import('@/routes/analysis').then(m => ({ default: m.AnalysisPage })))
const TailBuyPage = lazy(() => import('@/routes/tail-buy').then(m => ({ default: m.TailBuyPage })))
const ExportPage = lazy(() => import('@/routes/export').then(m => ({ default: m.ExportPage })))
const FeatureGuidePage = lazy(() => import('@/routes/feature-guide').then(m => ({ default: m.FeatureGuidePage })))
const ScreenerPage = lazy(() => import('@/routes/screener').then(m => ({ default: m.ScreenerPage })))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1 },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Suspense fallback={<WyckoffLoading />}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route element={<AuthGuard />}>
              <Route element={<AppLayout />}>
                <Route index element={<Navigate to="/chat" replace />} />
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/portfolio" element={<PortfolioPage />} />
                <Route path="/tracking" element={<TrackingPage />} />
                <Route path="/analysis" element={<AnalysisPage />} />
                <Route path="/screener" element={<ScreenerPage />} />
                <Route path="/tail-buy" element={<TailBuyPage />} />
                <Route path="/export" element={<ExportPage />} />
                <Route path="/guide" element={<FeatureGuidePage />} />
                <Route path="/changelog" element={<Navigate to="/guide" replace />} />
                <Route path="/settings" element={<SettingsPage />} />
              </Route>
            </Route>
          </Routes>
        </Suspense>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
