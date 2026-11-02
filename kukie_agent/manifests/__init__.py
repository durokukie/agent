"""GitOps — ArgoCD 가 관리하는 리소스를 매니페스트 레포에서 고칠 곳 찾기 + 렌더 비교 (DURO-133).

원칙: 고칠 곳은 코드(나중에 Helm 은 AI)가 고르고, 맞는지는 코드가 렌더링해서 확인한다.
설계: Linear 문서 "[GitOps] 고칠 곳 찾기 · 렌더 비교". 레포를 받아 오기·PR 만들기·툴·승인 카드는 이 패키지 밖이다.
"""
from kukie_agent.manifests.change import plan_change, verify_change
from kukie_agent.manifests.models import (
    AppSource, Change, Env, ImageTag, NewResource, Other, Ready, Refused, Replicas, Resources, Result, RiskWarning,
    Target,
)
from kukie_agent.manifests.new_resource import already_in_cluster, plan_new_resource, representative

__all__ = [
    "AppSource", "Change", "Env", "ImageTag", "NewResource", "Other", "Ready", "Refused", "Replicas", "Resources",
    "Result", "RiskWarning", "Target", "already_in_cluster", "plan_change", "plan_new_resource", "representative",
    "verify_change",
]
