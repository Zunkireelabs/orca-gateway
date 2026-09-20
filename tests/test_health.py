from fastapi.testclient import TestClient

from orca_gateway.main import app

client = TestClient(app)


def test_health_reports_status_and_sha_without_touching_a_backend(monkeypatch) -> None:
    from orca_gateway import deps

    monkeypatch.setattr(deps, "get_backend", lambda: (_ for _ in ()).throw(AssertionError))
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and "sha" in body


def test_no_docs_or_schema_routes() -> None:
    for path in ("/docs", "/redoc", "/openapi.json", "/v1/turn"):
        assert client.get(path).status_code == 404
