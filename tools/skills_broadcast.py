#!/usr/bin/env python3
"""
skills_broadcast.py — cluster-wide skill propagation via NATS JetStream.

Entry points:
  python3 skills_broadcast.py publish <skill_dir>      # publish one local skill
  python3 skills_broadcast.py publish-all              # publish every local skill
  python3 skills_broadcast.py subscribe                # daemon: install incoming
  python3 skills_broadcast.py audit                    # show last N broadcasts

Stream: SKILLS_BROADCAST  Subject: skills.broadcast.<skill_name>
Payload: msgpack-free; pure JSON envelope:
  {
    "v": 1, "name": "<skill>", "origin": "<hostname>", "sha256": "...",
    "format": "single_markdown" | "zip_bundle",
    "payload_b64": "<base64 .skill file bytes>",
    "ts": "<iso8601>"
  }

Idempotency: same {sha256, name} arriving on a node that already has that
skill content installed is a no-op (verified by re-hashing local SKILL.md).
Trust: only senders matching the gateway allowlist (sender_id of the form
'nats:<host>') can install. The subscriber sets sender_id = 'nats:' + payload.origin
so the existing skill_install pipeline gates incoming broadcasts uniformly.
"""
from __future__ import annotations
import argparse, asyncio, base64, hashlib, io, json, os, socket, sys, time, zipfile
from datetime import datetime, timezone
from pathlib import Path

# ensure hermes-agent on path for tools.skill_file_install
sys.path.insert(0, str(Path("/home/ubuntu/hermes-agent")))
from tools.skill_file_install import install_skill_file  # type: ignore

import nats  # nats-py

NATS_SERVERS = os.environ.get(
    "SKILLS_NATS_SERVERS",
    "nats://100.127.115.56:4222,nats://localhost:4222",
).split(",")
STREAM = "SKILLS_BROADCAST"
SUBJECT_ROOT = "skills.broadcast"
SKILLS_DIR = Path(os.environ.get("HERMES_SKILLS_DIR", "/home/ubuntu/.hermes/skills"))
def _node_name() -> str:
    """Stable cluster node identity.

    Resolution order:
      1. HERMES_CLUSTER_NODE_NAME env var (highest priority, runtime override)
      2. ~/.hermes/cluster_node_name (file, persistent across reboots)
      3. socket.gethostname() (fallback, brittle on EC2 / docker / k8s)
    """
    v = os.environ.get("HERMES_CLUSTER_NODE_NAME", "").strip()
    if v:
        return v
    p = Path(os.path.expanduser("~/.hermes/cluster_node_name"))
    if p.exists():
        try:
            v = p.read_text().strip()
            if v:
                return v
        except Exception:
            pass
    return socket.gethostname()


HOST = _node_name()


def _pack_skill(skill_dir: Path) -> tuple[bytes, str]:
    """Serialize skill_dir into a .skill payload. Returns (bytes, format)."""
    files = [p for p in skill_dir.rglob("*") if p.is_file() and ".usage" not in p.name]
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"{skill_dir} missing SKILL.md")
    # Single-file fast path: only SKILL.md, ship raw markdown
    if [p.name for p in files] == ["SKILL.md"]:
        return skill_md.read_bytes(), "single_markdown"
    # Bundle path: zip everything (relative paths)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, arcname=str(p.relative_to(skill_dir)))
    return buf.getvalue(), "zip_bundle"


def _envelope(name: str, payload: bytes, fmt: str) -> bytes:
    sha = hashlib.sha256(payload).hexdigest()
    env = {
        "v": 1,
        "name": name,
        "origin": HOST,
        "sha256": sha,
        "format": fmt,
        "payload_b64": base64.b64encode(payload).decode(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "size": len(payload),
    }
    return json.dumps(env, separators=(",", ":")).encode()


async def _publish_skill(js, skill_dir: Path) -> dict:
    name = skill_dir.name
    payload, fmt = _pack_skill(skill_dir)
    env = _envelope(name, payload, fmt)
    subject = f"{SUBJECT_ROOT}.{name}"
    ack = await js.publish(subject, env, timeout=10.0)
    return {"name": name, "subject": subject, "size": len(env), "stream_seq": ack.seq, "format": fmt}


async def cmd_publish(skill_dir: Path):
    nc = await nats.connect(servers=NATS_SERVERS, name=f"skills-pub-{HOST}")
    js = nc.jetstream()
    try:
        result = await _publish_skill(js, skill_dir)
        print(json.dumps(result, indent=2))
    finally:
        await nc.drain()


async def cmd_publish_all():
    nc = await nats.connect(servers=NATS_SERVERS, name=f"skills-pub-all-{HOST}")
    js = nc.jetstream()
    try:
        results = []
        for d in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if not (d / "SKILL.md").exists():
                continue
            try:
                results.append(await _publish_skill(js, d))
            except Exception as e:
                results.append({"name": d.name, "error": str(e)})
        print(json.dumps(results, indent=2))
    finally:
        await nc.drain()


async def cmd_subscribe():
    """Durable JS pull-consumer: install every incoming broadcast, idempotently."""
    nc = await nats.connect(servers=NATS_SERVERS, name=f"skills-sub-{HOST}", max_reconnect_attempts=-1)
    js = nc.jetstream()
    print(f"[skills-sub] connected; subscribing on {STREAM}/{SUBJECT_ROOT}.>", flush=True)
    sub = await js.pull_subscribe(
        f"{SUBJECT_ROOT}.>",
        durable=f"skills-sub-{HOST}",
        stream=STREAM,
    )
    while True:
        try:
            msgs = await sub.fetch(batch=10, timeout=30)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            print(f"[skills-sub] fetch error: {e}", flush=True)
            await asyncio.sleep(2)
            continue
        for msg in msgs:
            try:
                env = json.loads(msg.data)
                name = env["name"]; origin = env["origin"]; sha = env["sha256"]
                if origin == HOST:
                    # don't re-install our own broadcast
                    await msg.ack()
                    continue
                local = SKILLS_DIR / name / "SKILL.md"
                if local.exists():
                    local_sha = hashlib.sha256(local.read_bytes()).hexdigest()
                    # If our SKILL.md alone matches, treat single-file format as installed
                    if env["format"] == "single_markdown" and local_sha == sha:
                        await msg.ack(); continue
                payload = base64.b64decode(env["payload_b64"])
                report = install_skill_file(
                    payload=payload,
                    filename=f"{name}.skill",
                    sender_id=f"nats:{origin}",
                    platform="nats",
                )
                print(f"[skills-sub] {name} from {origin}: verdict={report.verdict} path={report.install_path}", flush=True)
                await msg.ack()
            except Exception as e:
                print(f"[skills-sub] msg error: {e}", flush=True)
                # term so we don't block the consumer; broadcasts are replayable
                try:
                    await msg.term()
                except Exception:
                    pass


async def cmd_audit(limit: int = 20):
    nc = await nats.connect(servers=NATS_SERVERS, name=f"skills-audit-{HOST}")
    js = nc.jetstream()
    try:
        info = await js.stream_info(STREAM)
        print(f"Stream {STREAM}: {info.state.messages} msgs, {info.state.bytes} bytes")
        # list last `limit` via ephemeral consumer
        sub = await js.pull_subscribe(f"{SUBJECT_ROOT}.>", stream=STREAM)
        try:
            msgs = await sub.fetch(batch=limit, timeout=2)
        except asyncio.TimeoutError:
            msgs = []
        for m in msgs:
            try:
                env = json.loads(m.data)
                print(f"  {env['ts']}  {env['name']:<40s} from {env['origin']:<14s} sha={env['sha256'][:12]} fmt={env['format']}")
            finally:
                await m.ack()
    finally:
        await nc.drain()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["publish", "publish-all", "subscribe", "audit"])
    p.add_argument("target", nargs="?", default=None)
    args = p.parse_args()
    if args.cmd == "publish":
        if not args.target:
            print("usage: skills_broadcast.py publish <skill_dir>", file=sys.stderr); sys.exit(2)
        asyncio.run(cmd_publish(Path(args.target).resolve()))
    elif args.cmd == "publish-all":
        asyncio.run(cmd_publish_all())
    elif args.cmd == "subscribe":
        asyncio.run(cmd_subscribe())
    elif args.cmd == "audit":
        asyncio.run(cmd_audit())


if __name__ == "__main__":
    main()
