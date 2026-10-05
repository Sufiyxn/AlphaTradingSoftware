# Alpha Trading Platform

Starter foundation for a broker-agnostic trading platform. It contains a standard-library Python status API and a React + TypeScript dashboard shell. No trading, broker, or AI decision logic is included.

## Run locally

Backend (Python 3.11+):

```powershell
Copy-Item .env.example .env
python -m backend
```

Frontend (Node.js):

```powershell
cd frontend
npm install
npm run dev
```

The API reports service status at `http://127.0.0.1:8000/api/health`. Configuration is read from environment variables; `.env` is ignored by Git. The backend has no third-party runtime dependencies, and its basic tests use `unittest`.
