"""설명 필드 강제 검증 — 검증 방법 1-3의 핵심: "설명이 항상 채워지는가".

TestModel로 결정론적으로 돌린다 (실제 LLM 호출 없음).
"""
import pytest

# TODO:
# 1. steps에 명령이 있는데 explanations가 비면 → ModelRetry 발생 확인
# 2. 명령의 플래그가 설명에 누락되면 → ModelRetry 발생 확인
# 3. 정상 응답은 통과 확인
#
# from pydantic_ai.models.test import TestModel
# with agent.override(model=TestModel()):
#     ...
