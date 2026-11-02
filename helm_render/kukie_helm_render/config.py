"""설정 — 값은 전부 시작값이다. 환경변수 HELM_RENDER_<이름> 으로 바꾼다.

시간 · 동시 실행 · 출력 · 메모리는 공개 차트 21개 실측(렌더링 중앙값 0.083초, 최대 0.6초, 메모리 최대 137MB, 출력 최대 5.7MB)으로 잡았다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

MIB = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    helm_bin: str = "helm"
    #: 요청마다 임시 폴더를 만드는 곳. 파드에서는 emptyDir 이다 (파일 시스템은 읽기 전용)
    work_dir: Path = Path(os.environ.get("TMPDIR", "/tmp")) / "helm-render"

    # 렌더링
    max_concurrent: int = 2
    timeout_seconds: float = 5.0
    max_output_bytes: int = 20 * MIB
    #: helm 한 프로세스의 메모리. 넘으면 그 프로세스만 끝낸다. 실측 최대 137MB. 2개(400MiB) + 파이썬(측정 50~72MiB)이 파드 제한 512Mi 안에 든다.
    #: 큰 요청과 큰 출력의 버퍼까지 겹치면 넘을 수 있고, 그때는 이 파드만 재시작된다
    max_rss_bytes: int = 200 * MIB
    max_failure_text: int = 2000

    # 요청과 차트
    #: 본문을 다 받기까지의 시간. 자리를 잡은 채 멈춘 연결이 렌더링을 계속 바쁘게 만들지 못하게 한다
    body_timeout_seconds: float = 10.0
    max_body_bytes: int = 32 * MIB
    max_chart_bytes: int = 16 * MIB
    #: 압축을 푼 합계. 안쪽 charts/*.tgz 를 푼 크기까지 센다
    max_unpacked_bytes: int = 64 * MIB
    max_chart_files: int = 5000
    max_chart_depth: int = 4
    max_schema_bytes: int = 2 * MIB
    max_value_files: int = 32
    max_values_bytes: int = 4 * MIB

    @classmethod
    def from_env(cls) -> Settings:
        values = {}
        for field in fields(cls):
            raw = os.environ.get(f"HELM_RENDER_{field.name.upper()}")
            if raw is None:
                continue
            kind = {"int": int, "float": float, "str": str, "Path": Path}[field.type]
            values[field.name] = kind(raw)
        return cls(**values)
