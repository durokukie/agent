import pytest
from pydantic_ai import models


models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _member_server_off_by_default(monkeypatch):
    """kukie 를 import 하면 load_dotenv(".env") 가 개발자 .env 의 KUKIE_MEMBER_URL 을 환경에 올린다.

    그대로 두면 같은 테스트가 .env 가 있는 컴퓨터에서만 회원 서버 모드로 돈다 (CI 에는 .env 가 없다).
    flat 엔드포인트가 회원 서버 모드에서 닫히면서(#75) 실제로 갈렸다. 회원 서버 모드가 필요한 테스트는
    직접 켠다 (test_membership.py 의 spring 픽스처).
    """
    monkeypatch.delenv("KUKIE_MEMBER_URL", raising=False)
    monkeypatch.delenv("KUKIE_ACCESS_COOKIE", raising=False)  # 쿠키 이름도 .env 에서 올라올 수 있다 (#79)
    # 개발 모드 스위치도 .env 에서 올라온다. flat 엔드포인트가 이제 이 스위치로만 열리므로(#82) 필요한 테스트가 직접 켠다
    monkeypatch.delenv("KUKIE_DEV_AUTH", raising=False)
