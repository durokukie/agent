"""렌더링 파드 시험 — 일회용 kind 의 진짜 에이전트 파드 안에서 돌린다 (pytest 가 모으지 않는다).

  kubectl -n kukie-dev exec -i deploy/agent -- env MODE=bad python - < tests/helm_render_e2e.py

MODE: bad(나쁜 차트 · 동시 3개), oom(HELM_RENDER_MAX_RSS_BYTES 를 크게 둔 뒤 메모리 폭주), kill(도는 중 파드 삭제), down(파드 0개).
매번 에이전트 /health 를 함께 본다. 끝난 뒤 에이전트 파드의 재시작 수가 0 이어야 한다.
"""
import gzip, io, os, tarfile, threading, time, urllib.request
from kukie_agent.manifests.helm import RenderConditions, ValueFile, render
CHART = "apiVersion: v2\nname: demo\nversion: 0.1.0\n"
CM = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}-x\n  namespace: {{ .Release.Namespace }}\ndata:\n  a: {{ .Values.a | default \"none\" | quote }}\n  kube: {{ .Capabilities.KubeVersion.Version | quote }}\n"
def tgz(files, links=None):
    b = io.BytesIO()
    with gzip.GzipFile(fileobj=b, mode="wb") as g, tarfile.open(fileobj=g, mode="w") as t:
        for n, c in files.items():
            d = c.encode() if isinstance(c, str) else c
            i = tarfile.TarInfo(n); i.size = len(d); t.addfile(i, io.BytesIO(d))
        for n, target in (links or {}).items():
            i = tarfile.TarInfo(n); i.type = tarfile.SYMTYPE; i.linkname = target; t.addfile(i)
    return b.getvalue()
def ch(**t): return {"demo/Chart.yaml": CHART, "demo/templates/cm.yaml": CM, **{f"demo/templates/{k}.yaml": v for k, v in t.items()}}
COND = RenderConditions(release_name="rel", namespace="team-a", kube_version="1.33.0")
def health():
    start = time.monotonic()
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as r:
        return f"{r.status} {int((time.monotonic()-start)*1000)}ms"
def one(name, archive, values=()):
    start = time.monotonic(); r = render(archive, list(values), COND); took = time.monotonic() - start
    what = f"ok {len(r.manifest)}B {r.helm_version}" if r.ok else f"{r.reason} (service_failed={r.service_failed})"
    print(f"{name:26} {what:42} {took:5.2f}s  agent /health {health()}", flush=True)
    return r
HOG = '{{ $s := "xxxxxxxxxxxxxxxx" }}{{ range until 26 }}{{ $s = printf "%s%s" $s $s }}{{ end }}{{ $s | len }}'
SPIN = "{{ range until 100000 }}{{ range until 100000 }}{{ $x := 1 }}{{ end }}{{ end }}"
mode = os.environ.get("MODE", "bad")
if mode == "bad":
    r = one("정상", tgz(ch()), [ValueFile("a", "a: 1st\n"), ValueFile("b", "a: 2nd\n")])
    print(r.manifest)
    one("무한 반복", tgz(ch(spin=SPIN)))
    one("큰 출력 30MB", tgz(ch(big='{{ range until 150000 }}# {{ repeat 200 "x" }}\n{{ end }}')))
    one("메모리 폭주", tgz(ch(hog=HOG)))
    one("심볼릭 링크", tgz(ch(), links={"demo/templates/x.yaml": "/etc/passwd"}))
    one("스키마 file $ref", tgz({**ch(), "demo/values.schema.json": '{"$ref":"file:///etc/passwd"}'}))
    one("스키마 http $ref", tgz({**ch(), "demo/values.schema.json": '{"$ref":"http://169.254.169.254/x"}'}))
    one("정상 (뒤)", tgz(ch()))
    print("-- 동시 3개 (둘이 도는 중 셋째)", flush=True)
    out = []
    ts = [threading.Thread(target=lambda: out.append(render(tgz(ch(spin=SPIN)), [], COND).reason)) for _ in range(2)]
    [t.start() for t in ts]; time.sleep(1)
    print("셋째:", render(tgz(ch()), [], COND).reason, "agent /health", health(), flush=True)
    [t.join() for t in ts]; print("앞의 둘:", out, flush=True)
elif mode == "oom":
    one("메모리 폭주 (감시 없음)", tgz(ch(hog=HOG)))
    print("-- 파드가 다시 뜨는 동안", flush=True)
    for _ in range(6):
        one("정상", tgz(ch())); time.sleep(2)
elif mode == "down":
    for _ in range(2):
        one("파드 없음", tgz(ch())); time.sleep(1)
elif mode == "kill":
    # 렌더링 중에 파드를 지운다 — 밖에서 지우는 동안 이쪽은 기다린다
    one("렌더링 중 파드 삭제", tgz(ch(spin=SPIN)))
