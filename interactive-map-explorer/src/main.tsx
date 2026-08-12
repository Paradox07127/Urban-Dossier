import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import MethodologyPage from './MethodologyPage.tsx';
import './index.css';

const pathname = window.location.pathname.replace(/\/+$/, '') || '/';
const RootComponent = pathname === '/methodology' ? MethodologyPage : App;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RootComponent />
  </StrictMode>,
);
