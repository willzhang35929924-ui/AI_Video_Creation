from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount
from app.main import FrontendFiles


def test_asset_libraries_refresh_on_production_host_while_missing_bundles_stay_404(tmp_path):
    (tmp_path/'index.html').write_text('<html>real-spa-entry</html>','utf-8')
    app=Starlette(routes=[Mount('/',app=FrontendFiles(directory=tmp_path,html=True))])
    with TestClient(app) as client:
        for route in ['/assets/products','/assets/videos','/assets/voices']:
            response=client.get(route)
            assert response.status_code==200 and 'real-spa-entry' in response.text
        assert client.get('/assets/missing-script.js').status_code==404
        assert client.get('/assets/missing.css').status_code==404
        assert client.get('/api/missing').status_code==404
