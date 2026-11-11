"""tests/clusters/gpu 공용 픽스처"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kukie_agent import server


@pytest.fixture
def client(dev_auth, fake_server):
    return TestClient(server.app)
