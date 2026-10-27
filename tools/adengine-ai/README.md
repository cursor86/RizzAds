# AdEngine-AI

Autonomous, creative-first ad deployment and optimization for Meta (Facebook/Instagram) and Google (YouTube Demand Gen).

Core idea: the creative is the targeting. Ad sets stay broad (country and age floor only, Advantage+ audience on Meta), and the engine launches many creative variations and lets each platform's matching system find buyers. The engine then pauses losing creative, scales winning campaigns, and flags winning angles for new variations.

## Loop

```
Generate  -> creative assets are written by humans or proposed by the LLM, then approved by a human
Deploy    -> approved assets are bundled into a launch matrix and pushed to the platform, paused first
Analyze   -> daily snapshots are pulled from the platform for the last N days
Optimize  -> kill, scale, and iterate rules run on those snapshots and every action is logged
```

## Project layout

```
adengine-ai/
├── pyproject.toml
├── .env.example                      # every setting, with safe defaults
├── app/
│   ├── main.py                       # FastAPI app: setup, approvals, launches, reporting, manual triggers
│   ├── worker.py                     # 24-hour optimization cycle (cron-friendly with --once)
│   ├── config.py                     # Settings and resolve_secret() for env-var token references
│   ├── db.py                         # async SQLAlchemy engine and session dependency
│   ├── models.py                     # Brand, AdAccount, CreativeAsset, Campaign, AdSet, Ad,
│   │                                 # PerformanceSnapshot, ActionLog, IterationRequest
│   ├── errors.py                     # domain error types mapped to HTTP status codes
│   ├── integrations/
│   │   ├── retry.py                  # exponential backoff with jitter and Retry-After support
│   │   ├── base.py                   # SnapshotRow and the AdPlatform protocol
│   │   ├── meta.py                   # Graph API client, rate-limit classification, Meta adapter
│   │   └── google_ads.py             # Google Ads adapter (threaded gRPC, same retry policy)
│   └── services/
│       ├── naming.py                 # [AdEngine]_[AngleType]_[Date]_[CreativeID]
│       ├── bundles.py                # 3 angles x 3 headlines x 3 visuals (Meta), and Google video bundle
│       ├── persistence.py            # approved-asset loader and launch recording
│       ├── deploy_meta.py            # CBO conversions campaign, broad ad set, one ad per angle
│       ├── deploy_google.py          # keyword custom segment, Demand Gen campaign, responsive video ads
│       ├── optimizer.py              # pure kill, scale, and iteration rules
│       ├── optimization_loop.py      # sync, act, log, and commit per account
│       └── iteration.py              # LLM variations for winning angles, stored unapproved
├── tests/
│   ├── test_optimizer.py             # every rule, including boundaries and cooldown
│   ├── test_naming.py
│   └── test_retry.py
```

## Running it

```bash
pip install -e ".[dev]"
cp .env.example .env            # fill in values; set AUTO_CREATE_SCHEMA=true only for a local database
pytest                          # pure-logic tests, no network or database needed
uvicorn app.main:app --reload
python -m app.worker --once     # run one optimization cycle and exit (schedule this from cron)
```

## Typical flow

1. `POST /brands` with the client name and brief.
2. `POST /brands/{id}/ad-accounts` with the platform, the account ID, and `secret_ref`, the name of the env var holding that account's token. The token itself is never stored in the database.
3. `POST /brands/{id}/creatives` for hooks, bodies, headlines, and visuals, then `POST /creatives/{id}/approve` for each one that is ready.
4. `POST /ad-accounts/{id}/launch/meta` or `/launch/google`. Launches fail with a 422 if the approved matrix is incomplete, and nothing is created in that case.
5. The worker runs daily. `POST /ops/optimize` runs a cycle on demand. `POST /ops/iterations` generates variations for flagged angles.
6. `GET /campaigns/{id}/performance?days=7` shows the daily snapshots with CPA, ROAS, CTR, hook rate, and hold rate.

## Rules

| Rule | Trigger | Action |
|---|---|---|
| Kill (spend) | spend > 2x target CPA and 0 conversions in the window | pause ad |
| Kill (hook) | hook rate < 15% after at least 2,000 impressions (video ads only) | pause ad |
| Scale | ROAS >= target, CPA <= target, daily CPA variation (CV) <= 0.25 over at least 3 converting days, outside a 72-hour cooldown | campaign budget x 1.15 |
| Iterate | an angle's ROAS >= 1.5x the median of the other angles, with at least 20 conversions and no request in the last 7 days | queue 3 LLM variations for human approval |

Hook and hold rates are only applied to Meta video ads. Google's report does not expose 3-second or 15-second views, so Google ads are judged on spend, CPA, and ROAS only.

## Safety choices

- Every launch is created paused and activated only after every child object exists. A failed launch pauses whatever was already created.
- Generated copy is stored with `approved=false` and cannot reach a platform until a person approves it.
- A budget change is never made within the cooldown window, and the change is logged with the reason.
- Each account commits separately. An error in one account rolls back that account's changes and the rest continue.
- Rate limits: Meta rate-limit codes and HTTP 429 wait at least 60 seconds, or the `Retry-After` value if one is sent. Google `RESOURCE_EXHAUSTED` follows the same rule. Transient 5xx and transport errors use exponential backoff with jitter.

## Known gaps before production

- **Live API verification.** The Meta and Google payloads were written against the documented API shapes, but no live account was available to run them. Run the launch and insights calls in a sandbox ad account before turning on the worker. The Google enum paths and the `demand_gen_video_responsive_ad` fields are the most likely to need adjusting after that test.
- **Migrations.** `AUTO_CREATE_SCHEMA` is for local development only. Add Alembic before production.
- **Advantage+ Shopping.** The engine launches a CBO conversions campaign with Advantage+ audience on the ad set. Full ASC campaign types are not created here.
- **Custom segment type.** Keyword custom segments are created through `CustomAudienceService`, not `UserListService`. The `UserListService` covers remarketing lists, not keyword segments.
- **Angle attribution on Meta.** Each angle is one ad. Attribution is per ad, not per text element inside Meta's dynamic creative.
