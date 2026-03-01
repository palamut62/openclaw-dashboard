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


# ─── APP BUILDER ─────────────────────────────────────────────────────────────
APPS_DB      = Path("/root/scripts/app-builder/projects.json")
PROJECT_DIRS = [
    Path("/root/apps"),
    Path("/root/.openclaw/workspace"),
    Path("/root/projects"),
]

# App olarak sayılmayacak workspace klasörleri
WORKSPACE_IGNORE = {
    ".git", ".openclaw", ".pi", "skills", "memory", "data", "drafts",
    "content", "error-log", "scripts", "Masaustu", "notion-mvp",
    "agent-team-system", "__pycache__", "node_modules",
}

def _is_app_dir(path):
    """Bir klasörün uygulama olup olmadığını tespit et."""
    p = path
    if p.name.startswith("."):
        return False
    if p.name in WORKSPACE_IGNORE:
        return False
    # package.json veya app.py/main.py içeriyorsa uygulama
    return (
        (p / "package.json").exists() or
        (p / "app.py").exists() or
        (p / "main.py").exists() or
        (p / "server.py").exists() or
        (p / "bot.py").exists()
    )

APP_TYPE_ICONS = {
    "web": "🌐", "bot": "🤖", "api": "⚡", "python": "🐍",
    "desktop": "🖥️", "extension": "🧩", "fullstack": "🔥", "other": "📦"
}


def _auto_register_new_apps():
    """PROJECT_DIRS deki yeni app klasorlerini otomatik kaydet."""
    try:
        db = load_apps_db()
        tracked_names = {p["name"] for p in db.get("projects", [])}
        changed = False
        now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()
        for base in PROJECT_DIRS:
            if not base.exists():
                continue
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                if d.name in tracked_names:
                    continue
                if not _is_app_dir(d):
                    continue
                ptype = "web"
                if (d / "package.json").exists():
                    try:
                        pkg = json.loads((d / "package.json").read_text())
                        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                        if "next" in deps:
                            ptype = "fullstack"
                        elif any(k in deps for k in ["express"]):
                            ptype = "api"
                        else:
                            ptype = "web"
                    except Exception:
                        ptype = "web"
                elif (d / "bot.py").exists():
                    ptype = "bot"
                elif any((d / f).exists() for f in ["app.py", "main.py", "server.py"]):
                    ptype = "python"
                db.setdefault("projects", []).append({
                    "name": d.name,
                    "path": str(d),
                    "type": ptype,
                    "tracked": False,
                    "added": now_str,
                    "built_at": now_str,
                    "description": "",
                    "status": "ready",
                    "deploy_target": "vps",
                    "deploy_url": "",
                })
                tracked_names.add(d.name)
                changed = True
        if changed:
            save_apps_db(db)
    except Exception:
        pass

def load_apps_db():
    if not APPS_DB.exists():
        APPS_DB.parent.mkdir(parents=True, exist_ok=True)
        return {"projects": []}
    try:
        return json.loads(APPS_DB.read_text())
    except Exception:
        return {"projects": []}

def save_apps_db(db):
    APPS_DB.write_text(json.dumps(db, ensure_ascii=False, indent=2))

def detect_project_type(path):
    p = Path(path)
    if (p / "package.json").exists():
        pkg = json.loads((p / "package.json").read_text())
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "next" in deps: return "fullstack"
        if "react" in deps: return "web"
        return "web"
    if (p / "requirements.txt").exists():
        req = (p / "requirements.txt").read_text().lower()
        if "telegram" in req: return "bot"
        if "flask" in req or "fastapi" in req: return "api"
        return "python"
    if (p / "manifest.json").exists(): return "extension"
    if (p / "index.html").exists(): return "web"
    return "other"

def scan_project_dir(base):
    found = []
    try:
        for d in Path(base).iterdir():
            if not d.is_dir(): continue
            if d.name.startswith(".") or d.name in ("__pycache__", "node_modules", "venv", ".git"): continue
            has_code = any((d / f).exists() for f in ["package.json", "requirements.txt", "index.html", "main.py", "app.py", "bot.py", "server.js", "manifest.json"])
            if has_code:
                found.append(str(d))
    except Exception:
        pass
    return found

def _path_priority(path):
    """Dusuk deger = daha yuksek oncelik."""
    if path.startswith("/root/apps"): return 0
    if path.startswith("/root/.openclaw/workspace"): return 1
    return 2

def get_all_projects():
    db = load_apps_db()
    tracked_by_path = {p["path"]: p for p in db.get("projects", [])}
    # Scan dirs for untracked apps
    scanned_paths = set()
    for base in PROJECT_DIRS:
        for path in scan_project_dir(base):
            scanned_paths.add(path)
    # Merge tracked + untracked
    candidates = {}
    for path, info in tracked_by_path.items():
        scanned_paths.discard(path)
        info["tracked"] = True
        name = info["name"]
        if name not in candidates or _path_priority(path) < _path_priority(candidates[name]["path"]):
            candidates[name] = info
    for path in sorted(scanned_paths):
        name = Path(path).name
        if name in candidates:
            continue  # tracked versiyonu var, skip
        ptype = detect_project_type(path)
        candidates[name] = {
            "name": name, "path": path, "type": ptype,
            "tracked": False, "added": "", "description": "",
            "status": "unknown", "deploy_url": ""
        }
    return list(candidates.values())

def get_container_status(project_name):
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={project_name}", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5
        )
        out = result.stdout.strip()
        return "running" if out else "stopped"
    except Exception:
        return "unknown"


@app.route("/api/apps")
def list_apps():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    # Yeni app'ları otomatik tara ve kaydet
    _auto_register_new_apps()
    projects = get_all_projects()
    return jsonify(projects)


@app.route("/api/apps", methods=["POST"])
def create_app_request():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    ptype = (data.get("type") or "web").strip()
    desc = (data.get("description") or "").strip()
    deploy_target = (data.get("deploy_target") or "vps").strip()

    if not name or not desc:
        return jsonify({"error": "name and description required"}), 400

    # Telegram'a bot isteği gönder
    icon = APP_TYPE_ICONS.get(ptype, "📦")
    deploy_note = "PC'ye gönder (pal_project)" if deploy_target == "pc" else "VPS'te çalıştır"
    tg_msg = (
        f"{icon} <b>Yeni Uygulama İsteği</b>\n\n"
        f"<b>Proje:</b> {name}\n"
        f"<b>Tip:</b> {ptype}\n"
        f"<b>Deploy:</b> {deploy_note}\n\n"
        f"<b>Açıklama:</b>\n{desc}"
    )
    try:
        bot_token = "8513721436:AAGwqUlreX0BLSy7Abgdzp1aWYDCSIMRHt0"
        chat_id   = "7183350213"
        payload = json.dumps({
            "chat_id": chat_id, "text": tg_msg,
            "parse_mode": "HTML", "disable_web_page_preview": True
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        return jsonify({"error": f"Telegram gonderim hatasi: {e}"}), 500

    # DB'ye kaydet (pending olarak)
    now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()
    db = load_apps_db()
    db["projects"].append({
        "name": name, "path": "", "type": ptype,
        "tracked": True, "added": now_str,
        "description": desc, "status": "pending",
        "deploy_target": deploy_target, "deploy_url": ""
    })
    save_apps_db(db)
    return jsonify({"ok": True, "message": "İstek Telegram'a gönderildi"})


@app.route("/api/apps/<path:name>", methods=["DELETE"])
def delete_app(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    # DB'den sil
    db = load_apps_db()
    projects = db.get("projects", [])
    to_del = next((p for p in projects if p["name"] == name), None)
    db["projects"] = [p for p in projects if p["name"] != name]
    save_apps_db(db)
    # Klasörü sil (güvenli — sadece /tmp ve /root/projects altındaki)
    deleted_dir = False
    import shutil as _shutil
    # Tracked proje path'i
    path_to_del = None
    if to_del and to_del.get("path"):
        path_to_del = Path(to_del["path"])
    # Untracked: PROJECT_DIRS'te ara
    if not path_to_del or not path_to_del.exists():
        for base in PROJECT_DIRS:
            candidate = base / name
            if candidate.exists() and candidate.is_dir():
                path_to_del = candidate
                break
    if path_to_del:
        allowed = any(str(path_to_del).startswith(str(b)) for b in PROJECT_DIRS)
        if allowed and path_to_del.exists():
            _shutil.rmtree(path_to_del, ignore_errors=True)
            deleted_dir = True
    return jsonify({"ok": True, "deleted_dir": deleted_dir})


@app.route("/api/apps/<path:name>/status")
def app_status(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    status = get_container_status(name)
    return jsonify({"name": name, "status": status})


@app.route("/api/apps/<path:name>/logs")
def app_logs(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        result = subprocess.run(
            ["docker", "logs", name, "--tail", "100"],
            capture_output=True, text=True, timeout=10
        )
        logs = result.stdout + result.stderr
        return jsonify({"logs": logs or "(log yok)"})
    except Exception as e:
        return jsonify({"logs": f"Hata: {e}"})


@app.route("/api/apps/<path:name>/stop", methods=["POST"])
def stop_app(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    killed = False
    # Process olarak çalışıyorsa durdur
    if name in _running_procs:
        info = _running_procs.pop(name)
        proc = info.get("proc")
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try: proc.kill()
                except Exception: pass
        killed = True
    # Docker container ise durdur
    try:
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=15)
        subprocess.run(["docker", "rm", name], capture_output=True, timeout=10)
        killed = True
    except Exception:
        pass
    # DB güncelle
    try:
        db = load_apps_db()
        proj = next((p for p in db.get("projects", []) if p["name"] == name), None)
        if proj:
            proj["status"] = "ready"
            proj.pop("run_port", None)
            proj.pop("run_url", None)
            save_apps_db(db)
    except Exception:
        pass
    return jsonify({"ok": killed})


@app.route("/api/apps/<path:name>/files")
def app_files(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    # Proje klasörünü bul
    db = load_apps_db()
    tracked = next((p for p in db.get("projects", []) if p["name"] == name), None)
    path = None
    if tracked and tracked.get("path"):
        path = Path(tracked["path"])
    else:
        for base in PROJECT_DIRS:
            candidate = base / name
            if candidate.exists():
                path = candidate
                break
    if not path or not path.exists():
        return jsonify({"error": "proje bulunamadi"}), 404
    files = []
    try:
        for f in sorted(path.rglob("*")):
            if any(part in (f.parts) for part in ["node_modules", ".git", "__pycache__", "venv", ".next"]):
                continue
            if f.is_file():
                rel = str(f.relative_to(path))
                size = f.stat().st_size
                files.append({"path": rel, "size": size})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"name": name, "files": files[:200]})



# ═══════════════════════════════════════════════════════════════
# APIS TAB — API Limit & Status Tracker
# ═══════════════════════════════════════════════════════════════
APIS_DB_PATH = Path("/root/scripts/dashboard/apis_config.json")
# ── Otomatik .env tarama ──────────────────────────────────────────
ENV_FILES = [
    Path("/root/.openclaw/.env"),
    Path("/root/.env"),
    Path("/root/scripts/dashboard/.env"),
]

KNOWN_API_REGISTRY = {
    "OPENROUTER_API_KEY": {
        "id": "openrouter", "name": "OpenRouter", "icon": "🔀",
        "base_url": "https://openrouter.ai/api/v1",
        "ping_type": "openrouter", "plan": "Pay-as-you-go",
        "monthly_cost_usd": 0,
        "tags": ["ai", "llm", "vision"],
        "notes": "Vision + LLM router — image_analyze.py, audio_transcribe.py"
    },
    "GEMINI_API_KEY": {
        "id": "gemini", "name": "Google Gemini", "icon": "✨",
        "base_url": "https://generativelanguage.googleapis.com",
        "ping_type": "gemini", "plan": "Free",
        "monthly_cost_usd": 0,
        "tags": ["ai", "vision"], "notes": "Google Gemini API"
    },
    "GOOGLE_API_KEY": {
        "id": "gemini", "name": "Google Gemini", "icon": "✨",
        "base_url": "https://generativelanguage.googleapis.com",
        "ping_type": "gemini", "plan": "Free",
        "monthly_cost_usd": 0,
        "tags": ["ai", "vision"], "notes": "Google Gemini API"
    },
    "ANTHROPIC_API_KEY": {
        "id": "anthropic", "name": "Anthropic / Claude", "icon": "🤖",
        "base_url": "https://api.anthropic.com",
        "ping_type": "anthropic", "plan": "Pay-as-you-go",
        "monthly_cost_usd": 0,
        "tags": ["ai", "llm"], "notes": "Claude API"
    },
    "OPENAI_API_KEY": {
        "id": "openai", "name": "OpenAI", "icon": "🟢",
        "base_url": "https://api.openai.com/v1",
        "ping_type": "openai", "plan": "Pay-as-you-go",
        "monthly_cost_usd": 0,
        "tags": ["ai", "llm"], "notes": "OpenAI GPT / Whisper"
    },
    "TELEGRAM_BOT_TOKEN": {
        "id": "telegram", "name": "Telegram Bot", "icon": "📱",
        "base_url": "https://api.telegram.org",
        "ping_type": "telegram", "plan": "Free",
        "monthly_cost_usd": 0,
        "tags": ["notification", "main"], "notes": "Ana Telegram botu"
    },
    "GITHUB_TOKEN": {
        "id": "github", "name": "GitHub", "icon": "🐙",
        "base_url": "https://api.github.com",
        "ping_type": "github", "plan": "Free",
        "monthly_cost_usd": 0,
        "tags": ["git", "devops"], "notes": "palamut62 hesabı — repo yönetimi"
    },
    "FIRECRAWL_API_KEY": {
        "id": "firecrawl", "name": "Firecrawl", "icon": "🔥",
        "base_url": "https://api.firecrawl.dev",
        "ping_type": "firecrawl", "plan": "Free",
        "monthly_cost_usd": 0,
        "tags": ["scraping", "research"], "notes": "Web scraping — research-agent"
    },
    "MINIMAX_API_KEY": {
        "id": "minimax", "name": "MiniMax", "icon": "🤖",
        "base_url": "https://api.minimax.io/anthropic",
        "ping_type": "minimax", "plan": "Free",
        "monthly_cost_usd": 0,
        "tags": ["ai", "llm", "openclaw"], "notes": "OpenClaw ana modeli — MiniMax portal"
    },
}


def _load_env_vars():
    """Tüm .env dosyalarını okur, birleşik dict döner."""
    env_vars = {}
    for ef in ENV_FILES:
        if ef.exists():
            for line in ef.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip().strip('"').strip("'")
    return env_vars


def scan_env_apis():
    """
    .env dosyalarındaki tanınan API keylerini tarar.
    apis_config.json'daki id'lerle çakışmayanları 'auto' flagiyle döner.
    """
    env_vars = _load_env_vars()
    db_ids = {a["id"] for a in load_apis_db().get("apis", [])}
    detected = {}  # id -> entry (deduplicate)

    for var_name, meta in KNOWN_API_REGISTRY.items():
        val = env_vars.get(var_name)
        if not val:
            continue
        api_id = meta["id"]
        if api_id in db_ids or api_id in detected:
            continue
        entry = dict(meta)
        entry["api_key"] = val
        entry["api_key_display"] = (val[:8] + "..." + val[-5:]) if len(val) > 15 else val
        entry["auto"] = True
        entry["env_var"] = var_name
        entry.setdefault("limits", {})
        entry.setdefault("models", [])
        entry["status"] = "unknown"
        entry["last_checked"] = ""
        entry["remaining"] = {}
        detected[api_id] = entry

    return list(detected.values())



def load_apis_db():
    if not APIS_DB_PATH.exists():
        return {"apis": []}
    try:
        return json.loads(APIS_DB_PATH.read_text())
    except Exception:
        return {"apis": []}

def save_apis_db(db):
    APIS_DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2))

def _ping_api(api):
    import ssl
    ctx = ssl.create_default_context()
    ptype = api.get("ping_type", "generic")
    key   = api.get("api_key", "")
    base  = api.get("base_url", "")
    result = {"status": "unknown", "remaining": {}, "detail": ""}
    try:
        if ptype == "minimax":
            body = json.dumps({"model":"MiniMax-M2.5","max_tokens":1,
                               "messages":[{"role":"user","content":"hi"}]}).encode()
            req = urllib.request.Request(
                f"{base}/v1/messages", data=body,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                d = json.loads(r.read())
            usage = d.get("usage", {})
            result.update({"status":"online","remaining":{
                "input_tokens_used": usage.get("input_tokens",0),
                "output_tokens_used": usage.get("output_tokens",0)
            },"detail":"Ping OK"})

        elif ptype == "telegram":
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{key}/getMe")
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                d = json.loads(r.read())
            if d.get("ok"):
                bot = d.get("result", {})
                result.update({"status":"online","remaining":{},
                    "detail": f"@{bot.get('username','?')} — {bot.get('first_name','?')}"})
            else:
                result.update({"status":"error","detail":str(d)})

        elif ptype == "github":
            req = urllib.request.Request(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"token {key}",
                         "User-Agent": "OpenClaw-Dashboard"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                d = json.loads(r.read())
            core = d.get("resources",{}).get("core",{})
            search = d.get("resources",{}).get("search",{})
            result.update({"status":"online","remaining":{
                "core_remaining": core.get("remaining", 0),
                "core_limit": core.get("limit", 5000),
                "search_remaining": search.get("remaining", 0),
                "search_limit": search.get("limit", 30)
            },"detail":f"Core: {core.get('remaining',0)}/{core.get('limit',5000)}"})

        elif ptype == "firecrawl":
            req = urllib.request.Request(
                "https://api.firecrawl.dev/v1/team/billing",
                headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                raw = r.read()
            d = json.loads(raw)
            data_d = d.get("data", d)
            remaining = data_d.get("remainingCredits", data_d.get("remaining_credits", None))
            total = data_d.get("totalCredits", data_d.get("total_credits", None))
            result.update({"status":"online","remaining":{
                "credits_remaining": remaining,
                "credits_total": total
            },"detail":f"Credits: {remaining}/{total}"})

        elif ptype == "twitter":
            req = urllib.request.Request(
                "https://api.twitter.com/2/users/me",
                headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                headers_resp = dict(r.headers)
                d = json.loads(r.read())
            rem = headers_resp.get("x-rate-limit-remaining") or headers_resp.get("X-Rate-Limit-Remaining")
            lim = headers_resp.get("x-rate-limit-limit") or headers_resp.get("X-Rate-Limit-Limit")
            result.update({"status":"online","remaining":{
                "rate_limit_remaining": rem,
                "rate_limit": lim
            },"detail":f"User: {d.get('data',{}).get('username','?')}"})

        elif ptype == "openrouter":
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}",
                         "User-Agent": "OpenClaw-Dashboard"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                d = json.loads(r.read())
            data_d = d.get("data", d)
            usage = data_d.get("usage", 0)
            limit = data_d.get("limit", None)
            result.update({"status": "online", "remaining": {
                "usage_usd": round(float(usage or 0), 6),
                "limit_usd": limit,
                "is_free_tier": data_d.get("is_free_tier", False)
            }, "detail": f"Used: ${float(usage or 0):.4f}"})

        elif ptype == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key,
                         "anthropic-version": "2023-06-01",
                         "User-Agent": "OpenClaw-Dashboard"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                d = json.loads(r.read())
            models = [m.get("id","?") for m in d.get("data",[])][:3]
            result.update({"status": "online", "remaining": {},
                "detail": f"Models: {', '.join(models)}"})

        elif ptype == "openai":
            req = urllib.request.Request(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}",
                         "User-Agent": "OpenClaw-Dashboard"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                d = json.loads(r.read())
            cnt = len(d.get("data", []))
            result.update({"status": "online", "remaining": {},
                "detail": f"{cnt} model erişilebilir"})

        elif ptype == "gemini":
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
                headers={"User-Agent": "OpenClaw-Dashboard"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                d = json.loads(r.read())
            cnt = len(d.get("models", []))
            result.update({"status": "online", "remaining": {},
                "detail": f"{cnt} Gemini model erişilebilir"})

        else:
            req = urllib.request.Request(base, headers={"User-Agent":"OpenClaw"})
            with urllib.request.urlopen(req, timeout=6, context=ctx) as r:
                result.update({"status":"online","detail":f"HTTP {r.status}"})

    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            result.update({"status":"auth_error","detail":f"HTTP {e.code} — key gecersiz"})
        elif e.code == 429:
            result.update({"status":"rate_limited","detail":"Rate limit asildi"})
        else:
            result.update({"status":"error","detail":f"HTTP {e.code}"})
    except Exception as e:
        result.update({"status":"offline","detail":str(e)[:80]})

    return result


@app.route("/api/apis")
def list_apis():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_apis_db()
    manual = db.get("apis", [])
    # .env'den otomatik tespit edilenler (apis_config'de olmayanlar)
    auto = scan_env_apis()
    return jsonify(manual + auto)


@app.route("/api/apis", methods=["POST"])
def add_api():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    db = load_apis_db()
    new_id = (data.get("id") or data.get("name","api").lower().replace(" ","_"))
    if any(a["id"] == new_id for a in db.get("apis",[])):
        return jsonify({"error": "API zaten mevcut"}), 409
    key = data.get("api_key","")
    entry = {
        "id": new_id,
        "name": data.get("name", new_id),
        "icon": data.get("icon", "🔑"),
        "base_url": data.get("base_url",""),
        "api_key_display": key[:8]+"..."+key[-5:] if len(key)>15 else key,
        "api_key": key,
        "plan": data.get("plan","Free"),
        "monthly_cost_usd": float(data.get("monthly_cost_usd",0) or 0),
        "limits": data.get("limits",{}),
        "models": data.get("models",[]),
        "ping_type": data.get("ping_type","generic"),
        "tags": data.get("tags",[]),
        "notes": data.get("notes",""),
        "status": "unknown",
        "last_checked": "",
        "remaining": {}
    }
    db.setdefault("apis",[]).append(entry)
    save_apis_db(db)
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/apis/sync-env", methods=["POST"])
def sync_env_apis_route():
    """Auto-detected API'leri apis_config.json'a kalıcı olarak kaydet."""
    if not check_key(): return jsonify({"error": "unauthorized"}), 401
    auto_apis = scan_env_apis()
    if not auto_apis:
        return jsonify({"ok": True, "added": 0, "message": "Yeni API bulunamadı"})
    db = load_apis_db()
    added = []
    for entry in auto_apis:
        e = {k: v for k, v in entry.items() if k not in ("auto", "env_var")}
        db.setdefault("apis", []).append(e)
        added.append(e["id"])
    save_apis_db(db)
    return jsonify({"ok": True, "added": len(added), "ids": added})


@app.route("/api/apis/<api_id>", methods=["DELETE"])
def delete_api_entry(api_id):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_apis_db()
    db["apis"] = [a for a in db.get("apis",[]) if a["id"] != api_id]
    save_apis_db(db)
    return jsonify({"ok": True})


@app.route("/api/apis/<api_id>", methods=["PUT"])
def update_api_entry(api_id):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    db = load_apis_db()
    for api in db.get("apis",[]):
        if api["id"] == api_id:
            for k, v in data.items():
                if k != "id":
                    api[k] = v
            break
    save_apis_db(db)
    return jsonify({"ok": True})


@app.route("/api/apis/<api_id>/ping")
def ping_api_entry(api_id):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_apis_db()
    # Önce manual listede ara
    api = next((a for a in db.get("apis",[]) if a["id"] == api_id), None)
    is_auto = False
    if not api:
        # Auto-detected listede ara
        auto_list = scan_env_apis()
        api = next((a for a in auto_list if a["id"] == api_id), None)
        is_auto = True
    if not api:
        return jsonify({"error": "API bulunamadi"}), 404
    res = _ping_api(api)
    if not is_auto:
        now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()
        api["status"] = res["status"]
        api["remaining"] = res["remaining"]
        api["last_checked"] = now_str
        save_apis_db(db)
    return jsonify({"ok": True, "status": res["status"],
                    "remaining": res["remaining"], "detail": res["detail"]})


@app.route("/api/apis/ping-all", methods=["POST"])
def ping_all_apis():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_apis_db()
    now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()
    results = []
    for api in db.get("apis",[]):
        res = _ping_api(api)
        api["status"] = res["status"]
        api["remaining"] = res["remaining"]
        api["last_checked"] = now_str
        results.append({"id": api["id"], "status": res["status"], "detail": res["detail"]})
    save_apis_db(db)
    return jsonify({"ok": True, "results": results})


# ─── APP RUN / STOP ──────────────────────────────────────────────────────────
import socket as _socket

_running_procs = {}  # name -> {pid, port, url}

def _find_free_port(start=5600):
    for p in range(start, start + 200):
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.bind(("", p))
                return p
        except OSError:
            continue
    return None

def _detect_run_cmd(path, port):
    p = Path(path)
    pkg = p / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except Exception:
            scripts = {}
        if "dev" in scripts:
            return f"npm run dev -- --port {port} --host 0.0.0.0"
        if "start" in scripts:
            return f"PORT={port} npm start"
    for pyfile in ["app.py", "main.py", "server.py", "run.py"]:
        if (p / pyfile).exists():
            return f"PORT={port} python3 {pyfile}"
    return None

@app.route("/api/apps/<path:name>/run", methods=["POST"])
def run_app(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_apps_db()
    proj = next((p for p in db.get("projects", []) if p["name"] == name), None)
    if not proj:
        return jsonify({"error": "app not found"}), 404
    # Zaten çalışıyor mu?
    if name in _running_procs:
        info = _running_procs[name]
        proc = info.get("proc")
        if proc and proc.poll() is None:
            return jsonify({"ok": True, "url": info["url"], "port": info["port"]})
        else:
            del _running_procs[name]
    path = proj.get("path", "")
    if not path or not Path(path).exists():
        return jsonify({"error": "app klasörü bulunamadı: " + path}), 404
    port = _find_free_port()
    if not port:
        return jsonify({"error": "boş port bulunamadı"}), 500
    cmd = _detect_run_cmd(path, port)
    if not cmd:
        return jsonify({"error": "çalıştırma komutu algılanamadı"}), 400
    log_path = f"/tmp/{name}.run.log"
    proc = subprocess.Popen(
        cmd, shell=True, cwd=path,
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT
    )
    url = f"http://100.123.254.69:{port}"
    _running_procs[name] = {"pid": proc.pid, "port": port, "url": url, "proc": proc}
    proj["status"] = "running"
    proj["run_port"] = port
    proj["run_url"] = url
    save_apps_db(db)
    return jsonify({"ok": True, "url": url, "port": port, "pid": proc.pid})

@app.route("/api/apps/<path:name>/update-url", methods=["POST"])
def update_app_url(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    db = load_apps_db()
    proj = next((p for p in db.get("projects", []) if p["name"] == name), None)
    if not proj:
        return jsonify({"error": "app not found"}), 404
    proj["deploy_url"] = url
    save_apps_db(db)
    return jsonify({"ok": True})

@app.route("/api/apps/<path:name>/register", methods=["POST"])
def register_app(name):
    """Bot tarafindan proje kaydedildiginde cagrilir."""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    db = load_apps_db()
    # Var olan pending kaydı güncelle veya yeni ekle
    existing = next((p for p in db.get("projects", []) if p["name"] == name), None)
    now_str = datetime.now(timezone(timedelta(hours=3))).isoformat()
    if existing:
        existing.update({
            "path": data.get("path", existing.get("path", "")),
            "status": data.get("status", "ready"),
            "deploy_url": data.get("deploy_url", existing.get("deploy_url", "")),
            "built_at": now_str,
        })
    else:
        db.setdefault("projects", []).append({
            "name": name,
            "path": data.get("path", ""),
            "type": data.get("type", "other"),
            "tracked": True,
            "added": now_str,
            "built_at": now_str,
            "description": data.get("description", ""),
            "status": data.get("status", "ready"),
            "deploy_target": data.get("deploy_target", "vps"),
            "deploy_url": data.get("deploy_url", ""),
        })
    save_apps_db(db)
    return jsonify({"ok": True})



# ─── OPENCLAW HEALTH ─────────────────────────────────────────────────────────
import subprocess, time as _time

_health_cache = {"data": None, "ts": 0}
_HEALTH_TTL = 300  # 5 dakika cache

def get_health_data(force=False):
    now = _time.time()
    if not force and _health_cache["data"] and (now - _health_cache["ts"]) < _HEALTH_TTL:
        return _health_cache["data"]
    try:
        result = subprocess.run(
            ["openclaw", "status", "--json"],
            capture_output=True, text=True, timeout=20
        )
        data = json.loads(result.stdout)
        _health_cache["data"] = data
        _health_cache["ts"] = now
        return data
    except Exception as e:
        return {"error": str(e)}


@app.route("/api/health")
def get_health():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    force = request.args.get("force") == "1"
    data = get_health_data(force=force)
    return jsonify(data)


@app.route("/api/health/update", methods=["POST"])
def trigger_update():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    try:
        subprocess.Popen(
            ["openclaw", "update", "--yes"],
            stdout=open("/tmp/openclaw-update.log", "w"),
            stderr=subprocess.STDOUT,
        )
        return jsonify({"ok": True, "message": "Guncelleme baslatildi, ~1-2 dk surer"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        "history": tool.get("history", []),
        "tags": tool.get("tags", []),
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


# --- TOOLS: GECMIS SIL ---
@app.route("/api/tools/<path:name>/history", methods=["DELETE"])
def clear_tool_history(name):
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401
    db = load_tools_db()
    if name not in db.get("tools", {}):
        return jsonify({"error": "not found"}), 404
    t = db["tools"][name]
    t["history"] = []
    t["last_eval"] = ""
    t["decision"] = ""
    t["report"] = ""
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



@app.route("/api/studio/x-posts", methods=["POST"])
def studio_x_posts():
    """X News keyword/hashtag/hesaplarından güncel AI tweet taslakları üret + humanize et."""
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    count = min(int(data.get("count", 6)), 10)

    # X config'den anahtar kelimeler, hashtag'ler ve hesaplar al
    xcfg = load_x_cfg()
    accounts = (xcfg.get("accounts") or [])[:5]
    hashtags = (xcfg.get("hashtags") or [])[:8]
    keywords = (xcfg.get("keywords") or [])[:6]

    accounts_str = ", ".join(f"@{a}" for a in accounts) if accounts else "@OpenAI, @AnthropicAI, @deepseek_ai"
    hashtags_str = ", ".join(f"#{h}" for h in hashtags) if hashtags else "#AI, #ChatGPT, #Claude"
    keywords_str = ", ".join(keywords) if keywords else "AI agent, LLM, open source AI"

    prompt = f"""Sen deneyimli bir Twitter/X içerik üreticisisin. Aşağıdaki AI/teknoloji kaynaklarından ilham alarak {count} adet özgün tweet taslağı hazırla.

Takip edilen kaynaklar: {accounts_str}
Popüler hashtag'ler: {hashtags_str}
Anahtar kelimeler: {keywords_str}

KURALLAR:
1. Her tweet maksimum 260 karakter (Twitter limiti)
2. Her tweet farklı bir konu/açı: haberler, hot-take, ipucu, karşılaştırma, soru, analiz vb.
3. Türkçe tweet yaz
4. Emoji kullan ama abartma (1-2 emoji/tweet)
5. Sonuna 1-2 ilgili hashtag ekle
6. ASLA "AI olarak" veya "dil modeli olarak" gibi ifadeler kullanma
7. Samimi, meraklı ve insan gibi yaz — robot izi olmamalı
8. Güncel AI gelişmelerini, modelleri, araçları, agent sistemlerini konu al

Sadece JSON döndür, başka hiçbir şey yazma:
{{"tweets": [
  {{"id": 1, "text": "tweet metni", "topic": "konu etiketi", "angle": "acı/tip/haber/analiz"}},
  ...
]}}"""

    try:
        import urllib.request as _req
        body = json.dumps({
            "model": _MINIMAX_MODEL,
            "max_tokens": 3000,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req_obj = _req.Request(
            f"{_MINIMAX_BASE}/v1/messages",
            data=body,
            headers={
                "x-api-key": _MINIMAX_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
        )
        with _req.urlopen(req_obj, timeout=90) as resp:
            raw = json.loads(resp.read())

        text_block = next((b for b in raw.get("content", []) if b.get("type") == "text"), None)
        if not text_block:
            return jsonify({"error": "AI yanıt vermedi"}), 500

        text = text_block["text"].strip()
        # JSON çıkar
        if "```" in text:
            parts = text.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("{"):
                    text = p
                    break
        data_parsed = json.loads(text)
        tweets = data_parsed.get("tweets", [])

        # Tweet metinlerini normalize et (280 karakter limiti)
        for t in tweets:
            t["text"] = t.get("text", "").strip()[:280]
            t["char_count"] = len(t["text"])

        return jsonify({"ok": True, "tweets": tweets, "sources": {
            "accounts": accounts,
            "hashtags": hashtags,
            "keywords": keywords
        }})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    if "min_likes" in data:
        try:
            cfg["min_likes"] = max(0, min(100, int(data["min_likes"])))
        except Exception:
            pass
    if "hours_back" in data:
        try:
            cfg["hours_back"] = max(1, min(168, int(data["hours_back"])))
        except Exception:
            pass
    save_x_cfg(cfg)
    return jsonify({"ok": True, "config": cfg})


# ─── X NEWS — TREND TAG SUGGEST ──────────────────────────────────────────────
_MINIMAX_BASE = "https://api.minimax.io/anthropic"
_MINIMAX_KEY  = "sk-api-t1bRhBnFlBvqDUUJFY6KGM5dfUhegQIGTKHw0Fllp9G-YC8bIagjaEx7HH7y1upWY8KBL_a34LG_wGIzuKZcZ_DExCC2c6yiT1jZM_Mf06lNWkcK-M-UVos"
_MINIMAX_MODEL = "MiniMax-M2.5"

_suggest_cache = {"data": None, "ts": 0}
_SUGGEST_TTL = 3600  # 1 saat cache

@app.route("/api/x/suggest-tags")
def x_suggest_tags():
    if not check_key():
        return jsonify({"error": "unauthorized"}), 401

    import _thread as _time_mod
    now = _time.time()
    if _suggest_cache["data"] and (now - _suggest_cache["ts"]) < _SUGGEST_TTL:
        return jsonify(_suggest_cache["data"])

    prompt = """List 70 trending Twitter/X hashtags and keywords about AI, LLM, bots, and AI tools (last 7 days). English only. Return ONLY valid JSON, no markdown, no explanation:
{"categories":{"LLM Models":["ChatGPT","GPT4o","Claude","Gemini","Llama","Mistral","DeepSeek","Grok","o3","Qwen"],"AI Tools":["Cursor","Copilot","Perplexity","Midjourney","Sora","ElevenLabs","Runway","Kling","Pika","HeyGen"],"Agents Automation":["AIAgent","AutoGPT","CrewAI","LangGraph","n8n","Zapier","MakeAI","AgentAI","MultiAgent","WorkflowAI"],"Dev Tech":["LangChain","RAG","VectorDB","Ollama","HuggingFace","OpenSource","FineTuning","Embeddings","MCP","APIdev"],"Trending":["ArtificialIntelligence","MachineLearning","GenerativeAI","AINews","TechNews","FutureOfAI","AIart","DeepLearning","NLP","ComputerVision"]},"all":["ChatGPT","GPT4o","Claude","Gemini","Llama","Mistral","DeepSeek","Grok","o3","Qwen","Cursor","Copilot","Perplexity","Midjourney","Sora","ElevenLabs","Runway","Kling","Pika","HeyGen","AIAgent","AutoGPT","CrewAI","LangGraph","n8n","Zapier","MakeAI","AgentAI","MultiAgent","WorkflowAI","LangChain","RAG","VectorDB","Ollama","HuggingFace","OpenSource","FineTuning","Embeddings","MCP","APIdev","ArtificialIntelligence","MachineLearning","GenerativeAI","AINews","TechNews","FutureOfAI","AIart","DeepLearning","NLP","ComputerVision"]}
Update this list with the LATEST trending tags you know about. Add 20-30 more relevant tags. Return complete updated JSON only."""

    try:
        body = json.dumps({
            "model": _MINIMAX_MODEL,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            f"{_MINIMAX_BASE}/v1/messages",
            data=body,
            headers={
                "x-api-key": _MINIMAX_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read())
        # content dizisinde thinking + text blokları olabilir
        text_block = next((b for b in raw.get("content", []) if b.get("type") == "text"), None)
        if not text_block:
            text_block = next((b for b in raw.get("content", []) if "text" in b), None)
        if not text_block:
            return jsonify({"error": "no text block in response"}), 500
        text = text_block["text"].strip()
        # JSON parse
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text)
        # all listesi yoksa categories'den derle
        if "all" not in data or not data["all"]:
            all_tags = []
            for cats in data.get("categories", {}).values():
                all_tags.extend(cats)
            data["all"] = all_tags
        _suggest_cache["data"] = data
        _suggest_cache["ts"] = _time.time()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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


# ══════════════════════════════════════════════════════
# BACKUP / RESTORE
# ══════════════════════════════════════════════════════
BACKUP_DIR = Path("/root/backups/openclaw")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE  = Path("/root/.openclaw/workspace")
GH_TOKEN   = os.getenv("GITHUB_TOKEN", "")
GH_REPO    = "palamut62/openclaw-config"

@app.route("/api/backup/list")
def backup_list():
    if not check_key(): return jsonify({"error": "unauthorized"}), 401
    files = []
    for f in sorted(BACKUP_DIR.glob("*.tar.gz"), reverse=True):
        stat = f.stat()
        files.append({
            "name": f.name,
            "size": stat.st_size,
            "size_mb": round(stat.st_size / 1_048_576, 2),
            "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "ts": stat.st_mtime,
        })
    return jsonify({"backups": files, "count": len(files)})

@app.route("/api/backup/create", methods=["POST"])
def backup_create():
    if not check_key(): return jsonify({"error": "unauthorized"}), 401
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = BACKUP_DIR / f"workspace_{ts}.tar.gz"
        result = subprocess.run(
            ["tar", "-czf", str(out),
             "--exclude=*/node_modules", "--exclude=*/__pycache__", "--exclude=*.pyc",
             str(WORKSPACE)],
            capture_output=True, timeout=60
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr.decode()[:300]}), 500
        stat = out.stat()
        return jsonify({
            "ok": True,
            "file": out.name,
            "size_mb": round(stat.st_size / 1_048_576, 2),
            "message": f"Yedek alindi: {out.name}"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backup/push-git", methods=["POST"])
def backup_push_git():
    if not check_key(): return jsonify({"error": "unauthorized"}), 401
    try:
        remote = f"https://{GH_TOKEN}@github.com/{GH_REPO}.git"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        cmds = [
            f"cd {WORKSPACE} && git config user.email palamut62@github.com",
            f"cd {WORKSPACE} && git config user.name palamut62",
            f"cd {WORKSPACE} && git remote set-url origin {remote} 2>/dev/null || git remote add origin {remote}",
            f"cd {WORKSPACE} && git add -A",
            f'cd {WORKSPACE} && git commit -m "Dashboard backup - {ts}" --allow-empty',
            f"cd {WORKSPACE} && git push -f origin master",
        ]
        log = []
        for cmd in cmds:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            log.append(r.stdout.strip() or r.stderr.strip())
        pushed = any("master" in l for l in log)
        return jsonify({
            "ok": pushed,
            "message": f"Git push {'basarili' if pushed else 'basarisiz'}",
            "log": [l for l in log if l][-3:]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backup/restore", methods=["POST"])
def backup_restore():
    if not check_key(): return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    filename = data.get("file", "").strip()
    if not filename or "/" in filename or not filename.endswith(".tar.gz"):
        return jsonify({"error": "Gecersiz dosya adi"}), 400
    backup_file = BACKUP_DIR / filename
    if not backup_file.exists():
        return jsonify({"error": "Yedek dosya bulunamadi"}), 404
    try:
        # Geri yuklemeden once mevcut workspace'i yedekle
        safe_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_out = BACKUP_DIR / f"pre_restore_{safe_ts}.tar.gz"
        subprocess.run(
            ["tar", "-czf", str(safe_out),
             "--exclude=*/node_modules", "--exclude=*/__pycache__",
             str(WORKSPACE)],
            capture_output=True, timeout=60
        )
        # Geri yukle
        result = subprocess.run(
            ["tar", "-xzf", str(backup_file), "-C", "/"],
            capture_output=True, timeout=120
        )
        if result.returncode != 0:
            return jsonify({"error": result.stderr.decode()[:300]}), 500
        return jsonify({
            "ok": True,
            "message": f"{filename} geri yuklendi. Onceki durum {safe_out.name} olarak yedeklendi.",
            "safety_backup": safe_out.name
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/backup/delete", methods=["DELETE"])
def backup_delete():
    if not check_key(): return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    filename = data.get("file", "").strip()
    if not filename or "/" in filename or not filename.endswith(".tar.gz"):
        return jsonify({"error": "Gecersiz dosya adi"}), 400
    backup_file = BACKUP_DIR / filename
    if not backup_file.exists():
        return jsonify({"error": "Dosya bulunamadi"}), 404
    backup_file.unlink()
    return jsonify({"ok": True, "message": f"{filename} silindi"})


if __name__ == "__main__":
    print("OpenClaw Dashboard starting on port 5300...")
    app.run(host="0.0.0.0", port=5300)


