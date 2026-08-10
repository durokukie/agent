"""kubectl 실행 계층.

원칙 (기능 1 §1):
- LLM은 구조화된 인자만 제출한다. 명령 문자열을 만들지 않는다.
- 조립 함수는 툴별 1벌만 존재한다 (assemble.py). 재조립 금지.
- 실행은 shell=False + args 리스트 (인젝션 차단).
"""
from kukie.kubectl.runner import KubectlResult, run_kubectl
from kukie.kubectl.assemble import assemble

__all__ = ["KubectlResult", "run_kubectl", "assemble"]
