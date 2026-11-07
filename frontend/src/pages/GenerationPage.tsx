import { Navigate, useLocation } from 'react-router-dom';

// Compatibility export for callers that still import this retired page.
export function GenerationPage(){
  const location=useLocation();
  return <Navigate replace to={`/storyboard${location.search}${location.hash}`}/>;
}
