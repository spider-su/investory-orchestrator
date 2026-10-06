from __future__ import annotations

import os
import re
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.tasks import TaskStore


class RepositoryConfig(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    enabled: bool = True
    base_branch: str = Field(default="develop", pattern=r"^[A-Za-z0-9._/-]+$")
    github_project_number: int | None = Field(default=None, ge=1)
    priority_field_name: str = Field(default="Priority", min_length=1, max_length=100)
    notification_login: str = Field(default="", pattern=r"^$|^[A-Za-z0-9-]+$")
    poll_interval_seconds: int = Field(default=60, ge=30, le=86400)


def create_app(store: TaskStore | None = None) -> FastAPI:
    task_store = store or TaskStore(
        os.getenv("DATABASE_URL") or os.getenv("TASK_DB", "/app/data/tasks.db")
    )
    app = FastAPI(title="Investory Orchestrator", version="0.1.0")
    expected_token = os.getenv("DASHBOARD_API_TOKEN", "")

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        if expected_token and authorization != f"Bearer {expected_token}":
            raise HTTPException(status_code=401, detail="Authentication required")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return DASHBOARD_HTML

    @app.get("/api/tasks", dependencies=[Depends(authorize)])
    async def list_tasks() -> list[dict[str, Any]]:
        return [asdict(task) | {"status": task.status.value} for task in task_store.list()]

    @app.get("/api/tasks/{task_id:path}/events", dependencies=[Depends(authorize)])
    async def task_events(task_id: str) -> list[dict[str, Any]]:
        if task_store.get(task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task_store.list_events(task_id)

    @app.get("/api/stats", dependencies=[Depends(authorize)])
    async def statistics() -> dict[str, Any]:
        return task_store.statistics()

    @app.get("/api/repositories", dependencies=[Depends(authorize)])
    async def repositories() -> list[dict[str, Any]]:
        return task_store.list_repositories()

    @app.put("/api/repositories", dependencies=[Depends(authorize)])
    async def upsert_repository(config: RepositoryConfig) -> dict[str, Any]:
        return task_store.save_repository(config.model_dump())

    @app.delete("/api/repositories/{repository:path}", dependencies=[Depends(authorize)])
    async def remove_repository(repository: str) -> dict[str, bool]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise HTTPException(status_code=422, detail="Invalid repository")
        return {"deleted": task_store.delete_repository(repository)}

    return app


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Investory Orchestrator</title><style>
body{font:15px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17212b;background:#f6f8fa}
header,.card{background:white;border:1px solid #d8dee4;border-radius:8px;padding:1rem;margin-bottom:1rem}
header{display:flex;justify-content:space-between;align-items:center}h1{font-size:1.35rem;margin:0}
.stats{display:flex;gap:.7rem;flex-wrap:wrap}.stat{min-width:120px}.muted{color:#57606a}
table{width:100%;border-collapse:collapse;background:white}th,td{text-align:left;border-bottom:1px solid #d8dee4;padding:.65rem}
input,button{font:inherit;padding:.45rem;border:1px solid #afb8c1;border-radius:5px}button{cursor:pointer;background:#0969da;color:white}
form{display:flex;gap:.5rem;flex-wrap:wrap}code{font-size:.9em}#error{color:#cf222e}
</style></head><body><header><h1>Investory Orchestrator</h1><label>API token <input id="token" type="password" placeholder="dashboard token"></label></header>
<p id="error"></p><section class="card"><h2>Task statistics</h2><div id="stats" class="stats"></div></section>
<section class="card"><h2>Tasks</h2><table><thead><tr><th>Task</th><th>Repository</th><th>Status</th><th>Priority</th><th>PR</th><th>Updated</th></tr></thead><tbody id="tasks"></tbody></table></section>
<section class="card"><h2>Repository configuration</h2><form id="repo"><input name="repository" value="spider-su/investory" required placeholder="owner/repo"><input name="base_branch" value="develop" required placeholder="base branch"><input name="notification_login" value="spider-su" placeholder="GitHub login to mention"><input name="github_project_number" type="number" min="1" placeholder="Project number"><input name="priority_field_name" value="Priority" required placeholder="Priority field"><input name="poll_interval_seconds" type="number" min="30" max="86400" value="60" required title="Issue polling interval in seconds"><label><input name="enabled" type="checkbox" checked> Enabled</label><button>Save repository</button></form><p class="muted">Repository settings are stored in PostgreSQL. Project number is optional until Projects access is configured.</p><ul id="repositories"></ul></section>
<script>
const tokenInput=document.querySelector('#token');tokenInput.value=sessionStorage.getItem('api-token')||'';tokenInput.onchange=()=>{sessionStorage.setItem('api-token',tokenInput.value);refresh()};
const apiPrefix=location.pathname.replace(/\\/+$/,'');
async function api(path,options={}){const response=await fetch(apiPrefix+path,{...options,headers:{'Content-Type':'application/json','Authorization':'Bearer '+tokenInput.value,...options.headers}});if(!response.ok)throw Error((await response.text())||response.statusText);return response.json()}
function date(value){return new Date(value*1000).toLocaleString()}
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refresh(){try{document.querySelector('#error').textContent='';const [stats,tasks,repos]=await Promise.all([api('/api/stats'),api('/api/tasks'),api('/api/repositories')]);document.querySelector('#stats').innerHTML=Object.entries({'Total':stats.total,'Last 7 days':stats.last_7_days,...stats.by_status}).map(([k,v])=>`<div class="stat"><b>${esc(v)}</b><div class="muted">${esc(k)}</div></div>`).join('');document.querySelector('#tasks').innerHTML=tasks.map(t=>`<tr><td>${esc(t.title)}<br><small class="muted">${esc(t.task_id)}</small></td><td>${esc(t.repository)}</td><td>${esc(t.status)}</td><td>${t.priority}</td><td>${String(t.pr_url||'').startsWith('https://github.com/')?`<a href="${esc(t.pr_url)}" rel="noopener">#${t.pr_number}</a>`:'—'}</td><td>${esc(date(t.updated_at))}</td></tr>`).join('');document.querySelector('#repositories').innerHTML=repos.map(r=>`<li>${r.enabled?'●':'○'} <b>${esc(r.repository)}</b> → ${esc(r.base_branch)} · notify @${esc(r.notification_login||'not set')} <button data-delete="${esc(r.repository)}">Delete</button></li>`).join('');document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=async()=>{await api('/api/repositories/'+encodeURIComponent(b.dataset.delete),{method:'DELETE'});refresh()})}catch(e){document.querySelector('#error').textContent=e.message}}
document.querySelector('#repo').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const project=f.get('github_project_number');try{await api('/api/repositories',{method:'PUT',body:JSON.stringify({repository:f.get('repository'),base_branch:f.get('base_branch'),notification_login:f.get('notification_login'),github_project_number:project?Number(project):null,priority_field_name:f.get('priority_field_name'),poll_interval_seconds:Number(f.get('poll_interval_seconds')),enabled:f.has('enabled')})});refresh()}catch(err){document.querySelector('#error').textContent=err.message}};
refresh();setInterval(refresh,15000);
</script></body></html>"""
