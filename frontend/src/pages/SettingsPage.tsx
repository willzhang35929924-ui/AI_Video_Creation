import { AIServicesPage } from './AIServicesPage';
import { QualityProfileEditor } from '../components/QualitySettings';

export function SettingsPage() {
  return <div className="simple-settings">
    <header className="page-header"><h1>系统设置</h1></header>
    <div className="settings-content-grid">
      <section className="settings-services" aria-label="服务档案"><AIServicesPage embedded /></section>
      <QualityProfileEditor scope="system" targetId="default" title="制作画质" showDetails={false} />
    </div>
  </div>;
}
