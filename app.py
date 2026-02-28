#!/usr/bin/env python3
"""OpenClaw Dashboard API - VPS üzerinde çalışır, Tailscale üzerinden erişilir"""

from flask import Flask, jsonify, send_from_directory, request
from pathlib import Path
import json, os, subprocess, re, glob as globmod, time
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

app = Flask(__name__, static_folder="static")
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

@app.after_request
def add_no_cache_headers(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

OPENCLAW_DIR = Path("/root/.openclaw")
WORKSPACE = OPENCLAW_DIR / "workspace"
AGENTS_DIR = OPENCLAW_DIR / "agents"
IDEAS_DIR = WORKSPACE / "data" / "ideas"
SCRIPTS_DIR = Path("/root/scripts")
API_KEY = "nootle-dashboard-2024-secret"


def get_active_sessions():
    """Parse openclaw status to find active agent sessions and heartbeats"""
    result = {"sessions": {}, "heartbeats": {}}
    try:
        r = subprocess.run(["openclaw", "status"], capture_output=True, text=True, timeout=15)
        out = r.stdout

        # Parse Sessions table: lines like "│ agent:main:main │ direct │ 6m ago │ ..."
        for line in out.split("\n"):
            # Session lines: agent:AGENT_ID:xxx
            m = re.search(r'agent:([^:\s│]+):(\S+)\s*│\s*\w+\s*│\s*(\S+\s*\S*)\s*│\s*(\S+)', line)
            if m:
                agent_id = m.group(1)
                session_age = m.group(3).strip()
                model = m.group(4).strip()
                result["sessions"][agent_id] = {
                    "active": True,
                    "age": session_age,
                    "model": model
                }

            # Heartbeat line: "30m (main), disabled (ai-agent), ..."
            if "heartbeat" in line.lower():
                # Extract all (status) (agent-name) pairs
                hb_text = line.split("│")[-1] if "│" in line else line
                for hm in re.finditer(r'(\w[\w\s]*?)\s*\(([^)]+)\)', hb_text):
                    status = hm.group(1).strip().lower()
                    agent_name = hm.group(2).strip()
                    # Map agent name to id (e.g. "ai-agent" -> "ai-agent", "main" -> "main")
                    result["heartbeats"][agent_name] = status != "disabled"

    except Exception:
        pass
    return result


def check_key():
    return request.headers.get("X-API-Key", "") == API_KEY or request.args.get("key") == API_KEY


AGENT_TEAM_ROLES = [
    {"id": "agent-team-manager", "name": "Ralph Manager", "emoji": "🧠", "role": "Agent Team Orchestrator"},
    {"id": "agent-team-planner", "name": "Ralph Planner", "emoji": "🗺️", "role": "Plan + Scope"},
    {"id": "agent-team-coder", "name": "Ralph Coder", "emoji": "💻", "role": "Implementation"},
    {"id": "agent-team-reviewer", "name": "Ralph Reviewer", "emoji": "🔍", "role": "Code Review"},
    {"id": "agent-team-tester", "name": "Ralph Tester", "emoji": "🧪", "role": "Test Runner"},
    {"id": "agent-team-reporter", "name": "Ralph Reporter", "emoji": "📣", "role": "Result Report"},
]

AGENT_TEAM_STEP_TO_ID = {
    "planner": "agent-team-planner",
    "coder": "agent-team-coder",
    "reviewer": "agent-team-reviewer",
    "tester": "agent-team-tester",
    "reporter": "agent-team-reporter",
}


def _parse_iso_ts(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _get_agent_team_tasks():
    try:
        code, data = agent_team_request("GET", "/api/tasks", timeout=5)
        if code == 200 and isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _extract_agent_team_step(task):
    logs = task.get("logs") or ""
    current_step = None
    for raw in reversed(logs.splitlines()):
        line = raw.lower()
        if "planner agent started" in line:
            current_step = "planner"
            break
        if "coder agent started" in line:
            current_step = "coder"
            break
        if "reviewer agent started" in line:
            current_step = "reviewer"
            break
        if "tester agent started" in line:
            current_step = "tester"
            break
        if "reporter agent started" in line:
            current_step = "reporter"
            break

    report = task.get("report")
    if report and isinstance(report, str):
        try:
            parsed = json.loads(report)
            steps = parsed.get("steps", [])
            if steps:
                last = steps[-1].get("agent")
                if last in AGENT_TEAM_STEP_TO_ID:
                    current_step = last
        except Exception:
            pass
    return current_step


def _build_agent_team_virtual_agents(tasks):
    if not tasks:
        return []

    tasks_sorted = sorted(tasks, key=lambda t: _parse_iso_ts(t.get("updated_at", "")), reverse=True)
    latest = tasks_sorted[0]
    latest_goal = str(latest.get("goal", ""))[:80]
    latest_status = latest.get("status", "unknown")
    latest_step = _extract_agent_team_step(latest)

    virtual_agents = []
    for role in AGENT_TEAM_ROLES:
        rid = role["id"]
        step_key = rid.replace("agent-team-", "")
        is_manager = rid == "agent-team-manager"
        active = False
        current_skill = ""
        current_task = ""

        if is_manager:
            active = latest_status in {"queued", "in_progress"}
            current_skill = "orchestration"
            current_task = latest_goal or "waiting tasks"
        else:
            active = latest_status == "in_progress" and AGENT_TEAM_STEP_TO_ID.get(latest_step or "") == rid
            current_skill = step_key
            if latest_goal:
                current_task = latest_goal

        virtual_agents.append(
            {
                "id": rid,
                "name": role["name"],
                "emoji": role["emoji"],
                "role": role["role"],
                "active": active,
                "last_active": latest.get("updated_at", "unknown"),
                "model": "agent-team",
                "session": latest_status in {"queued", "in_progress"},
                "heartbeat_enabled": True,
                "current_task": current_task,
                "current_skill": current_skill,
                "files": {},
                "skills": [],
            }
        )
    return virtual_agents


def _build_agent_team_activity(tasks):
    agents_info = {}
    events = []
    if not tasks:
        return agents_info, events

    for role in AGENT_TEAM_ROLES:
        agents_info[role["id"]] = {"current_task": None, "current_skill": None, "last_updated": 0}

    tasks_sorted = sorted(tasks, key=lambda t: _parse_iso_ts(t.get("updated_at", "")), reverse=False)
    for task in tasks_sorted[-10:]:
        goal = str(task.get("goal", ""))[:80]
        status = str(task.get("status", "unknown"))
        task_id = task.get("id", 0)
        updated_at = task.get("updated_at", "")
        updated_ts = _parse_iso_ts(updated_at)
        logs = task.get("logs") or ""
        previous = "agent-team-manager"

        for line in logs.splitlines():
            low = line.lower()
            ts = line.split("|", 1)[0].strip()
            step = None
            if "planner agent started" in low:
                step = "planner"
            elif "coder agent started" in low:
                step = "coder"
            elif "reviewer agent started" in low:
                step = "reviewer"
            elif "tester agent started" in low:
                step = "tester"
            elif "reporter agent started" in low:
                step = "reporter"

            if not step:
                continue

            to_id = AGENT_TEAM_STEP_TO_ID[step]
            agents_info[to_id] = {
                "current_task": goal,
                "current_skill": step,
                "last_updated": _parse_iso_ts(ts),
            }
            events.append(
                {
                    "id": f"agent-team:{task_id}:{ts}:{step}",
                    "type": "agent_call",
                    "from": previous,
                    "to": to_id,
                    "msg": f"Task #{task_id}: {step}",
                    "ts": ts or updated_at,
                }
            )
            previous = to_id

        if status == "completed":
            events.append(
                {
                    "id": f"agent-team:{task_id}:done",
                    "type": "task_done",
                    "from": "agent-team-reporter",
                    "to": "agent-team-manager",
                    "msg": f"Task #{task_id} completed",
                    "ts": updated_at,
                }
            )
        elif status == "failed":
            events.append(
                {
                    "id": f"agent-team:{task_id}:failed",
                    "type": "task_start",
                    "from": "agent-team-manager",
                    "to": "agent-team-manager",
                    "msg": f"Task #{task_id} failed",
                    "ts": updated_at,
                }
            )

        agents_info["agent-team-manager"] = {
            "current_task": goal,
            "current_skill": "orchestration",
            "last_updated": updated_ts,
        }

    return agents_info, events[-30:]


# --- DASHBOARD UI ---
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "service": "openclaw-dashboard"})


# --- AGENTS ---
@app.route("/api/agents")
def get_agents():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    agents = []
    config = json.loads((OPENCLAW_DIR / "openclaw.json").read_text())

    status_info = get_active_sessions()
    sessions = status_info["sessions"]
    heartbeats = status_info["heartbeats"]

    for agent_conf in config.get("agents", {}).get("list", []):
        agent_id = agent_conf["id"]

        if agent_id == "main":
            ws = WORKSPACE
        else:
            ws = AGENTS_DIR / agent_id / "workspace"

        agent = {"id": agent_id, "files": {}, "skills": []}

        # Activity from live sessions
        session = sessions.get(agent_id)
        if session:
            agent["active"] = True
            agent["last_active"] = session["age"]
            agent["model"] = session.get("model", "")
            agent["session"] = True
        else:
            agent["active"] = False
            agent["session"] = False
            agent["model"] = ""

        # Heartbeat enabled?
        agent["heartbeat_enabled"] = heartbeats.get(agent_id, False)

        # Check heartbeat file for last activity time (fallback)
        hb = ws / "HEARTBEAT.md"
        if hb.exists():
            try:
                mtime = hb.stat().st_mtime
                ago = time.time() - mtime
                if ago < 300:
                    if not agent["active"]:
                        agent["active"] = True
                    if not session:
                        agent["last_active"] = "just now"
                elif not session:
                    if ago < 3600:
                        agent["last_active"] = f"{int(ago/60)}m ago"
                    elif ago < 86400:
                        agent["last_active"] = f"{int(ago/3600)}h ago"
                    else:
                        agent["last_active"] = f"{int(ago/86400)}d ago"
            except Exception:
                pass
        if "last_active" not in agent:
            agent["last_active"] = "unknown"

        # Check current task from HEARTBEAT.md content
        if hb.exists():
            try:
                hb_content = hb.read_text()
                for line in hb_content.split("\n"):
                    if "task:" in line.lower() or "current:" in line.lower() or "doing:" in line.lower():
                        agent["current_task"] = line.split(":", 1)[1].strip()[:60]
                        break
                    if "skill:" in line.lower() or "using:" in line.lower():
                        agent["current_skill"] = line.split(":", 1)[1].strip()[:40]
                        break
            except Exception:
                pass

        # Read identity
        identity_file = ws / "IDENTITY.md"
        if identity_file.exists():
            content = identity_file.read_text()
            agent["files"]["IDENTITY.md"] = content
            for line in content.split("\n"):
                if "name:" in line.lower():
                    agent["name"] = line.split(":", 1)[1].strip().strip("*")
                if "emoji:" in line.lower():
                    agent["emoji"] = line.split(":", 1)[1].strip().strip("*")
                if "role:" in line.lower():
                    agent["role"] = line.split(":", 1)[1].strip().strip("*")

        # Read other files
        for fname in ["SOUL.md", "TOOLS.md", "RULES.md", "BRAIN.md", "HEARTBEAT.md", "AGENTS.md", "LESSONS.md"]:
            fpath = ws / fname
            if fpath.exists():
                agent["files"][fname] = fpath.read_text()

        # Skills
        skills_dir = ws / "skills"
        if skills_dir.exists():
            for skill_dir in sorted(skills_dir.iterdir()):
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    skill = {"name": skill_dir.name}
                    if skill_file.exists():
                        content = skill_file.read_text()
                        skill["content"] = content
                        # Parse triggers
                        in_trigger = False
                        triggers = []
                        for line in content.split("\n"):
                            if "trigger:" in line.lower():
                                in_trigger = True
                                continue
                            if in_trigger:
                                if line.strip().startswith("- "):
                                    triggers.append(line.strip()[2:])
                                elif line.strip().startswith("---"):
                                    break
                                elif not line.strip():
                                    continue
                                else:
                                    break
                        skill["triggers"] = triggers
                    agent["skills"].append(skill)

        agents.append(agent)

    # Virtual agents for Agent Team workflow (Planner/Coder/Reviewer/Tester/Reporter).
    team_tasks = _get_agent_team_tasks()
    agents.extend(_build_agent_team_virtual_agents(team_tasks))

    return jsonify(agents)


@app.route("/api/activity")
def get_activity():
    """Per-agent current tasks + recent agent-to-agent events from gateway log"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    result = {"agents": {}, "events": []}

    try:
        config = json.loads((OPENCLAW_DIR / "openclaw.json").read_text())
    except Exception:
        config = {}

    known_ids = {a["id"] for a in config.get("agents", {}).get("list", [])}

    # Per-agent current task from HEARTBEAT.md
    for agent_conf in config.get("agents", {}).get("list", []):
        agent_id = agent_conf["id"]
        ws = WORKSPACE if agent_id == "main" else AGENTS_DIR / agent_id / "workspace"
        info = {"current_task": None, "current_skill": None, "last_updated": 0}
        hb = ws / "HEARTBEAT.md"
        if hb.exists():
            try:
                mtime = hb.stat().st_mtime
                info["last_updated"] = mtime
                content = hb.read_text()
                for line in content.split("\n"):
                    l = line.strip()
                    if not l:
                        continue
                    low = l.lower()
                    for kw in ["task:", "current task:", "current:", "doing:", "working on:", "🎯", "📋"]:
                        if low.startswith(kw.lower()):
                            val = l[len(kw):].strip(" :-")
                            if len(val) > 3:
                                info["current_task"] = val[:80]
                                break
                    if info["current_task"]:
                        break
                    for kw in ["skill:", "using:", "running:", "⚡"]:
                        if low.startswith(kw.lower()):
                            val = l[len(kw):].strip(" :-")
                            if len(val) > 2:
                                info["current_skill"] = val[:50]
                                break
            except Exception:
                pass
        result["agents"][agent_id] = info

    # Parse gateway log for recent agent-to-agent events
    try:
        log_files = sorted(globmod.glob("/tmp/openclaw/openclaw-*.log"), reverse=True)
        events = []
        for log_file in log_files[:2]:
            try:
                r = subprocess.run(
                    ["tail", "-150", log_file],
                    capture_output=True, text=True, timeout=5
                )
                for line in r.stdout.split("\n"):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                        msg = str(obj.get("0", obj.get("msg", "")))
                        if not msg or len(msg) < 5:
                            continue
                        meta = obj.get("_meta", {})
                        ts = meta.get("date", "")
                        msg_low = msg.lower()
                        evt = None

                        # Agent-to-agent delegation patterns
                        if any(k in msg_low for k in [
                            "agenttoagent", "calling agent", "→ agent",
                            "delegate", "agent call", "forward to"
                        ]):
                            found = re.findall(r'\b([a-z][a-z0-9-]{2,20})\b', msg_low)
                            known = [f for f in found if f in known_ids]
                            if len(known) >= 2:
                                evt = {"type": "agent_call", "from": known[0],
                                       "to": known[1], "msg": msg[:100], "ts": ts}
                            elif len(known) == 1:
                                evt = {"type": "agent_call", "from": "main",
                                       "to": known[0], "msg": msg[:100], "ts": ts}

                        if not evt and any(k in msg_low for k in [
                            "task started", "new task", "başladı", "task:"
                        ]):
                            evt = {"type": "task_start", "from": None, "to": None,
                                   "msg": msg[:100], "ts": ts}

                        if not evt and any(k in msg_low for k in [
                            "completed", "done", "finished", "tamamlandı", "bitti"
                        ]):
                            evt = {"type": "task_done", "from": None, "to": None,
                                   "msg": msg[:100], "ts": ts}

                        if evt:
                            evt["id"] = f"{ts}:{msg[:30]}"
                            events.append(evt)
                    except Exception:
                        pass
            except Exception:
                pass
        result["events"] = events[-20:]
    except Exception:
        result["events"] = []

    # Merge Agent Team runtime activity so Office 3D can animate those workers too.
    team_tasks = _get_agent_team_tasks()
    team_agents, team_events = _build_agent_team_activity(team_tasks)
    result["agents"].update(team_agents)
    result["events"] = (result.get("events", []) + team_events)[-40:]

    return jsonify(result)


# --- SKILLS (main workspace) ---
@app.route("/api/skills")
def get_skills():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    skills = []
    skills_dir = WORKSPACE / "skills"
    if skills_dir.exists():
        for skill_dir in sorted(skills_dir.iterdir()):
            if skill_dir.is_dir():
                skill = {"name": skill_dir.name}
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    skill["content"] = skill_file.read_text()
                skills.append(skill)

    return jsonify(skills)


# --- IDEAS ---
@app.route("/api/ideas")
def get_ideas():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    archive_file = IDEAS_DIR / "archive.json"
    if archive_file.exists():
        ideas = json.loads(archive_file.read_text())
        return jsonify(ideas)
    return jsonify([])


@app.route("/api/ideas/daily/<date>")
def get_daily_ideas(date):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    daily_file = IDEAS_DIR / f"{date}.md"
    if daily_file.exists():
        return jsonify({"date": date, "content": daily_file.read_text()})
    return jsonify({"error": "not found"}), 404


# --- SYSTEM STATUS ---
@app.route("/api/status")
def get_status():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    status = {}

    # Docker
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
                          capture_output=True, text=True, timeout=5)
        containers = []
        for line in r.stdout.strip().split("\n"):
            if line:
                parts = line.split("\t")
                containers.append({
                    "name": parts[0] if len(parts) > 0 else "",
                    "status": parts[1] if len(parts) > 1 else "",
                    "ports": parts[2] if len(parts) > 2 else ""
                })
        status["docker"] = containers
    except:
        status["docker"] = []

    # System
    try:
        r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
        status["memory"] = r.stdout.strip()
    except:
        status["memory"] = "N/A"

    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        status["disk"] = r.stdout.strip()
    except:
        status["disk"] = "N/A"

    try:
        r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        status["uptime"] = r.stdout.strip()
    except:
        status["uptime"] = "N/A"

    # Cron jobs
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        status["cron"] = r.stdout.strip()
    except:
        status["cron"] = "N/A"

    # Browser agent
    try:
        import requests as req
        r = req.get("http://localhost:5200/ping",
                    headers={"X-API-Key": "nootle-browser-2024-secret"}, timeout=3)
        status["browser_agent"] = "online" if r.status_code == 200 else "offline"
    except:
        status["browser_agent"] = "offline"

    # PC
    try:
        import requests as req
        r = req.get("http://100.89.62.116:5123/ping",
                    headers={"X-API-Key": "nootle-pc-control-2024-secret"}, timeout=3)
        status["pc"] = "online" if r.status_code == 200 else "offline"
    except:
        status["pc"] = "offline"

    # Agent Team
    try:
        code, _ = agent_team_request("GET", "/ping", timeout=3)
        status["agent_team"] = "online" if code == 200 else "offline"
    except:
        status["agent_team"] = "offline"

    return jsonify(status)


# --- CRON SYSTEM ---
TR_TZ = timezone(timedelta(hours=3))

KNOWN_JOBS = {
    "agent-tips.py": {"name": "Agent Tips", "icon": "💡", "desc": "Rastgele saatlerde agent kullanım ipuçları gönderir"},
    "idea-finder.py": {"name": "Idea Finder", "icon": "💡", "desc": "Günde 2 kez web'den uygulama fikri arar"},
    "ai-news-fetcher.sh": {"name": "AI News", "icon": "📰", "desc": "Günlük AI haberleri toplar"},
    "sabah-raporu.sh": {"name": "Sabah Raporu", "icon": "🌅", "desc": "Her sabah sistem durum raporu gönderir"},
}


def parse_cron_schedule(minute, hour, dom, month, dow):
    """Cron zamanlamasını okunabilir Türkçe'ye çevir"""
    days_tr = {0: "Paz", 1: "Pzt", 2: "Sal", 3: "Çar", 4: "Per", 5: "Cum", 6: "Cmt"}

    time_str = ""
    if minute != "*" and hour != "*":
        time_str = f"{int(hour):02d}:{int(minute):02d} UTC"
        try:
            utc_h = int(hour)
            tr_h = (utc_h + 3) % 24
            time_str += f" ({tr_h:02d}:{int(minute):02d} TR)"
        except:
            pass
    elif hour != "*":
        time_str = f"Her saat {hour} UTC"
    elif minute != "*":
        time_str = f"Her saat, dakika {minute}"
    else:
        time_str = "Her dakika"

    freq = ""
    if dom == "*" and month == "*" and dow == "*":
        freq = "Her gün"
    elif dow != "*":
        if "," in dow:
            freq = "Günler: " + ", ".join(days_tr.get(int(d), d) for d in dow.split(","))
        else:
            freq = days_tr.get(int(dow), f"Gün {dow}")
    elif dom != "*":
        freq = f"Ayın {dom}. günü"

    return {"time": time_str, "frequency": freq}


@app.route("/api/cron")
def get_cron():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    jobs = []

    # Parse crontab
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue

            minute, hour, dom, month, dow = parts[0:5]
            command = parts[5]

            # Extract script name
            script_match = re.search(r'([a-zA-Z0-9_-]+\.(py|sh))', command)
            script_name = script_match.group(1) if script_match else command[:50]

            # Extract log file
            log_match = re.search(r'>>\s*(\S+)', command)
            log_file = log_match.group(1) if log_match else None

            known = KNOWN_JOBS.get(script_name, {})
            schedule = parse_cron_schedule(minute, hour, dom, month, dow)

            job = {
                "script": script_name,
                "name": known.get("name", script_name),
                "icon": known.get("icon", "⏰"),
                "description": known.get("desc", ""),
                "schedule_raw": f"{minute} {hour} {dom} {month} {dow}",
                "schedule_time": schedule["time"],
                "schedule_freq": schedule["frequency"],
                "command": command,
                "log_file": log_file,
            }

            # Get state file info
            state_file = SCRIPTS_DIR / script_name.replace(".py", "-state.json").replace(".sh", "-state.json")
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                    job["state"] = state
                    if "last_run_date" in state:
                        job["last_run"] = state["last_run_date"]
                    if "run_count" in state:
                        job["run_count"] = state["run_count"]
                    if "sent_dates" in state:
                        job["last_run"] = state["sent_dates"][-1] if state["sent_dates"] else None
                except:
                    pass

            # Get last log lines
            if log_file:
                try:
                    r2 = subprocess.run(["tail", "-20", log_file], capture_output=True, text=True, timeout=3)
                    job["log_tail"] = r2.stdout.strip()
                except:
                    job["log_tail"] = ""

            jobs.append(job)

    except:
        pass

    return jsonify(jobs)


@app.route("/api/cron/log")
def get_cron_log():
    """Belirli bir cron job'un tam logunu getir"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    log_path = request.args.get("path", "")
    lines = int(request.args.get("lines", 100))

    if not log_path or ".." in log_path:
        return jsonify({"error": "invalid path"}), 400

    # Only allow /root/ and /tmp/ paths
    if not (log_path.startswith("/root/") or log_path.startswith("/tmp/")):
        return jsonify({"error": "access denied"}), 403

    try:
        r = subprocess.run(["tail", f"-{lines}", log_path], capture_output=True, text=True, timeout=5)
        return jsonify({"path": log_path, "content": r.stdout, "lines": lines})
    except:
        return jsonify({"error": "not found"}), 404


@app.route("/api/cron/run", methods=["POST"])
def run_cron_job():
    """Bir cron job'u manuel çalıştır"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json() or {}
    script = data.get("script", "")

    if not script or ".." in script or "/" in script:
        return jsonify({"error": "invalid script"}), 400

    # Only allow .py and .sh
    if not (script.endswith(".py") or script.endswith(".sh")):
        return jsonify({"error": "invalid script type"}), 400

    script_path = SCRIPTS_DIR / script
    if not script_path.exists():
        # Try other locations
        alt_paths = [
            Path("/root") / script,
            Path("/root/.openclaw/workspace/scripts") / script,
        ]
        script_path = None
        for p in alt_paths:
            if p.exists():
                script_path = p
                break

    if not script_path or not script_path.exists():
        return jsonify({"error": "script not found"}), 404

    try:
        cmd = ["python3", str(script_path)] if script.endswith(".py") else ["bash", str(script_path)]
        r = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return jsonify({"status": "started", "script": script, "pid": r.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- CRON: UPDATE SCHEDULE ---
@app.route("/api/cron/job", methods=["PUT"])
def update_cron_job():
    """Bir cron job'un zamanlamasini degistir"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    old_schedule = data.get("old_schedule", "").strip()  # "* * * * *"
    new_schedule = data.get("new_schedule", "").strip()  # "0 9 * * *"
    command = data.get("command", "").strip()

    if not old_schedule or not new_schedule or not command:
        return jsonify({"error": "old_schedule, new_schedule, command required"}), 400

    # Validate schedule (5 fields)
    if len(new_schedule.split()) != 5:
        return jsonify({"error": "schedule must have 5 fields"}), 400

    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
        new_lines = []
        found = False
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            parts = stripped.split(None, 5)
            if len(parts) >= 6:
                line_schedule = " ".join(parts[:5])
                line_cmd = parts[5]
                if line_schedule == old_schedule and command in line_cmd:
                    new_lines.append(new_schedule + " " + line_cmd)
                    found = True
                    continue
            new_lines.append(line)

        if not found:
            return jsonify({"error": "job not found"}), 404

        new_crontab = "\n".join(new_lines)
        if not new_crontab.endswith("\n"):
            new_crontab += "\n"
        p = subprocess.run(["crontab", "-"], input=new_crontab, text=True, timeout=5)
        return jsonify({"ok": True, "new_schedule": new_schedule})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cron/job", methods=["DELETE"])
def delete_cron_job():
    """Bir cron job'u kaldir"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    schedule = data.get("schedule", "").strip()
    command = data.get("command", "").strip()

    if not schedule or not command:
        return jsonify({"error": "schedule and command required"}), 400

    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.splitlines()
        new_lines = []
        removed = False
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            parts = stripped.split(None, 5)
            if len(parts) >= 6:
                line_schedule = " ".join(parts[:5])
                line_cmd = parts[5]
                if line_schedule == schedule and command in line_cmd:
                    removed = True
                    continue  # skip = delete
            new_lines.append(line)

        if not removed:
            return jsonify({"error": "job not found"}), 404

        new_crontab = "\n".join(new_lines)
        if not new_crontab.endswith("\n"):
            new_crontab += "\n"
        subprocess.run(["crontab", "-"], input=new_crontab, text=True, timeout=5)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- LOGS SYSTEM ---
LOG_SOURCES = [
    {
        "id": "gateway",
        "name": "OpenClaw Gateway",
        "icon": "🦅",
        "category": "core",
        "path_pattern": "/tmp/openclaw/openclaw-*.log",
        "format": "jsonl",
        "description": "Ana gateway işlemleri, agent çağrıları, Telegram mesajları",
    },
    {
        "id": "config-audit",
        "name": "Config Değişiklikleri",
        "icon": "⚙️",
        "category": "core",
        "path": "/root/.openclaw/logs/config-audit.jsonl",
        "format": "jsonl",
        "description": "openclaw.json yapılandırma değişiklik geçmişi",
    },
    {
        "id": "syslog",
        "name": "Sistem Logları",
        "icon": "🖥️",
        "category": "system",
        "command": ["grep", "-i", "openclaw", "/var/log/syslog"],
        "format": "text",
        "description": "Syslog'daki OpenClaw ile ilgili kayıtlar",
    },
    {
        "id": "idea-finder",
        "name": "Idea Finder",
        "icon": "💡",
        "category": "cron",
        "path": "/root/scripts/idea-finder.log",
        "format": "text",
        "description": "Uygulama fikri arama log kayıtları",
    },
    {
        "id": "agent-tips",
        "name": "Agent Tips",
        "icon": "📌",
        "category": "cron",
        "path": "/root/scripts/agent-tips.log",
        "format": "text",
        "description": "Agent kullanım ipucu gönderim logları",
    },
    {
        "id": "ai-news",
        "name": "AI News",
        "icon": "📰",
        "category": "cron",
        "path": "/root/ai-news.log",
        "format": "text",
        "description": "Günlük AI haber toplama logları",
    },
    {
        "id": "morning-report",
        "name": "Sabah Raporu",
        "icon": "🌅",
        "category": "cron",
        "path": "/root/.openclaw/workspace/memory/morning-reports.log",
        "format": "text",
        "description": "Günlük sabah durum raporu logları",
    },
    {
        "id": "browser-agent",
        "name": "Browser Agent",
        "icon": "🌐",
        "category": "service",
        "path": "/root/browser-agent.log",
        "format": "text",
        "description": "Headless browser agent işlem logları",
    },
    {
        "id": "dashboard",
        "name": "Dashboard",
        "icon": "📊",
        "category": "service",
        "path": "/tmp/dashboard.log",
        "format": "text",
        "description": "Bu dashboard uygulamasının logları",
    },
]


def read_log_file(path, lines=100, search=None):
    """Log dosyasını oku, isteğe bağlı filtreleme"""
    if not os.path.exists(path):
        return ""
    try:
        if search:
            r = subprocess.run(
                ["grep", "-i", search, path],
                capture_output=True, text=True, timeout=5
            )
            content = r.stdout.strip()
            # Limit lines
            content_lines = content.split("\n")
            return "\n".join(content_lines[-lines:])
        else:
            r = subprocess.run(
                ["tail", f"-{lines}", path],
                capture_output=True, text=True, timeout=5
            )
            return r.stdout.strip()
    except:
        return ""


def parse_gateway_log(content):
    """Gateway JSONL logunu okunabilir kayıtlara çevir"""
    entries = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            meta = obj.get("_meta", {})
            entry = {
                "time": meta.get("date", obj.get("time", "")),
                "level": meta.get("logLevelName", "INFO"),
                "message": obj.get("0", obj.get("msg", str(obj)[:200])),
            }
            # Extract useful fields
            path_info = meta.get("path", {})
            if path_info.get("method"):
                entry["source"] = path_info["method"]

            entries.append(entry)
        except json.JSONDecodeError:
            entries.append({
                "time": "",
                "level": "RAW",
                "message": line[:300],
            })
    return entries


def get_log_stats(path):
    """Log dosyası istatistikleri"""
    stats = {"size": 0, "modified": "", "lines": 0, "exists": False}
    if not os.path.exists(path):
        return stats
    try:
        st = os.stat(path)
        stats["exists"] = True
        stats["size"] = st.st_size
        stats["modified"] = datetime.fromtimestamp(st.st_mtime, tz=TR_TZ).strftime("%Y-%m-%d %H:%M:%S")
        r = subprocess.run(["wc", "-l", path], capture_output=True, text=True, timeout=3)
        stats["lines"] = int(r.stdout.strip().split()[0]) if r.stdout.strip() else 0
    except:
        pass
    return stats


@app.route("/api/logs")
def get_logs_overview():
    """Tüm log kaynaklarının genel durumu"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    sources = []
    for src in LOG_SOURCES:
        info = {
            "id": src["id"],
            "name": src["name"],
            "icon": src["icon"],
            "category": src["category"],
            "description": src["description"],
            "format": src["format"],
        }

        # Resolve path
        if "path_pattern" in src:
            files = sorted(globmod.glob(src["path_pattern"]), reverse=True)
            info["path"] = files[0] if files else None
            info["all_files"] = files[:7]  # Last 7 days
        elif "path" in src:
            info["path"] = src["path"]
        elif "command" in src:
            info["path"] = None
            info["is_command"] = True

        # Stats
        if info.get("path"):
            info["stats"] = get_log_stats(info["path"])
        elif info.get("is_command"):
            info["stats"] = {"exists": True, "size": 0, "modified": "", "lines": 0}
        else:
            info["stats"] = {"exists": False}

        # Last few lines preview
        if info.get("path"):
            preview = read_log_file(info["path"], lines=5)
            info["preview"] = preview
        elif "command" in src:
            try:
                r = subprocess.run(src["command"], capture_output=True, text=True, timeout=5)
                lines = r.stdout.strip().split("\n")
                info["preview"] = "\n".join(lines[-5:])
            except:
                info["preview"] = ""

        sources.append(info)

    return jsonify(sources)


@app.route("/api/logs/<log_id>")
def get_log_detail(log_id):
    """Belirli bir log kaynağının detaylı içeriği"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    lines = int(request.args.get("lines", 100))
    search = request.args.get("search", "").strip() or None
    level = request.args.get("level", "").strip() or None

    src = None
    for s in LOG_SOURCES:
        if s["id"] == log_id:
            src = s
            break

    if not src:
        return jsonify({"error": "unknown log source"}), 404

    # Resolve path
    path = None
    if "path_pattern" in src:
        date_param = request.args.get("date", "")
        if date_param:
            path = f"/tmp/openclaw/openclaw-{date_param}.log"
        else:
            files = sorted(globmod.glob(src["path_pattern"]), reverse=True)
            path = files[0] if files else None
    elif "path" in src:
        path = src["path"]

    result = {
        "id": log_id,
        "name": src["name"],
        "format": src["format"],
    }

    if path:
        result["path"] = path
        content = read_log_file(path, lines=lines, search=search)

        if src["format"] == "jsonl" and log_id == "gateway":
            entries = parse_gateway_log(content)
            if level:
                entries = [e for e in entries if e.get("level", "").upper() == level.upper()]
            result["entries"] = entries
            result["raw"] = content
        elif src["format"] == "jsonl":
            result["raw"] = content
            # Parse each line
            entries = []
            for line in content.split("\n"):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    entries.append(obj)
                except:
                    entries.append({"raw": line})
            result["entries"] = entries
        else:
            if search:
                result["raw"] = content
                result["search"] = search
            else:
                result["raw"] = content
    elif "command" in src:
        try:
            cmd = src["command"][:]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            content = r.stdout.strip()
            if search:
                content = "\n".join(l for l in content.split("\n") if search.lower() in l.lower())
            lines_list = content.split("\n")
            result["raw"] = "\n".join(lines_list[-lines:])
        except Exception as e:
            result["raw"] = f"Hata: {str(e)}"
    else:
        result["raw"] = "Log dosyası bulunamadı."

    return jsonify(result)


@app.route("/api/logs/search")
def search_all_logs():
    """Tüm log kaynaklarında arama"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    query = request.args.get("q", "").strip()
    if not query or len(query) < 2:
        return jsonify({"error": "min 2 karakter gerekli"}), 400

    results = []
    for src in LOG_SOURCES:
        path = None
        if "path_pattern" in src:
            files = sorted(globmod.glob(src["path_pattern"]), reverse=True)
            path = files[0] if files else None
        elif "path" in src:
            path = src["path"]

        matches = ""
        if path and os.path.exists(path):
            try:
                r = subprocess.run(
                    ["grep", "-i", "-n", query, path],
                    capture_output=True, text=True, timeout=5
                )
                matches = r.stdout.strip()
            except:
                pass
        elif "command" in src:
            try:
                r = subprocess.run(src["command"], capture_output=True, text=True, timeout=5)
                matches = "\n".join(l for l in r.stdout.split("\n") if query.lower() in l.lower())
            except:
                pass

        if matches:
            match_lines = matches.split("\n")
            results.append({
                "source_id": src["id"],
                "source_name": src["name"],
                "icon": src["icon"],
                "match_count": len(match_lines),
                "matches": "\n".join(match_lines[-20:]),  # Last 20 matches
            })

    return jsonify({"query": query, "results": results, "total_sources": len(results)})


# --- PC CONTROL API ---
PC_API = "http://100.89.62.116:5123"
PC_API_KEY = "nootle-pc-control-2024-secret"
AGENT_TEAM_API = os.environ.get("AGENT_TEAM_BASE_URL", "http://127.0.0.1:5400")
AGENT_TEAM_KEY = os.environ.get("AGENT_TEAM_API_KEY", "agent-team-dev-key")


def agent_team_request(method, path, payload=None, timeout=15):
    """Proxy request to Agent Team service"""
    url = AGENT_TEAM_API.rstrip("/") + path
    body = None
    headers = {"X-API-Key": AGENT_TEAM_KEY}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url=url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return resp.status, data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error": raw}
    except Exception as exc:
        return 502, {"error": str(exc)}


@app.route("/api/pc/status")
def pc_status():
    """PC Control API detaylı durum"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import requests as req
        r = req.get(f"{PC_API}/ping", headers={"X-API-Key": PC_API_KEY}, timeout=3)
        data = r.json()
        data["online"] = True
        return jsonify(data)
    except:
        return jsonify({"online": False})


@app.route("/api/pc/restart", methods=["POST"])
def pc_restart_api():
    """PC Control API'yi yeniden başlat (PC'de çalışan server'a exec komutu gönderir)"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import requests as req
        # PC API çalışıyorsa, kendini restart etmesini söyle
        r = req.post(f"{PC_API}/exec",
                     headers={"X-API-Key": PC_API_KEY, "Content-Type": "application/json"},
                     json={
                         "command": 'Start-Process powershell -ArgumentList "-NoProfile -Command Start-Sleep 2; cd C:\\Users\\umuti\\Desktop\\deneembos\\pc-control-api; $env:PC_CONTROL_API_KEY=\'nootle-pc-control-2024-secret\'; python server.py" -WindowStyle Hidden',
                         "shell": "powershell"
                     },
                     timeout=5)
        return jsonify({"status": "restart_sent", "response": r.json()})
    except:
        return jsonify({"status": "offline", "message": "PC API ulasilamiyor. PC'de manuel baslatiniz."})


@app.route("/api/pc/exec", methods=["POST"])
def pc_exec_proxy():
    """PC'de komut çalıştır (proxy)"""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        import requests as req
        data = request.get_json() or {}
        r = req.post(f"{PC_API}/exec",
                     headers={"X-API-Key": PC_API_KEY, "Content-Type": "application/json"},
                     json=data, timeout=30)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# --- AGENT TEAM API ---
@app.route("/api/agent-team/ping")
def agent_team_ping():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    code, data = agent_team_request("GET", "/ping", timeout=5)
    return jsonify(data), code


@app.route("/api/agent-team/config")
def agent_team_config():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    code, data = agent_team_request("GET", "/api/config")
    return jsonify(data), code


@app.route("/api/agent-team/tasks")
def agent_team_tasks():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    code, data = agent_team_request("GET", "/api/tasks")
    return jsonify(data), code


@app.route("/api/agent-team/tasks/<int:task_id>")
def agent_team_task_detail(task_id):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    code, data = agent_team_request("GET", f"/api/tasks/{task_id}")
    return jsonify(data), code


@app.route("/api/agent-team/tasks", methods=["POST"])
def agent_team_create_task():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    payload = {
        "goal": data.get("goal", ""),
        "project_path": data.get("project_path", ""),
        "implementation_command": data.get("implementation_command", ""),
        "test_command": data.get("test_command", ""),
        "notify_endpoint": data.get("notify_endpoint", ""),
    }
    code, out = agent_team_request("POST", "/api/tasks", payload=payload, timeout=30)
    return jsonify(out), code


@app.route("/api/agent-team/tasks/<int:task_id>/retry", methods=["POST"])
def agent_team_retry(task_id):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    code, data = agent_team_request("POST", f"/api/tasks/{task_id}/retry")
    return jsonify(data), code


@app.route("/api/agent-team/worker/<action>", methods=["POST"])
def agent_team_worker(action):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    if action not in ("start", "stop"):
        return jsonify({"error": "invalid action"}), 400
    code, data = agent_team_request("POST", f"/api/worker/{action}")
    return jsonify(data), code


# --- FILE READER ---
@app.route("/api/file")
def read_file():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    path = request.args.get("path", "")
    if not path or ".." in path:
        return jsonify({"error": "invalid path"}), 400

    # Only allow reading under openclaw dir
    full_path = Path(path)
    if not str(full_path).startswith("/root/.openclaw"):
        return jsonify({"error": "access denied"}), 403

    if full_path.exists() and full_path.is_file():
        return jsonify({"path": str(full_path), "content": full_path.read_text()})
    return jsonify({"error": "not found"}), 404




# --- TOOLS API ---
TOOLS_DB_PATH = Path("/root/scripts/ai-tool-evaluator/tools_db.json")


def load_tools_db():
    if not TOOLS_DB_PATH.exists():
        return {"tools": {}}
    try:
        return json.loads(TOOLS_DB_PATH.read_text())
    except Exception:
        return {"tools": {}}


def save_tools_db(db):
    TOOLS_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2))


@app.route("/api/tools")
def get_tools():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_tools_db()
    tools_list = []
    for name, t in db.get("tools", {}).items():
        tools_list.append({
            "name": name,
            "tags": t.get("tags", []),
            "added": t.get("added", ""),
            "last_eval": t.get("last_eval", ""),
            "decision": t.get("decision", ""),
            "evaluating": t.get("evaluating", False),
            "has_report": bool(t.get("report", "")),
            "report_preview": t.get("report", "")[:200] if t.get("report") else "",
        })
    tools_list.sort(key=lambda x: x.get("last_eval", "") or "", reverse=True)
    return jsonify(tools_list)


@app.route("/api/tools/<path:name>")
def get_tool_report(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_tools_db()
    tool = db.get("tools", {}).get(name)
    if not tool:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "name": name,
        "added": tool.get("added", ""),
        "last_eval": tool.get("last_eval", ""),
        "decision": tool.get("decision", ""),
        "evaluating": tool.get("evaluating", False),
        "report": tool.get("report", ""),
    })


@app.route("/api/tools", methods=["POST"])
def add_tool():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    db = load_tools_db()
    if name in db.get("tools", {}):
        return jsonify({"error": "already exists"}), 409
    now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    db.setdefault("tools", {})[name] = {
        "name": name,
        "tags": tags,
        "added": now_str,
        "last_eval": "",
        "decision": "",
        "report": "",
        "evaluating": False,
    }
    save_tools_db(db)
    return jsonify({"ok": True, "name": name})


@app.route("/api/tools/<path:name>", methods=["DELETE"])
def delete_tool(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_tools_db()
    if name not in db.get("tools", {}):
        return jsonify({"error": "not found"}), 404
    del db["tools"][name]
    save_tools_db(db)
    return jsonify({"ok": True})


@app.route("/api/tools/<path:name>/evaluate", methods=["POST"])
def trigger_evaluate(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_tools_db()
    if name not in db.get("tools", {}):
        # Auto-add if not exists
        now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()
        db.setdefault("tools", {})[name] = {
            "name": name, "added": now_str, "last_eval": "",
            "decision": "", "report": "", "evaluating": False,
        }
    db["tools"][name]["evaluating"] = True
    save_tools_db(db)
    # Run in background
    import subprocess
    subprocess.Popen(
        ["python3", "/root/scripts/ai-tool-evaluator/ai_evaluator.py", name],
        stdout=open("/tmp/evaluator.log", "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"ok": True, "message": f"{name} degerlendirmesi baslatildi"})




# --- TOOLS: PATCH (etiket güncelle) ---
@app.route("/api/tools/<path:name>", methods=["PATCH"])
def patch_tool(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_tools_db()
    if name not in db.get("tools", {}):
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    tool = db["tools"][name]
    if "tags" in data:
        tool["tags"] = [t.strip() for t in data["tags"] if t.strip()]
    if "notes" in data:
        tool["notes"] = data["notes"]
    if "new_name" in data:
        new_name = data["new_name"].strip()
        if new_name and new_name != name:
            db["tools"][new_name] = db["tools"].pop(name)
            db["tools"][new_name]["name"] = new_name
            save_tools_db(db)
            return jsonify({"ok": True, "renamed": new_name})
    save_tools_db(db)
    return jsonify({"ok": True})


# --- CONTENT STUDIO ---
STUDIO_HISTORY = Path("/root/scripts/content-studio/history.json")

STUDIO_FORMATS = ["thread", "liste", "egitim", "storytelling", "karsilastirma", "hot-take", "spark", "ozet"]
STUDIO_STYLES  = ["grok", "profesyonel", "samimi", "hoca", "merakli"]


def load_studio_history():
    if not STUDIO_HISTORY.exists():
        return {"items": []}
    try:
        return json.loads(STUDIO_HISTORY.read_text())
    except Exception:
        return {"items": []}


@app.route("/api/studio/history")
def get_studio_history():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    h = load_studio_history()
    items = h.get("items", [])
    # En yeni önce, max 20
    items = sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[:20]
    # content'i preview'a kes
    result = []
    for it in items:
        result.append({
            "id": it.get("id", ""),
            "topic": it.get("topic", ""),
            "format": it.get("format", ""),
            "style": it.get("style", ""),
            "created_at": it.get("created_at", ""),
            "preview": (it.get("content", "")[:150] + "..." if len(it.get("content","")) > 150 else it.get("content","")),
            "sent": it.get("sent_to_telegram", False),
            "generating": it.get("generating", False),
        })
    return jsonify(result)


@app.route("/api/studio/history/<item_id>")
def get_studio_item(item_id):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    h = load_studio_history()
    for it in h.get("items", []):
        if it.get("id") == item_id:
            return jsonify(it)
    return jsonify({"error": "not found"}), 404


@app.route("/api/studio/generate", methods=["POST"])
def studio_generate():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    topic  = (data.get("topic") or "").strip()
    fmt    = (data.get("format") or "thread").strip()
    style  = (data.get("style") or "samimi").strip()
    all_fmt = data.get("all_formats", False)

    if not topic:
        return jsonify({"error": "topic required"}), 400
    if fmt not in STUDIO_FORMATS:
        fmt = "thread"
    if style not in STUDIO_STYLES:
        style = "samimi"

    import uuid
    item_id = str(uuid.uuid4())[:8]
    now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()

    # History'ye "generating" kaydı ekle
    h = load_studio_history()
    h.setdefault("items", []).append({
        "id": item_id,
        "topic": topic,
        "format": "all" if all_fmt else fmt,
        "style": style,
        "created_at": now_str,
        "content": "",
        "sent_to_telegram": False,
        "generating": True,
    })
    STUDIO_HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=2))

    # Arka planda çalıştır
    import subprocess
    cmd = ["python3", "/root/scripts/content-studio/content_studio.py",
           topic, "all" if all_fmt else fmt, style, "--save-id", item_id]
    subprocess.Popen(cmd, stdout=open("/tmp/studio.log", "a"), stderr=subprocess.STDOUT)

    return jsonify({"ok": True, "id": item_id, "message": f"'{topic}' için içerik üretimi başladı"})


@app.route("/api/studio/formats")
def studio_formats():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"formats": STUDIO_FORMATS, "styles": STUDIO_STYLES})


# --- STUDIO: DELETE ---
@app.route("/api/studio/history/<item_id>", methods=["DELETE"])
def delete_studio_item(item_id):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    h = load_studio_history()
    items = h.get("items", [])
    new_items = [it for it in items if it.get("id") != item_id]
    if len(new_items) == len(items):
        return jsonify({"error": "not found"}), 404
    h["items"] = new_items
    STUDIO_HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=2))
    return jsonify({"ok": True})


# --- X (TWITTER) CONFIG ---
X_CONFIG_PATH = Path("/root/scripts/x-ai-news/x_config.json")

def load_x_cfg():
    if not X_CONFIG_PATH.exists():
        return {
            "accounts": ["OpenAI","AnthropicAI","deepseek_ai","xai","MistralAI"],
            "hashtags": ["GPT5","GPT4o","Claude","Gemini","LLaMA4","DeepSeek","Grok"],
            "keywords": ["AI agent","language model","AI coding","open source AI"],
            "max_tweets_per_query": 50
        }
    try:
        return json.loads(X_CONFIG_PATH.read_text())
    except Exception:
        return {}

def save_x_cfg(cfg):
    X_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

@app.route("/api/x/config")
def get_x_config():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(load_x_cfg())

@app.route("/api/x/config", methods=["PUT"])
def put_x_config():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    cfg = load_x_cfg()
    if "accounts" in data:
        cfg["accounts"] = [a.strip() for a in data["accounts"] if str(a).strip()]
    if "hashtags" in data:
        cfg["hashtags"] = [h.strip().lstrip("#") for h in data["hashtags"] if str(h).strip()]
    if "keywords" in data:
        cfg["keywords"] = [k.strip() for k in data["keywords"] if str(k).strip()]
    if "max_tweets_per_query" in data:
        try:
            cfg["max_tweets_per_query"] = max(10, min(100, int(data["max_tweets_per_query"])))
        except Exception:
            pass
    save_x_cfg(cfg)
    return jsonify({"ok": True, "config": cfg})

@app.route("/api/x/run", methods=["POST"])
def run_x_news():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    import subprocess
    subprocess.Popen(
        ["python3", "/root/scripts/x-ai-news/x_ai_news.py"],
        stdout=open("/tmp/x_news.log", "a"),
        stderr=subprocess.STDOUT,
    )
    return jsonify({"ok": True, "message": "X haber toplayici baslatildi"})


if __name__ == "__main__":
    print("OpenClaw Dashboard starting on port 5300...")
    app.run(host="0.0.0.0", port=5300)


