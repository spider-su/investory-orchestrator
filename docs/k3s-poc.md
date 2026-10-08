# k3s + devMac POC

## Topology

- The scheduler and dashboard run as separate Deployments in the
  `investory-orchestrator` namespace.
- Task rows, status events, repository configuration, and LangGraph checkpoints
  use the existing PostgreSQL service in namespace `postgres`, under the
  `investory_orchestrator` schema.
- The scheduler claims work in PostgreSQL and starts a bounded SSH command on
  devMac. The Mac checkout runs the existing workflow graph and local Codex
  CLI, with its isolated Git checkouts, Dev Containers, Git operations, and GitHub App
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
python3 -c 'import getpass, subprocess; token=getpass.getpass("Slack bot token (input hidden): ").strip(); subprocess.run(["kubectl", "-n", "investory-orchestrator", "create", "secret", "generic", "orchestrator-slack", "--from-file=bot-token=/dev/stdin"], input=token.encode(), check=True)'
```

`DATABASE_URL` must connect to the existing `postgres.postgres.svc.cluster.local`
service and use a role allowed to create and use the dedicated schema. Use the
same DSN in the Mac `.env`. Do not paste secret values into manifests or source
control.

Slack task updates are delivered centrally by the Kubernetes scheduler from
the shared task activity log. The bot token is stored only in the
`orchestrator-slack` Kubernetes Secret; the channel ID is non-secret
configuration in `k8s/config.yaml`. The Slack app needs the `chat:write` and
`chat:write.customize` bot scopes and must be a member of `#orchestrator`. The
Mac runner does not need either Slack credential. Do not put tokens in this
repository. The app-level `xapp` token is not needed for these outbound posts.

On devMac, update the orchestrator checkout to the release used for the
container image, install its requirements in the configured virtualenv, and
set its `.env` to use that PostgreSQL DSN, local authenticated Codex home,
GitHub App credentials, `MAC_CLI_PYTHON` pointing to the checkout's virtualenv
Python, `GITHUB_REPOSITORY=spider-su/investory`, and `BASE_BRANCH=develop`.
The forced SSH entrypoint stores task workspaces under
`~/.investory-orchestrator/task-workspaces` by default, independent of container
paths passed in the SSH environment. Set `MAC_WORKSPACES_DIR` in the Mac
runner environment to choose another writable location.
Failed-attempt patch files are stored under
`~/.investory-orchestrator/runs` by default; override this with
`MAC_RUNS_DIR` when configuring the Mac runner.
Ensure the same `.env` sets `GITHUB_PRIVATE_KEY_PATH` to the key on devMac and
that the Codex and Node executables are available in the non-interactive SSH
`PATH` (the entrypoint adds `/Users/alex/.local/bin` and selects the default
NVM Node version when NVM is installed). The Mac also needs Docker Compose v2
available as the `docker compose` CLI plugin for target scripts that use it.
Verify `scripts/mac-ssh-entrypoint.sh` is executable and test the forced SSH
command as that Mac account before starting the scheduler.

## Build and deploy

The `Docker Build and Publish` GitHub Actions workflow publishes two targets
in `aserobaba/orchestrator` after `Agent PR validation` succeeds on `main`:
`latest`/`sha-<commit>` retain the full local tooling image, while
`k3s-latest`/`k3s-sha-<commit>` contain only the Python service runtime and SSH
client needed to reach devMac. Both images are scanned and receive an SBOM.
BuildKit caches each target separately so source-only changes can reuse the
larger dependency layers. The workflow can also be started manually after
selecting a source branch.

Configure the repository Actions secrets `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN`. The token must have permission to push to the
`aserobaba/orchestrator` Docker Hub repository. Keep the image public for
unauthenticated k3s pulls, or configure an image pull secret.

The `ops-autopilot` GitOps repository owns the Helm chart and Argo CD
registration for the development POC. Its dev deployment follows
`k3s-latest`; publishing an image does not make Argo CD roll it out. Run the
`Promote orchestrator development image` workflow in `ops-autopilot` with the
`k3s-sha-...` tag from the publish workflow. Review and merge the generated
GitOps PR to deploy it. Production deployment requires a published immutable
digest and reviewed GitOps promotion before it is registered.
The application requires the runtime and SSH secrets described above before
its pods can become ready.

For a direct, temporary deployment, build and push an image to a registry
accessible to k3s, then select that tag in `k8s/deployment.yaml`:

```sh
docker buildx build --platform linux/amd64 \
  --target k3s -t aserobaba/orchestrator:k3s-<version> --push .
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

Reconcile an issue whose PR was merged before the scheduler recorded it:

```sh
kubectl -n investory-orchestrator exec deployment/orchestrator-scheduler -- \
  python -m app --reconcile-merged-pr 'spider-su/investory#104'
```

Open the dashboard locally:

```sh
kubectl -n investory-orchestrator port-forward \
  service/orchestrator-dashboard 8080:8080
```

Then visit `http://127.0.0.1:8080` and enter `DASHBOARD_API_TOKEN`. The
dashboard shows tasks, current status, pull request, priority, counts, and
configured repositories. READY, BLOCKED, and COMPLETED issue tasks update one
stable GitHub issue comment that mentions the configured login. Final review runs on
devMac over SSH against the exact PR head. Pull requests require human review
and merge; after merge, the scheduler waits for post-merge CI, records
`COMPLETED`, and closes the linked issue. Use `--reconcile-merged-pr <task-id>`
for a human merge completed before the scheduler could track it.

## Current acceptance boundary

Implemented in this branch: PostgreSQL task/checkpoint support, task event
history, repository configuration CRUD, task/status dashboard APIs, a single
Mac SSH dispatch path with restart-aware task locks, terminal issue mentions,
and k3s manifests.

The remaining live acceptance is to label an issue `ready_to_develop` and
watch it through remote execution, CI, final review, human review and merge,
post-merge completion, and issue closure. GitHub Projects/priority
synchronization remains optional and is not part of label-based intake.
