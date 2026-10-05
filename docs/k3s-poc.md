# k3s + devMac POC

## Topology

- The scheduler and dashboard run as separate Deployments in the
  `investory-orchestrator` namespace.
- Task rows, status events, repository configuration, and LangGraph checkpoints
  use the existing PostgreSQL service in namespace `postgres`, under the
  `investory_orchestrator` schema.
- The scheduler claims work in PostgreSQL and starts a bounded SSH command on
  devMac. The Mac checkout runs the existing workflow graph and local Codex
  CLI, with its worktrees, Dev Containers, Git operations, and GitHub App
  credentials remaining on the Mac.
- A per-task file lock on devMac prevents duplicate execution. On scheduler
  restart, it probes that lock over SSH before recovering an active task.
- The dashboard is a ClusterIP service. Access it with port forwarding; this
  POC does not publish it through a public ingress.

This keeps execution compatible with the existing Mac Codex login and avoids
moving repository write access or the Codex home into k3s. It requires the Mac
checkout to have the same code version as the scheduler image.

## Prepare secrets

Do not commit the following Kubernetes secrets. First create an SSH key pair
for the scheduler and add its public key to the Mac account that owns the
authenticated Codex home (`alex` in the current checkout) in
`~/.ssh/authorized_keys`, with a forced command and forwarding disabled. For
example, add this single line, replacing the key material:

```text
command="/Users/alex/projects/investory-orchestrator/scripts/mac-ssh-entrypoint.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... k3s-orchestrator
```

The forced command accepts only task `run` and lock `probe` requests. Verify
the Mac host-key fingerprint from the Mac console, then place the matching
`ssh-keyscan` record in a local `known_hosts` file.

Create the namespace, then create these secrets in it:

```sh
kubectl apply -f k8s/namespace.yaml
kubectl -n investory-orchestrator create secret generic mac-runner-ssh \
  --from-file=id_ed25519=/secure/path/orchestrator-mac-key \
  --from-file=known_hosts=/secure/path/mac-known_hosts
kubectl -n investory-orchestrator create secret generic orchestrator-runtime \
  --from-literal=database-url="$DATABASE_URL" \
  --from-literal=dashboard-api-token="$DASHBOARD_API_TOKEN" \
  --from-literal=github-installation-id="$GITHUB_INSTALLATION_ID" \
  --from-file=github-app-private-key=/secure/path/github-app.pem
```

`DATABASE_URL` must connect to the existing `postgres.postgres.svc.cluster.local`
service and use a role allowed to create and use the dedicated schema. Use the
same DSN in the Mac `.env`. Do not paste secret values into manifests or source
control.

On devMac, update the orchestrator checkout to the release used for the
container image, install its requirements in the configured virtualenv, and
set its `.env` to use that PostgreSQL DSN, local authenticated Codex home,
GitHub App credentials, `MAC_CLI_PYTHON` pointing to the checkout's virtualenv
Python, `GITHUB_REPOSITORY=spider-su/investory`, and `BASE_BRANCH=develop`.
Ensure the same `.env` sets `GITHUB_PRIVATE_KEY_PATH` to the key on devMac and
that the Codex executable is available in the non-interactive SSH `PATH` (the
entrypoint adds `/Users/alex/.local/bin`).
Verify `scripts/mac-ssh-entrypoint.sh` is executable and test the forced SSH
command as that Mac account before starting the scheduler.

## Build and deploy

Build and push an image to a registry accessible to k3s, then replace the image
tag in `k8s/deployment.yaml` with that immutable tag. For example:

```sh
docker buildx build --platform linux/amd64 \
  -t ghcr.io/spider-su/investory-orchestrator:<version> --push .
kubectl apply -k k8s/
kubectl -n investory-orchestrator rollout status deployment/orchestrator-scheduler
kubectl -n investory-orchestrator rollout status deployment/orchestrator-dashboard
```

The included NetworkPolicy adds permission for this namespace to reach the
Postgres pod on TCP/5432. Verify the cluster's registry pull credentials if the image
is private. The pod-to-Mac TCP/22 path was verified from the cluster, but SSH
key authentication and an actual Mac task have not yet been verified.

## Operate

Add or update the first repository configuration from the dashboard or API.
The default is `spider-su/investory`, base branch `develop`, and notification
mention `@spider-su`. GitHub Projects number and priority field are configurable
but project polling and priority synchronization are not part of this POC
slice yet.

Queue an issue through the scheduler pod:

```sh
kubectl -n investory-orchestrator exec deployment/orchestrator-scheduler -- \
  python -m app --submit-issue <issue-number>
```

Open the dashboard locally:

```sh
kubectl -n investory-orchestrator port-forward \
  service/orchestrator-dashboard 8080:8080
```

Then visit `http://127.0.0.1:8080` and enter `DASHBOARD_API_TOKEN`. The
dashboard shows tasks, current status, pull request, priority, counts, and
configured repositories. READY and BLOCKED issue tasks update one stable
GitHub issue comment that mentions the configured login. Pull requests remain
draft and require human review and merge.

## Current acceptance boundary

Implemented in this branch: PostgreSQL task/checkpoint support, task event
history, repository configuration CRUD, task/status dashboard APIs, a single
Mac SSH dispatch path with restart-aware task locks, terminal issue mentions,
and k3s manifests.

Still required before calling the POC verified: provision runtime secrets,
build/publish an immutable image, confirm SSH key authentication, apply the
manifest, verify the Mac uses the same PostgreSQL schema, configure an actual
GitHub Project and its priority field (the current GitHub CLI token lacks
`read:project`), and run one issue through READY/BLOCKED notification plus
human PR review. Do not enable unattended issue polling until those checks
pass.
