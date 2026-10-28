# GitHub New PoW / Node Project -> Discord Bot

Searches GitHub for **brand-new repositories** that look like a mineable **Proof-of-Work coin**
or a **node / masternode project**, and posts each one to Discord once, like this:

```
New record added: https://github.com/saltern-core/saltern
```

Discord turns the link into the GitHub preview card on its own (name, description, image).

## How it works
1. Every run, it does 14 GitHub searches for repos created in the last 48 hours. They look for
   "proof of work", mining algorithms (RandomX, KawPow, Ethash, Scrypt, SHA256d, kHeavyHash…),
   `topic:pow` / `topic:mining` / `topic:masternode`, "genesis block", "block reward",
   "fair launch", "stratum", "xmrig", "masternode", "full node"…
2. For each new repo it reads the README and gives it a **score**:
   - **+** proof-of-work words, algorithm names, mining/pool/hashrate
   - **+** coin/chain signs: genesis block, block reward, block time, supply, halving, premine,
     mainnet/testnet, wallet, explorer, node software
   - **+** node signs: masternode, node rewards, run a node, collateral
   - **−** noise: captchas/anti-bot proof-of-work, password hashing, tutorials/homework/demos,
     portfolios, ERC20/meme tokens, trading bots
3. Repos scoring at least `MIN_SCORE` (default 7) that look like a real chain are posted.

To see why a repo was or wasn't posted:
```bash
python bot.py --explain saltern-core/saltern
#   score 12 (need 7)  ->  POST as POW
#   +pow:algorithm +pow:proof-of-work +chain:block time +chain:genesis ...
```

## Never posts the same repo twice
- A repo is saved as "posted" **before** the message is sent, so a crash or restart can't repeat it.
- Posted repos are remembered forever, by repo ID and by owner/name.
- **One post per project:** after posting a repo, other repos from the same owner (their wallet,
  pool, explorer, miner…) are skipped for 30 days (`ONE_PER_OWNER_DAYS`).
- New commits to an already-posted repo never post it again.
- A repo with no README yet is checked again after its next push, and posted then if it qualifies.
- If the bot starts with no memory, it only remembers what's out there and posts nothing.
- The memory file is saved safely with a backup. If both copies are damaged, the bot stops.
- Only one copy can run at a time. **Run the bot in one place only.**

## Setup

### 1. Discord webhook
Discord channel -> **Edit Channel -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL**.
The webhook's name and avatar (e.g. "Captain Hook") are what show in Discord.

### 2a. GitHub Actions (runs 24/7, no PC needed)
1. On GitHub, create `.github/workflows/github_pow_bot.yml` (**Add file -> Create new file**) and
   paste in the contents of `github_bot/github-workflow.yml`.
2. **Settings -> Secrets and variables -> Actions -> New repository secret**:
   - `DISCORD_WEBHOOK_URL_GITHUB` = webhook for this bot's channel. If you leave it out, the bot
     uses `DISCORD_WEBHOOK_URL` (the same channel as the Bitcointalk bot).
3. **Actions -> "GitHub PoW project notifier" -> Run workflow** to start it. The first run only
   remembers what's already out there. After that it posts new projects every hour.

Actions brings its own GitHub token, so you don't need to make one.

> On a **private** repo the free plan gives 2,000 Action minutes a month. This bot uses about
> 1–2 minutes per run, so hourly is about 1,100 min/month. Together with the Bitcointalk bot
> every 15 minutes, that's over the limit. Make the repo public (no limit) or slow the bots down.

### 2b. Your own PC or VPS
```bash
cd github_bot
pip install -r requirements.txt
cp .env.example .env              # paste your webhook URL (and a GitHub token) into .env
python bot.py --once --dry-run    # test: prints what it would post, sends nothing
python bot.py                     # runs forever, checks every 30 minutes
```

## Settings
| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Discord webhook (required) |
| `GITHUB_TOKEN` | — | GitHub token; without it only 60 requests/hour |
| `BOT_NAME` | webhook name | Name shown in Discord |
| `CHECK_INTERVAL_MINUTES` | 30 | Loop interval when not using `--once` |
| `MAX_AGE_HOURS` | 48 | Only repos created in the last N hours |
| `MIN_SCORE` | 7 | Higher = stricter (try 9–10 if you get noise) |
| `INCLUDE_NODE` | true | Also post node/masternode projects |
| `ONE_PER_OWNER_DAYS` | 30 | Skip the same owner's other repos for N days (0 = off) |
| `MIN_STARS` | 0 | Ignore repos with fewer stars |
| `SHOW_DETAILS` | false | Add a small 2nd line: type · algorithm · language |
| `POST_ON_FIRST_RUN` | false | Post matches on the very first run too |

To tune what counts as a match, edit `POW_PATTERNS`, `CHAIN_PATTERNS`, `NODE_PATTERNS` and
`NEG_PATTERNS` in `bot.py`. Use `--explain OWNER/REPO` and `-v` to see the scores.

To reset the memory, delete `state/seen.json` and `state/seen.json.bak` (or, on GitHub, the
`github-bot-state` branch).
