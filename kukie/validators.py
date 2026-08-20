"""출력 검증 — 현재 비어 있음 (역할은 DURO-44에서 확정).

원래 이 PR에서 "explanations 비면/플래그 빠지면 ModelRetry" 검증기를 구현했으나,
explanations를 LLM이 아니라 코드(FLAG_GLOSSARY 사전)가 채우는 방향이 확정되어
(GitHub #20 · Linear DURO-44) LLM 감시형 검증기는 넣지 않는다 —
코드가 채우는 값을 검증하는 것은 무의미하다.

DURO-44에서의 새 역할: 사전에 없는 플래그를 감지해 로그로 남긴다
(반려가 아니라 사전 보강 목록용). 그때까지 explanations는 프롬프트 유도로만
채워진다 — 이 공백 기간에는 실사용 경로(CLI는 DURO-43)가 없어 위험 없음.
"""
from __future__ import annotations
