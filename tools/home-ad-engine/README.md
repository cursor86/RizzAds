# HomeAdEngine

Single-file local utility: connects a client's Facebook Page and linked Instagram Business account,
then publishes Reels in the background.

Run locally:

    pip install fastapi sqlmodel httpx uvicorn
    export HOMEAD_API_KEY=... META_APP_ID=... META_APP_SECRET=...
    python app.py        # binds to 127.0.0.1:8000

Tokens are stored in plaintext in the local SQLite file. Protect that file with OS permissions.
