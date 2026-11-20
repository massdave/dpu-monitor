# DPU Monitor — Setup

A public website that tracks the Massachusetts DPU File Room: what the DPU did since yesterday, your watchlist dockets, every docket's full filing history and key dates, new dockets, and news headlines. GitHub runs the updates on its servers every day, so your laptop does not need to be on.

The site and the repository are public: anyone can see them. Everything on the site comes from public DPU records and public news headlines. Your personal list of dockets is kept as stars in your own browser, not on the site. Do not put personal notes in this project.

Setup takes about 30 minutes. The first data appears within about an hour, and the site fills in completely on its own within about a day.

---

## Step 1. Create a GitHub account

Go to https://github.com and sign up (free). Your username becomes part of the site's address: `https://USERNAME.github.io/dpu-monitor/`, and it shows on the repository. Choose a username that doesn't identify you if you'd rather the site not be tied to your name.

## Step 2. Create the repository

1. Click the **+** at the top right, then **New repository**.
2. Repository name: `dpu-monitor`
3. Choose **Public**.
4. Check **Add a README file**.
5. Click **Create repository**.

## Step 3. Upload the files

1. Unzip `dpu-monitor.zip` on your computer.
2. In the repository on GitHub, click **Add file** → **Upload files**.
3. Open the unzipped `dpu-monitor` folder. Select **everything inside it** (Ctrl+A), including the `.github` folder, and drag it into the browser window. Drag the contents, not the folder itself.
4. Click **Commit changes**.
5. Check that the repository now shows a `.github` folder. Click into it: it should contain `workflows/update.yml`. If the `.github` folder is missing, see "If the .github folder did not upload" at the end.

## Step 4. Two settings

1. **Settings** → **Actions** → **General**. Under **Workflow permissions**, choose **Read and write permissions**. Click **Save**.
2. **Settings** → **Pages**. Under **Build and deployment** → **Source**, choose **GitHub Actions**.

## Step 5. Start the first update

1. Click the **Actions** tab. If GitHub asks, click the button to enable workflows.
2. Click **Update DPU Monitor** on the left, then **Run workflow** → **Run workflow**.
3. The first run takes about 45 minutes. When it shows a green check, open `https://USERNAME.github.io/dpu-monitor/`.

The first run reads the newest dockets first. After that, it runs every 6 hours on its own until the whole File Room is covered (about a day), then once a day. A banner on the site says "Still building" until then.

**If the run fails with a red X:** click it and look at the log. If it says the DPU sent back its web page instead of data, the DPU is refusing requests from GitHub's servers. Send that message to Claude; there are other ways to run the update.

---

## Everyday use

**Your starred dockets (private).** Click the ☆ next to any docket number on the site. Starred dockets appear under **★ Starred** on the Overview and in the Activity filter. They are saved in your browser only: they are not in the repository, and no visitor can see them. To use them on another device, click **Move to another device** under Starred, which copies a link; open that link on the other device and confirm. Anyone who has that link could see the list, so send it only to yourself. Clearing your browser data erases the stars.

**Topic groups (public).** `watchlist.txt` holds the topic groups everyone sees, such as GSEP 2026 or Future of gas. Keep them general. To edit, open the file in the repository, click the pencil icon, and click **Commit changes**. The site picks up the change on the next run. Each `[Group name]` starts a group; a `*` matches anything, so `26-GSEP-*` covers 26-GSEP-01 through 26-GSEP-06; an optional short name after a `|` becomes the card title.

**Upcoming dates (public).** `dates.txt` holds dates to show on the site, one per line: `26-62 | 2026-11-23 | Proposed information requests due`. Like everything in the repository, these are visible to anyone.

**News.** `news.txt` lists news searches. Only headlines and links are shown, with the source.

**Download all files.** On a docket's page, this button hands off to the DPU mirror on your laptop, which downloads every file into `C:\File Room` and makes them searchable and ready for Export for Claude. Start the mirror first (`start_dashboard.bat`), and confirm the download when it asks. On other devices, use the file links on the page or **Save list of file links**.

## What the site shows

- **Since yesterday:** DPU orders, new dockets, appeals and reconsideration requests, notices, hearing officer memos, and approvals.
- **Watchlist:** your ★ Starred dockets and the public topic groups, one card per docket with its estimated stage, last DPU action, upcoming dates, and latest filings.
- **DPU activity:** the last 45 days of DPU actions across all dockets, with filters.
- **All dockets:** every docket in the File Room, searchable by number, company, or topic.
- **Docket pages:** every filing with its files, a filter, and a Key dates timeline (opening, notices, procedural schedule, technical conferences, hearings, briefs, orders, appeals, compliance).

**Stages are estimates.** The site infers Opened → Discovery → Hearings → Briefs → Order → Closed from filing types. Check the docket itself before relying on a stage.

## Good to know

- **How current it is.** Active dockets (any filing in the last 120 days, or open and opened in the last 3 years) and your watchlist are rechecked daily, along with new dockets. Every other docket is rechecked about once a month.
- **The DPU's server.** The update waits one second between requests to avoid burdening it. A daily run makes a few thousand requests.
- **If GitHub pauses the schedule.** GitHub may pause scheduled runs in a repository with no activity for 60 days and send you an email. The daily updates should count as activity; if it does pause, open the Actions tab and click **Enable workflow**.
- **Cost.** GitHub Actions and Pages are free for public repositories.

## If the .github folder did not upload

1. In the repository, click **Add file** → **Create new file**.
2. In the name box, type `.github/workflows/update.yml` (the slashes create the folders).
3. Open `update.yml` from the unzipped folder in Notepad, copy everything, and paste it into the large box.
4. Click **Commit changes**.
