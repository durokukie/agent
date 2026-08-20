"""툴별 kubectl args 조립 함수 — 여기 한 벌만 존재한다.

가드레일 훅(①단계)이 승인 화면용으로, 툴 본체가 실행용으로 각각 호출한다.
둘이 같은 함수·같은 입력이라 결과가 항상 같다 → "승인된 명령 = 실행된 명령".
그래서 조립은 **순수 함수**여야 한다 — 시각·랜덤·파일 생성 같은 부수효과 금지.
(apply의 매니페스트는 임시파일 대신 `-f -` + stdin으로 넘겨 이 원칙을 지킨다.)
조립 로직을 다른 곳에 한 벌 더 만드는 것도 금지 (기능 2 §1.1 "재조립 금지").
"""
from __future__ import annotations


def _guard(args: dict, *fields: str) -> None:
    """값 자리에 플래그 모양(-로 시작) 값이 오면 거부한다.

    shell=False로 셸 인젝션은 막았지만, kubectl 자체의 옵션 파싱은 별개다 —
    예: name="--all"이면 `delete deployment --all`이 되어 해당 종류 전체가 삭제된다.
    LLM이 채우는 값(kind/name/namespace)은 리소스 식별자여야 하므로 '-' 시작은
    정상 입력에 존재하지 않는다. (CodeRabbit 지적 반영)
    """
    for f in fields:
        v = args.get(f)
        if isinstance(v, str) and v.startswith("-"):
            raise ValueError(f"{f}에 플래그 형태의 값은 허용되지 않는다: {v!r}")


def assemble(tool_name: str, args: dict) -> list[str]:
    """툴 이름 + 구조화 인자 → kubectl args 리스트 ("kubectl" 제외)."""
    _guard(args, "kind", "name", "namespace")
    fn = _ASSEMBLERS[tool_name]
    return fn(args)


def _delete_resource(a: dict) -> list[str]:
    return ["delete", a["kind"], a["name"], "-n", a["namespace"]]


def _scale_resource(a: dict) -> list[str]:
    return ["scale", a["kind"], a["name"], f"--replicas={a['replicas']}", "-n", a["namespace"]]


def _apply_manifest(a: dict) -> list[str]:
    # 매니페스트 본문은 args에 안 넣는다 — run_kubectl(stdin=manifest_yaml)로 따로 전달.
    # 임시파일을 만들면 조립이 순수하지 않게 되고(호출마다 경로가 달라짐)
    # 승인 화면 명령과 실행 명령이 어긋날 수 있다.
    args = ["apply", "-f", "-"]
    if a.get("namespace"):
        args += ["-n", a["namespace"]]
    return args


def _rollout_restart(a: dict) -> list[str]:
    return ["rollout", "restart", f"{a['kind']}/{a['name']}", "-n", a["namespace"]]


_ASSEMBLERS = {
    "delete_resource": _delete_resource,
    "scale_resource": _scale_resource,
    "apply_manifest": _apply_manifest,
    "rollout_restart": _rollout_restart,
}
