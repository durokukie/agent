"""kubectl 플래그·서브커맨드 사전 — explanations를 코드가 채우는 근거 (DURO-44, A안).

LLM이 매번 설명을 새로 쓰지 않는다. 실제 실행된 명령에서 토큰을 뽑아 이 사전으로
설명을 붙인다 → 표현 일관, 오답 불가, 토큰 절약. 사전은 서버 한 곳에만 있고
렌더러(CLI·앱)는 받아서 그리기만 한다.

사전에 없는 토큰은 반려하지 않고 validators.log_unregistered_flags로 기록만 한다
(사전 보강 목록용).

검수 필요: 아래 설명 문구는 k8s 경험자 리뷰 대상이다 ("상상 금지 구역").
툴이 실제로 만들어내는 토큰만 등재한다 — 쓰지 않는 플래그를 미리 채워두지 않는다.
"""
from __future__ import annotations

import shlex

from kukie.skills.base import FieldExplanation

# 값을 하나 더 먹는 플래그 — 설명 칸에 "플래그 값" 형태로 묶어 보여준다.
VALUE_FLAGS: frozenset[str] = frozenset({"-n", "-o", "-c", "-f", "--context"})

# "플래그 값" 조합에 더 정확한 설명이 있으면 그쪽을 우선한다.
FLAG_GLOSSARY: dict[str, str] = {
    # ── 공통 ──
    "--context": "kubeconfig에 등록된 여러 클러스터 중 어느 것에 접속할지 지정 (세션 시작 시 확정)",
    "-n": "대상 네임스페이스 지정. 생략하면 kubeconfig의 현재 네임스페이스가 쓰인다",
    "--all-namespaces": "네임스페이스 구분 없이 클러스터 전체에서 조회",
    "-o": "출력 형식 지정",
    "-o wide": "기본 표에 노드 이름·파드 IP 등 열을 추가해 자세히 출력",
    "-o yaml": "리소스 전체를 YAML로 출력 (모든 필드 확인용)",
    "--dry-run=server": "실제로 적용하지 않고 서버(API 서버)가 검증만 수행 — 가드레일의 모의 실행",
    # ── 읽기 툴이 만드는 토큰 ──
    "get": "리소스 목록·요약 조회",
    "describe": "리소스 하나의 상세 상태(조건·이벤트·설정) 조회",
    "logs": "파드 안 컨테이너가 출력한 로그 조회",
    "explain": "리소스·필드의 공식 스키마 설명 조회 (클러스터 상태와 무관)",
    "--sort-by=.lastTimestamp": "마지막 발생 시각 기준으로 정렬 — 최근 이벤트가 아래로",
    "--tail": "마지막 N줄만 출력 (로그 양 제한)",
    "-c": "파드에 컨테이너가 여러 개일 때 어느 컨테이너의 로그인지 지정",
    "--previous": "재시작되기 전, 직전 컨테이너의 로그 조회 (CrashLoopBackOff 원인 확인용)",
    # ── 변경 툴이 만드는 토큰 ──
    "delete": "리소스 삭제 — 되돌릴 수 없다",
    "scale": "Deployment 등의 레플리카(복제본) 수 조정",
    "--replicas": "목표 레플리카 수",
    "apply": "매니페스트(YAML)에 적힌 상태로 리소스를 생성·갱신 (선언형)",
    "-f": "매니페스트 파일 지정",
    "-f -": "매니페스트를 파일 대신 표준입력(stdin)으로 전달",
    "rollout": "배포 이력·상태 관련 작업",
    "restart": "파드를 순차 교체해 재시작 (이미지·설정 다시 읽기)",
}


def explain_command(command: str) -> tuple[list[FieldExplanation], list[str]]:
    """실행된 명령 문자열 → (설명 목록, 사전에 없던 토큰 목록).

    토큰 규칙:
    - "kubectl" 자체는 건너뛴다.
    - "--key=value" 는 "--key" 로 사전을 찾고, 표시는 원문 그대로.
    - VALUE_FLAGS 는 다음 토큰을 값으로 묶는다 ("-n study"). "플래그 값" 조합 항목이
      사전에 있으면 그것을 우선한다 ("-o wide").
    - 플래그가 아닌 토큰은 서브커맨드 사전에서 찾고, 없으면 리소스 이름 등으로 보고 무시한다.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    explanations: list[FieldExplanation] = []
    unknown: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if tok == "kubectl":
            continue

        if tok.startswith("-"):
            key = tok.split("=", 1)[0]
            shown = tok
            if key in VALUE_FLAGS and "=" not in tok and i < len(tokens):
                value = tokens[i]
                i += 1
                shown = f"{tok} {value}"
                combined = FLAG_GLOSSARY.get(shown)
                if combined is not None:
                    explanations.append(FieldExplanation(field=shown, meaning=combined))
                    continue
            # "--dry-run=server" 처럼 값까지 포함한 항목이 있으면 우선, 없으면 "--key" 로
            meaning = FLAG_GLOSSARY.get(tok) if "=" in tok else None
            if meaning is None:
                meaning = FLAG_GLOSSARY.get(key)
            if meaning is None:
                unknown.append(key)
            else:
                explanations.append(FieldExplanation(field=shown, meaning=meaning))
            continue

        # 플래그가 아닌 토큰: 서브커맨드만 설명한다 (리소스 종류·이름은 설명 대상 아님)
        meaning = FLAG_GLOSSARY.get(tok)
        if meaning is not None:
            explanations.append(FieldExplanation(field=tok, meaning=meaning))

    return explanations, unknown
