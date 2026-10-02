# School lunch board (DAKboard block)

A tiny web page that shows the school lunch for both schools, sized for a small DAKboard iFrame block:

- **Before 2pm** (school days): today's lunch. **From 2pm on**: the next school day's lunch
  (so Friday afternoon shows Monday). Weekends show Monday.
- Two sections: Elementary (Merrillan) and High School (Alma Center). Shows "No school" or
  "Menu not posted yet" when that applies.
- Text automatically resizes to fit whatever box size DAKboard gives it.

Page address (after GitHub Pages is on): `https://<github-username>.github.io/<repo-name>/`
Add `?theme=dark` to the end for light text on a dark tile.

## How it works
- `.github/workflows/update-menu.yml` runs twice a day. It runs `send_lunch_menu.py --export-json menu.json`,
  which reads the school's monthly menu PDFs (same code as the private lunch-email project) and writes this month's
  and next month's lunches to `menu.json`. It commits `menu.json` only when something changed.
- `index.html` reads `menu.json` and picks the right day in Central time, so the display's own clock/timezone
  does not matter. It re-checks the time every minute and re-reads `menu.json` every 30 minutes.
- This repo is public on purpose (GitHub Pages needs that on a free plan). It contains only school menu
  information that is already public, and no passwords or email addresses.

## Set up (once)
1. Create a **public** GitHub repo and add these files (keep the `.github/workflows` folder path).
2. Repo > Settings > Pages > "Build and deployment": Source = "Deploy from a branch", Branch = `main`, folder = `/ (root)`. Save.
3. Actions tab > "Update lunch menu data" > Run workflow (to confirm it works and refresh `menu.json`).
4. In DAKboard (Premium) open your screen layout, add an **iFrame** block, paste the page address, and size and place it
   where you want it.

## Testing the page
- `index.html?now=2026-10-02T09:30` shows the page as if it were that Central time (handy for checking the afternoon
  switch, a no-school day, or a month with no menu yet).
- `python send_lunch_menu.py --dump 2026-10` prints every lunch it read, to compare against the school's PDFs.

## If it stops updating
- Open the Actions tab and look at the latest "Update lunch menu data" run. A failure usually means the school changed
  its menu PDF layout or link text (see the `SCHOOLS` list at the top of `send_lunch_menu.py`).
- GitHub can disable scheduled workflows in a public repo after 60 days with no activity. The menu data changes
  every month, which counts as activity; if runs ever stop, click "Enable workflow" in the Actions tab.
