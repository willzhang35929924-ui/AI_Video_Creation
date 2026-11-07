import { Component, type ErrorInfo, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { DashboardPage } from "./pages/DashboardPage";
import { StoryboardPage } from "./pages/StoryboardPage";
import { CharactersPage } from "./pages/CharactersPage";
import { CharacterFactoryPage } from './pages/CharacterFactoryPage';
import { ReviewPage } from "./pages/ReviewPage";
import { PublishingPage } from "./pages/PublishingPage";
import { SettingsPage } from "./pages/SettingsPage";
import { FinalPage } from "./pages/FinalPage";
import { AssemblyPage } from './pages/AssemblyPage';
import { ContentPage } from './pages/ContentPage';
import { StudioPage } from './pages/StudioPage';
import { ScreenplayGenerationPage } from './pages/ScreenplayGenerationPage';
import { ProductLibraryPage } from './pages/ProductLibraryPage';
import { ReferenceVideoLibraryPage } from './pages/ReferenceVideoLibraryPage';
import { VoiceLibraryPage } from './pages/VoiceLibraryPage';

interface BoundaryState {
  error: Error | null;
}

class AppErrorBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep this deliberately free of request payloads and credentials.
    console.error("Frontend render error", error.name, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <main className="fatal-error">
          <div>
            <p className="eyebrow">界面错误</p>
            <h1>这个页面暂时无法显示</h1>
            <p>{this.state.error.message}</p>
            <button className="button button-primary" type="button" onClick={() => window.location.reload()}>
              重新加载
            </button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}

function LegacyWorkspaceRedirect({ target }: { target: '/storyboard' | '/review' }) {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  params.delete('workspace');
  const search = params.toString();
  return <Navigate replace to={`${target}${search ? `?${search}` : ''}${location.hash}`}/>;
}

export default function App() {
  return (
    <AppErrorBoundary>
      <AppShell>
        <Routes>
          <Route path="/" element={<StudioPage />} />
          <Route path="/screenplay-generation" element={<ScreenplayGenerationPage />} />
          <Route path="/assets/products" element={<ProductLibraryPage />} />
          <Route path="/assets/videos" element={<ReferenceVideoLibraryPage />} />
          <Route path="/assets/voices" element={<VoiceLibraryPage />} />
          <Route path="/studio-review" element={<LegacyWorkspaceRedirect target="/review" />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/storyboard" element={<StoryboardPage />} />
          <Route path="/characters" element={<CharacterFactoryPage />} />
          <Route path="/characters/legacy" element={<CharactersPage />} />
          <Route path="/characters/:characterId" element={<CharacterFactoryPage />} />
          <Route path="/generation" element={<LegacyWorkspaceRedirect target="/storyboard" />} />
          <Route path="/video-production" element={<LegacyWorkspaceRedirect target="/storyboard" />} />
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/final" element={<AssemblyPage />} />
          <Route path="/final/legacy" element={<FinalPage />} />
          <Route path="/publishing" element={<PublishingPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/settings/ai-services" element={<SettingsPage />} />
          <Route path="/settings/:section" element={<SettingsPage />} />
          <Route path="/content" element={<ContentPage />} />
          <Route path="/production" element={<LegacyWorkspaceRedirect target="/storyboard" />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AppShell>
    </AppErrorBoundary>
  );
}
