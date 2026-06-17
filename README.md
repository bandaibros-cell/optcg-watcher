# One Piece TCG Watch Agent

A small Python agent that watches the One Piece Card Game and pings a **Discord
webhook** when something happens:

| Watcher    | Source                                            | Alerts on |
|------------|---------------------------------------------------|-----------|
| `releases` | Official news — en.onepiece-cardgame.com/news/    | new news / set posts |
| `meta`     | Limitless One Piece — onepiece.limitlesstcg.com   | new tournaments |
| `stock`    | Any product page you list                         | out-of-stock → in-stock |
| `prices`   | A TCG price API (free tiers exist)                | watchlist card crosses a price threshold |

It keeps a `state.json` so each event only alerts once.

---

## 1. Get a Discord webhook
Discord → your server → **Edit Channel → Integrations → Webhooks → New Webhook
→ Copy URL**. That URL is all the agent needs to post.

## 2. Configure
```bash
cp config.example.yaml config.yaml
```
Edit `config.yaml`: enable the watchers you want. Leave `discord_webhook` blank
and instead set it as an environment variable (recommended) so it never gets
committed.

## 3. Run locally
```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK="https://discord.com/api/webhooks/..."
python watcher.py --test    # confirms the webhook works
python watcher.py --prime   # records current state, sends NO alerts (do this first)
python watcher.py           # real run — alerts on anything new since --prime
```
`--prime` matters: without it, the first run treats everything currently on a
page as "new" only for the link watchers (they self-prime), but priming first
is the clean way to start silent.

To run it on a schedule on your own machine, add a cron entry (Linux/Mac):
```
*/15 * * * * cd /path/to/optcg-watcher && /usr/bin/python3 watcher.py >> log.txt 2>&1
```
(Windows: use Task Scheduler.) Note: it only runs while your computer is on.

## 4. Run it free, 24/7, on GitHub Actions (recommended)
1. Push this folder to a new GitHub repo.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `DISCORD_WEBHOOK` = your webhook URL
   - `TCG_API_KEY` = your price-API key (only if you enable `prices`)
3. The workflow in `.github/workflows/watch.yml` runs every 15 minutes,
   posts to Discord, and commits the updated `state.json` back so it remembers
   what it has seen. You can also trigger it manually from the **Actions** tab.

GitHub's free scheduler can be late or skip runs under load; 15 min is a sane
floor. If you need every-minute restock checks, run it on a Raspberry Pi or a
cheap always-on VPS instead.

---

## Notes & tuning
- **Stock**: you must give each product the exact `out_of_stock_text` shown on
  its page (or an `in_stock_text` like "Add to Cart", which is usually more
  reliable). View the page source to find the phrase.
- **Prices**: confirm two fields against your price provider's docs —
  `search_url` (request shape) and `price_path` (where the number sits in the
  JSON). The code tolerates missing data and tells you if the path is wrong.
- **Selectors break**: if a site redesigns, a watcher may stop finding items
  and will say so in the log. Adjust `link_contains` and you're back in
  business.
- **Be polite**: 15-minute polling is fine. Don't hammer sites every few
  seconds — you may get rate-limited or blocked, and some sites' terms restrict
  automated access. Use official APIs where available.

## Layout
```
watcher.py                 # the agent
config.example.yaml        # copy to config.yaml and edit
requirements.txt
.github/workflows/watch.yml
state.json                 # created on first run
```
