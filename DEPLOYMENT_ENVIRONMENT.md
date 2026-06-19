# MetaCentrum Deployment Environment — Findings & Coexistence Check

**Date:** 2026-06-19
**Question answered:** Can the new geoeconomic newsletter pipeline run side by side, cleanly, with the app already in the MetaCentrum account?
**Verdict:** ✅ **Yes — cleanly.** No real conflict. A few things must be done right (see §5).

---

## 1. Where things actually run

The account's existing app does **not** run on the SSH frontend (`skirit`). It runs on the
**CERIT-SC / e-INFRA Kubernetes (Rancher) cloud**.

| Fact | Value |
|---|---|
| SSH frontend | `skirit.metacentrum.cz` → `skirit.grid.cesnet.cz` (Debian Bookworm) |
| Home | `/storage/brno2/home/jalalhussein` (symlink); other homes on `brno12-cerit`, `praha1` |
| K8s API | `https://rancher.cloud.e-infra.cz/k8s/clusters/c-m-qvndqhf6` |
| Kubeconfig | `~/.kube/kuba-cluster.yaml` (context `kuba-cluster`) |
| Namespace | `hussein-ns` |
| K8s user | `u-gq5yxf3imu` |
| kubectl | `~/bin/kubectl` v1.28 (only tool in `~/bin`) |

The frontend has **no long-lived process, no tmux/screen, no crontab**. `~/sahel-project`, `~/siem`
(scratch/validation scripts) and `~/ondemand` (Open OnDemand portal logs) are not the app.
**PBS (`qstat`) is Kerberos-gated** and unavailable over key-only SSH — but irrelevant, since the
deployment target is Kubernetes, not PBS.

---

## 2. The existing app — "siem-project" (in `hussein-ns`)

| Component | Detail |
|---|---|
| `siem-api` (Deployment, 1 pod) | API, up ~46d. Image `jalalhussein/siem-api:20260503-1` |
| Ingress `siem-api` | Public host **`siem-project.com`** via `kuba-pub.cerit-sc.cz`, nginx, TLS |
| `siem-db` (StatefulSet `siem-db-0`) | **PostgreSQL 17 + PostGIS** (`postgis/postgis:17-3.5`), 5432, up ~74d |
| PVC `db-data-siem-db-0` | 10Gi, `nfs-csi`, ReclaimPolicy **Retain** |
| Services | `siem-db` (headless), `siem-db-rw` (5432), `siem-api` (80) |
| CronJob `acled-daily-update` | **`0 6 * * *` (06:00 UTC), currently SUSPENDED.** Image `jalalhussein/siem-ingest`. Ingests ACLED conflict data into `siem-db`. Last success 2026-05-14 |
| NetworkPolicy `siem-db-network-policy` | Restricts ingress to pods matching `cnpg.io/cluster=siem-db` |
| Secrets | `acled-credentials`, `siem-db-credentials`, `siem-app-credentials`, `siem-project-tls` |
| ConfigMaps | `siem-db-init`, `kube-root-ca.crt` |

> Note: `siem-db` pods carry CloudNativePG (CNPG) labels, but **no `Cluster` CR named `siem-db`
> currently exists** — it appears to be an orphaned/standalone StatefulSet now. (Another reason not
> to graft the new app onto it.)

**Build/deploy pattern in use:** self-built images pushed to Docker Hub under `jalalhussein/*`,
deployed as namespaced K8s objects with a hardened pod security context.

---

## 3. Namespace capacity (`hussein-ns`)

Only ResourceQuota `default-28hbj` exists — it governs **CPU/memory only** (no PVC/storage/pod-count cap).

| Resource | Used | Hard | Headroom |
|---|---|---|---|
| requests.cpu | 200m | 20 | ~19.8 cores |
| requests.memory | 512Mi | 40Gi | ~39.5Gi |
| limits.cpu | 1 | 32 | 31 cores |
| limits.memory | 1.5Gi | 64Gi | ~62.5Gi |

**LimitRange `default-6cdfq`:** default per-container = **1 CPU / 512Mi** request *and* limit
(no min/max). Containers with no explicit `resources` inherit 512Mi — too small for the new pipeline.

**Storage classes available:** `nfs-csi` (default, Retain), `csi-ceph-rbd-du` (RBD block, expandable),
`beegfs-csi`, `rook-cephfs`, `s3-csi`, `nfs-csi-temporary`, `nfs-csi-backup`, `zfs-csi`, `cvmfs`, `onedata`, `sshfs`.

**Confirmed deploy permissions** (`kubectl auth can-i create …` = yes for all):
cronjobs, jobs, deployments, statefulsets, persistentvolumeclaims, secrets, services, configmaps,
networkpolicies, ingresses, **`clusters.postgresql.cnpg.io`**, `pooler.postgresql.cnpg.io`.
→ The new app can deploy everything it needs, including its own **CNPG-managed Postgres**.
(Cluster-scoped operator CRDs `acid.zalan.do` and `ray.io` are forbidden — not needed.)

---

## 4. Coexistence analysis

| Dimension | Conflict? | Why |
|---|---|---|
| Compute quota | ❌ None | SIEM uses ~1% of CPU quota; new daily batch is short-lived |
| Storage quota | ❌ None | No storage/PVC cap; new PVCs fine |
| Scheduling | ❌ None | Existing 06:00 (suspended) vs new 07:00; separate CronJobs |
| Naming | ❌ None | Existing `siem-*`/`acled-*`; new uses `geoecon-*` |
| Ingress / hosts | ❌ None | `siem-project.com` not reused; new app likely needs no ingress |
| Permissions | ✅ OK | Full namespace edit confirmed |

**Bottom line:** the new pipeline drops in as additive, isolated objects and does not touch any
`siem-*` resource.

---

## 5. Must-do care-points for the new app

1. **Separate database.** `schema.sql` uses generic table names (`runs`, `articles`, `clusters`, …)
   that would collide if shared. Stand up a **dedicated Postgres** — a fresh CNPG `Cluster`
   (e.g. `geoecon-db`) with its own PVC/credentials. Do **not** reuse `siem-db`.
   Use a pgvector-capable image if/when the deferred embedding column is wanted.
2. **Explicit resources.** Override the 512Mi LimitRange default — request ~2 CPU / 6Gi for the
   pipeline pod (BGE-M3 ≈2GB resident + clustering + ~150 articles). Quota allows far more.
3. **Timezone.** Existing cron runs 06:00 **UTC** (no `timeZone`). Decide whether blueprint "07:00 AM"
   means UTC or `Europe/Prague` and set `spec.timeZone` explicitly (K8s 1.28 supports it).
4. **Mirror the hardened pattern.** Copy the ACLED CronJob's security context (runAsNonRoot, uid/gid
   1000, drop ALL caps, seccomp RuntimeDefault, `concurrencyPolicy: Forbid`, history limits),
   use `envFrom` a `geoecon-*` secret, and add a NetworkPolicy for the new DB like `siem-db` has.

---

## 6. Open / unverified (not coexistence blockers)

- **Synthesis model backend.** Blueprint names `deepseek-v3.2` / `kimi-k2.6`. If these are API calls,
  fine. If they're meant to run **locally on MetaCentrum GPUs**, that's a separate GPU-quota question —
  the current quota shows **no GPU**. (Embeddings for 150 docs/day run fine on CPU.)
- **Egress.** ACLED job already calls external APIs, so egress to GDELT + LLM APIs should work; no
  default-deny egress policy seen. Confirm if a new DB NetworkPolicy is added.
- **No app code yet** — only `PIPELINE_BLUEPRINT.md` and `schema.sql`. Build is the next track.

---

## 7. SSH access (how this session connected)

- New key generated: `~/.ssh/id_ed25519_metacentrum_2026`
  (fingerprint `SHA256:uO272GTDW3erzm8bUkgQK2ov0dkcvxzuHHp8epM3ipU`), no passphrase.
- Authorized by appending the pubkey to `~/.ssh/authorized_keys` on skirit (and added to the
  MetaCentrum portal as the durable copy).
- `~/.ssh/config` alias updated: `Host skirit … skirit.metacentrum.cz …` → this key, `IdentitiesOnly yes`.
- Old MetaCentrum keys (`id_ed25519_meta_2026`, `id_ed25519_metacentrum`, `id_ed25519_metacentrum_new`,
  `metacentrum_new`, `metacentrum_codex`) are **pending cleanup** — to be removed/backed up once the
  new key is confirmed stable (it is, as of this session).
