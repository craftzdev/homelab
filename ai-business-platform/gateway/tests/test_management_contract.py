from app import config_releases

AUTH = {"Authorization": "Bearer test-gateway-credential"}


def test_management_contract_is_authenticated_and_versioned(client):
    assert client.get("/v1/config/contract").status_code == 401
    contract = client.get("/v1/config/contract", headers=AUTH).json()
    assert contract["api_version"] == "1.0"
    schema = client.get(contract["schema_url"], headers=AUTH)
    assert schema.status_code == 200
    assert all(path.startswith("/v1/config/") for path in schema.json()["paths"])
    assert "/v1/config/releases/{release_id}/promote" in schema.json()["paths"]


def test_unknown_state_is_readable_but_does_not_offer_mutations():
    assert all(not command["enabled"] for command in config_releases.commands({"state": "FUTURE_STATE"}))
    assert config_releases.commands({"state": "VERIFIED"})[0]["enabled"]
