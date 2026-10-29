"""PR 길 변경 툴 — ArgoCD 가 관리하는 리소스를 매니페스트 레포 PR 로 바꾼다 (DURO-134).

기존 변경 4종(tools/mutate.py)처럼 Shared 대화에만 열린다. 승인 전 준비(고칠 곳 찾기·렌더 비교·브랜치·커밋·PR)와
승인 뒤 실행(머지·sync 지켜보기)은 전부 guardrail/gitops_hook.py 가 한다 — 툴 본체는 훅 없이 불리면 거절한다(fail-closed).
kubectl 툴과 달리 본체가 실행하지 않는 이유: 머지에는 승인 전에 만든 PR(번호·승인한 파일 내용)이 필요하고, 그건 툴 인자가
아니라 Plan 에 있다.
"""
from __future__ import annotations

from pydantic_ai import RunContext

from kukie_agent.deps import Deps
from kukie_agent.kubectl import KubectlResult
from kukie_agent.manifests import Change
from kukie_agent.tools.mutate import Risk

TOOL_NAME = "propose_git_change"


class GitOpsResult(KubectlResult):
    """승인 뒤 머지 결과. 화면 블록(response.collect_steps)이 KubectlResult 로 그리도록 같은 칸에 더한다.

    command 는 `github pr merge <owner/repo>#<번호>` 모양의 표시용 문자열이다 — 실제로 돈 kubectl 은 없다.
    """
    pull_request: dict | None = None        # {number, url}
    merge_commit: str | None = None
    sync_result: dict | None = None         # gitops/watch.py 의 결과 모양


def propose_git_change(ctx: RunContext[Deps], kind: str, name: str, change: Change,
                       intent: str, expected_effects: list[str], side_effects: list[str],
                       namespace: str | None = None) -> GitOpsResult:
    """ArgoCD 가 관리하는 리소스를 클러스터에 직접 쓰지 않고, ArgoCD 가 보는 GitHub 레포에 PR 을 올려 바꾼다.

    리소스 조회 결과에 "변경은 PR 길" 이라고 나온 리소스(ArgoCD 관리 + 레포 연결됨)를 바꿀 때 kubectl 변경 툴 대신 쓴다.
    바꿀 수 있는 것은 넷이다 — change 의 what 으로 고른다:
      replicas(count) · image_tag(tag, container) · resources(container, requests, limits) · env(container, values, remove)
    그 밖의 변경, ArgoCD 가 관리하지 않는 리소스, 레포가 연결되지 않은 리소스는 kubectl 변경 툴로 한다 —
    잘못 부르면 시스템이 이유와 함께 다시 하라고 알려 준다.
    시스템이 고칠 파일을 찾아 렌더링으로 확인하고 PR 을 연 뒤 승인 카드를 띄운다. 승인하면 PR 을 머지하고
    ArgoCD sync 가 끝날 때까지 지켜본 결과를 돌려준다 — 클러스터는 머지 뒤 ArgoCD 가 sync 해야 바뀐다.
    intent/expected_effects/side_effects 는 사용자가 이해할 수 있는 말로 채운다 — 승인 카드와 PR 본문에 실린다.
    """
    raise RuntimeError("propose_git_change 는 가드레일 훅(guardrail/gitops_hook.py)을 거쳐서만 실행된다")


GITOPS_TOOLS = [propose_git_change]
RISK = Risk.CAUTION
