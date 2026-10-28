#!/usr/bin/env python3
"""
GitHub -> Discord notifier for NEW PoW / node-based crypto projects.

Searches GitHub for repositories created in the last MAX_AGE_HOURS that look
like a new mineable (Proof-of-Work) coin or a node / masternode project, reads
each README to filter out noise (captchas, password hashing, portfolios,
tutorials, tokens...), and posts each match to Discord ONCE:

    New record added: https://github.com/saltern-core/saltern

Discord turns the link into a preview card automatically.

Usage:
    python bot.py --once              # check one time (cron / GitHub Actions)
    python bot.py                     # run forever, every CHECK_INTERVAL_MINUTES
    python bot.py --once --dry-run    # print matches, send nothing, save nothing
    python bot.py --explain OWNER/REPO   # show why a repo would / wouldn't be posted

Settings (environment variables or a .env file next to this script):
    DISCORD_WEBHOOK_URL     required (unless --dry-run / --explain)
    GITHUB_TOKEN            strongly recommended (5000 API calls/hour instead of 60)
    CHECK_INTERVAL_MINUTES  default 30
    MAX_AGE_HOURS           only repos created in the last N hours. default 48
    MIN_SCORE               how sure the bot must be (higher = stricter). default 7
    INCLUDE_NODE            "true" = also post node/masternode projects. default true
    ONE_PER_OWNER_DAYS      after posting a repo, skip other repos of the same
                            owner for N days (wallet, pool, explorer...). 0 = off. default 30
    MIN_STARS               ignore repos with fewer stars. default 0
    SHOW_DETAILS            "true" = add a 2nd line (type, algorithm, language). default false
    POST_ON_FIRST_RUN       "true" = post matches on the very first run too. default false

No-repeat guarantee (same design as the Bitcointalk bot):
    * A repo is saved as "posted" BEFORE the Discord message is sent.
    * Posted repos are remembered forever by repo ID AND by owner/name.
    * With no memory (first start / wiped state) it remembers and posts nothing.
    * Atomic state writes + backup; refuses to run if both copies are damaged.
    * Only one copy can run at a time (state/bot.lock).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.github.com"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state" / "seen.json"
BACKUP_FILE = HERE / "state" / "seen.json.bak"
LOCK_FILE = HERE / "state" / "bot.lock"
STATE_VERSION = 1
SEARCH_DELAY = 2.5   # GitHub allows 30 searches/minute with a token
FROZEN = "*"         # marker: never re-evaluate this repo

log = logging.getLogger("gh-bot")

# --------------------------------------------------------------------------- #
# What to search for. GitHub allows at most 5 AND/OR/NOT operators per query,
# so the algorithm list is split into small groups.
# --------------------------------------------------------------------------- #
WHERE = "in:name,description,topics,readme"
SEARCH_TERMS = [
    f'"proof of work" {WHERE}',
    f'"proof-of-work" {WHERE}',
    "topic:proof-of-work",
    "topic:pow",
    "topic:cryptocurrency",
    "topic:mining",
    "topic:masternode",
    f"randomx OR kawpow OR ethash OR etchash OR progpow {WHERE}",
    f"sha256d OR scrypt OR equihash OR x11 OR x16r {WHERE}",
    f"kheavyhash OR autolykos OR yespower OR ghostrider OR cuckatoo {WHERE}",
    f"blake3 OR firopow OR meowpow OR karlsenhash OR verthash {WHERE}",
    f'"genesis block" OR "block reward" OR "fair launch" OR "no premine" {WHERE}',
    f'"mining pool" OR stratum OR xmrig OR cpuminer OR "solo mining" {WHERE}',
    f'masternode OR "full node" OR "node rewards" OR "run a node" {WHERE}',
]

# --------------------------------------------------------------------------- #
# Scoring patterns
# --------------------------------------------------------------------------- #
ALGORITHMS = [
    "RandomX", "KawPow", "ProgPow", "Ethash", "Etchash", "SHA256d", "SHA-256d", "SHA256",
    "SHA3", "Scrypt", "Equihash", "X11", "X16R", "X16RV2", "X25X", "kHeavyHash", "HeavyHash",
    "Autolykos", "Yespower", "YesCrypt", "GhostRider", "Cuckatoo", "Cuckaroo", "Cuckoo Cycle",
    "Blake3", "Blake2b", "FiroPow", "MeowPow", "KarlsenHash", "FishHash", "XelisHash",
    "Verthash", "NeoScrypt", "Lyra2REv3", "Lyra2", "Argon2d", "CryptoNight", "Keccak",
    "Groestl", "Skein", "Qubit", "BLAKE",
]
ALGO_RE = re.compile(r"\b(" + "|".join(re.escape(a) for a in ALGORITHMS) + r")\b", re.I)

# Signals that something is MINED with proof-of-work.
POW_PATTERNS = {
    "proof-of-work": r"proof[\s-]*of[\s-]*work|\bpow\b",
    "algorithm": ALGO_RE.pattern,
    "mining": r"\b(cpu|gpu|asic)[\s-]*(mining|miner|mineable|minable|friendly|resistant)\b|\bmine (it|blocks|coins)\b",
    "miner": r"\bxmrig\b|\bcpuminer\b|\bsrbminer\b|\blolminer\b|\bt-?rex\b|\bminer (binary|software|release)",
    "pool": r"\bmining pool|\bstratum\b|\bpplns\b|\bsolo[\s-]*min(e|ing)\b",
    "hashrate": r"\bhash[\s-]*rate\b|\bnethash\b|\bdifficulty (adjustment|retarget|algorithm)\b|\blwma\b|\bdaa\b",
}
# Signals that the repo is an actual coin / chain (not a library or a demo).
CHAIN_PATTERNS = {
    "genesis": r"\bgenesis (block|hash|coinbase)\b",
    "reward": r"\bblock reward\b|\bblock subsidy\b|\breward per block\b|\bcoinbase reward\b",
    "block time": r"\bblock[\s-]*time\b|\btarget (block )?spacing\b|\bseconds per block\b",
    "supply": r"\b(max(imum)?|total|circulating) supply\b|\bemission\b|\bhalving\b",
    "premine": r"\b(no|zero|without)[\s-]*pre[\s-]*mine\b|\bfair[\s-]*launch\b|\bpremine\b|\bdev (fee|fund)\b",
    "network": r"\bmainnet\b|\btestnet\b|\bseed ?nodes?\b|\bp2p port\b|\brpc port\b|\bpeers?\b",
    "wallet": r"\bwallet\b",
    "explorer": r"\bblock explorer\b|\bexplorer\b",
    "address": r"\baddress prefix\b|\bticker\b|\bsymbol\b|\bcoin name\b",
    "node": r"\b(full|seed|reference) node\b|\bnode software\b|\b(\w+)d daemon\b|\bdaemon\b",
    "coin": r"\bcryptocurrency\b|\bcoin\b|\baltcoin\b|\bblockchain\b|\blayer[\s-]*1\b|\bl1\b",
}
# Signals for node-based projects (masternodes / run-a-node rewards).
NODE_PATTERNS = {
    "masternode": r"\bmaster[\s-]*nodes?\b|\bsuper[\s-]*nodes?\b|\bservice[\s-]*nodes?\b",
    "node rewards": r"\bnode rewards?\b|\brewards? for (running|hosting) (a )?nodes?\b|\bnode operators?\b",
    "run a node": r"\brun (a|your( own)?) (full )?node\b|\bnode (setup|install(ation)?) guide\b",
    "collateral": r"\bcollateral\b",
}
# Signals that it is NOT what we want.
NEG_PATTERNS = {
    # (nginx / rate-limit / ddos are NOT here: real node setup guides mention them all the time)
    "anti-bot/captcha": r"\bcaptcha\b|\banti[\s-]*(spam|bot|abuse)\b|\bbot (protection|check|challenge)\b|\bbrowser (check|challenge)\b|\bhashcash\b|\bweb ?application firewall\b",
    "password hashing": r"\bpassword(s)?\b|\bkey derivation\b|\bkdf\b|\bencrypt(ion|ed)? (file|vault)\b",
    "learning/demo": r"\btutorial\b|\bhomework\b|\bassignment\b|\bcourse\b|\buniversity\b|\bstudents?\b|\bfor learning\b|\blearning (project|purposes|exercise)\b|\beducational\b|\btoy\b|\bsimple blockchain\b|\bminimal blockchain\b|\bbasic blockchain\b|\bblockchain (demo|simulation|simulator)\b|\bsmall,? readable\b|\bexercise\b|\bbootcamp\b",
    "portfolio/other": r"\bportfolio\b|\bresume\b|\bcv\b|\bmy (personal )?website\b|\blanding page\b|\bproof[\s-]*of[\s-]*work (page|portfolio|repo(sitory)?)\b|\bstrategic proof of work\b",
    "token/defi": r"\berc[\s-]*20\b|\bbep[\s-]*20\b|\bspl token\b|\bsolidity\b|\bhardhat\b|\bpresale\b|\bairdrop\b|\bmeme ?coin\b|\bpump\.?fun\b|\bnft\b|\bdefi\b|\buniswap\b|\bpancakeswap\b",
    "bot/tool": r"\btrading bot\b|\btelegram bot\b|\bdiscord bot\b|\bprice (tracker|alert)\b|\bportfolio tracker\b|\bmining calculator\b|\bprofitability\b|\bawesome[\s-]list\b",
}

_C = lambda d: {k: re.compile(v, re.I) for k, v in d.items()}  # noqa: E731
POW_RE, CHAIN_RE, NODE_RE, NEG_RE = map(_C, (POW_PATTERNS, CHAIN_PATTERNS, NODE_PATTERNS, NEG_PATTERNS))


@dataclass
class Repo:
    id: int
    full_name: str
    url: str
    owner: str
    description: str = ""
    topics: list = field(default_factory=list)
    language: str | None = None
    stars: int = 0
    created_at: str = ""
    pushed_at: str = ""
    fork: bool = False
    archived: bool = False
    size: int = 0
    readme: str | None = None
    kind: str | None = None      # "pow" | "node" | None
    score: int = 0
    algo: str | None = None
    reasons: list = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> "Repo":
        return cls(
            id=int(d["id"]), full_name=d["full_name"], url=d["html_url"],
            owner=d["owner"]["login"], description=d.get("description") or "",
            topics=d.get("topics") or [], language=d.get("language"),
            stars=int(d.get("stargazers_count") or 0), created_at=d.get("created_at") or "",
            pushed_at=d.get("pushed_at") or "", fork=bool(d.get("fork")),
            archived=bool(d.get("archived")), size=int(d.get("size") or 0),
        )


def _hits(text: str, patterns: dict) -> set[str]:
    return {name for name, rx in patterns.items() if rx.search(text)}


def classify(repo: Repo, min_score: int, include_node: bool) -> None:
    """Set repo.kind / score / algo / reasons from name, description, topics and README."""
    name = re.sub(r"[-_.]+", " ", repo.full_name.split("/")[-1])
    head = " ".join([name, repo.description, " ".join(t.replace("-", " ") for t in repo.topics)])
    body = (repo.readme or "")[:40000]
    body = re.sub(r"```.*?```", " ", body, flags=re.S)  # ignore code blocks (passwords in configs etc.)

    pow_h, pow_b = _hits(head, POW_RE), _hits(body, POW_RE)
    node_h, node_b = _hits(head, NODE_RE), _hits(body, NODE_RE)
    chain = _hits(head + " " + body, CHAIN_RE)
    neg_h, neg_b = _hits(head, NEG_RE), _hits(body, NEG_RE)

    score = 2 * len(pow_h) + len(pow_b - pow_h)
    score += 2 * len(node_h) + len(node_b - node_h)
    score += min(len(chain), 8)
    score -= 4 * len(neg_h) + 2 * len(neg_b - neg_h)

    m = ALGO_RE.search(head) or ALGO_RE.search(body)
    repo.algo = m.group(1) if m else None
    repo.score = score
    repo.reasons = (
        [f"+pow:{x}" for x in sorted(pow_h | pow_b)]
        + [f"+node:{x}" for x in sorted(node_h | node_b)]
        + [f"+chain:{x}" for x in sorted(chain)]
        + [f"-{x}" for x in sorted(neg_h | neg_b)]
    )

    is_pow = bool(pow_h | pow_b) and len(chain) >= 3
    is_node = include_node and bool(node_h | node_b) and len(chain) >= 3
    if score < min_score or neg_h & {"anti-bot/captcha", "portfolio/other", "learning/demo"}:
        repo.kind = None
    elif is_pow:
        repo.kind = "pow"
    elif is_node:
        repo.kind = "node"
    else:
        repo.kind = None


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "pow-project-discord-notifier/1.0",
})


def gh_get(url: str, params: dict | None = None, raw: bool = False) -> requests.Response | None:
    headers = {"Accept": "application/vnd.github.raw"} if raw else {}
    for attempt in range(4):
        try:
            r = session.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as e:
            log.warning("Request error %s: %s", url, e)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (403, 429) and (
            r.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in r.text.lower()
        ):
            reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = int(r.headers.get("Retry-After", max(5, reset - time.time() + 2)))
            if wait > 120:
                raise RuntimeError(f"GitHub rate limit reached, resets in {wait}s. Set GITHUB_TOKEN.")
            log.info("Rate limited, waiting %ss", wait)
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        return r
    return None


def search_new_repos(max_age_hours: float) -> list[Repo]:
    since = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found: dict[int, Repo] = {}
    for term in SEARCH_TERMS:
        q = f"{term} created:>={since} fork:false archived:false"
        r = gh_get(f"{API}/search/repositories",
                   {"q": q, "sort": "updated", "order": "desc", "per_page": 100})
        time.sleep(SEARCH_DELAY)
        if r is None or r.status_code != 200:
            log.warning("Search failed (%s) for: %s", r.status_code if r else "no response", term)
            continue
        items = r.json().get("items", [])
        for d in items:
            found.setdefault(int(d["id"]), Repo.from_api(d))
        log.debug("%3d results: %s", len(items), term)
    log.info("Found %d new repos across %d searches", len(found), len(SEARCH_TERMS))
    return list(found.values())


def fetch_readme(repo: Repo) -> None:
    r = gh_get(f"{API}/repos/{repo.full_name}/readme", raw=True)
    repo.readme = r.text if (r is not None and r.status_code == 200) else None


def get_repo(full_name: str) -> Repo:
    r = gh_get(f"{API}/repos/{full_name}")
    if r is None or r.status_code != 200:
        raise SystemExit(f"Repo not found: {full_name}")
    return Repo.from_api(r.json())


# --------------------------------------------------------------------------- #
# State (memory) -- never trimmed for posted repos
# --------------------------------------------------------------------------- #
class StateError(RuntimeError):
    pass


class AlreadyRunning(RuntimeError):
    pass


def _empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "seen": {},            # repo id -> pushed_at when last checked ("*" = never re-check)
        "notified": {},        # repo id -> {name, url, at}   (permanent)
        "notified_names": [],  # lower-case owner/name of every posted repo (permanent)
        "owners": {},          # lower-case owner -> last time one of their repos was posted
    }


def _read_state_file(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "notified" not in data:
        raise ValueError("not a bot state file")
    return data


def load_state() -> dict:
    damaged = []
    for path in (STATE_FILE, BACKUP_FILE):
        if not path.exists():
            continue
        try:
            data = _read_state_file(path)
        except (ValueError, OSError, UnicodeDecodeError) as e:
            log.warning("Could not read %s: %s", path.name, e)
            damaged.append(path.name)
            continue
        if path == BACKUP_FILE:
            log.warning("Main state file was damaged -- using backup")
        state = _empty_state()
        state.update(data)
        return state
    if damaged:
        raise StateError(
            f"State file(s) {', '.join(damaged)} are damaged. Refusing to start fresh because "
            "that could re-post old repos. Delete the files in github_bot/state/ to reset."
        )
    return _empty_state()


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = STATE_VERSION
    if len(state["seen"]) > 30000:  # only "seen" is trimmed; old repos fall out of the search window anyway
        keep = sorted(state["seen"], key=int)[-30000:]
        state["seen"] = {k: state["seen"][k] for k in keep}
    state["notified_names"] = sorted(set(state["notified_names"]))
    data = json.dumps(state, indent=2, ensure_ascii=False)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if STATE_FILE.exists():
        try:
            _read_state_file(STATE_FILE)
            shutil.copy2(STATE_FILE, BACKUP_FILE)
        except (ValueError, OSError, UnicodeDecodeError):
            pass
    os.replace(tmp, STATE_FILE)


def merge_state_files(other_path: Path) -> None:
    """Union another copy of the state into ours (used by the GitHub Actions workflow)."""
    if not other_path.exists():
        return
    try:
        other = _read_state_file(other_path)
    except (ValueError, OSError, UnicodeDecodeError) as e:
        log.warning("Ignoring unreadable state copy %s: %s", other_path, e)
        return
    try:
        state = load_state()
    except StateError:
        state = _empty_state()
    for k, v in (other.get("seen") or {}).items():
        if state["seen"].get(k) != FROZEN:
            state["seen"][k] = FROZEN if v == FROZEN else state["seen"].get(k, v)
    notified = dict(other.get("notified") or {})
    notified.update(state["notified"])
    state["notified"] = notified
    state["notified_names"] = list(set(state["notified_names"]) | set(other.get("notified_names", [])))
    for o, t in (other.get("owners") or {}).items():
        state["owners"][o] = max(t, state["owners"].get(o, ""))
    state["initialized"] = bool(state["initialized"] or other.get("initialized"))
    save_state(state)
    log.info("Merged state from %s", other_path)


class SingleInstance:
    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(LOCK_FILE, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise AlreadyRunning(
                "Another copy of the GitHub bot is already running (state/bot.lock). "
                "Run only one copy, otherwise repos get posted once per copy."
            ) from None
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def build_message(repo: Repo, show_details: bool) -> str:
    msg = f"New record added: {repo.url}"
    if show_details:
        bits = ["PoW coin" if repo.kind == "pow" else "Node project"]
        if repo.algo:
            bits.append(repo.algo)
        if repo.language:
            bits.append(repo.language)
        msg += "\n-# " + " · ".join(bits)
    return msg


def send_discord(webhook: str, content: str, username: str | None) -> None:
    payload = {"content": content}
    if username:
        payload["username"] = username
    for _ in range(5):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        r.raise_for_status()
        break
    time.sleep(1.2)  # stay under Discord's webhook rate limit


# --------------------------------------------------------------------------- #
# Main check
# --------------------------------------------------------------------------- #
def blocked_reason(state: dict, repo: Repo, cfg: dict, now: datetime) -> str | None:
    if str(repo.id) in state["notified"] or repo.full_name.lower() in state["notified_names"]:
        return "already posted"
    days = cfg["one_per_owner_days"]
    last = state["owners"].get(repo.owner.lower())
    if days > 0 and last and datetime.fromisoformat(last) > now - timedelta(days=days):
        return f"owner {repo.owner} already posted in the last {days:g} days"
    return None


def check_once(cfg: dict, dry_run: bool = False) -> int:
    now = datetime.now(timezone.utc)
    state = load_state()
    repos = search_new_repos(cfg["max_age_hours"])
    if not repos:
        log.info("No new repos found this time")
        if not dry_run:
            state["initialized"] = True
            save_state(state)
        return 0

    # No memory yet: remember everything that's out there and post nothing.
    if not state["initialized"] and not cfg["post_on_first_run"]:
        log.info("First run / no saved state: remembering %d current repos, posting nothing. "
                 "From now on only brand-new projects are posted.", len(repos))
        if not dry_run:
            for r in repos:
                state["seen"][str(r.id)] = FROZEN
            state["initialized"] = True
            save_state(state)
        return 0

    to_send: list[Repo] = []
    for repo in sorted(repos, key=lambda r: r.created_at):
        prev = state["seen"].get(str(repo.id))
        if prev == FROZEN or prev == repo.pushed_at:
            continue  # nothing new since last time
        if blocked_reason(state, repo, cfg, now):
            state["seen"][str(repo.id)] = FROZEN
            continue
        if repo.fork or repo.archived or repo.stars < cfg["min_stars"]:
            state["seen"][str(repo.id)] = FROZEN
            continue
        if repo.size == 0:
            state["seen"][str(repo.id)] = repo.pushed_at  # empty for now; re-check after next push
            continue
        fetch_readme(repo)
        classify(repo, cfg["min_score"], cfg["include_node"])
        if repo.kind:
            reason = blocked_reason(state, repo, cfg, now)  # owner rule vs. repos picked this run
            if reason or any(r.owner.lower() == repo.owner.lower() for r in to_send if cfg["one_per_owner_days"] > 0):
                log.info("Skip (%s): %s", reason or "same owner this run", repo.full_name)
                state["seen"][str(repo.id)] = FROZEN
                continue
            log.info("MATCH %s score=%d [%s] %s", repo.kind.upper(), repo.score, repo.algo or "-", repo.url)
            to_send.append(repo)
        else:
            log.debug("no  score=%3d %s %s", repo.score, repo.full_name, " ".join(repo.reasons))
            # Not a match *yet*. It will be re-checked only if the repo gets new pushes
            # (e.g. the README is written later) while it is still new.
            state["seen"][str(repo.id)] = repo.pushed_at

    if dry_run:
        for r in to_send:
            print(build_message(r, cfg["show_details"]))
            print(f"    score={r.score} {' '.join(r.reasons)}")
        if not to_send:
            log.info("No new PoW/node projects this time")
        return len(to_send)

    if not to_send:
        state["initialized"] = True
        save_state(state)
        log.info("No new PoW/node projects this time")
        return 0

    # WRITE-AHEAD: save as posted BEFORE sending. Worst case after a crash is a
    # missed message, never a repeated one.
    for r in to_send:
        state["notified"][str(r.id)] = {"name": r.full_name, "url": r.url, "kind": r.kind,
                                        "at": now.isoformat(timespec="seconds")}
        state["notified_names"] = list(set(state["notified_names"]) | {r.full_name.lower()})
        state["owners"][r.owner.lower()] = now.isoformat(timespec="seconds")
        state["seen"][str(r.id)] = FROZEN
    state["initialized"] = True
    save_state(state)
    for r in to_send:
        send_discord(cfg["webhook"], build_message(r, cfg["show_details"]), cfg["username"])
    log.info("Sent %d notification(s) to Discord", len(to_send))
    return len(to_send)


def explain(full_name: str, cfg: dict) -> None:
    repo = get_repo(full_name)
    fetch_readme(repo)
    classify(repo, cfg["min_score"], cfg["include_node"])
    print(f"{repo.url}\n  created {repo.created_at}  stars {repo.stars}  fork {repo.fork}  "
          f"readme {'yes' if repo.readme else 'NO'}")
    print(f"  score {repo.score} (need {cfg['min_score']})  ->  "
          f"{'POST as ' + repo.kind.upper() if repo.kind else 'not posted'}")
    print("  " + " ".join(repo.reasons))


def load_dotenv() -> None:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def main() -> None:
    p = argparse.ArgumentParser(description="GitHub new PoW/node projects -> Discord")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--dry-run", action="store_true", help="print matches; send and save nothing")
    p.add_argument("--explain", metavar="OWNER/REPO", help="show the score for one repo and exit")
    p.add_argument("--merge-state", metavar="FILE", help="merge another seen.json into ours and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="also log rejected repos")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    load_dotenv()
    if args.merge_state:
        merge_state_files(Path(args.merge_state))
        return

    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        log.warning("No GITHUB_TOKEN set: GitHub allows only 60 requests/hour, results may be cut short.")

    cfg = {
        "webhook": os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
        "username": os.getenv("BOT_NAME", "").strip() or None,
        "interval": float(os.getenv("CHECK_INTERVAL_MINUTES", "30")),
        "max_age_hours": float(os.getenv("MAX_AGE_HOURS", "48")),
        "min_score": int(os.getenv("MIN_SCORE", "7")),
        "include_node": _bool("INCLUDE_NODE", "true"),
        "one_per_owner_days": float(os.getenv("ONE_PER_OWNER_DAYS", "30")),
        "min_stars": int(os.getenv("MIN_STARS", "0")),
        "show_details": _bool("SHOW_DETAILS", "false"),
        "post_on_first_run": _bool("POST_ON_FIRST_RUN", "false"),
    }
    if args.explain:
        explain(args.explain, cfg)
        return
    if not cfg["webhook"] and not args.dry_run:
        sys.exit("ERROR: set DISCORD_WEBHOOK_URL (env var or .env file), or use --dry-run")

    try:
        with SingleInstance():
            if args.once:
                check_once(cfg, args.dry_run)
                return
            log.info("Running every %.0f minutes. Ctrl+C to stop.", cfg["interval"])
            while True:
                try:
                    check_once(cfg, args.dry_run)
                except StateError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.error("Check failed: %s", e)
                time.sleep(cfg["interval"] * 60)
    except (AlreadyRunning, StateError) as e:
        sys.exit(f"ERROR: {e}")
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
