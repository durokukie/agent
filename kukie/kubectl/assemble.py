"""툴별 kubectl args 조립 함수 — 여기 한 벌만 존재한다.

가드레일 훅(②단계)이 호출하고, 조립 결과가 판정→승인→실행까지
그대로 전달된다. 다른 곳에서 재조립하면 "승인된 명령 ≠ 실행된 명령"이
될 수 있으므로 금지 (기능 2 §1.1).
"""
from __future__ import annotations


def assemble(tool_name: str, args: dict) -> list[str]:
    """툴 이름 + 구조화 인자 → kubectl args 리스트 ("kubectl" 제외)."""
    fn = _ASSEMBLERS[tool_name]
    return fn(args)


def _delete_resource(a: dict) -> list[str]:
    return ["delete", a["kind"], a["name"], "-n", a["namespace"]]


def _scale_resource(a: dict) -> list[str]:
    return ["scale", a["kind"], a["name"], f"--replicas={a['replicas']}", "-n", a["namespace"]]


def _apply_manifest(a: dict) -> list[str]:
    # TODO: 매니페스트를 임시 파일로 저장 후 -f 경로 전달
    raise NotImplementedError


def _rollout_restart(a: dict) -> list[str]:
    return ["rollout", "restart", f"{a['kind']}/{a['name']}", "-n", a["namespace"]]


_ASSEMBLERS = {
    "delete_resource": _delete_resource,
    "scale_resource": _scale_resource,
    "apply_manifest": _apply_manifest,
    "rollout_restart": _rollout_restart,
}
