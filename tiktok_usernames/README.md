# tiktok_usernames

Generates rare TikTok handle candidates, scores them for rarity, and checks
each one for a public profile. Python 3.10+, standard library only.

## Quick start

```bash
# See what you'd check, rarest first. No network calls.
python -m tiktok_usernames generate --pattern CVCV --length 3 --repeats --limit 100

# Check them. Results go to tiktok_usernames.db; reruns skip cached names.
python -m tiktok_usernames check --pattern CVCV --limit 300 --delay 4 --out hits.csv

# Rarest handles with no public profile, across all runs.
python -m tiktok_usernames report --top 30 --min-rarity 60
```

## Candidate sources

| Flag | What it generates | Example |
|---|---|---|
| `--length N` | all-letter names of length N | `--length 4` -> `qzor` |
| `--pattern P` | `C` consonant, `V` vowel, `L` letter, `D` digit, `A` letter-or-digit, anything else literal | `CVCV` -> `kavo`, `x_VC` -> `x_ak` |
| `--wordlist F` | dictionary words 3-8 letters; also enables the real-word bonus | `--wordlist words.txt` |
| `--repeats` | `aaa`, `abab`, `abba`, `aabb` shapes | `zzzz`, `lool` |
| `--file F` | your own list, one per line | |

All flags combine and repeat. `--limit` caps each pattern; large spaces are
randomly sampled, so use `--seed` for reproducible runs.

## Rarity score (0-100)

| Factor | Points |
|---|---|
| Length 2 / 3 / 4 / 5 / 6 / 7 / 8+ | 60 / 50 / 38 / 26 / 16 / 8 / 0 |
| Letters only | +15 |
| Each digit (max -15) / each `_` or `.` | -5 / -10 |
| Pronounceable (no 3+ consonant or vowel runs) | +10 |
| Real word (with `--wordlist`) | +15 |
| Palindrome or single repeated letter | +5 |

Candidates are checked rarest first, so a run cut short has spent its
requests on the names that matter.

## What a result means

| Status | Meaning |
|---|---|
| `taken` | TikTok returned a user for that handle, private accounts included. |
| `no_profile` | TikTok answered cleanly with no user. **Candidate only.** |
| `unknown` | Bot wall, captcha, network error or a page layout change. Never treated as available, always rechecked. |

`no_profile` is not the same as claimable. Banned, deleted and
TikTok-reserved handles also have no public profile but cannot be
registered. The `tiktok_code` column records TikTok's own status code for each
hit: once you have confirmed a few handles in the app, the code tells you
which ones were genuinely free, and you can filter on it. The last step is
always the app: Profile > Edit profile > Username.

## Rate limits

TikTok's terms restrict automated access, and it blocks IPs that hammer it.
The checker is built to stay under the radar honestly rather than evade it:

- one request at a time, `--delay` (default 3s) plus random `--jitter`;
- exponential backoff on HTTP 429;
- stops the run after 5 unusable responses in a row;
- refuses a `--delay` under 1s.

No proxy rotation, no captcha solving. If you get blocked, wait and rerun
with a longer delay. At the defaults, expect about 950 checks per hour.

## Tests

```bash
python -m unittest discover -s tiktok_usernames/tests -t .
```

The parser is tested against fixture pages that mirror TikTok's embedded
`__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON (and the older `SIGI_STATE`). If
TikTok changes its page layout, every check comes back `unknown` and the run
stops; update `parse_profile_html` in `checker.py`.
