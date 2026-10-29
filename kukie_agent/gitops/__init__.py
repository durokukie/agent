"""GitOps PR 길 — ArgoCD 가 관리하는 리소스를 kubectl 대신 매니페스트 레포 PR 로 바꾼다 (DURO-134).

설계: Linear 허브 문서 "[설계] GitOps 클러스터" 4절, 주제 문서 "[GitOps] PR 길 판단 · kubectl 경고".

    route.py          리소스마다 PR 길인지 kubectl 길인지 정한다 (판단 6절). 승인 순간에도 다시 부른다
    repo.py           GitHub 레포 주소 → 소문자 `owner/repo` (kukie-server RepoName 과 같은 규칙)
    github.py         GitHub REST — 파일 받기 · 브랜치·커밋·PR · 머지 · 닫기. 토큰은 server 가 준 레포 범위 1시간 토큰
    watch.py          머지 뒤 Application 의 sync·health 지켜보기 (검증 09)
    pull_requests.py  propose_git_change 의 몸통 — PR 만들기(승인 전), 머지하기(승인 뒤), 닫기(거절)

고칠 곳 찾기·렌더 비교는 kukie_agent/manifests (DURO-133), 승인·기록 파이프라인은 guardrail/gitops_hook.py 다.
"""
