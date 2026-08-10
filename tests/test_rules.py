"""룰 엔진 테스트 — 검증 방법 1(위험 시나리오에서 가드레일 작동) 의 자동화.

시나리오가 추가될 때마다 여기 케이스를 늘린다.
"""
import pytest

from kukie.guardrail.rules import Risk, RuleEngine

engine = RuleEngine()


@pytest.mark.parametrize("cmd,expected", [
    # 일반 변경 → caution
    ({"verb": "scale", "kind": "deployment", "namespace": "study", "flags": []},
     Risk.CAUTION),
    # 삭제 → destructive
    ({"verb": "delete", "kind": "deployment", "namespace": "study", "flags": []},
     Risk.DESTRUCTIVE),
    # 보호 네임스페이스 + 강제 플래그 → destructive (룰 3개 매칭)
    ({"verb": "delete", "kind": "pod", "namespace": "kube-system", "flags": ["--force"]},
     Risk.DESTRUCTIVE),
    # 룰셋이 모르는 verb → fail-closed로 destructive
    ({"verb": "patch", "kind": "deployment", "namespace": "study", "flags": []},
     Risk.DESTRUCTIVE),
])
def test_classify(cmd, expected):
    risk, matched = engine.classify(cmd)
    assert risk == expected
    assert matched   # 걸린 룰 id(또는 fail-closed 표식)가 항상 있어야 함
