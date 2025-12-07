# Well Focus Classes

A small Flask app to track tuition students and fees.

## Quick start

1. Create a virtualenv and install dependencies:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Set environment variables (local development):
```bash
export MONGO_URI="mongodb://localhost:27017"
export SECRET_KEY="change-me"
export ADMIN_USERNAME="admin"
export ADMIN_PASSWORD="yourpassword"   # creates the admin on first run
```

3. Run:
```bash
python app.py
```

Open http://localhost:5000 and log in at /login

## Deploying to Vercel

- Add environment variables in Vercel dashboard: `MONGO_URI`, `SECRET_KEY`, `ADMIN_USERNAME`, and either `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH`.
- Connect repo to Vercel and deploy.

