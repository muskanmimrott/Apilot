# Deploying APilot to Streamlit Community Cloud

## A. Put the code on GitHub (choose one)

**Option 1: browser only, no git needed**
1. Go to https://github.com/new. Repository name: `apilot`. Visibility: **Public**. Do NOT tick "Add a README". Click **Create repository**.
2. On the new repo page click **uploading an existing file**.
3. Unzip `apilot_deploy.zip`. Open the unzipped folder, select **everything inside it** (app.py, data, samples, tests, ...), and drag it onto the page.
   - The `.streamlit` folder is hidden on macOS (press Cmd+Shift+. in Finder to show it). If it doesn't upload, the app still works; only the colour theme falls back to Streamlit's default.
   - Do **not** upload `apilot.db` if one exists.
4. Commit message "Initial commit", select **Commit directly to the main branch**, then click **Commit changes**.
5. Check that `app.py`, `requirements.txt` and `packages.txt` appear at the top level of the repo (not inside a sub-folder).

**Option 2: git command line**
```bash
cd apilot_deploy            # the unzipped folder, containing app.py
git init -b main
git add .
git commit -m "APilot: AI-powered AP invoice processing"
git remote add origin https://github.com/<your-username>/apilot.git
git push -u origin main     # GitHub asks you to sign in in the browser
```

## B. Deploy
1. Go to https://share.streamlit.io and **Continue with GitHub** (authorise Streamlit when asked).
2. Click **Create app** → **Deploy a public app from GitHub**.
3. Repository: `<your-username>/apilot`. Branch: `main`. Main file path: `app.py`.
4. App URL: choose something like `apilot-invoice` (gives `https://apilot-invoice.streamlit.app`).
5. **Advanced settings** → Python version **3.12** (the version everything was tested on). Secrets: leave empty
   (optional: `GEMINI_API_KEY = "your-key"` only if you want AI extraction; never commit a key to GitHub).
6. Click **Deploy**. The first build takes about 3–6 minutes (it installs Tesseract and the Python packages).

## C. Smoke test on the live URL (about 3 minutes)
1. **Process invoice** → demo invoice `01_happy_path` → **Process invoice** → expect **APPROVE**, with 11 stages ticking.
2. Pick `02_duplicate…` → expect **DUPLICATE**.
3. Pick `03_scanned…` → expect **REVIEW** (this proves Tesseract installed correctly; OCR takes a few seconds).
4. **Upload a PDF** → choose any file from `samples/` on your computer → it processes.
5. **Review queue** → approve the scanned invoice with a name + note → **Audit trail** shows your action.
6. **Dashboard** shows the counts.

## If the build fails
Open **Manage app** (bottom-right of the app) → logs.
- `TesseractNotFoundError`: `packages.txt` is missing from the repo root, or is misspelled. It must contain exactly `tesseract-ocr`.
- `No matching distribution found for …`: the Python version isn't 3.12. Delete the app and redeploy with 3.12 under Advanced settings.
- Theme looks plain: `.streamlit/config.toml` wasn't uploaded. On GitHub use Add file → Create new file, name it `.streamlit/config.toml`, and paste in the file's contents.

## Limitations
- **Sleeping:** free apps sleep after a period without visitors. The first visitor then sees "This app has gone to sleep", clicks **Yes, get this app back up**, and waits about 30–60 seconds. Open the link yourself shortly before the interview.
- **Temporary, shared storage:** the SQLite history lives on the app's container. It is shared by everyone using the link and is wiped whenever the app restarts or redeploys. The demo samples are always available, so this doesn't stop testing. Click **Reset demo data** before recording or presenting, so duplicates and PO balances start clean.
- Anyone with the link can use **Reset demo data** and the review buttons. That's acceptable for a demo; production would add login.
