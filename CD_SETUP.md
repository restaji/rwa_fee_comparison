# CD Repo Setup — `rwa-comparison-api`

## Overview

`rwa-comparison-api` is a Python 3.11 Flask API (with WebSocket via flask-socketio/eventlet) that compares trading fees and slippage across multiple exchanges (Hyperliquid, Lighter, Aster, Avantis, Ostium, Extended, EdgeX, GRVT). It's stateless — no database, no Redis, no internal service dependencies. It only makes outbound HTTP calls to public/authenticated exchange APIs.

## ECR Image

- **Repository:** `avantis/rwa-comparison-api`
- **Container port:** `8000`
- **Health check:** `GET /` returns the index page (200 OK)

## Folder / File Structure to Create

Follow the same pattern as `avantis-server`. You need:

```
services/rwa-comparison-api/
├── values.yaml
└── overlays/
    └── mainnet/
        └── kustomization.yaml
```

## values.yaml

```yaml
project: avantis

secretId: ""

envFromSecrets:
  - variable: ASTER_API_KEY
    secretKey: ASTER_API_KEY
    secretId: "{{ quote .Values.secretId }}"
  - variable: ASTER_SECRET_KEY
    secretKey: ASTER_SECRET_KEY
    secretId: "{{ quote .Values.secretId }}"
  - variable: GRVT_API_KEY
    secretKey: GRVT_API_KEY
    secretId: "{{ quote .Values.secretId }}"
  - variable: EXTENDED_API_KEY
    secretKey: EXTENDED_API_KEY
    secretId: "{{ quote .Values.secretId }}"

env:
  PORT: "8000"

image:
  repository: avantis/rwa-comparison-api

global:
  version: main-0000000

deployment:
  readinessProbe:
    httpGet:
      path: /
      port: 8000
      scheme: HTTP
    initialDelaySeconds: 10
    periodSeconds: 10
    timeoutSeconds: 5
  resources:
    requests:
      memory: "128Mi"
      cpu: "100m"
    limits:
      memory: "512Mi"
      cpu: "500m"

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 5

service:
  ports:
    - portName: http
      port: 8000
      exposePort: 8080

securityContext:
  allowPrivilegeEscalation: false
  capabilities:
    drop:
      - ALL
```

## kustomization.yaml (mainnet overlay)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
helmCharts:
  - name: app
    releaseName: rwa-comparison-api
    includeCRDs: false
    version: 0.0.3
    namespace: avantis
    valuesFile: ../values.yaml
    valuesInline:
      environment: mainnet
      secretId: mainnet/rwa-comparison-api

helmGlobals:
  chartHome: ../../../helm-charts
```

## Resource Estimates

| Resource | Request | Limit  | Rationale |
|----------|---------|--------|-----------|
| CPU      | 100m    | 500m   | I/O-bound (outbound HTTP to exchanges), minimal compute. Single gunicorn worker with eventlet. |
| Memory   | 128Mi   | 512Mi  | Python 3.11 + Flask + eventlet + requests. No large data structures or caches. Idle ~80-120MB, peak ~300MB under concurrent load. |
| Replicas | 2 min   | 5 max  | Stateless, so scales horizontally. 2 for availability, 5 handles traffic spikes. Single eventlet worker per pod means each pod handles moderate concurrency. |

No need for `large` node tolerations/selectors — this is a lightweight service.

## Secrets to Provision

All four are **optional** — the app falls back to public API data if they're missing. But for accurate fee data, provision them:

| Secret Key         | Purpose                                   |
|--------------------|-------------------------------------------|
| `ASTER_API_KEY`    | Authenticated Aster DEX fee queries       |
| `ASTER_SECRET_KEY` | HMAC signing for Aster authenticated APIs |
| `GRVT_API_KEY`     | GRVT authenticated login + fee data       |
| `EXTENDED_API_KEY` | Extended Exchange (Starknet) API access   |

Store these under `mainnet/rwa-comparison-api` in your secrets manager.

## Ingress (optional)

Add if you want a public endpoint:

```yaml
ingress:
  enabled: true
  hosts:
    - host: rwa-api.avantisfi.com
      paths:
        - path: /
          portName: http
```

## Notes

- **Single worker:** The app runs `gunicorn -k eventlet -w 1` — one worker using cooperative concurrency. This is intentional (eventlet + socketio requires a single worker). Scaling is horizontal via replicas.
- **WebSocket support:** The service exposes flask-socketio on the same port. If you need WebSocket through ingress, ensure your ingress controller has WebSocket/upgrade support enabled.
- **No internal service dependencies:** Unlike `avantis-server`, this service doesn't call any internal cluster services. All traffic is outbound to public exchange APIs.
