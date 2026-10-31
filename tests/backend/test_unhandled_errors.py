from fastapi.testclient import TestClient

from app.main import create_app


def test_unhandled_api_error_returns_request_id_and_logs_traceback(settings, caplog):
    app = create_app(settings)

    @app.get('/api/test-unhandled-error')
    def raise_unhandled_error():
        raise RuntimeError('synthetic regression exception')

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get('/api/test-unhandled-error')

    assert response.status_code == 500
    body = response.json()['error']
    assert body['code'] == 'internal_error'
    assert body['request_id']
    assert response.headers['x-request-id'] == body['request_id']
    assert 'synthetic regression exception' in caplog.text
    assert body['request_id'] in caplog.text
