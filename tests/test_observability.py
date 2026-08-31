"""관측 계측 — opt-in 이고, 실패해도 에이전트를 막지 않는다.

계측은 본업의 곁가지다. 조건이 없으면 조용히 꺼져 있어야 하고(테스트·평상시 실행이
바깥으로 데이터를 보내면 안 된다), 설정이 실패해도 예외가 새어 나오면 안 된다.
"""
import pytest

from kukie import observability


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("LOGFIRE_TOKEN", "OTEL_EXPORTER_OTLP_ENDPOINT", "KUKIE_TRACE_CONTENT"):
        monkeypatch.delenv(key, raising=False)


def test_설정이_없으면_계측을_켜지_않는다(monkeypatch):
    """평상시·테스트에서 켜지면 안 된다 — logfire 를 import 조차 하지 않아야 한다.

    logfire.configure 를 패치하는 방식은 이 계약을 못 지킨다: 테스트가 먼저 logfire 를
    import 해버리면 sys.modules 에 남아, 구현이 모듈 수준 import 로 바뀌어도 통과한다
    (CodeRabbit 지적). import 자체를 막아 "시도조차 없음"을 검증한다.
    """
    import builtins

    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name == "logfire":
            raise AssertionError("계측이 꺼진 상황에서 logfire 를 import 했다")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    assert observability.setup() is False


@pytest.mark.parametrize("key,value", [
    ("LOGFIRE_TOKEN", "pylf_test"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
])
def test_토큰이나_엔드포인트가_있으면_켠다(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    seen = {}

    import logfire
    monkeypatch.setattr(logfire, "configure", lambda **kw: seen.update(kw))
    monkeypatch.setattr(logfire, "instrument_pydantic_ai",
                        lambda *a, **kw: seen.update(instrument=kw))

    assert observability.setup() is True
    assert seen["service_name"] == "kukie"
    # 토큰이 없는 구성(OTLP 만)에서는 SaaS 로 보내지 않는다
    assert seen["send_to_logfire"] is (key == "LOGFIRE_TOKEN")


def test_내용_포함은_환경변수로_끄고_켠다(monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_test")
    seen = {}

    import logfire
    monkeypatch.setattr(logfire, "configure", lambda **kw: None)
    monkeypatch.setattr(logfire, "instrument_pydantic_ai",
                        lambda *a, **kw: seen.update(kw))

    observability.setup()
    assert seen["include_content"] is False        # 기본은 내용 제외

    monkeypatch.setenv("KUKIE_TRACE_CONTENT", "1")
    observability.setup()
    assert seen["include_content"] is True


def test_계측_설정이_실패해도_예외가_새지_않는다(monkeypatch, caplog):
    """관측이 에이전트를 멈춰 세우면 안 된다 — 경고만 남기고 False 를 돌려준다."""
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_test")

    def boom(**kw):
        raise RuntimeError("수집 서버 연결 실패")

    import logfire
    monkeypatch.setattr(logfire, "configure", boom)

    assert observability.setup() is False
    assert any("observability setup failed" in r.message for r in caplog.records)
