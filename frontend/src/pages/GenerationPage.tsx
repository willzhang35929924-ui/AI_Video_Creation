import { Navigate, useLocation } from 'react-router-dom';

// Compatibility export. Production controls live in VideoProductionPage/VideoTaskPanel.
export function GenerationPage(){
  const location=useLocation();
  return <Navigate replace to={`/video-production${location.search}${location.hash||'#tasks'}`}/>;
}
