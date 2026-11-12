"""클러스터 개요의 gpu 칸 — GPU 노드, 그 위의 DCGM 지표, 서빙 중인 vLLM 모델 (DURO-118), ServedModel 상태 (DURO-182),
Karpenter GPU NodePool (DURO-184), 측정 기록 (DURO-188), 카운터의 429 비율 (DURO-173), 남아 있는 모델 볼륨.

overview.py 처럼 kubectl 출력(문자열)을 받아 응답을 만드는 순수 함수만 둔다. kubectl 은 clusters_api.py 가 돌린다.

읽는 순서 (clusters_api.cluster_overview)
  1. NODES_COMMAND   노드 목록. 못 읽으면(고객 EKS 는 namespace 범위 권한뿐이라 Forbidden) 여기서 멈추고 gpu 는 null.
                     GPU 노드가 없으면 GPU 를 기다리는 파드(waiting)를 찾는다 — 아래 "꺼진 GPU 클러스터"
  2. PODS_COMMAND    모든 namespace 의 파드. GPU 노드 위의 DCGM exporter·vLLM 파드를 찾는다. 못 읽으면 NAMESPACES_COMMAND 로
                     namespace 를 읽고 namespace 마다 파드를 읽어 읽힌 것만 모은다 — Kukie GPU 읽기 RBAC
                     (kukie-infra customer-templates/gpu-read-rbac-v1.yaml)는 namespace 목록은 클러스터 전체에, 파드 목록은 고객이
                     고른 namespace 에만 준다. namespace 목록도 못 읽으면 개요가 이미 읽은 클러스터 namespace 의 파드에서만 찾는다
                     (running_pods 로 한 번만 파싱한다)
  3. NODE_POOLS_COMMAND  Karpenter NodePool (클러스터 범위). 파드를 읽은 라운드마다 한 번 — 아래 "Karpenter GPU 풀"
  4. SERVED_MODELS_COMMAND  모든 namespace 의 ServedModel. 지표보다 먼저 읽는다 — 지표 파드가 매달려도 예산이 남게.
                     GPU 노드가 없으면 기다리는 파드나 GPU 풀이 있거나 server 값이 GPU 클러스터(true)일 때만 읽는다 — 아래 "ServedModel"
  5. BENCHMARKS_COMMAND  측정 기록 ConfigMap. ServedModel 이 읽힌 라운드에만 읽는다 — 아래 "측정 기록"
     MODEL_VOLUMES_COMMAND  오븐이 만든 모델 PVC. 측정 기록과 같은 조건·같은 몫에서 그 뒤에 읽는다 — 아래 "모델 볼륨"
  6. proxy_command   찾은 파드의 /metrics 를 API 서버 **파드 프록시**로 읽는다 (Prometheus 없이 kubectl 권한만으로).
                     서비스 프록시가 아니라 파드 프록시인 이유: DCGM exporter 는 노드마다 하나인 DaemonSet 이라 서비스로
                     부르면 아무 파드 하나에 붙어 다른 노드의 값을 받는다. vLLM 도 레플리카마다 값이 다르다.
                     카운터(kukie-counter)만은 **서비스 프록시**(counter_proxy_command)로 같은 풀·같은 시간 예산 안에서 읽는다 —
                     ServedModel 이 읽힌 라운드에만, 아래 "카운터"
  지표를 못 읽으면 그 노드의 실시간 값만 null, 그 파드만 serving 에서 빠진다 — 개요는 그대로 200.

GPU 노드 — nodes
  status.allocatable 에 GPU_RESOURCES 중 하나가 1 이상인 노드. allocatable 에 그 자원 칸이 아예 없을 때만 capacity 를 본다 —
  device plugin 이 죽으면 kubelet 이 capacity 는 두고 allocatable 만 0 으로 내리는데, 그 노드는 쓸 수 있는 GPU 가 없다.
  count    라벨 nvidia.com/gpu.count(GPU Feature Discovery 가 붙이는 물리 GPU 수)가 있으면 그것, 없으면 자원 수.
           time-slicing·MPS 를 켜면 자원 수가 나눈 조각 수로 불어나기 때문이다
  product  라벨 nvidia.com/gpu.product (AMD 는 amd.com/gpu.product-name), 없으면 인스턴스 종류 라벨, 그것도 없으면 ""
  memory_mib  노드 전체 GPU 메모리 = GPU 하나 메모리(라벨 nvidia.com/gpu.memory, MiB) × count.
              라벨이 없으면 DCGM 의 FB_USED + FB_FREE + FB_RESERVED 합, 그것도 없으면 0
  instance_type  라벨 node.kubernetes.io/instance-type, 없으면 null
  zone           라벨 topology.kubernetes.io/zone, 없으면 null
  capacity_type  on-demand·spot 처럼 소문자와 - 로. 라벨 karpenter.sh/capacity-type, 없으면 eks.amazonaws.com/capacityType(ON_DEMAND·SPOT),
                 둘 다 없으면 null. 앱이 온디맨드 단가를 스팟 노드에 말하지 않게 하는 값이다
  실시간 값(DCGM) — GPU 마다 나오는 줄을 노드 하나로 모은다: 사용률은 평균, 메모리 사용·전력은 합, 온도는 가장 높은 것.
  MIG 인스턴스는 부모 GPU 의 UUID 를 같이 쓰고 GPU_I_ID 로만 갈리므로 (UUID, GPU_I_ID) 로 묶는다.
  AMD 는 DCGM 이 없어 실시간 값이 늘 null 이다.

서빙 — serving
  GPU 노드 위의 vLLM 파드(컨테이너 이미지나 명령에 vllm 이 있는 것)의 /metrics 에서 model_name 라벨마다 한 줄.
  GPU 노드로 좁히는 것은 `vllm bench` Job 같은 클라이언트 파드가 지표 대상 칸(MAX_METRIC_TARGETS)을 먹지 않게 하려는 것이다.
  아래 지표만 본다 — vllm:cache_config_info 처럼 model_name 이 없는 info 지표로 유령 줄이 생기지 않게.
    running·waiting   vllm:num_requests_running·waiting (엔진이 여럿이면 합)
    kv_cache_pct      vllm:kv_cache_usage_perc (옛 vLLM 은 gpu_cache_usage_perc) × 100, 엔진이 여럿이면 평균
    ttft_p95_ms       vllm:time_to_first_token_seconds 히스토그램의 **최근 구간** p95
    itl_p95_ms        vllm:inter_token_latency_seconds (옛 vLLM 은 time_per_output_token_seconds) 히스토그램의 최근 구간 p95
    latency_window_seconds  그 구간의 길이(초) — 직전 수집부터 이번 수집까지
  p95 는 Prometheus histogram_quantile 처럼 버킷 안에서 선형으로 나눈 값이다. 히스토그램은 서빙 프로세스가 뜬 뒤 누적이라
  한 번 읽은 값으로 내면 "뜬 뒤 전체" 의 p95 다 — 부하가 끝나도 안 내려가고 방금 느려진 것도 안 보인다 (DURO-124).
  그래서 파드마다 직전 수집의 누적 히스토그램(LatencySample — 버킷·count·sum)을 기억하고 두 수집의 차이로 p95 를 낸다
  (최근 구간 p95). 기억은 clusters_api 가 연결·파드마다 메모리에 둔다 (pod_identity 로 같은 프로세스인지 가린다).
  구간의 시각은 그 파드의 /metrics 를 읽은 시각이다 — 라운드의 끝이 아니다 (다른 파드가 느려도 구간이 안 늘어난다).
    기억이 없다(첫 수집·파드가 바뀜·직전이 너무 오래됨)  p95 null, latency_window_seconds null
    값이 줄었다(프로세스가 다시 떠 처음부터 센다)         기억을 버리고 이번은 둘 다 null — 다음 수집부터 다시 잇는다
    구간에 요청이 없었다                                    p95 null, latency_window_seconds 는 있다 (앱은 "최근 요청 없음")
  vLLM 지표가 하나도 없는 파드(라우터 등)는 빠진다.

꺼진 GPU 클러스터 — waiting
  GPU 노드 그룹을 0대로 내려 둔 클러스터(우리 kukie-gpu 처럼 평소 0대)는 노드 목록에 GPU 노드가 없다. 그래도 GPU 를 요청한
  파드(vLLM 등)가 남아 Pending 이면 "GPU 클러스터인데 지금 꺼짐" 이다. 그 파드를 waiting 에 싣고 nodes 는 빈 배열로 둔다.
  끝난 파드(Succeeded·Failed)는 세지 않는다. 파드는 2와 같은 길로 찾는다(이때는 지표를 읽지 않으니 남은 예산을 다 쓴다). 다만 server 의 hasGpu 가 false 인 클러스터
  (GPU 가 없다고 이미 안 곳)는 찾지 않는다 — GPU 없는 클러스터마다 개요 때 파드 조회가 늘지 않게. 그런 곳도 몇 분에 한 번은
  다시 찾는다(clusters_api.GPU_ABSENT_RECHECK — 0대인 채 GPU 워크로드를 새로 올린 경우). GPU 노드가 있을 때도
  같은 파드 목록에서 GPU 를 기다리는 파드를 waiting 에 싣는다(늘어나는 중인 레플리카 등).

Karpenter GPU 풀 — node_pools
  Karpenter 가 노드를 띄우는 클러스터는 노드가 0대여도 GPU 클러스터다. 앱은 이 칸이 비어 있지 않으면 노드 켜기·끄기 버튼을
  숨기고 "모델을 시작하면 노드가 자동으로 켜진다" 고 안내한다. 한 줄이 GPU 풀 하나다: name, gpu_limit(spec.limits 의 GPU 수), instance_types. 이름순.
    null  거부·시간 초과·예산 부족·모양이 다른 출력으로 못 읽었다. 앱은 "확인하지 못했어요" 로 그린다. 개요는 그대로 200
    []    GPU 풀이 없다 — CRD 가 없거나(Karpenter 를 안 깐 곳, 앱이 설치를 안내한다) 읽었는데 GPU 풀이 없다
          (CPU 풀만 있는 Karpenter 클러스터는 GPU 클러스터가 아니다). 빈 배열만으로는 gpu 칸을 만들지 않는다
  거부는 응답에서는 null 이지만 hasGpu 판단에서는 CRD 없음과 같다 — 볼 수 없는 곳은 못 본 곳으로 치지 않는다(node_pools_absent).
  [] 는 "노드를 띄울 수 있는 GPU 풀이 없다" 만 말한다. CRD 없음·CPU 풀만 있음·GPU 풀을 다 꺼 둠(아래)이 같은 값이라, Karpenter 가
  깔렸는지까지 가려 주지는 않는다 (agent#139 리뷰). 가르려면 칸을 더해야 하고 그것은 약속을 고치는 일이다.
  instance_types  spec.template.spec.requirements 의 node.kubernetes.io/instance-type 이 In 으로 고른 값. 적힌 순서를 지키고 중복은 뺀다 (DURO-186).
    앱이 시작 창에서 고를 수 있는 GPU 수를 이 목록으로 정한다. null 은 "제한을 모른다" 다 — 앱은 1장만 보낸다
      키가 없다(instance-family 같은 다른 조건만 있어도 그렇다), In 이 아니다, 키가 여러 번 나온다(Karpenter 가 합친 결과를 여기서 계산하지 않는다),
      requirements·values 모양이 이상하다(문자열이 아닌 값, 빈 문자열, 빈 목록),
      GPU 장수를 안 바꾼다고 아는 키(_GPU_COUNT_NEUTRAL_KEYS) 밖의 조건이 같이 있다(karpenter.k8s.aws/instance-gpu-count In [1] 처럼
      고를 범위를 좁힐 수 있다. Karpenter 는 모든 조건의 교집합만 띄우므로 앱이 띄울 수 없는 칸을 보여 주지 않게 한다. 모르는 키도 같다)
    GPU 풀 판별과 hasGpu 판단에는 쓰지 않는다. 그래서 모양이 이상해도 ValueError 를 내지 않고 이 칸만 null 이다 — 예외를 내면
    node_pools 전체가 null 이 되어 hasGpu 판단이 바뀐다.
  GPU 풀 판별 — 넷 중 하나면 된다. 빈 목록은 hasGpu 를 false 로 덮는 근거라 GPU 풀을 고르는 흔한 설정을 빠뜨리지 않는다
    spec.template.spec.taints 의 key 가 GPU_RESOURCES
    spec.limits 에 GPU_RESOURCES 칸이 있다
    spec.template.spec.requirements 의 instance-family·instance-type·instance-category 가 operator In 으로 g·gr·p 계열을 고른다.
      NotIn 은 "GPU 는 빼라" 라는 일반 풀의 흔한 설정이라 보지 않는다. m7g(Graviton)처럼 g 로 끝나는 계열은 GPU 가 아니다
    requirements 가 karpenter.k8s.aws/instance-gpu-* (count·manufacturer·name 등) 를 NotIn·DoesNotExist 밖의 연산자로 요구한다 —
      이 라벨은 GPU 인스턴스에만 붙으므로 요구하는 것만으로 GPU 인스턴스로 좁혀진다
  다만 limits 의 어느 자원이든(cpu·memory·GPU) 0 인 풀은 응답에서 뺀다 — 노드를 띄우지 못하게 꺼 둔 풀이다. 앱이 "모델을 시작하면 노드가
  켜진다" 고 안내하면 안 된다. 그래도 "GPU 풀 없음" 과 다르다: 꺼 둔 풀만 있으면 node_pools 는 [] 이지만 hasGpu 를 false 로 덮지 않는다
  (상한을 잠깐 0 으로 내린 GPU 클러스터가 목록에서 빠지지 않게).
  이 칸은 "GPU 풀이 있다" 는 사실만 말한다. 그 풀이 지금 있는 GPU 노드를 관리하는지는 가리지 않는다 — 노드 그룹과 GPU 풀을 같이 쓰는
  클러스터도 채워진다 (agent#139 리뷰). 약속(DURO-184)은 고객 전제 조건을 Karpenter 로 두고 앱이 이 칸으로 방식을 고른다.
  GPU 풀이 있으면 노드가 0대이고 기다리는 파드도 모델도 없어도 빈 gpu 칸(nodes 빈 배열)을 주고 GPU 클러스터로 둔다.
  GPU 노드가 없고 server 값이 true 가 아닌 곳은 기다리는 파드를 찾는 라운드(clusters_api.GPU_ABSENT_RECHECK)에만 읽는다 —
  일반 클러스터의 다른 라운드에는 kubectl 이 늘지 않는다.

ServedModel — served_models
  kukie-gpu-operator 의 CRD. 한 줄이 ServedModel 하나다: namespace·name(metadata), model(spec.source.huggingface),
  phase·reason·message·hint(status), download_percent(status.download.percent), download_bytes(status.download.downloadedBytes),
  download_total_bytes(status.download.totalBytes), max_concurrency(status.baseline.maxConcurrency),
  engine_image(spec.engine.image), gpus(spec.engine.gpus — 파드 하나의 GPU 수, 1 이상 정수가 아니면 null. DURO-186),
  started_at(status.startedAt), stops_at(status.stopsAt), measured_at(status.baseline.measuredAt).
  세 시각은 오퍼레이터가 쓴 RFC3339 문자열 그대로이고, 없으면 null 이다. measured_at 은 측정 기록의 at 과 견주는 값이라 형식이 다르면 null 이다.
  여기서 계산하지 않는다 — 언제 멈추는지는 spec.maxRuntime·차트 기본값·연장 annotation 을 모두 아는 오퍼레이터가 정한다.
  reason 은 오퍼레이터가 실패를 가른 이름(HfAuth·DiskShort·GpuMemoryShort·ImagePull 등, Loading 중이면 StillLoading),
  message 는 그 한국어 문장, hint 는 "다음에 할 일" 한국어 문장이다(DURO-171·173). 셋 다 오퍼레이터가 쓴 그대로 싣고 없으면 "" 다 —
  로그 원문은 status 에 없다(오퍼레이터가 싣지 않는다).
  namespace·이름순. 오퍼레이터가 아직 status 를 쓰지 않았으면 문자열 칸은 "", 숫자·시각 칸은 null 이다.
    null  CRD 가 없거나(애드온을 안 깐 곳) 거부·시간 초과·예산 부족·모양이 다른 출력으로 못 읽었다. 개요는 그대로 200
    []    읽었는데 하나도 없다
  GPU 노드가 없어도 읽는 곳: 기다리는 파드나 GPU 풀이 있거나 server 값이 true 인 곳(꺼진 GPU 클러스터). 그 밖의 GPU 노드 없는 곳은
  읽지 않는다 — GPU 없는 일반 클러스터마다 개요 때 kubectl 이 늘지 않게. 꺼진 GPU 클러스터에서 읽혔으면 하나도 없어도(애드온이
  깔린 곳이다) 빈 gpu 칸(nodes 빈 배열)을 주고 GPU 클러스터로 둔다 — 모델을 지우고 노드를 끈 뒤에도 GPU 화면이 이어지게.

측정 기록 — benchmarks
  오븐이 측정을 정상으로 마칠 때마다 namespace llm 에 남기는 ConfigMap(라벨 serving.kukie.io/measure-result=true, 주인 없음)이 한 줄이다.
  모델을 지워도 남아서, 모델이 돌지 않는 클러스터에도 줄이 있다. 팀 누구나 같은 값을 받는다. 최근 순으로 MAX_BENCHMARKS 개까지.
  한 줄: id(metadata.name), model, at(measuredAt), input_tokens·output_tokens·sla(baseline.json 의 sla), steps, gpus, partial, max_concurrency.
  ConfigMap 의 다른 칸(engineImage·engineArgs 등)과 baseline.json 의 다른 칸은 싣지 않는다.
  steps 한 칸: concurrency(c), ttft_p95_ms(ttft95), itl_p95_ms(itl95), output_tokens_per_s(tps). 적힌 순서 그대로다.
    ttft95·itl95 가 명시적 null 인 단계는 그 단계만 뺀다 — 오븐이 그 단계의 p95 를 얻지 못하면 쓰는 값이고, 앱은 숫자만 그린다.
    키가 아예 없는 단계는 오븐이 쓴 모양이 아니라 아래 "모양이 다른 항목" 이다.
  모양이 다른 항목은 그 줄만 건너뛴다: 이름·model 이 없다, measuredAt 이 RFC3339 가 아니다, baseline.json 이 JSON 이 아니다,
  sla 칸이 빠졌다, 단계에 칸이 빠졌다, 숫자가 아니거나 음수·NaN·무한대다(NaN·무한대는 JSON 응답을 만들 때 예외가 나 개요를 깬다), 남는 단계가 없다,
  단계·model 이 상한보다 길다. gpus·max_concurrency 는 문자열로 적힌 1 이상 정수만 싣고 나머지는 null, partial 은 "true" 만 true 다.
  읽는 조건은 ServedModel 과 같다 — served_models 가 null 이면(애드온이 없거나 못 읽었다) 조회하지 않는다. 그 라운드의 값은 아래 기억 규칙을 따른다. GPU 없는 일반 클러스터에 kubectl 이 늘지 않는다.
  시간 초과·예산 부족·깨진 출력이면 연결마다 기억해 둔 마지막 성공 값을 싣고(clusters_api._read_benchmarks — 한 번 실패로 표가 깜빡이지 않게),
  그것도 없으면 [] 다. 거부와 CRD 없음은 잠깐의 실패가 아니라 "볼 수 없다" 는 답이라 기억을 지우고 [] 다. 웹이 배열로 읽으므로 null 은 쓰지 않는다.
  이 칸은 GPU 클러스터 판단(hasGpu)에 쓰지 않는다.

모델 볼륨 — model_volumes
  오븐이 ServedModel 마다 만드는 모델 파일 PVC(`<ServedModel 이름>-models`, 라벨 serving.kukie.io/servedmodel=<ServedModel 이름>)가
  한 줄이다. spec.storage.retainOnDelete 로 만든 PVC 는 주인(ownerReferences)이 없어 ServedModel 을 지워도 남는다 — 같은 이름으로
  다시 만들면 오븐이 그 PVC 를 다시 붙여, 이미 받은 파일은 다시 받지 않는다. 앱은 이 칸으로 후보 표에 "받아 둠" 을 보인다.
  PVC 에는 허깅페이스 모델 이름이 없다 — 앱이 후보 id 에서 서버와 같은 규칙(ServedModelName.of)으로 ServedModel 이름을 만들어 잇는다.
  한 줄: name(라벨의 ServedModel 이름), pvc(metadata.name), size_bytes(status.capacity.storage, 없으면 spec.resources.requests.storage
  — 바이트 정수, 단위를 못 읽으면 null), phase(status.phase — Bound·Pending·Lost, 없으면 ""), retained(주인이 없다 — 지금 도는
  ServedModel 의 것이 아니다), created_at(metadata.creationTimestamp). ServedModel 이름순, MAX_MODEL_VOLUMES 줄까지.
  안의 파일이 다 받아졌는지는 모른다 — 받다가 멈춘 PVC 도 줄이 있다. 앱은 "다시 받지 않는다" 가 아니라 "받던 파일을 이어 쓴다" 로 말한다.
  이름·라벨이 없는 항목은 그 줄만 건너뛴다.
    null  읽지 않았다(ServedModel 을 못 읽은 라운드) 또는 못 읽었다(거부·시간 초과·예산 부족·깨진 출력 — 그 라운드만)
    []    읽었는데 하나도 없다
  읽는 조건은 측정 기록과 같다 — served_models 가 null 이면 조회하지 않는다. GPU 없는 일반 클러스터에 kubectl 이 늘지 않는다.
  읽기 권한은 ServedModel·측정 기록과 같은 연결 권한이다(llm namespace 의 읽기). PVC 를 볼 수 없는 곳은 null 이다.
  이 칸은 GPU 클러스터 판단(hasGpu)에 쓰지 않는다.

카운터 — counter
  애드온 차트가 설치 namespace 에 띄우는 키 발급제 입구 kukie-counter(Deployment, 파드 1개, CPU 노드)의 /metrics 로 낸 모델마다의
  **최근 구간** 429(동시 수 한도에 걸려 거절) 비율이다 (DURO-173). 한 줄: model, requests, rejected, rejected_ratio, window_seconds. model 이름순.
    requests        구간에 끝난 요청 수 — kukie_counter_requests_total{model,key,code} 의 차이를 key·code 를 모두 더해 모델마다.
                    카운터는 끝난 요청을 상태 코드와 함께 하나도 빼지 않고 센다(server.go 의 defer finishRequest) — 429 로 거절한
                    요청도 code="429" 로 여기 들어 있다. 503·499(클라이언트가 떠남)도 들어 있다
    rejected        구간에 429 로 거절한 수 — 같은 requests_total 의 code="429" 줄 차이의 합. 카운터가 429 를 내는 곳은 동시 수
                    한도(errAtLimit, rejected_total 의 reason limit·brake)뿐이라 rejected_total limit+brake 와 같은 수다.
                    분자·분모를 한 지표에서 읽어 둘이 오르는 시점이 어긋나지 않는다(rejected_total 은 거절하는 자리에서, requests_total 은
                    응답이 끝난 뒤 오른다). 503(unavailable, 모델 서버가 내려감)은 세지 않는다
    rejected_ratio  rejected / requests (0~1, 소수 넷째 자리). 분모에 429 가 들어 있으니 "끝난 요청 중 몇이 429 였나" 다
    window_seconds  구간 길이(초) — 같은 카운터 namespace 의 직전 수집부터 이번 수집까지
  key 라벨(키 id)은 싣지 않는다 — 모델마다 더한 값만 낸다. model 라벨이 비었거나 "unknown"(모델을 정하기 전에 끝난 요청 —
  잘못된 키·경로·본문)인 줄은 뺀다. 카운터는 설정한 모델의 rejected_total 을 0 으로 미리 만들어 요청이 없던 모델도 줄이 있다 —
  rejected_total 은 모델 목록에만 쓴다.
  서빙의 최근 구간 p95 와 같은 방식이다: 연결마다 카운터 namespace 의 직전 누적값(CounterSample)을 기억하고 두 수집의 차이를 낸다.
  파드가 아니라 namespace 로 기억한다 — Service 로 읽어 파드를 모른다. 카운터가 다시 뜨면 값이 줄어드는 것으로 안다.
  기억은 clusters_api 가 LATENCY_WINDOW_MAX 초까지 둔다.
    기억이 없다(첫 수집·namespace 가 바뀜·직전이 너무 오래됨)  그 라운드는 모든 줄의 requests·rejected·rejected_ratio·window_seconds 가 null
    값이 줄었다·process_start_time_seconds 가 바뀌었다(카운터가 다시 떴다)  기억을 버리고 이번은 모두 null — 다음 수집부터 다시 잇는다
    직전 수집에 없던 모델                                     그 줄만 모두 null — 다음 수집부터
    줄(model·key·code)마다 견준다. 직전에 있던 줄이 사라져도 다시 뜬 것으로 본다 — 카운터는 요청 줄을 지우지 않는다
    구간에 요청이 없었다                                     requests 0, rejected 0, rejected_ratio null, window_seconds 는 있다
  롤링 업데이트로 카운터 파드가 잠깐 둘이면 Service 가 어느 쪽에 붙을지 몰라 그동안 값이 줄었다 늘었다 한다 — 줄거나 프로세스 시작
  시각이 바뀌면 위처럼 null 이다 (차트는 레플리카 1, 겹치는 것은 잠깐이다).
  칸 전체:
    null  카운터를 못 찾았다·지표를 못 읽었다(거부·없음·시간 초과·예산 부족 — 그 라운드만)·읽지 않았다(GPU 노드가 없다·ServedModel 을 못 읽었다)
    []    읽었는데 모델 줄이 없다
  읽는 길: 애드온 차트가 카운터를 켜고 RBAC 를 켜면 설치 namespace 에 ClusterIP Service kukie-counter-metrics(이름 없는 포트 9090 →
  컨테이너 metrics 포트)와, Kukie 읽기 그룹에 그 Service 하나의 services/proxy get 만 주는 Role·RoleBinding 을 만든다.
  카운터 namespace 의 파드 목록·파드 프록시 권한은 일부러 주지 않는다(kukie-gpu-operator runbooks/counter.md). 그래서
  kubectl get --raw /api/v1/namespaces/<ns>/services/kukie-counter-metrics/proxy/metrics 로 읽는다 (counter_proxy_command).
  어느 namespace 를 읽나 — 짐작하지 않는다:
    1. 읽은 파드 목록(2)에 라벨 app.kubernetes.io/name=kukie-counter 이거나 이미지 저장소 이름이 kukie-counter 인 Running 파드가 있으면
       그 namespace(counter_namespaces — 여럿이면 COUNTER_DEFAULT_NAMESPACE 가 먼저, 그다음 이름순 첫째). 노드로 좁히지 않는다(CPU 노드에서
       돈다). 모든 namespace 파드를 읽고 파싱했는데 없으면 null
    2. 그 밖(모든 파드를 못 읽었고 읽은 namespace 에도 없다)이면 COUNTER_DEFAULT_NAMESPACE(kukie-gpu, 우리가 차트를 까는 namespace)
       하나만 읽는다. 어떤 실패든 그 라운드는 null 이다
    그래서 모든 파드를 못 읽는 클러스터에서 다른 namespace 에 깐 카운터는 찾지 못한다 — 차트가 설치 namespace 를 읽기 그룹이 볼 수
    있는 곳에 적는 것이 후속이다. 실패를 기억해 다음 라운드를 바꾸지 않는다(찾기 상태가 없다).
  읽기는 지표 대상 하나로 DCGM·vLLM 과 같은 풀(GPU_METRICS_WORKERS)·같은 시간 예산에서 맨 앞에 둔다.
  읽는 조건: GPU 노드가 있고 served_models 가 null 이 아닌 라운드(애드온이 있고 읽혔다)만. GPU 없는 일반 클러스터에 kubectl 이
  늘지 않는다. GPU 노드가 없는 꺼진 클러스터는 읽지 않아 null 이다 — 돌고 있는 모델이 없고, 그 길은 지표 몫을 따로 남기지 않는다.
  이 칸은 GPU 클러스터 판단(hasGpu)에 쓰지 않는다.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: 노드 목록 (클러스터 범위). 부르는 쪽이 `-n` 을 붙이지 않는다
NODES_COMMAND: list[str] = ["get", "nodes", "-o", "json"]
#: DCGM exporter·vLLM 파드를 찾는 파드 목록 (모든 namespace). 부르는 쪽이 `-n` 을 붙이지 않는다
PODS_COMMAND: list[str] = ["get", "pods", "-A", "-o", "json"]

#: 모든 namespace 의 ServedModel (모듈 설명 "ServedModel"). 부르는 쪽이 `-n` 을 붙이지 않는다
SERVED_MODELS_COMMAND: list[str] = ["get", "servedmodels.serving.kukie.io", "-A", "-o", "json"]

#: 측정 기록 ConfigMap (모듈 설명 "측정 기록"). 오븐이 llm 에만 남긴다. 부르는 쪽이 `-n` 을 붙이지 않는다
BENCHMARKS_COMMAND: list[str] = ["get", "configmaps", "-n", "llm", "-l", "serving.kukie.io/measure-result=true", "-o", "json"]
#: 응답에 싣는 측정 기록 수 상한. 개요는 20초마다 다시 읽으므로 응답이 커지지 않게 한다
MAX_BENCHMARKS = 50
#: 측정 기록 한 줄의 단계 수·model 길이 상한. 오븐은 단계가 7개이고 결과를 4096바이트에 담는다 — 넘으면 오븐이 쓴 값이 아니다
MAX_BENCHMARK_STEPS = 64
MAX_BENCHMARK_MODEL = 256
#: 측정 시각. 대문자 T·Z 와 오프셋까지 있어야 한다 — 시각끼리 견주어 최근 순으로 놓으려면 시간대가 있어야 한다.
#: 소수 초는 정렬 때 따로 읽는다 — datetime 은 마이크로초까지만 담는다 (_measured_time)
_RFC3339 = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.([0-9]{1,9}))?(?:Z|[+-][0-9]{2}:[0-9]{2})")

#: 오븐이 만든 모델 PVC (모듈 설명 "모델 볼륨"). 오븐 ServedModel 은 llm 에 만든다. 라벨이 있는 것만 — 값은 ServedModel 이름이다
MODEL_VOLUMES_COMMAND: list[str] = ["get", "persistentvolumeclaims", "-n", "llm", "-l", "serving.kukie.io/servedmodel", "-o", "json"]
#: 응답에 싣는 모델 볼륨 수 상한
MAX_MODEL_VOLUMES = 50
_SERVED_MODEL_LABEL = "serving.kukie.io/servedmodel"
#: PVC 크기 — 정수나 소수에 쿠버네티스 단위 하나. 지수 표기(1e3)는 PVC 에 쓰지 않아 모르는 값으로 둔다
_STORAGE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?")
_STORAGE_UNITS: dict[str, int] = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
                                  "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5}

#: Karpenter NodePool (모듈 설명 "Karpenter GPU 풀"). 클러스터 범위 자원이라 `-A`·`-n` 이 없다
NODE_POOLS_COMMAND: list[str] = ["get", "nodepools.karpenter.sh", "-o", "json"]

#: 모든 namespace 파드를 못 읽을 때 namespace 마다 파드를 읽으려고 보는 namespace 목록 (클러스터 범위)
NAMESPACES_COMMAND: list[str] = ["get", "namespaces", "-o", "json"]
#: namespace 마다 파드를 읽는 수 상한. 넘으면 GPU 쪽 이름(NAMESPACE_HINTS)이 들어간 것부터 고른다
MAX_POD_NAMESPACES = 24
#: DCGM exporter·vLLM 이 흔히 도는 namespace 이름 조각 — 상한을 넘을 때 먼저 고른다. kube-prometheus-stack 으로 깐 DCGM 은 보통 monitoring 에 있다
NAMESPACE_HINTS: tuple[str, ...] = ("gpu", "nvidia", "dcgm", "vllm", "llm", "serving", "inference",
                                    "monitor", "metric", "observ", "prom")

#: GPU 로 보는 확장 자원 (위에 있는 것이 먼저)
GPU_RESOURCES: tuple[str, ...] = ("nvidia.com/gpu", "amd.com/gpu")

#: NodePool requirements 에서 인스턴스 종류를 고르는 키
_INSTANCE_TYPE_KEY = "node.kubernetes.io/instance-type"
_INSTANCE_KEYS: tuple[str, ...] = ("karpenter.k8s.aws/instance-family", _INSTANCE_TYPE_KEY,
                                   "karpenter.k8s.aws/instance-category")
#: GPU 인스턴스 계열 — g·p(category), g6e·g6e.xlarge·gr6·p4d. 앞에서부터 맞춘다 (m7g·c7gn 은 GPU 가 아니다)
_GPU_INSTANCE = re.compile(r"(?:gr?|p)(?:\d|$)")
#: GPU 인스턴스에만 붙는 라벨의 앞부분과, 그 라벨을 요구하지 않는 연산자 (모듈 설명 "Karpenter GPU 풀")
_GPU_LABEL_PREFIX = "karpenter.k8s.aws/instance-gpu-"
_GPU_LABEL_EXCLUDING: tuple[str, ...] = ("NotIn", "DoesNotExist")
#: instance_types 와 같이 있어도 되는 requirements 키 — GPU 장수를 바꾸지 않는다. 그 밖의 키가 있으면 instance_types 는 null 이다
_GPU_COUNT_NEUTRAL_KEYS: tuple[str, ...] = ("karpenter.sh/capacity-type", "topology.kubernetes.io/zone", "topology.k8s.aws/zone-id",
                                            "kubernetes.io/arch", "kubernetes.io/os")

#: NodePool limits 의 0 — 0·"0"·"0Gi"·"0.0"
_ZERO_QUANTITY = re.compile(r"0+(?:\.0+)?[A-Za-z]*")

#: 지표를 읽는 파드 수 상한 (DCGM·vLLM 따로). 한 번 읽기가 시간 제한까지 걸려도 개요가 끝없이 늘지 않게
MAX_METRIC_TARGETS = 8

DCGM_PORT = 9400
VLLM_PORT = 8000
#: 카운터 지표 Service — 애드온 차트가 설치 namespace 에 만든다. 포트는 이름 없는 9090 하나(→ 카운터 컨테이너 metrics 포트)
COUNTER_SERVICE = "kukie-counter-metrics"
#: 모든 namespace 파드를 못 읽을 때 카운터를 읽는 namespace — 우리가 애드온 차트를 까는 namespace (모듈 설명 "카운터")
COUNTER_DEFAULT_NAMESPACE = "kukie-gpu"
#: 카운터가 동시 수 한도로 거절한 응답 코드 (counter/server.go errAtLimit — 429 를 내는 곳은 이것뿐이다)
_COUNTER_429_CODE = "429"
#: 카운터가 모델을 정하기 전에 끝난 요청의 model 라벨 (counter/config.go labelUnknown)
_COUNTER_UNKNOWN_MODEL = "unknown"


# ── 노드 ─────────────────────────────────────────────────

def gpu_nodes(stdout: str) -> list[dict[str, Any]]:
    """노드 목록에서 GPU 노드만 (모듈 설명 "GPU 노드"). 실시간 값은 아직 null 이다 — apply_dcgm 이 채운다.

    모양이 다르면 ValueError — 부르는 쪽이 gpu 를 null 로 둔다. 다른 목록(_items)과 달리 items 칸이 없거나 객체가 아닌
    항목이 섞여도 ValueError 다: 빈 목록은 "GPU 노드 없음" 으로 server 의 hasGpu 를 false 로 덮기 때문이다 (PR #119 리뷰).
    """
    rows: list[dict[str, Any]] = []
    for item in _strict_items(stdout):
        meta = item.get("metadata") or {}
        status = item.get("status") or {}
        labels = meta.get("labels") or {}
        resource, amount = _gpu_resource(status)
        if resource is None:
            continue
        count = _positive_int(labels.get("nvidia.com/gpu.count")) or amount
        per_gpu = _memory_label_mib(labels)
        rows.append({
            "node": meta.get("name", ""),
            "product": _product(labels, resource),
            "count": count,
            "memory_mib": per_gpu * count if per_gpu else 0,
            "instance_type": _text_or_none(labels.get("node.kubernetes.io/instance-type")),
            "zone": _text_or_none(labels.get("topology.kubernetes.io/zone")),
            "capacity_type": _capacity_type(labels),
            "utilization_pct": None,
            "memory_used_mib": None,
            "temperature_c": None,
            "power_w": None,
        })
    rows.sort(key=lambda r: r["node"])
    return rows


def _capacity_type(labels: dict[str, Any]) -> str | None:
    """Karpenter 는 on-demand, 관리형 노드 그룹은 ON_DEMAND 로 적는다 — 한 가지 말로 맞춘다."""
    value = _text_or_none(labels.get("karpenter.sh/capacity-type")) or _text_or_none(labels.get("eks.amazonaws.com/capacityType"))
    return value.lower().replace("_", "-") if value else None


def _gpu_resource(status: dict[str, Any]) -> tuple[str | None, int]:
    allocatable = status.get("allocatable") or {}
    capacity = status.get("capacity") or {}
    for name in GPU_RESOURCES:
        amount = _positive_int(allocatable[name] if name in allocatable else capacity.get(name))
        if amount:
            return name, amount
    return None, 0


def _product(labels: dict[str, Any], resource: str) -> str:
    keys = ("nvidia.com/gpu.product",) if resource == "nvidia.com/gpu" else (
        "amd.com/gpu.product-name", "beta.amd.com/gpu.product-name")
    for key in (*keys, "node.kubernetes.io/instance-type"):
        value = labels.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _memory_label_mib(labels: dict[str, Any]) -> int:
    """GPU 하나 메모리(MiB). NVIDIA GFD 는 MiB 숫자, AMD node labeller 는 `64G` 처럼 단위를 붙인다."""
    nvidia = _positive_int(labels.get("nvidia.com/gpu.memory"))
    if nvidia:
        return nvidia
    amd = labels.get("amd.com/gpu.vram") or labels.get("beta.amd.com/gpu.vram")
    match = re.fullmatch(r"(\d+)\s*([GM])i?B?", amd.strip(), re.IGNORECASE) if isinstance(amd, str) else None
    if match:
        return int(match[1]) * (1024 if match[2].upper() == "G" else 1)
    return 0


# ── ServedModel ──────────────────────────────────────────

def served_models(stdout: str) -> list[dict[str, Any]]:
    """ServedModel 목록을 응답 줄로 (모듈 설명 "ServedModel"). 이름 없는 항목은 뺀다. 모양이 다르면 ValueError —
    부르는 쪽이 served_models 를 null 로 둔다. 빈 목록은 "없다" 라는 뜻이라 gpu_nodes 처럼 엄격하게 읽는다.
    download_percent·download_bytes·download_total_bytes·max_concurrency 는 CRD 스키마가 integer 라 오퍼레이터가 정수로만 쓴다 —
    다른 값은 null 이다. gpus 는 모델을 만드는 쪽이 쓰는 값이라 1 이상 정수만 싣고, 0·음수·정수가 아닌 값은 null 이다.
    오븐 CRD 는 gpus 1~8 을 모두 받고 서버는 시작 요청에서 1, 2, 4, 8 만 받는다 — 여기서는 거르지 않아 3 도 그대로 싣는다. 오븐이 전체 크기를 모르면 totalBytes 가 없어 download_total_bytes 가 null 이고, 옛 오븐은 둘 다 null 이다."""
    rows: list[dict[str, Any]] = []
    for item in _strict_items(stdout):
        meta = item.get("metadata")
        if not isinstance(meta, dict) or not meta.get("name"):
            continue
        status = item.get("status") or {}
        download = status.get("download") or {}
        spec = item.get("spec") or {}
        engine = spec.get("engine") or {}
        baseline = status.get("baseline") or {}
        rows.append({
            "namespace": meta.get("namespace") or "default",
            "name": meta["name"],
            "model": (spec.get("source") or {}).get("huggingface") or "",
            "phase": _text_or_none(status.get("phase")) or "",
            "reason": _text_or_none(status.get("reason")) or "",
            "message": _text_or_none(status.get("message")) or "",
            "hint": _text_or_none(status.get("hint")) or "",
            "download_percent": _int_or_none(download.get("percent")),
            "download_bytes": _int_or_none(download.get("downloadedBytes")),
            "download_total_bytes": _int_or_none(download.get("totalBytes")),
            "max_concurrency": _int_or_none(baseline.get("maxConcurrency")),
            "engine_image": _text_or_none(engine.get("image")),
            "gpus": _count_or_none(engine.get("gpus")),
            "started_at": _text_or_none(status.get("startedAt")),
            "stops_at": _text_or_none(status.get("stopsAt")),
            "measured_at": baseline.get("measuredAt") if _measured_time(baseline.get("measuredAt")) else None,
        })
    rows.sort(key=lambda r: (r["namespace"], r["name"]))
    return rows


def served_models_absent(stderr: str) -> bool:
    """ServedModel 조회 실패가 "CRD 가 없다" 나 "볼 수 없다" 라고 확인된 것인가 — 시간 초과처럼 모르는 실패와 가른다.
    거부(Forbidden)는 _pods_by_namespace 와 같이 Kukie 가 볼 수 없는 곳이라 못 본 곳으로 치지 않는다.
    server 의 hasGpu 를 false 로 덮는 근거라 kubectl 문구로 좁힌다 — 사이 프록시의 `403 Forbidden` 본문은 모르는 실패다.
    그 밖의 문구(NotFound 등)도 모르는 실패다: hasGpu 를 덮지 않는 안전한 쪽이다."""
    lowered = stderr.lower()
    return "doesn't have a resource type" in lowered or "(forbidden)" in lowered or " is forbidden" in lowered


# ── 측정 기록 ─────────────────────────────────────────────

def benchmarks(stdout: str) -> list[dict[str, Any]]:
    """측정 기록 ConfigMap 목록을 응답 줄로 (모듈 설명 "측정 기록"). 모양이 다른 항목은 그 줄만 건너뛰어 나머지를 살린다.
    그래서 _strict_items 가 아니라 _items 다. 목록 자체가 깨졌으면 ValueError — 부르는 쪽이 [] 로 둔다."""
    runs = [run for item in _items(stdout) if (run := _benchmark_run(item)) is not None]
    runs.sort(key=lambda run: (run[0], run[1]["id"]), reverse=True)
    return [row for _, row in runs[:MAX_BENCHMARKS]]


def _benchmark_run(item: dict[str, Any]) -> tuple[tuple[datetime, int], dict[str, Any]] | None:
    """ConfigMap 하나를 (측정 시각의 정렬 키, 응답 줄)로. 모양이 다르면 None — 어떤 모양에도 예외를 내지 않는다."""
    meta, data = item.get("metadata"), item.get("data")
    if not isinstance(meta, dict) or not isinstance(data, dict):
        return None
    name, model, at = meta.get("name"), data.get("model"), data.get("measuredAt")
    measured = _measured_time(at)
    baseline = _baseline_json(data.get("baseline.json"))
    if (measured is None or baseline is None or not isinstance(name, str) or not name
            or not isinstance(model, str) or not model or len(model) > MAX_BENCHMARK_MODEL):
        return None
    sla = baseline.get("sla")
    if not isinstance(sla, dict):
        return None
    ttft, itl = _measure_number(sla.get("ttftMs")), _measure_number(sla.get("itlMs"))
    input_tokens, output_tokens = _count_or_none(sla.get("inputTokens")), _count_or_none(sla.get("outputTokens"))
    steps = _benchmark_steps(baseline.get("steps"))
    if ttft is None or itl is None or input_tokens is None or output_tokens is None or not steps:
        return None
    return measured, {
        "id": name,
        "model": model,
        "at": at,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "sla": {"ttft_p95_ms": ttft, "itl_p95_ms": itl},
        "steps": steps,
        "gpus": _count_text_or_none(data.get("gpus")),
        "partial": data.get("partial") == "true",
        "max_concurrency": _count_text_or_none(data.get("maxConcurrency")),
    }


def _benchmark_steps(raw: Any) -> list[dict[str, Any]] | None:
    """baseline.json 의 steps 를 응답 칸으로. p95 가 명시적 null 인 단계만 빼고, 그 밖에 모양이 다른 단계(키가 없는 것 포함)가
    있으면 None 이다 — 가운데 단계가 빠진 표를 온전한 측정처럼 보이지 않게 (agent#144 리뷰)."""
    if not isinstance(raw, list) or len(raw) > MAX_BENCHMARK_STEPS:
        return None
    rows: list[dict[str, Any]] = []
    for step in raw:
        if not isinstance(step, dict) or "ttft95" not in step or "itl95" not in step:
            return None
        if step["ttft95"] is None or step["itl95"] is None:
            continue
        row = {"concurrency": _count_or_none(step.get("c")), "ttft_p95_ms": _measure_number(step["ttft95"]),
               "itl_p95_ms": _measure_number(step["itl95"]), "output_tokens_per_s": _measure_number(step.get("tps"))}
        if any(value is None for value in row.values()):
            return None
        rows.append(row)
    return rows


def _baseline_json(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _measured_time(value: Any) -> tuple[datetime, int] | None:
    """RFC3339 측정 시각의 정렬 키 — (시각, 나노초). 형식이 다르면 None. datetime 은 마이크로초 아래를 버리므로 소수 초를
    따로 견주어 그 아래 자리까지 순서를 지킨다 (agent#144 리뷰)."""
    match = _RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        return datetime.fromisoformat(value), int((match[1] or "").ljust(9, "0"))
    except ValueError:      # 13월처럼 모양만 맞는 값
        return None


def _measure_number(value: Any) -> int | float | None:
    """0 이상의 유한한 숫자만. json.loads 는 NaN·Infinity 를 받고, 이런 값은 JSON 응답을 만들 때 예외를 낸다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:   # float 로 못 바꾸는 큰 정수
        return None
    return value if finite and value >= 0 else None


def _count_text_or_none(value: Any) -> int | None:
    """ConfigMap data 의 숫자 칸(문자열). 1 이상 정수만, 나머지는 null 이다."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}", value):
        return None
    return _count_or_none(int(value))


# ── Karpenter GPU 풀 ─────────────────────────────────────

def node_pools(stdout: str) -> tuple[list[dict[str, Any]], bool]:
    """(노드를 띄울 수 있는 GPU 풀 줄, 꺼 둔 GPU 풀이 있나) — 모듈 설명 "Karpenter GPU 풀". 이름 없는 항목은 뺀다.
    모양이 다르면 ValueError — 부르는 쪽이 node_pools 를 null 로 둔다.
    빈 목록은 "GPU 풀 없음" 으로 hasGpu 를 false 로 덮는 근거라 엄격하게 읽는다. instance_types 는 그 판단에 쓰지 않아
    모양이 이상하면 그 칸만 null 이다 (_instance_types)."""
    rows: list[dict[str, Any]] = []
    disabled = False
    for item in _strict_items(stdout):
        meta = item.get("metadata")
        if not isinstance(meta, dict) or not meta.get("name"):
            continue
        spec = item.get("spec") or {}
        limits = spec.get("limits") or {}
        limited = next((name for name in GPU_RESOURCES if name in limits), None)
        if limited is None and not _gpu_template((spec.get("template") or {}).get("spec") or {}):
            continue
        if any(_ZERO_QUANTITY.fullmatch(str(value)) for value in limits.values()):
            disabled = True
            continue
        rows.append({"name": meta["name"], "gpu_limit": _quantity_or_none(limits[limited]) if limited else None,
                     "instance_types": _instance_types(spec)})
    rows.sort(key=lambda r: r["name"])
    return rows, disabled


def _instance_types(spec: dict[str, Any]) -> list[str] | None:
    """NodePool 이 instance-type 을 In 으로 고른 값 (모듈 설명 "Karpenter GPU 풀"). 어떤 모양에도 예외를 내지 않는다 —
    GPU 풀 판별은 GPU 상한이나 taint, 앞쪽 조건이 맞으면 requirements 를 끝까지 읽지 않는다. 그런 풀은 뒤쪽 모양이 이상해도
    지금까지 읽혔으니, 여기서 예외가 나면 그 풀이 node_pools 째 null 이 된다."""
    template = spec.get("template")
    template_spec = template.get("spec") if isinstance(template, dict) else None
    requirements = template_spec.get("requirements") if isinstance(template_spec, dict) else None
    if not isinstance(requirements, list) or not all(isinstance(req, dict) for req in requirements):
        return None
    picks = [req for req in requirements if req.get("key") == _INSTANCE_TYPE_KEY]
    if len(picks) != 1 or picks[0].get("operator") != "In":
        return None
    if any(req.get("key") not in (_INSTANCE_TYPE_KEY, *_GPU_COUNT_NEUTRAL_KEYS) for req in requirements):
        return None
    values = picks[0].get("values")
    if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
        return None
    return list(dict.fromkeys(values))


def _gpu_template(template: dict[str, Any]) -> bool:
    """NodePool 템플릿이 GPU 노드를 띄우는가 — GPU taint, g·p 계열을 고르거나 GPU 라벨을 요구하는 requirements."""
    if any(taint.get("key") in GPU_RESOURCES for taint in template.get("taints") or []):
        return True
    return any(_gpu_requirement(req) for req in template.get("requirements") or [])


def _gpu_requirement(req: dict[str, Any]) -> bool:
    key, operator = str(req.get("key") or ""), req.get("operator")
    if key.startswith(_GPU_LABEL_PREFIX):
        return bool(operator) and operator not in _GPU_LABEL_EXCLUDING
    return key in _INSTANCE_KEYS and operator == "In" and any(
        isinstance(value, str) and _GPU_INSTANCE.match(value) for value in req.get("values") or [])


def node_pools_absent(stderr: str) -> bool:
    """NodePool 조회 실패가 "CRD 가 없다" 나 "볼 수 없다" 라고 확인된 것인가 — served_models_absent 와 같은 기준이다.
    hasGpu 를 false 로 덮어도 되는지를 가른다. 응답 칸의 값은 node_pools_crd_missing 이 가른다."""
    return served_models_absent(stderr)


def node_pools_crd_missing(stderr: str) -> bool:
    """NodePool 조회 실패가 "CRD 가 없다" 인가 — Karpenter 를 안 깐 곳이라 응답은 []("GPU 풀 없음") 이다. 거부는 모르는 것이라 null 이다."""
    return "doesn't have a resource type" in stderr.lower()


# ── 지표를 읽을 파드 ───────────────────────────────────────

def running_pods(stdout: str) -> list[dict[str, Any]]:
    """파드 목록에서 Running 이고 이름이 있는 것. 모양이 다르면 ValueError."""
    rows = []
    for item in _items(stdout):
        meta = item.get("metadata")
        if isinstance(meta, dict) and meta.get("name") and (item.get("status") or {}).get("phase") == "Running":
            rows.append(item)
    return rows


def dcgm_targets(pods: list[dict[str, Any]], gpu_node_names: set[str]) -> dict[str, tuple[str, str, int]]:
    """GPU 노드마다 그 위의 DCGM exporter 파드 하나 — {노드: (namespace, 파드, 포트)}. pods 는 running_pods."""
    targets: dict[str, tuple[str, str, int]] = {}
    for item in pods:
        node = (item.get("spec") or {}).get("nodeName")
        if node not in gpu_node_names or node in targets or not _is_dcgm(item):
            continue
        meta = item["metadata"]
        targets[node] = (meta.get("namespace") or "default", meta["name"],
                         _port(item, names=("metrics", "gpu-metrics"), default=DCGM_PORT, match=_dcgm_container))
        if len(targets) >= MAX_METRIC_TARGETS:
            break
    return targets


def vllm_targets(pods: list[dict[str, Any]], gpu_node_names: set[str]) -> list[tuple[str, str, int, str, str]]:
    """GPU 노드 위의 vLLM 파드 — [(namespace, 파드, 포트, 노드, 정체)]. 이름순. pods 는 running_pods. 정체는 pod_identity."""
    targets: list[tuple[str, str, int, str, str]] = []
    for item in pods:
        if (item.get("spec") or {}).get("nodeName") not in gpu_node_names:
            continue
        if _is_dcgm(item) or not any(_vllm_container(c) for c in _containers(item)):
            continue
        meta = item["metadata"]
        targets.append((meta.get("namespace") or "default", meta["name"], _vllm_port(item),
                        (item.get("spec") or {}).get("nodeName") or "", pod_identity(item)))
    targets.sort(key=lambda t: (t[0], t[1]))
    return targets[:MAX_METRIC_TARGETS]


def pod_identity(item: dict[str, Any]) -> str:
    """파드의 정체 — uid(없으면 시작 시각)와 vLLM 컨테이너의 재시작 수. 최근 구간 p95 의 기억(LatencySample)을 같은 서빙
    프로세스에만 잇는다: 같은 이름으로 다시 뜬 파드(StatefulSet 의 qwen-0)는 uid 가, 컨테이너만 다시 뜬 파드는 재시작 수가
    다르다. 둘 다 히스토그램을 처음부터 다시 세는 프로세스라 옛 기억과 빼면 안 된다.
    재시작 수는 vLLM 컨테이너(_vllm_container, spec.containers — vllm_targets 가 찾는 것과 같은 범위)의 것만 센다 —
    사이드카(메시·로그)가 다시 떠도 vLLM 프로세스와 히스토그램은 그대로라 기억을 버릴 까닭이 없다 (agent#137 Codex 리뷰).
    native sidecar(initContainers + restartPolicy Always)로 띄운 vLLM 은 애초에 대상이 아니다 (_containers)."""
    meta = item.get("metadata") or {}
    status = item.get("status") or {}
    vllm_names = {c.get("name") for c in _containers(item) if _vllm_container(c)}
    restarts = sum(_positive_int(c.get("restartCount")) for c in status.get("containerStatuses") or []
                   if isinstance(c, dict) and c.get("name") in vllm_names)
    return f"{meta.get('uid') or status.get('startTime') or ''}:{restarts}"


def counter_namespaces(pods: list[dict[str, Any]]) -> list[str]:
    """카운터 파드가 도는 namespace 들, 이름순. pods 는 running_pods (모듈 설명 "카운터" 의 찾는 법 1).
    노드로 좁히지 않는다(CPU 노드에서 돈다)."""
    names: set[str] = set()
    for item in pods:
        try:
            if isinstance(item.get("metadata"), dict) and _is_counter(item):
                names.add(str(item["metadata"].get("namespace") or "default"))
        except (ValueError, AttributeError, TypeError):
            continue                                        # 모양이 이상한 파드 하나로 개요를 깨지 않는다
    return sorted(names)


def counter_proxy_command(namespace: str) -> list[str]:
    """카운터 지표 Service(COUNTER_SERVICE)를 API 서버 **서비스 프록시**로 읽는 kubectl 인자 (모듈 설명 "카운터")."""
    return ["get", "--raw", f"/api/v1/namespaces/{namespace}/services/{COUNTER_SERVICE}/proxy/metrics"]


def namespace_names(stdout: str, *, skip: str | None = None,
                    prefer: set[str] | frozenset[str] = frozenset()) -> tuple[list[str], bool]:
    """(파드를 읽어 볼 namespace, 목록 전부인가) — kube-* 와 skip(개요가 이미 읽은 클러스터 namespace)은 뺀다(Kukie 역할은
    시스템 namespace 를 읽지 않는다). prefer(GPU 지표·기다리는 파드가 나온 적 있는 것)와 GPU 쪽 이름이 먼저, 그다음
    이름순으로 MAX_POD_NAMESPACES 개까지 — 상한이 지표 namespace 를 자르지 않게. 잘랐으면 두 번째 값이 False 다
    (끝까지 읽었다고 말할 수 없다). 모양이 다르면 ValueError."""
    names = sorted({name for item in _items(stdout)
                    if isinstance(name := (item.get("metadata") or {}).get("name"), str) and name
                    and not name.startswith("kube-") and name != skip})
    names.sort(key=lambda name: not (name in prefer or hinted_namespace(name)))
    return names[:MAX_POD_NAMESPACES], len(names) <= MAX_POD_NAMESPACES


def hinted_namespace(name: str) -> bool:
    """GPU 지표·서빙 쪽 이름(NAMESPACE_HINTS)이 들어간 namespace."""
    return any(hint in name for hint in NAMESPACE_HINTS)


def namespace_pods_command(namespace: str) -> list[str]:
    """namespace 하나의 파드 목록 kubectl 인자 (모듈 설명 "읽는 순서" 2의 대체 길)."""
    return ["get", "pods", "-n", namespace, "-o", "json"]


def waiting_pods(stdout: str) -> list[dict[str, Any]]:
    """GPU 를 요청했는데 아직 돌지 않는 파드 — [{namespace, pod, gpus, phase}]. 끝난 파드(Succeeded·Failed)와 Running 은 뺀다.
    이름순으로 MAX_METRIC_TARGETS 개까지. 모양이 다르면 ValueError."""
    rows: list[dict[str, Any]] = []
    for item in _items(stdout):
        meta = item.get("metadata")
        phase = (item.get("status") or {}).get("phase") or "Pending"
        if not isinstance(meta, dict) or not meta.get("name") or phase in ("Running", "Succeeded", "Failed"):
            continue
        gpus = sum(_requested_gpus(c) for c in _containers(item))
        if gpus:
            rows.append({"namespace": meta.get("namespace") or "default", "pod": meta["name"],
                         "gpus": gpus, "phase": phase})
    rows.sort(key=lambda r: (r["namespace"], r["pod"]))
    return rows[:MAX_METRIC_TARGETS]


def _requested_gpus(container: dict[str, Any]) -> int:
    """컨테이너가 요청한 GPU 수. 확장 자원은 limits 만 적어도 requests 가 같은 값이 된다 — 둘 중 큰 것."""
    resources = container.get("resources") or {}
    return max((_positive_int((resources.get(kind) or {}).get(name))
                for kind in ("limits", "requests") for name in GPU_RESOURCES), default=0)


def has_waiting_pods(stdout: str) -> bool:
    """파드 목록에 GPU 를 기다리는 파드가 있는가. 모양이 다르면 False (기억을 고칠 때만 쓴다)."""
    try:
        return bool(waiting_pods(stdout))
    except (ValueError, AttributeError, TypeError):
        return False


def has_gpu_targets(stdout: str, gpu_node_names: set[str]) -> bool:
    """파드 목록에 GPU 노드 위의 DCGM exporter 나 vLLM 파드가 있는가. 모양이 다르면 False (개요를 실패시키지 않는다)."""
    try:
        pods = running_pods(stdout)
        return bool(dcgm_targets(pods, gpu_node_names) or vllm_targets(pods, gpu_node_names))
    except (ValueError, AttributeError, TypeError):
        return False


def merge_pod_lists(outputs: list[str]) -> str:
    """파드 목록 출력 여럿을 하나로 — (namespace, 이름)이 같은 파드는 한 번만. 깨진 출력 하나는 건너뛴다."""
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for stdout in outputs:
        try:
            rows = _items(stdout)
        except ValueError:      # JSON 이 아니거나 items 가 목록이 아니다 (json.JSONDecodeError 도 ValueError)
            continue
        for item in rows:
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            key = (str(meta.get("namespace") or ""), str(meta.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return json.dumps({"items": items})


def proxy_command(namespace: str, pod: str, port: int) -> list[str]:
    """API 서버 파드 프록시로 /metrics 를 읽는 kubectl 인자 (모듈 설명 "읽는 순서" 6)."""
    return ["get", "--raw", f"/api/v1/namespaces/{namespace}/pods/{pod}:{port}/proxy/metrics"]


def _containers(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (item.get("spec") or {}).get("containers") or [] if isinstance(c, dict)]


def _dcgm_container(container: dict[str, Any]) -> bool:
    return "dcgm-exporter" in str(container.get("image") or "").lower()


def _is_dcgm(item: dict[str, Any]) -> bool:
    labels = (item.get("metadata") or {}).get("labels") or {}
    return (labels.get("app") in ("nvidia-dcgm-exporter", "dcgm-exporter")
            or labels.get("app.kubernetes.io/name") == "dcgm-exporter"
            or any(_dcgm_container(c) for c in _containers(item)))


def _vllm_container(container: dict[str, Any]) -> bool:
    words = [str(container.get("image") or ""), *map(str, container.get("command") or []),
             *map(str, container.get("args") or [])]
    return any("vllm" in word.lower() for word in words)


def _is_counter(item: dict[str, Any]) -> bool:
    labels = (item.get("metadata") or {}).get("labels")
    labels = labels if isinstance(labels, dict) else {}
    return (labels.get("app.kubernetes.io/name") == "kukie-counter"
            or any(_image_repository(c.get("image")) == "kukie-counter" for c in _containers(item)))


def _image_repository(image: Any) -> str:
    """이미지의 저장소 이름 마지막 조각 — `registry/durokukie/kukie-counter:0.3.0@sha256:…` → `kukie-counter`.
    `kukie-counter-loadtest` 같은 다른 이미지를 카운터로 보지 않게 (agent#147 리뷰)."""
    name = str(image or "").lower().split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def _vllm_port(item: dict[str, Any]) -> int:
    """`--port N`·`--port=N` 인자가 있으면 그것, 없으면 http 라는 이름의 포트, 첫 포트, vLLM 기본 8000 순."""
    for container in _containers(item):
        if not _vllm_container(container):
            continue
        words = [str(w) for w in [*(container.get("command") or []), *(container.get("args") or [])]]
        for i, word in enumerate(words):
            if word == "--port" and i + 1 < len(words) and words[i + 1].isdigit():
                return int(words[i + 1])
            if word.startswith("--port=") and word[7:].isdigit():
                return int(word[7:])
    return _port(item, names=("http",), default=VLLM_PORT, match=_vllm_container)


def _port(item: dict[str, Any], *, names: tuple[str, ...], default: int, match) -> int:
    ports = [p for c in _containers(item) if match(c) for p in c.get("ports") or [] if isinstance(p, dict)]
    for port in ports:
        if port.get("name") in names and _positive_int(port.get("containerPort")):
            return int(port["containerPort"])
    for port in ports:
        if _positive_int(port.get("containerPort")):
            return int(port["containerPort"])
    return default


# ── DCGM ─────────────────────────────────────────────────

def apply_dcgm(row: dict[str, Any], text: str) -> None:
    """DCGM exporter /metrics 로 노드 한 줄의 실시간 값을 채운다 (모듈 설명 "GPU 노드")."""
    per_gpu: dict[tuple[str, str], dict[str, float]] = {}
    for name, labels, value in parse_metrics(text):
        if not name.startswith("DCGM_FI_DEV_"):
            continue
        gpu = (labels.get("UUID") or labels.get("gpu") or "", labels.get("GPU_I_ID") or "")
        per_gpu.setdefault(gpu, {})[name] = value
    if not per_gpu:
        return

    def values(metric: str) -> list[float]:
        return [gpu[metric] for gpu in per_gpu.values() if metric in gpu]

    util, used, temp, power = (values(m) for m in (
        "DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_GPU_TEMP", "DCGM_FI_DEV_POWER_USAGE"))
    row["utilization_pct"] = round(sum(util) / len(util)) if util else None
    row["memory_used_mib"] = round(sum(used)) if used else None
    row["temperature_c"] = round(max(temp)) if temp else None
    row["power_w"] = round(sum(power)) if power else None
    if not row["memory_mib"]:
        total = sum(values("DCGM_FI_DEV_FB_USED") + values("DCGM_FI_DEV_FB_FREE") + values("DCGM_FI_DEV_FB_RESERVED"))
        row["memory_mib"] = round(total)


# ── vLLM ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Histogram:
    """Prometheus 히스토그램 하나 — 누적 버킷 {상한: 개수} 와 _count·_sum. 엔진이 여럿이면 같은 le 끼리 더한 값."""
    buckets: dict[float, float]
    count: float = 0.0
    sum: float = 0.0


@dataclass(frozen=True)
class LatencySample:
    """파드 하나의 /metrics 를 한 번 읽어 둔 누적 히스토그램 — (모델, 종류) 마다 하나. 종류는 _HISTOGRAM_KINDS 의 값.

    at 은 읽은 시각(부르는 쪽의 시계 — clusters_api 는 time.monotonic). 다음 수집이 이것과의 차이로 최근 구간 p95 를 낸다.
    """
    at: float
    histograms: dict[tuple[str, str], Histogram]


#: 히스토그램 지표 이름(vllm: 과 _bucket·_count·_sum 을 뗀 것) → 종류
_HISTOGRAM_KINDS = {
    "time_to_first_token_seconds": "ttft",
    "inter_token_latency_seconds": "itl",
    "time_per_output_token_seconds": "itl_old",      # 옛 vLLM
}
_GAUGE_METRICS = frozenset({"num_requests_running", "num_requests_waiting", "kv_cache_usage_perc", "gpu_cache_usage_perc"})


def serving_rows(text: str, *, pod: str, node: str, previous: LatencySample | None = None,
                 at: float = 0.0) -> tuple[list[dict[str, Any]], LatencySample]:
    """(vLLM /metrics 에서 모델마다 한 줄, 이번에 읽은 누적 히스토그램) — 모듈 설명 "서빙". vLLM 지표가 없으면 빈 목록.

    p95 는 previous(같은 파드의 직전 수집)와의 차이로 낸다. previous 가 없으면 null 이고, 어느 히스토그램이든 값이 줄었으면
    (서빙 프로세스가 다시 떴다) 그 파드의 모든 줄이 null 이다 — 부르는 쪽은 돌려준 LatencySample 을 다음 기억으로 두면 된다.
    둘 다 latency_window_seconds 가 null 이다. 구간은 있는데 요청이 없었으면 p95 만 null 이다.
    """
    models: dict[str, dict[str, Any]] = {}
    histograms: dict[tuple[str, str], dict[str, Any]] = {}
    for name, labels, value in parse_metrics(text):
        metric = name[len("vllm:"):] if name.startswith("vllm:") else ""
        base, _, part = metric.rpartition("_")
        kind = _HISTOGRAM_KINDS.get(base) if part in ("bucket", "count", "sum") else None
        if metric not in _GAUGE_METRICS and kind is None:
            continue
        model_name = labels.get("model_name") or pod
        model = models.setdefault(model_name, {"running": 0.0, "waiting": 0.0, "kv": [], "kv_old": []})
        if metric == "num_requests_running":
            model["running"] += value
        elif metric == "num_requests_waiting":
            model["waiting"] += value
        elif metric == "kv_cache_usage_perc":
            model["kv"].append(value)
        elif metric == "gpu_cache_usage_perc":
            model["kv_old"].append(value)
        else:
            histogram = histograms.setdefault((model_name, kind), {"buckets": {}, "count": 0.0, "sum": 0.0})
            if part == "bucket":
                _add_bucket(histogram["buckets"], labels, value)
            else:
                histogram[part] += value

    sample = LatencySample(at=at, histograms={key: Histogram(**h) for key, h in histograms.items()})
    recent = recent_histograms(sample, previous) or {}
    rows = []
    for model_name, m in sorted(models.items()):
        kv = m["kv"] or m["kv_old"]
        # 이 모델의 히스토그램이 직전 수집에도 있었을 때만 구간이 있다 — 새로 나타난 모델은 다음 수집부터
        mine = {kind: histogram for (model, kind), histogram in recent.items() if model == model_name}

        def p95(*kinds: str) -> float | None:
            histogram = next((mine[kind] for kind in kinds if kind in mine), None)
            return None if histogram is None else _ms(histogram_quantile(0.95, histogram.buckets))

        rows.append({
            "model": model_name,
            "pod": pod,
            "node": node,
            "running": round(m["running"]),
            "waiting": round(m["waiting"]),
            "kv_cache_pct": round(sum(kv) / len(kv) * 100, 1) if kv else 0,
            "ttft_p95_ms": p95("ttft"),
            "itl_p95_ms": p95("itl", "itl_old"),
            "latency_window_seconds": round(at - previous.at, 1) if mine and previous is not None else None,
        })
    return rows, sample


def recent_histograms(current: LatencySample, previous: LatencySample | None) -> dict[tuple[str, str], Histogram] | None:
    """두 수집 사이에 쌓인 히스토그램 — current 에 있고 previous 에도 있는 (모델, 종류)만. 기억이 없으면 None.

    어느 하나라도 줄었으면(버킷·count·sum — 서빙 프로세스가 다시 떠 처음부터 센다) 파드 전체가 None 이다: 히스토그램은
    한 프로세스의 것이라 하나만 줄어도 나머지도 옛 기억과 이어지지 않는다.
    le 집합이 달라진 키(버킷 설정이 다른 vLLM 으로 바뀜)는 잇지 않고 건너뛴다 — 직전에 없던 le 를 0 부터 빼면 차이 버킷이
    누적 순서를 어겨(위 le 보다 커져) p95 가 실제보다 낮게 나온다 (agent#137 리뷰). 엔진이 늘어 같은 le 의 합만 뛴 것은
    그만큼 요청이 있었던 것으로 본다.
    """
    if previous is None:
        return None
    recent: dict[tuple[str, str], Histogram] = {}
    for key, now in current.histograms.items():
        before = previous.histograms.get(key)
        if before is None or set(before.buckets) != set(now.buckets):
            continue
        if now.count < before.count or now.sum < before.sum:
            return None
        buckets = {}
        for le, value in now.buckets.items():
            buckets[le] = value - before.buckets[le]
            if buckets[le] < 0:
                return None
        recent[key] = Histogram(buckets, now.count - before.count, now.sum - before.sum)
    return recent


def _add_bucket(buckets: dict[float, float], labels: dict[str, str], value: float) -> None:
    """같은 le 는 더한다 — 엔진이 여럿(데이터 병렬)이면 모델 하나에 히스토그램이 여럿 나온다."""
    le = _float(labels.get("le"))
    if le is not None:
        buckets[le] = buckets.get(le, 0.0) + value


def histogram_quantile(q: float, buckets: dict[float, float]) -> float | None:
    """누적 버킷 {상한: 개수} 의 q 분위수 — Prometheus histogram_quantile 과 같은 선형 보간. 요청이 없으면 None.

    +Inf 버킷에 걸리면 가장 큰 유한 상한을 준다 (Prometheus 와 같다).
    """
    if math.inf not in buckets or buckets[math.inf] <= 0:
        return None
    total = buckets[math.inf]
    rank = q * total
    lower_bound, lower_count = 0.0, 0.0
    for bound in sorted(buckets):
        count = buckets[bound]
        if count >= rank:
            if bound == math.inf:
                return lower_bound
            if count == lower_count:
                return bound
            return lower_bound + (bound - lower_bound) * (rank - lower_count) / (count - lower_count)
        lower_bound, lower_count = bound, count
    return None


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


# ── 카운터 ───────────────────────────────────────────────

@dataclass(frozen=True)
class CounterSample:
    """카운터 /metrics 를 한 번 읽어 둔 누적값 (모듈 설명 "카운터"). series 는 kukie_counter_requests_total 의 줄마다 하나 —
    (model, key, code). key id 는 이 프로세스 기억에만 있고 응답에 싣지 않는다. models 는 지표에 나온 모델(요청 줄이 아직 없어도
    rejected_total 의 0 줄로 나온다).

    at 은 읽은 시각(부르는 쪽의 시계 — clusters_api 는 time.monotonic). 다음 수집이 이것과의 차이로 최근 구간 비율을 낸다.
    """
    at: float
    series: dict[tuple[str, str, str], float]
    models: frozenset[str] = frozenset()
    started: float | None = None        # process_start_time_seconds — 카운터 프로세스가 뜬 시각 (없으면 None)


def counter_sample(text: str, *, at: float = 0.0) -> CounterSample:
    """카운터 /metrics 를 누적값으로 (모듈 설명 "카운터"). 카운터 지표가 없으면 빈 series·models."""
    series: dict[tuple[str, str, str], float] = {}
    models: set[str] = set()
    started = None
    for name, labels, value in parse_metrics(text):
        if name == "process_start_time_seconds":
            started = value
            continue
        model = labels.get("model") or ""
        if not model or model == _COUNTER_UNKNOWN_MODEL:
            continue
        if name == "kukie_counter_requests_total":
            key = (model, labels.get("key") or "", labels.get("code") or "")
            series[key] = series.get(key, 0.0) + value
            models.add(model)
        elif name == "kukie_counter_rejected_total":
            models.add(model)
    return CounterSample(at=at, series=series, models=frozenset(models), started=started)


def counter_window(sample: CounterSample, previous: CounterSample | None) -> list[dict[str, Any]]:
    """모델마다 한 줄 — sample 과 previous(같은 카운터 namespace 의 직전 수집)의 차이 (모듈 설명 "카운터").

    줄(series)마다 뺀다 — 직전에 있던 줄이 하나라도 줄었거나 사라졌으면 카운터가 다시 뜬 것이라 모든 줄이 null 이다. 카운터는 요청 줄을
    지우지 않으므로(counter/metrics.go) 사라진 줄은 다시 떠 아직 그 요청이 없다는 뜻이다 — 다시 뜬 뒤 끝난 요청이 하나도 없어도
    0 으로 내지 않게 (agent#147 리뷰). process_start_time_seconds(카운터의 프로세스 지표)가 바뀌었어도 다시 뜬 것이다 — 다음 수집 전에
    모든 줄이 직전 값보다 커져 줄어든 것으로는 못 잡는 재시작. previous 가 없으면 모든 줄이 null 이다.
    """
    before = previous.series if previous is not None else {}
    restarted = previous is not None and None not in (sample.started, previous.started) and sample.started != previous.started
    connected = previous is not None and not restarted and all(key in sample.series and sample.series[key] >= value
                                                               for key, value in before.items())
    done: dict[str, float] = {}
    refused: dict[str, float] = {}
    for key, value in sample.series.items():
        delta = value - before.get(key, 0.0)
        done[key[0]] = done.get(key[0], 0.0) + delta
        if key[2] == _COUNTER_429_CODE:
            refused[key[0]] = refused.get(key[0], 0.0) + delta
    rows = []
    for model in sorted(sample.models):
        row: dict[str, Any] = {"model": model, "requests": None, "rejected": None, "rejected_ratio": None, "window_seconds": None}
        if connected and model in previous.models:
            requests, rejected = round(done.get(model, 0.0)), round(refused.get(model, 0.0))
            row.update(requests=requests, rejected=rejected, rejected_ratio=round(rejected / requests, 4) if requests else None,
                       window_seconds=round(sample.at - previous.at, 1))
        rows.append(row)
    return rows


# ── 모델 볼륨 ─────────────────────────────────────────────

def model_volumes(stdout: str) -> list[dict[str, Any]]:
    """모델 PVC 목록을 응답 줄로 (모듈 설명 "모델 볼륨"). 이름·라벨이 없는 항목은 그 줄만 건너뛴다. 목록 자체가 깨졌으면
    ValueError — 부르는 쪽이 null 로 둔다."""
    rows: list[dict[str, Any]] = []
    for item in _items(stdout):
        meta = item.get("metadata")
        if not isinstance(meta, dict):
            continue
        labels = meta.get("labels")
        name = labels.get(_SERVED_MODEL_LABEL) if isinstance(labels, dict) else None
        pvc = _text_or_none(meta.get("name"))
        if not _text_or_none(name) or pvc is None:
            continue
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        capacity = status.get("capacity") if isinstance(status.get("capacity"), dict) else {}
        resources = spec.get("resources") if isinstance(spec.get("resources"), dict) else {}
        requests = resources.get("requests") if isinstance(resources.get("requests"), dict) else {}
        size = _storage_bytes(capacity.get("storage"))
        owners = meta.get("ownerReferences")
        rows.append({
            "name": name,
            "pvc": pvc,
            "size_bytes": size if size is not None else _storage_bytes(requests.get("storage")),
            "phase": _text_or_none(status.get("phase")) or "",
            "retained": not (isinstance(owners, list) and owners),
            "created_at": _text_or_none(meta.get("creationTimestamp")),
        })
    rows.sort(key=lambda row: (row["name"], row["pvc"]))
    return rows[:MAX_MODEL_VOLUMES]


def _storage_bytes(value: Any) -> int | None:
    """"100Gi"·"40G"·"1073741824" 를 바이트로. 모르는 모양이면 None."""
    match = _STORAGE.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    return int(float(match[1]) * _STORAGE_UNITS[match[2] or ""])


# ── 합치기 ───────────────────────────────────────────────

def build(nodes: list[dict[str, Any]], serving: list[dict[str, Any]],
          waiting: list[dict[str, Any]] | None = None,
          served_models: list[dict[str, Any]] | None = None,
          node_pools: list[dict[str, Any]] | None = None,
          benchmarks: list[dict[str, Any]] | None = None,
          counter: list[dict[str, Any]] | None = None,
          model_volumes: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """ClusterGpu. GPU 노드도 GPU 를 기다리는 파드도 GPU 풀도 없고 ServedModel 도 못 읽었으면 None — 앱은 "GPU 노드가 없어요" 를 띄운다.
    노드는 없고 기다리는 파드나 GPU 풀, 읽어 낸 ServedModel(빈 배열 포함)이 있으면 꺼진 GPU 클러스터다(nodes 빈 배열).
    served_models·node_pools 의 null 은 "못 읽었다", 빈 배열은 "없다" 다. 빈 node_pools 는 served_models 와 달리 gpu 칸을 만들지
    않는다 — CPU 풀만 있는 Karpenter 클러스터는 GPU 클러스터가 아니다. benchmarks·counter 는 칸을 만드는 근거가 아니다.
    counter 의 null 은 "못 찾았거나 못 읽었다", 빈 배열은 "읽었는데 모델이 없다" 다. model_volumes 도 칸을 만드는 근거가 아니다."""
    if not nodes and not waiting and served_models is None and not node_pools:
        return None
    return {"nodes": nodes, "serving": serving, "benchmarks": benchmarks or [], "waiting": waiting or [],
            "served_models": served_models, "node_pools": node_pools, "counter": counter, "model_volumes": model_volumes}


# ── Prometheus 글 형식 ────────────────────────────────────

_LINE = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Prometheus 글 형식(exposition)을 (이름, 라벨, 값) 목록으로. 읽지 못하는 줄은 건너뛴다.

    부가 정보라 깨진 출력도 오류로 올리지 않는다 — 개요 전체를 502 로 만들 까닭이 없다.
    """
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match is None:
            continue
        value = _float(match[3])
        if value is None or not math.isfinite(value):
            continue            # ±Inf 값은 round() 에서 OverflowError 다. 버킷의 le="+Inf" 는 라벨이라 따로 읽는다
        labels = {key: raw.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
                  for key, raw in _LABEL.findall(match[2] or "")}
        rows.append((match[1], labels, value))
    return rows


def _float(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return float(value)        # "+Inf"·"Inf" 도 읽는다
    except ValueError:
        return None


# ── 공통 ─────────────────────────────────────────────────

def _items(stdout: str) -> list[dict[str, Any]]:
    data = json.loads(stdout or "{}")
    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError("kubectl 출력의 items 가 목록이 아니다")
    return [item for item in items if isinstance(item, dict)]


def _strict_items(stdout: str) -> list[dict[str, Any]]:
    """_items 와 달리 items 칸이 없거나 객체가 아닌 항목이 섞여도 ValueError 다 — 빈 목록이 "없다" 로 읽히는 목록에 쓴다."""
    data = json.loads(stdout or "")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("kubectl 목록 모양이 아니다")
    return items


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count_or_none(value: Any) -> int | None:
    """1 이상 정수만. 0·음수·문자열·소수·bool 은 null 이다."""
    number = _int_or_none(value)
    return number if number is not None and number >= 1 else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _quantity_or_none(value: Any) -> int | None:
    """NodePool limits 의 GPU 수 — 2 나 "2". `1k` 같은 단위는 GPU 수에 쓰지 않아 null 이다."""
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _positive_int(value: Any) -> int:
    """자원 수량·라벨 숫자. 정수가 아니거나 0 이하면 0 (`500m` 같은 소수 수량은 GPU 에 없다)."""
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
