from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import httpx
from remnawave import RemnawaveSDK

from app.bot.utils.remnawave import RemnawaveLookupError, fetch_user_info
from app.config import RemnawaveConfig


def _config() -> RemnawaveConfig:
    return RemnawaveConfig(
        API_BASE="https://panel.example.com/api",
        API_TOKEN="token",
        CADDY_TOKEN=None,
        SSL_IGNORE=False,
    )


def _sdk_with_users(users: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        users=SimpleNamespace(
            get_users_stream=AsyncMock(return_value=SimpleNamespace(users=users)),
        ),
        nodes=SimpleNamespace(get_one_node=AsyncMock()),
        external_squads=SimpleNamespace(get_external_squad_by_uuid=AsyncMock()),
        hwid=SimpleNamespace(
            get_hwid_user=AsyncMock(
                return_value=SimpleNamespace(
                    total=1,
                    devices=[SimpleNamespace(
                        device_model="iPhone",
                        platform="iOS",
                        os_version="18",
                        user_agent=None,
                        hwid="device-hwid",
                    )],
                )
            ),
        ),
        bandwidthstats=SimpleNamespace(
            get_stats_user_usage=AsyncMock(
                return_value=SimpleNamespace(
                    response=SimpleNamespace(
                        sparkline_data=[1024],
                        top_nodes=[],
                        series=[],
                    )
                )
            ),
        ),
        _client=SimpleNamespace(aclose=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_fetch_user_info_uses_remnawave_3_2_numeric_user_id():
    created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    first_connected_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
    online_at = datetime(2025, 1, 3, tzinfo=timezone.utc)
    user = SimpleNamespace(
        id=42,
        username="test_user",
        telegram_id=123456,
        status="ACTIVE",
        created_at=created_at,
        expire_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        subscription_url="https://panel.example.com/sub/test",
        hwid_device_limit=3,
        external_squad_uuid=None,
        active_internal_squads=[SimpleNamespace(name="Germany")],
        user_traffic=SimpleNamespace(
            used_traffic_bytes=1024,
            lifetime_used_traffic_bytes=2048,
            online_at=online_at,
            first_connected_at=first_connected_at,
            last_connected_node_uuid=None,
        ),
    )
    sdk = _sdk_with_users([user])

    with patch("app.bot.utils.remnawave.RemnawaveSDK", return_value=sdk):
        info = await fetch_user_info(_config(), telegram_id=123456)

    assert info is not None
    assert info.user_id == 42
    assert info.created_at == created_at
    assert info.first_connected_at == first_connected_at
    assert info.last_connected_at == online_at
    assert info.devices_count == 1
    assert info.devices_names == ["iPhone 18"]
    sdk.users.get_users_stream.assert_awaited_once_with(
        size=1000,
        telegram_id="123456",
    )
    sdk.hwid.get_hwid_user.assert_awaited_once_with(42)
    sdk.bandwidthstats.get_stats_user_usage.assert_awaited_once()
    assert sdk.bandwidthstats.get_stats_user_usage.await_args.args == (42,)
    sdk._client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_user_info_returns_none_only_when_user_is_absent():
    sdk = _sdk_with_users([])

    with patch("app.bot.utils.remnawave.RemnawaveSDK", return_value=sdk):
        info = await fetch_user_info(_config(), telegram_id=999)

    assert info is None
    sdk.hwid.get_hwid_user.assert_not_awaited()
    sdk._client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_user_info_does_not_mask_api_errors_as_user_not_found():
    sdk = _sdk_with_users([])
    sdk.users.get_users_stream.side_effect = RuntimeError("panel is unavailable")

    with patch("app.bot.utils.remnawave.RemnawaveSDK", return_value=sdk):
        with pytest.raises(RemnawaveLookupError):
            await fetch_user_info(_config(), telegram_id=123456)

    sdk._client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_user_info_matches_real_remnawave_3_2_http_contract():
    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/stream"):
            return httpx.Response(
                200,
                json={
                    "response": {
                        "users": [{
                            "id": 42,
                            "shortUuid": "short-id",
                            "username": "contract_user",
                            "status": "ACTIVE",
                            "expireAt": "2026-01-01T00:00:00Z",
                            "telegramId": 123456,
                            "trojanPassword": "password",
                            "vlessUuid": "00000000-0000-4000-8000-000000000001",
                            "ssPassword": "password",
                            "createdAt": "2025-01-01T00:00:00Z",
                            "updatedAt": "2025-01-03T00:00:00Z",
                            "subscriptionUrl": "https://panel.example.com/sub/short-id",
                            "activeInternalSquads": [],
                            "userTraffic": {
                                "usedTrafficBytes": 1024,
                                "lifetimeUsedTrafficBytes": 2048,
                                "onlineAt": "2025-01-03T00:00:00Z",
                                "firstConnectedAt": "2025-01-02T00:00:00Z",
                                "lastConnectedNodeUuid": None,
                            },
                        }],
                        "nextCursor": None,
                        "hasMore": False,
                    }
                },
            )
        if request.url.path.endswith("/hwid/devices/42"):
            return httpx.Response(200, json={"response": {"total": 0, "devices": []}})
        if request.url.path.endswith("/bandwidth-stats/users/42"):
            return httpx.Response(
                200,
                json={
                    "response": {
                        "categories": ["2025-01-03"],
                        "sparklineData": [1024],
                        "topNodes": [],
                        "series": [],
                    }
                },
            )
        return httpx.Response(404, json={"message": "unexpected test request"})

    client = httpx.AsyncClient(
        base_url="https://panel.example.com/api",
        transport=httpx.MockTransport(handle_request),
    )
    sdk = RemnawaveSDK(client=client)

    with patch("app.bot.utils.remnawave.RemnawaveSDK", return_value=sdk):
        info = await fetch_user_info(_config(), telegram_id=123456)

    assert info is not None
    assert info.user_id == 42
    assert info.first_connected_at == datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert [request.url.path for request in requests] == [
        "/api/users/stream",
        "/api/hwid/devices/42",
        "/api/bandwidth-stats/users/42",
    ]
    assert requests[0].url.params["telegramId"] == "123456"
    assert requests[0].url.params["size"] == "1000"
